"""Attach an existing signal to the daily runner without replaying old books."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from trading.research import system_daily_refresh as daily
from trading.research import leader_dated_catchup_daily as dated
from trading.research.tests.test_system_daily_refresh import saved_system, write, D, E, F


class BeforeFirstClose(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromisoformat("2026-10-18T18:00:00+08:00").astimezone(tz)


def test_attach_preserves_existing_registration_and_routes_and_is_readonly_on_repeat(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    monkeypatch.setattr(daily, "datetime", BeforeFirstClose)
    profile = {"last_source_day": D, "first_decision_day": E, "first_execution_day": F,
        "run_directory": str(tmp_path / "new_four_case_books")}
    calls = []
    monkeypatch.setattr(dated, "bootstrap_profile", lambda: calls.append("read") or deepcopy(profile))
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("Attachment cannot run commands"))
    original_registration = (saved.directory / "registration.json").read_bytes()
    original = deepcopy(saved.head)
    first = daily.attach_dated_catchup(saved.directory)
    after = daily._read(saved.directory / "head.json")
    assert first["status"] == "dated_catchup_attached_without_running"
    assert (saved.directory / "registration.json").read_bytes() == original_registration
    assert all(after[key] == value for key, value in original.items())
    before_repeat = (saved.directory / "head.json").read_bytes()
    repeated = daily.attach_dated_catchup(saved.directory)
    assert repeated["status"] == "existing_dated_catchup_attachment_reused"
    assert (saved.directory / "head.json").read_bytes() == before_repeat and calls == ["read"]
    assert not Path(profile["run_directory"]).exists()
    attachment = Path(first["attachment"]["file"])
    attachment.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="attachment changed"):
        daily._load(saved.directory)


def test_attachment_refuses_skipped_cursor_before_any_write(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    monkeypatch.setattr(daily, "datetime", BeforeFirstClose)
    monkeypatch.setattr(dated, "bootstrap_profile", lambda: {
        "last_source_day": E, "first_decision_day": F, "first_execution_day": "2026-10-21"})
    before = (saved.directory / "head.json").read_bytes()
    with pytest.raises(ValueError, match="exact existing source cursor"):
        daily.attach_dated_catchup(saved.directory)
    assert (saved.directory / "head.json").read_bytes() == before
    assert not (saved.directory / "addenda").exists()


def test_dated_step_uses_shared_child_binding_and_only_its_own_profile(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    head["dated_catchup_attachment"] = write(tmp_path / "attachment.json", {"synthetic": True})
    profile = {"last_source_day": D, "first_decision_day": E, "first_execution_day": F}
    head["dated_catchup_profile"] = deepcopy(profile)
    core = {"output_paths": {"eight_inputs": {"manifest": str(tmp_path / "manifest.json")},
        "environment": {"result": str(tmp_path / "environment.json")}}}
    operation = saved.directory / "operations/20261019"
    plan = write(operation / "dated_catchup/plan.json", {"synthetic_exact_plan": True})
    source = {"day": E, "source_folder": str(saved.package[E])}
    calls = []

    def command(step, directory, day):
        calls.append(step)
        assert directory == operation / "commands" and day == E
        assert step["binding"] == {"kind": "dated_catchup", "registration": head["registration"],
            "attachment": head["dated_catchup_attachment"], "source": source, "plan": plan}
        return {"status": "completed"}

    def run(profile_arg, day, panel, analysis, out, *, run_step):
        assert profile_arg == profile and day == E and out == operation / "dated_catchup"
        assert panel == core["output_paths"]["eight_inputs"]["manifest"]
        assert analysis == core["output_paths"]["environment"]["result"]
        run_step({"id": "catchup_input", "command": ["leader-dated-catchup-inputs"],
            "expected_files": [str(out / "input.json")]})
        return {"result": {"status": "published_waiting_first_execution", "producer_complete": True,
            "consumer_required": False, "consumer_complete": False},
            "next_profile": {**profile_arg, "last_source_day": day}}

    monkeypatch.setattr(dated, "run_profile", run)
    monkeypatch.setattr(daily, "_run_command", command)
    original_core = deepcopy(head["profile"])
    result = daily._dated_refresh(head, {}, operation, E, source, core)
    assert result["producer_complete"] and not result["consumer_required"]
    assert head["dated_catchup_profile"]["last_source_day"] == E
    assert head["profile"] == original_core and len(calls) == 1
    monkeypatch.setattr(dated, "run_profile", lambda *a, **k: pytest.fail("No pre-first-D producer"))
    assert daily._dated_refresh(head, {}, operation, D, source, None)["status"] == "not_due_before_first_decision"


def test_saved_forward_block_is_successful_evidence_but_not_fabricated_day_commit(tmp_path):
    result = tmp_path / "forward_result.json"
    write(result, {"schema": "leader_dated_catchup_shared_engine_result_v1",
        "status": "blocked_or_waiting_research_only", "cases": [],
        "candidate_panel_only": True, "execution_admitted": False,
        "selection_enabled": False, "strategy_validated": False, "metrics": None})
    step = {"command": ["leader-dated-catchup-forward"], "expected_files": [str(result)]}
    assert daily._outputs(step, E) == [daily._ref(result)]
    value = daily._read(result)
    value["execution_admitted"] = True
    write(result, value)
    with pytest.raises(ValueError, match="actual research-only result"):
        daily._outputs(step, E)


def test_same_day_consumer_block_preserves_other_routes_and_original_source_separation(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    operation = saved.directory / "operations/20261016"
    prior_catalog = write(tmp_path / "prior_catalog/catalog.json", {"old": True})
    current_catalog = write(operation / "dated_catchup/artifacts/catalog/catalog.json", {"new": True})
    before = {"last_source_day": "2026-10-15", "first_decision_day": "2026-10-15",
        "first_execution_day": D, "catalog": prior_catalog}
    current = {**before, "last_source_day": D, "catalog": current_catalog}
    head = daily._read(saved.directory / "head.json")
    attachment = {"schema": "system_daily_dated_catchup_attachment_v1",
        "system_registration": head["registration"], "initial_profile": before, **daily.FLAGS}
    head.update(dated_catchup_attachment=write(saved.directory / "addenda/dated_catchup.json", attachment),
        dated_catchup_profile=current)
    write(saved.directory / "head.json", head)
    write(operation / "dated_catchup/plan.json", {"before_profile": before})
    protected = []
    monkeypatch.setattr(dated, "input_directories", lambda p:
        protected.append(deepcopy(p)) or [str(Path(p["catalog"]["file"]).parent)])
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("Old routes cannot rerun"))
    monkeypatch.setattr(daily, "_dated_refresh", lambda *a, **k: {
        "status": "catchup_publication_saved_consumer_blocked_or_pending",
        "producer_complete": True, "consumer_required": True, "consumer_complete": False})
    result = daily.run(saved.directory, D, saved.package[D])
    assert protected == [before]
    assert result["status"] == "partial_research_routes"
    assert result["core_producers"] == "saved_exact_source_day_reused_without_reexecution"
    assert result["native_production"]["status"] == "saved_exact_source_day_reused_without_rescoring"
    assert all(result["native_forward_" + key]["status"] == "exact_execution_day_already_committed"
        for key in ("v1", "qualified"))
    assert saved.openings == [D]


def test_recovery_locates_exact_dated_plan_and_attachment_without_running_it(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    head["dated_catchup_profile"] = {"first_decision_day": E}
    head["dated_catchup_attachment"] = write(tmp_path / "attachment.json", {"original": True})
    operation = saved.directory / "operations/20261019"
    source = {"day": E, "source_folder": str(saved.package[E])}
    write(operation / "receipt.json", {"schema": daily.SCHEMA, "day": E,
        "source": source, **daily.FLAGS})
    panel, analysis = write(tmp_path / "panel.json", {}), write(tmp_path / "analysis.json", {})
    plan = {"source_day": E, "source_refs": [panel, analysis], "steps": [{
        "id": "catchup_catalog", "command": ["leader-dated-identity"],
        "expected_files": [str(operation / "dated_catchup/catalog.json")]}]}
    reference = write(operation / "dated_catchup/plan.json", plan)
    checked = []
    monkeypatch.setattr(dated, "validate_plan", lambda *args: checked.append(args) or plan)
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("Recovery cannot run a producer"))
    returned, step = daily._recovery_step(saved.directory, E, "catchup_catalog", head,
        daily._read(saved.directory / "registration.json"))
    assert returned == operation and len(checked) == 1
    assert checked[0] == (plan, head["dated_catchup_profile"], E,
        panel["file"], analysis["file"], operation / "dated_catchup")
    assert step["binding"] == {"kind": "dated_catchup", "registration": head["registration"],
        "attachment": head["dated_catchup_attachment"], "source": source, "plan": reference}
