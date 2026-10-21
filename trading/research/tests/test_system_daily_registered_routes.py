"""New optional daily-route wiring only; no producers, networks or old replays."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import daily_refresh_recipe as recipe
from trading.research import leader_entry_comparison_daily as entry
from trading.research import system_daily_refresh as daily
from trading.research import system_daily_command_child as child
from trading.research import seat_daily as seat
from trading.research.tests.test_system_daily_refresh import saved_system, write, D, E, F


@pytest.mark.parametrize("kind,attach_name,count", [
    ("entry_comparison", "attach_entry_comparison", 15),
    ("public_seat", "attach_public_seat", 4),
])
def test_attach_saved_route_preserves_original_head_registration_and_repeat_is_readonly(
        tmp_path, monkeypatch, kind, attach_name, count):
    saved = saved_system(tmp_path, monkeypatch)
    # The original first decision really was published on16; attachment on18
    # adopts that saved source, rather than creating a new registration on19.
    profile = {"last_source_day": D, "first_decision_day": D, "first_execution_day": E,
        "case_ids": ["case" + str(i) for i in range(count)], "run_directory": str(tmp_path / (kind + "_books"))}
    calls = []
    stub = SimpleNamespace(MUTABLE_PROFILE_KEYS={"last_source_day", "operations"},
        bootstrap_profile=lambda current_source_day: calls.append(current_source_day) or deepcopy(profile))
    monkeypatch.setattr(daily, "_attached_recipe", lambda selected: stub if selected == kind else
        pytest.fail("Attachment read another route"))
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("Attachment cannot run a command"))
    original = deepcopy(saved.head)
    old_registration = (saved.directory / "registration.json").read_bytes()
    attach = getattr(daily, attach_name)
    answer = attach(saved.directory)
    after = daily._read(saved.directory / "head.json")
    assert answer["commands_executed"] is False and calls == [D]
    assert after[kind + "_profile"] == profile
    assert all(after[key] == value for key, value in original.items())
    assert (saved.directory / "registration.json").read_bytes() == old_registration
    assert not (tmp_path / (kind + "_books")).exists()
    sealed_head = (saved.directory / "head.json").read_bytes()
    repeated = attach(saved.directory)
    assert repeated["commands_executed"] is False and calls == [D]
    assert (saved.directory / "head.json").read_bytes() == sealed_head
    attachment = after[kind + "_attachment"]
    assert daily._read(attachment["file"])["schema"] == "system_daily_" + kind + "_attachment_v1"
    write(attachment["file"], {"tampered": True})
    with pytest.raises(ValueError, match="attachment changed"):
        daily._load(saved.directory)


def test_entry_uses_current_own_bundle_and_extras_even_when_other_core_stage_fails(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = daily._read(saved.directory / "head.json")
    profile = {"last_source_day": D, "first_decision_day": D, "first_execution_day": E,
        "case_ids": ["case" + str(i) for i in range(15)], "operations": []}
    attachment = {"schema": "system_daily_entry_comparison_attachment_v1",
        "system_registration": head["registration"], "initial_profile": profile, **daily.FLAGS}
    head.update(entry_comparison_attachment=write(saved.directory / "addenda/entry_comparison.json", attachment),
        entry_comparison_profile=profile)
    # Native exact-E books are already saved: this focused test must not run
    # their producer/consumer merely to exercise the new entry route.
    head["native_profile"]["source_day"] = E
    for row in head["native_profile"]["forward_templates"].values():
        write(Path(row["run_dir"]) / "head.json", {"cases": {"lr": {"state": {"day": E}}}})
    write(saved.directory / "head.json", head)
    operation = saved.directory / "operations/20261019"
    own, extras = str(operation / "artifacts/pub20/input_bundle/bundle.json"), str(operation / "artifacts/extras/result.json")
    next_core = {**deepcopy(head["profile"]), "last_source_day": E,
        "output_paths": {"pub20": {"bundle": own}, "extras": {"result": extras}}}
    steps = [{"id": key, "command": [command], "requires": [], "expected_files": [str(operation / (key + ".json"))]}
        for key, command in (("extras", "baostock-bulk-extras"), ("pub20", "leader-close-publication"),
            ("eight_inputs", "leader-eight-type-inputs"))]
    monkeypatch.setattr(recipe, "next_plan", lambda *a: {"steps": steps, "next_profile": next_core})
    calls = []

    def command(step, directory, day):
        calls.append(step)
        if step["id"] == "eight_inputs":
            return {"status": "failed_or_interrupted_no_automatic_retry"}
        if step["id"] == "entry_comparison_forward":
            assert step["binding"] == {"kind": "entry_comparison", "registration": head["registration"],
                "attachment": head["entry_comparison_attachment"],
                "source": {"day": E, "source_folder": str(saved.package[E]), "synthetic_complete_close": True},
                "plan": daily._ref(operation / "entry_comparison/plan.json")}
        return {"status": "completed"}

    def run_profile(original, day, bundle, current_extras, out, *, run_step):
        assert original == profile and day == E and bundle == own and current_extras == extras
        assert out == operation / "entry_comparison"
        write(out / "plan.json", {"synthetic_exact_plan": True})
        run_step({"id": "entry_comparison_forward", "command": ["leader-entry-comparison-forward"],
            "expected_files": [str(out / "forward_result.json")]})
        return {"result": {"status": "entry_publication_saved_consumer_blocked_or_pending",
            "producer_complete": True, "consumer_required": True, "consumer_complete": False},
            "next_profile": {**profile, "last_source_day": E}}

    stub = SimpleNamespace(MUTABLE_PROFILE_KEYS=entry.MUTABLE_PROFILE_KEYS,
        input_directories=lambda profile: [], run_profile=run_profile)
    monkeypatch.setattr(daily, "_attached_recipe", lambda kind: stub)
    monkeypatch.setattr(entry, "run_profile", run_profile)
    monkeypatch.setattr(daily, "_run_command", command)
    result = daily.run(saved.directory, E, saved.package[E])
    assert result["entry_comparison"]["producer_complete"] is True
    assert result["entry_comparison"]["consumer_complete"] is False
    assert result["status"] == "partial_research_routes"
    assert [step["id"] for step in calls] == ["extras", "pub20", "eight_inputs", "entry_comparison_forward"]
    assert daily._read(saved.directory / "head.json")["profile"] == head["profile"]


def test_entry_recovery_uses_exact_two_source_refs_and_saved_attachment_without_running(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    head["entry_comparison_profile"] = {"first_decision_day": D}
    head["entry_comparison_attachment"] = write(tmp_path / "attachment.json", {"saved": True})
    operation = saved.directory / "operations/20261019"
    source = {"day": E, "source_folder": str(saved.package[E])}
    write(operation / "receipt.json", {"schema": daily.SCHEMA, "day": E, "source": source, **daily.FLAGS})
    own, extras = write(tmp_path / "own.json", {}), write(tmp_path / "extras.json", {})
    plan = {"source_day": E, "source_refs": [own, extras], "steps": [{"id": "entry_comparison_publication",
        "command": ["leader-entry-comparison", "publish"], "expected_files": [str(operation / "entry_comparison/publication.json")]}]}
    plan_ref = write(operation / "entry_comparison/plan.json", plan)
    checked = []
    stub = SimpleNamespace(validate_plan=lambda *args: checked.append(args) or plan)
    monkeypatch.setattr(daily, "_attached_recipe", lambda kind: stub)
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("Recovery cannot run a route"))
    returned, step = daily._recovery_step(saved.directory, E, "entry_comparison_publication", head,
        daily._read(saved.directory / "registration.json"))
    assert returned == operation
    assert checked == [(plan, head["entry_comparison_profile"], E, own["file"], extras["file"], operation / "entry_comparison")]
    assert step["binding"] == {"kind": "entry_comparison", "registration": head["registration"],
        "attachment": head["entry_comparison_attachment"], "source": source, "plan": plan_ref}


@pytest.mark.parametrize("command,schema", [
    ("seat-hypothesis-dated-forward", "public_seat_four_arm_dated_forward_result_v1"),
    ("seat-append-forward", "public_seat_append_four_arm_forward_result_v1"),
])
def test_seat_research_terminal_evidence_does_not_claim_account_completion(tmp_path, command, schema):
    file = tmp_path / "forward_result.json"
    result = {"schema": schema, "status": "blocked_or_waiting_research_only", "cases": [],
        "case_count": 0, "metrics": None, "candidate_panel_only": True,
        "execution_admitted": False, "selection_enabled": False, "strategy_validated": False}
    write(file, result)
    step = {"command": [command], "expected_files": [str(file)]}
    assert daily._outputs(step, E) == [daily._ref(file)]
    assert daily._read(file) == result and not (tmp_path / "head.json").exists()
    assert daily._command(["seat-daily", "project-static-audit"])[-2:] == ["seat-daily", "project-static-audit"]
    with pytest.raises(ValueError, match="may only project"):
        daily._command(["seat-daily", "register"])
    with pytest.raises(ValueError, match="may only publish"):
        daily._command(["leader-entry-comparison", "register"])


def test_public_seat_shared_binding_keeps_saved_consumer_partial_without_creating_account(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    profile = {"last_source_day": D, "run_directory": str(tmp_path / "not_created_books")}
    head["public_seat_profile"] = profile
    head["public_seat_attachment"] = write(tmp_path / "seat_attachment.json", {"saved": True})
    operation = saved.directory / "operations/20261019"
    source = {"day": E, "source_folder": str(saved.package[E])}
    catalog = tmp_path / "explicit_catalog.json"
    plan_ref = write(operation / "public_seat/plan.json", {"synthetic_plan": True})
    calls = []

    def command(step, directory, day):
        calls.append(step)
        assert directory == operation / "commands" and day == E
        assert step["binding"] == {"kind": "public_seat", "registration": head["registration"],
            "attachment": head["public_seat_attachment"], "source": source, "plan": plan_ref}
        return {"status": "completed"}

    def run_profile(original, day, package, out, *, run_step, catalog):
        assert original == profile and day == E and package == saved.package[E]
        assert out == operation / "public_seat" and catalog.name == "explicit_catalog.json"
        run_step({"id": "append_seat_forward", "command": ["seat-append-forward"],
            "expected_files": [str(out / "forward_result.json")]})
        return {"result": {"status": "publication_saved_consumer_blocked_or_pending", "daily_complete": False,
            "account_count": 0, "metrics": None}, "next_profile": {**profile, "last_source_day": E}}

    monkeypatch.setattr(seat, "run_profile", run_profile)
    monkeypatch.setattr(daily, "_run_command", command)
    old_core = deepcopy(head["profile"])
    result = daily._public_seat_refresh(head, {}, operation, E, source, saved.package[E], catalog=catalog)
    assert result["daily_complete"] is False and result["metrics"] is None
    assert head["public_seat_profile"]["last_source_day"] == E and head["profile"] == old_core
    assert len(calls) == 1 and not (tmp_path / "not_created_books").exists()


def test_http_source_request_keeps_network_unknown_until_original_outcome_and_child_rejects_false_claim(
        tmp_path, monkeypatch):
    outcome = tmp_path / "audit/outcome.json"
    step = {"id": "append_seat_source_audit", "command": ["seat-append-source-audit", "--out", str(outcome.parent)],
        "expected_files": [str(outcome)]}
    assert daily._command_flags(step)["network_accessed"] is None
    assert daily._command_flags(step)["network_access_unknown"] is True
    assert daily._command_network_observation(step) == {"network_accessed": None, "network_access_unknown": True}
    files = child.paths(tmp_path / "commands", step["id"])
    request = {"schema": child.REQUEST_SCHEMA, "step": daily._step_spec(step),
        "argv": daily._command(step["command"]), "cwd": str(daily.ROOT), "day": E,
        **daily._command_flags(step)}
    wrong = {**request, "network_accessed": False}
    bad_ref = write(files["request"], wrong)
    calls = []

    def process(*args, **kwargs):
        calls.append(args)
        def wait():
            write(outcome, {"network_accessed": True, "original_saved_source_audit": True})
            return 0
        return SimpleNamespace(pid=12345, wait=wait)

    monkeypatch.setattr(child.subprocess, "Popen", process)
    with pytest.raises(ValueError, match="exact saved daily command"):
        child.run_request(files["request"], bad_ref["sha256"])
    assert not calls and not files["started"].exists()
    correct = write(files["request"], request)
    terminal = child.run_request(files["request"], correct["sha256"])
    started = daily._read(files["started"])
    assert started["network_accessed"] is None and started["network_access_unknown"] is True
    assert terminal["status"] == "completed" and terminal["network_accessed"] is True
    assert terminal["network_access_unknown"] is False and len(calls) == 1
    assert daily._command_network_observation(step) == {"network_accessed": True, "network_access_unknown": False}
