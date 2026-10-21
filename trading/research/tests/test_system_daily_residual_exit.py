"""Attach/continue a new exit method without repeating existing daily routes."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from trading.research import leader_eight_type_residual_daily as residual
from trading.research import system_daily_refresh as daily
from trading.research.tests.test_system_daily_refresh import saved_system, write, D, E, F


class BeforeFirstClose(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromisoformat("2026-10-18T19:00:00+08:00").astimezone(tz)


def test_attach_cli_preserves_all_old_fields_and_repeat_does_not_write_or_run(tmp_path, monkeypatch, capsys):
    saved = saved_system(tmp_path, monkeypatch)
    monkeypatch.setattr(daily, "datetime", BeforeFirstClose)
    profile = {"last_source_day": D, "first_decision_day": E, "first_execution_day": F,
        "run_directory": str(tmp_path / "new_residual_books")}
    calls = []
    monkeypatch.setattr(residual, "bootstrap_profile", lambda: calls.append("read") or deepcopy(profile))
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("Attachment cannot run a route"))
    old_registration = (saved.directory / "registration.json").read_bytes()
    original = deepcopy(saved.head)
    assert daily.main(["attach-residual-exit", "--run-dir", str(saved.directory)]) == 0
    assert "residual_exit_attached_without_running" in capsys.readouterr().out
    after = daily._read(saved.directory / "head.json")
    assert all(after[key] == value for key, value in original.items())
    assert (saved.directory / "registration.json").read_bytes() == old_registration
    before_repeat = (saved.directory / "head.json").read_bytes()
    result = daily.attach_residual_exit(saved.directory)
    assert result["status"] == "existing_residual_exit_attachment_reused" and calls == ["read"]
    assert (saved.directory / "head.json").read_bytes() == before_repeat
    assert not Path(profile["run_directory"]).exists()
    Path(result["attachment"]["file"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="attachment changed"):
        daily._load(saved.directory)


def test_attachment_refuses_wrong_source_cursor_before_any_write(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    monkeypatch.setattr(daily, "datetime", BeforeFirstClose)
    monkeypatch.setattr(residual, "bootstrap_profile", lambda: {
        "last_source_day": E, "first_decision_day": F, "first_execution_day": "2026-10-21"})
    before = (saved.directory / "head.json").read_bytes()
    with pytest.raises(ValueError, match="exact existing source cursor"):
        daily.attach_residual_exit(saved.directory)
    assert (saved.directory / "head.json").read_bytes() == before
    assert not (saved.directory / "addenda").exists()


def test_residual_recipe_binds_existing_core_publication_and_own_attachment(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    head["residual_exit_attachment"] = write(tmp_path / "attachment.json", {"synthetic": True})
    profile = {"last_source_day": D, "first_decision_day": E, "first_execution_day": F,
        "case_ids": ["case" + str(i) for i in range(32)]}
    head["residual_exit_profile"] = deepcopy(profile)
    core = {"output_paths": {"eight_inputs": {"manifest": str(tmp_path / "manifest.json")},
        "paper32": {"publication": str(tmp_path / "original_paper.json")}}}
    operation = saved.directory / "operations/20261019"
    plan = write(operation / "residual_exit/plan.json", {"synthetic_exact_plan": True})
    source = {"day": E, "source_folder": str(saved.package[E])}
    calls = []

    def command(step, directory, day):
        calls.append(step)
        assert directory == operation / "commands" and day == E
        assert step["binding"] == {"kind": "residual_exit", "registration": head["registration"],
            "attachment": head["residual_exit_attachment"], "source": source, "plan": plan}
        return {"status": "completed"}

    def run(profile_arg, day, panel, base_publication, out, *, run_step):
        assert profile_arg == profile and day == E and out == operation / "residual_exit"
        assert panel == core["output_paths"]["eight_inputs"]["manifest"]
        assert base_publication == core["output_paths"]["paper32"]["publication"]
        run_step({"id": "residual_publication", "command": ["leader-eight-type-residual", "publish"],
            "expected_files": [str(out / "paper_plan.json")]})
        return {"result": {"status": "published_first_decision_execution_source_not_yet_available",
            "producer_complete": True, "consumer_required": False, "consumer_complete": False},
            "next_profile": {**profile_arg, "last_source_day": day}}

    monkeypatch.setattr(residual, "run_profile", run)
    monkeypatch.setattr(daily, "_run_command", command)
    old_core = deepcopy(head["profile"])
    result = daily._residual_refresh(head, {}, operation, E, source, core)
    assert result["producer_complete"] and not result["consumer_required"]
    assert head["residual_exit_profile"]["last_source_day"] == E and head["profile"] == old_core
    assert len(calls) == 1
    monkeypatch.setattr(residual, "run_profile", lambda *a, **k: pytest.fail("No missing-core producer"))
    assert daily._residual_refresh(head, {}, operation, D, source, None)["status"] == "not_due_before_first_decision"
    assert daily._residual_refresh(head, {}, operation, E, source, None)["status"] == "blocked_by_current_eight_type_publication"


def test_recovery_validates_exact_method_plan_before_returning_shared_step(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    head["residual_exit_profile"] = {"first_decision_day": E}
    head["residual_exit_attachment"] = write(tmp_path / "attachment.json", {"original": True})
    operation = saved.directory / "operations/20261019"
    source = {"day": E, "source_folder": str(saved.package[E])}
    write(operation / "receipt.json", {"schema": daily.SCHEMA, "day": E, "source": source, **daily.FLAGS})
    panel, base_pub = write(tmp_path / "panel.json", {}), write(tmp_path / "base_pub.json", {})
    plan = {"source_day": E, "source_refs": [panel, base_pub], "steps": [{
        "id": "residual_publication", "command": ["leader-eight-type-residual", "publish"],
        "expected_files": [str(operation / "residual_exit/paper_plan.json")]}]}
    reference = write(operation / "residual_exit/plan.json", plan)
    checked = []
    monkeypatch.setattr(residual, "validate_plan", lambda *args: checked.append(args) or plan)
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("Recovery cannot run a producer"))
    returned, step = daily._recovery_step(saved.directory, E, "residual_publication", head,
        daily._read(saved.directory / "registration.json"))
    assert returned == operation and checked == [(plan, head["residual_exit_profile"], E,
        panel["file"], base_pub["file"], operation / "residual_exit")]
    assert step["binding"] == {"kind": "residual_exit", "registration": head["registration"],
        "attachment": head["residual_exit_attachment"], "source": source, "plan": reference}
    with pytest.raises(ValueError, match="only publish or consume"):
        daily._command(["leader-eight-type-residual", "register"])


def test_blocked_result_is_valid_terminal_evidence_but_must_remain_research_only(tmp_path):
    result = tmp_path / "forward_result.json"
    write(result, {"schema": "leader_eight_type_residual_shared_engine_result_v1",
        "status": "blocked_or_waiting_research_only", "cases": [],
        "candidate_panel_only": True, "execution_admitted": False,
        "selection_enabled": False, "strategy_validated": False, "metrics": None})
    step = {"command": ["leader-eight-type-residual", "forward"], "expected_files": [str(result)]}
    assert daily._outputs(step, E) == [daily._ref(result)]
    payload = daily._read(result)
    payload["execution_admitted"] = True
    write(result, payload)
    with pytest.raises(ValueError, match="actual research-only result"):
        daily._outputs(step, E)


@pytest.mark.parametrize("raises", [False, True])
def test_same_day_residual_failure_preserves_old_routes_and_original_input_roots(tmp_path, monkeypatch, raises):
    saved = saved_system(tmp_path, monkeypatch)
    operation = saved.directory / "operations/20261016"
    previous_panel = write(tmp_path / "original_inputs/manifest.json", {"old": True})
    current_panel = write(operation / "residual_exit/artifacts/manifest.json", {"new": True})
    before = {"last_source_day": "2026-10-15", "first_decision_day": "2026-10-15",
        "first_execution_day": D, "panels": [previous_panel["file"]]}
    adopted = {**before, "last_source_day": D, "panels": [current_panel["file"]]}
    head = daily._read(saved.directory / "head.json")
    attachment = {"schema": "system_daily_residual_exit_attachment_v1",
        "system_registration": head["registration"], "initial_profile": before, **daily.FLAGS}
    head.update(residual_exit_attachment=write(saved.directory / "addenda/residual_exit.json", attachment),
        residual_exit_profile=adopted)
    write(saved.directory / "head.json", head)
    write(operation / "residual_exit/plan.json", {"before_profile": before})
    protected = []
    monkeypatch.setattr(residual, "input_directories", lambda profile:
        protected.append(deepcopy(profile)) or [str(Path(profile["panels"][0]).parent)])
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("Old routes cannot rerun"))
    def unfinished(*args, **kwargs):
        if raises:
            raise ValueError("specific missing reference")
        return {"status": "residual_publication_saved_consumer_blocked_or_pending",
            "producer_complete": True, "consumer_required": True, "consumer_complete": False}
    monkeypatch.setattr(daily, "_residual_refresh", unfinished)
    result = daily.run(saved.directory, D, saved.package[D])
    assert protected == [before] and result["status"] == "partial_research_routes"
    assert result["core_producers"] == "saved_exact_source_day_reused_without_reexecution"
    assert result["native_production"]["status"] == "saved_exact_source_day_reused_without_rescoring"
    assert all(result["native_forward_" + key]["status"] == "exact_execution_day_already_committed"
        for key in ("v1", "qualified"))
    assert saved.openings == [D]
    after = daily._read(saved.directory / "head.json")
    assert all(after[key] == head[key] for key in ("profile", "native_profile", "lowvol", "registration"))
