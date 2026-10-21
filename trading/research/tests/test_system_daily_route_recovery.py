"""Saved-only route adoption; fixtures do not launch children or trading Engines."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import leader_entry_comparison_daily as entry
from trading.research import system_daily_refresh as daily
from trading.research import system_daily_route_recovery as recovery
from trading.research.tests.test_system_daily_refresh import D, E, F, saved_system, write


def fixture(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    # The source coordinator has genuinely gone beyond the route's missing D.
    head["last_source_day"] = F
    head["profile"]["last_source_day"] = F
    head["native_profile"]["source_day"] = F
    profile = {"last_source_day": D, "first_decision_day": D, "first_execution_day": E,
        "case_ids": ["fixture_case"], "operations": [], "publications": ["original_D_publication"]}
    attachment = {"schema": "system_daily_entry_comparison_attachment_v1",
        "system_registration": head["registration"], "initial_profile": deepcopy(profile), **daily.FLAGS}
    head["entry_comparison_attachment"] = write(saved.directory / "addenda/entry_comparison.json", attachment)
    head["entry_comparison_profile"] = profile
    write(saved.directory / "head.json", head)
    operation = saved.directory / "operations" / E.replace("-", "")
    out = operation / "entry_comparison"
    source = {"day": E, "source_folder": str(saved.package[E])}
    write(operation / "receipt.json", {"schema": daily.SCHEMA, "day": E, "source": source, **daily.FLAGS})
    own = write(tmp_path / "own/bundle.json", {"source_day": E})
    extras = write(tmp_path / "extras/result.json", {"date": E})
    publication = out / "publication.json"
    write(publication, {"decision_day": E, "execution_day": F, "published_at": E + "T18:20:00+08:00"})
    forward = out / "forward_result.json"
    write(forward, {"schema": "leader_entry_comparison_forward_result_v1", "status": "fixture_saved_only",
        "execution_progress_unknown": False, "case_count": 1,
        "cases": [{"case_id": "fixture_case", "last_processed_day": E}],
        "candidate_panel_only": True, "execution_admitted": False,
        "selection_enabled": False, "strategy_validated": False})
    steps = [{"id": "entry_comparison_publication", "requires": [],
        "command": ["leader-entry-comparison", "publish"], "expected_files": [str(publication)]},
        {"id": "entry_comparison_forward", "requires": ["entry_comparison_publication"],
        "command": ["leader-entry-comparison-forward"], "expected_files": [str(forward)]}]
    plan = {"source_day": E, "execution_day": F, "before_profile": deepcopy(profile),
        "parent_profile_sha256": entry._hash(profile), "source_refs": [own, extras], "steps": steps,
        "next_values": {"last_source_day": E, "publications": [*profile["publications"], str(publication)]}}
    plan_ref = write(out / "plan.json", plan)
    original_next_plan = lambda before, day, bundle, current_extras, directory: deepcopy(plan)
    monkeypatch.setattr(entry, "next_plan", original_next_plan)
    # Retain the real recipe validation/adoption and real daily terminal checks.
    write(out / "receipt.json", {"schema": entry.RESULT_SCHEMA, "day": E, "execution_day": F,
        "plan": plan_ref, "steps": {}, "consumer_required": True, "consumer_complete": False,
        "status": "original_interrupted_parent", **entry.FLAGS})
    for step in steps:
        bound = {**step, "binding": {"kind": "entry_comparison", "registration": head["registration"],
            "attachment": head["entry_comparison_attachment"], "source": source, "plan": plan_ref}}
        spec = daily._step_spec(bound)
        files = daily.command_child.paths(operation / "commands", step["id"])
        request = {"schema": daily.command_child.REQUEST_SCHEMA, "day": E, "step": spec,
            "argv": daily._command(spec["command"]), "cwd": str(daily.ROOT), "binding": bound["binding"],
            **daily._command_flags(spec)}
        request_ref = write(files["request"], request)
        started_ref = write(files["started"], {"schema": daily.command_child.START_SCHEMA,
            "request_ref": request_ref, "supervisor_pid": 32123, **daily._command_flags(spec)})
        write(files["terminal"], {"schema": daily.command_child.TERMINAL_SCHEMA,
            "request_ref": request_ref, "started_ref": started_ref, "day": E, "step": spec,
            "argv": request["argv"], "supervisor_pid": 32123,
            "status": "completed", "exit_code": 0, "execution_progress_unknown": False,
            "output_refs": daily._outputs(spec, E), **daily.FLAGS})
        write(operation / "commands" / (step["id"] + ".json"), {"step": step["id"], "day": E,
            "status": "started", "command": step["command"], "argv": request["argv"],
            "expected_files": spec["expected_files"], "child_request_ref": request_ref,
            "child_pid": 32123, "execution_progress_unknown": True, **daily.FLAGS})
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("No command runner in recovery"))
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: pytest.fail("No child in recovery"))
    monkeypatch.setattr(daily, "_opening", lambda *a, **k: pytest.fail("No opening rebuild in recovery"))
    return SimpleNamespace(saved=saved, head=head, operation=operation, out=out, plan=plan,
        publication=publication, source_files=[Path(own["file"]), Path(extras["file"])])


def test_historical_adoption_changes_only_route_prefix_and_repeat_is_no_write(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    protected = [f.operation / "receipt.json", f.out / "plan.json", f.publication,
        *f.source_files, *list((f.operation / "commands").iterdir())]
    original = {path: path.read_bytes() for path in protected}
    route_receipt = (f.out / "receipt.json").read_bytes()
    result = recovery.recover_route(f.saved.directory, E, "entry_comparison")
    assert result["status"] == "recovered_saved_route_complete"
    assert result["consumer_complete"] is True and result["engine_advanced"] is False
    head = daily._read(f.saved.directory / "head.json")
    after = head.pop("entry_comparison_profile")
    before = deepcopy(f.head)
    before.pop("entry_comparison_profile")
    assert head == before
    assert after["last_source_day"] == E and after["publications"][-1] == str(f.publication)
    assert after["operations"] == [str(f.out / "receipt.json")]
    # A real next-source profile can now append from E, without a date reset.
    assert after["last_source_day"] < F and f.plan["next_values"]["last_source_day"] == E
    assert all(path.read_bytes() == old for path, old in original.items())
    assert Path(result["original_route_receipt_backup"]["file"]).read_bytes() == route_receipt
    snapshot = {path: path.read_bytes() for path in f.saved.directory.rglob("*") if path.is_file()}
    repeated = recovery.recover_route(f.saved.directory, E, "entry_comparison")
    assert repeated["reused_saved_recovery"] is True and repeated["system_head_mutated"] is False
    assert all(path.read_bytes() == old for path, old in snapshot.items())


def test_missing_publisher_terminal_is_unknown_without_any_mutation(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    daily.command_child.paths(f.operation / "commands", "entry_comparison_publication")["terminal"].unlink()
    snapshot = {path: path.read_bytes() for path in f.saved.directory.rglob("*") if path.is_file()}
    result = recovery.recover_route(f.saved.directory, E, "entry_comparison")
    assert result["status"] == "unknown_saved_route_publication_incomplete"
    assert result["system_head_mutated"] is False and result["consumer_complete"] is False
    assert all(path.read_bytes() == old for path, old in snapshot.items())
    assert not (f.saved.directory / "route_recoveries").exists()


def test_saved_publication_can_be_adopted_with_unknown_consumer(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    daily.command_child.paths(f.operation / "commands", "entry_comparison_forward")["terminal"].unlink()
    result = recovery.recover_route(f.saved.directory, E, "entry_comparison")
    assert result["status"] == "recovered_saved_route_partial"
    assert result["producer_complete"] is True and result["consumer_complete"] is False
    assert daily._read(f.saved.directory / "head.json")["entry_comparison_profile"]["last_source_day"] == E
    projected = daily._read(f.out / "receipt.json")
    assert projected["steps"]["entry_comparison_forward"]["execution_progress_unknown"] is True
    assert result["opening_rebuilt"] is False and result["opening_refresh_required"] is True
    partial = result["command_evidence"]["entry_comparison_forward"]
    assert partial["request"] is not None and partial["terminal"] is None
    # An absent terminal may stay unknown, but an already observed request may
    # never change unnoticed after the partial adoption.
    request = Path(partial["request"]["file"])
    request.write_bytes(request.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="evidence changed"):
        recovery.recover_route(f.saved.directory, E, "entry_comparison")


def test_source_or_terminal_changes_reject_before_projection(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    path = daily.command_child.paths(f.operation / "commands", "entry_comparison_publication")["request"]
    request = daily._read(path)
    request["binding"]["source"]["day"] = F
    write(path, request)
    old = (f.saved.directory / "head.json").read_bytes()
    with pytest.raises(ValueError, match="different daily inputs"):
        recovery.recover_route(f.saved.directory, E, "entry_comparison")
    assert (f.saved.directory / "head.json").read_bytes() == old
    assert not (f.saved.directory / "route_recoveries").exists()


def test_route_already_beyond_D_cannot_roll_back(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    head = deepcopy(f.head)
    head["entry_comparison_profile"]["last_source_day"] = F
    write(f.saved.directory / "head.json", head)
    old = (f.saved.directory / "head.json").read_bytes()
    with pytest.raises(ValueError, match="cannot be rolled back"):
        recovery.recover_route(f.saved.directory, E, "entry_comparison")
    assert (f.saved.directory / "head.json").read_bytes() == old


def test_prepared_adoption_resumes_failed_head_write_without_repeating_recipe(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    # The existing ordinary recover-step remains conservative by default.
    with pytest.raises(ValueError, match="superseded"):
        daily._recovery_step(f.saved.directory, E, "entry_comparison_publication", f.head,
            daily._read(f.saved.directory / "registration.json"))
    protected = [f.operation / "receipt.json", f.out / "plan.json", f.publication,
        *f.source_files, *list((f.operation / "commands").iterdir())]
    snapshot = {path: path.read_bytes() for path in protected}
    old_receipt = (f.out / "receipt.json").read_bytes()
    original_atomic = daily._atomic

    def fail_head(path, payload):
        if Path(path) == f.saved.directory / "head.json":
            raise OSError("fixture interrupted head replacement")
        return original_atomic(path, payload)

    monkeypatch.setattr(daily, "_atomic", fail_head)
    with pytest.raises(OSError, match="interrupted head"):
        recovery.recover_route(f.saved.directory, E, "entry_comparison")
    evidence = f.saved.directory / "route_recoveries" / E.replace("-", "") / "entry_comparison"
    assert (evidence / "adoption.json").is_file() and not (evidence / "receipt.json").exists()
    assert daily._read(f.saved.directory / "head.json") == f.head
    assert (evidence / "original_route_receipt.json").read_bytes() == old_receipt
    monkeypatch.setattr(daily, "_atomic", original_atomic)
    monkeypatch.setattr(entry, "run_profile", lambda *a, **k: pytest.fail("Prepared adoption cannot repeat recipe"))
    result = recovery.recover_route(f.saved.directory, E, "entry_comparison")
    assert result["status"] == "recovered_saved_route_complete"
    updated = daily._read(f.saved.directory / "head.json")
    assert updated["entry_comparison_profile"]["last_source_day"] == E
    updated["entry_comparison_profile"] = f.head["entry_comparison_profile"]
    assert updated == f.head
    assert all(path.read_bytes() == content for path, content in snapshot.items())


def test_public_seat_real_recipe_retains_partial_branch_and_exact_after_repeat(tmp_path, monkeypatch):
    from trading.research import seat_daily as seat

    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    head["last_source_day"] = F
    head["profile"]["last_source_day"] = F
    head["native_profile"]["source_day"] = F
    arms = ["a", "b", "c", "d"]
    branch = {"publications": ["fixture_original_publication"], "last_publication_day": D,
        "raw_closes": [str(saved.package[D])], "first_execution_day": E,
        "last_execution_day": F, "last_decision_day": D,
        "last_processed_execution_day": None}
    catalog = write(tmp_path / "catalog.json", {"fixture_saved_catalog": True})
    profile = {"last_source_day": D, "operations": [], "arm_ids": arms,
        "old_seat": deepcopy(branch), "append_seat": {**deepcopy(branch), "catalog": catalog}}
    head["public_seat_attachment"] = write(saved.directory / "addenda/public_seat.json", {
        "schema": "system_daily_public_seat_attachment_v1", "system_registration": head["registration"],
        "initial_profile": deepcopy(profile), **daily.FLAGS})
    head["public_seat_profile"] = profile
    write(saved.directory / "head.json", head)
    operation = saved.directory / "operations" / E.replace("-", "")
    out = operation / "public_seat"
    source = {"day": E, "source_folder": str(saved.package[E])}
    write(operation / "receipt.json", {"schema": daily.SCHEMA, "day": E, "source": source, **daily.FLAGS})
    publication = out / "append_publication.json"
    write(publication, {"decision_day": E, "execution_day": F, "published_at": E + "T18:25:00+08:00"})
    steps = [{"id": "append_seat_publication", "requires": [], "command": ["seat-append-dated-inputs"],
        "expected_files": [str(publication)]}]
    for key, command, schema in (("old_seat", "seat-hypothesis-dated-forward", "public_seat_four_arm_dated_forward_result_v1"),
            ("append_seat", "seat-append-forward", "public_seat_append_four_arm_forward_result_v1")):
        result = {"schema": schema, "status": "advanced_four_isolated_dated_research_books",
            "account_count": 4, "cases": [{"arm_id": arm, "last_processed_execution_day": E} for arm in arms],
            "candidate_panel_only": True, "execution_admitted": False,
            "selection_enabled": False, "strategy_validated": False}
        if key == "append_seat":
            result["cases"][-1]["blocking_evidence"] = {"reason": "fixture_saved_partial"}
        file = out / (key + "_forward_result.json")
        write(file, result)
        steps.append({"id": key + "_forward", "requires": [], "command": [command], "expected_files": [str(file)]})
    plan = {"source_day": E, "execution_day": F, "before_profile": deepcopy(profile),
        "parent_profile_sha256": seat._hash(profile), "package": str(saved.package[E]),
        "catalog": catalog["file"], "append_catalog": catalog, "steps": steps, "blocked": {},
        "publication_files": {"old_seat": str(out / "unused_old_pub.json"), "append_seat": str(publication)}}
    plan_ref = write(out / "plan.json", plan)
    monkeypatch.setattr(seat, "next_plan", lambda *a, **k: deepcopy(plan))
    write(out / "receipt.json", {"schema": seat.RESULT_SCHEMA, "day": E, "execution_day": F,
        "plan": plan_ref, "steps": {}, "branches": {}, "metrics": None, **seat.FLAGS})
    original_route_bytes = (out / "receipt.json").read_bytes()
    for step in steps:
        bound = {**step, "binding": {"kind": "public_seat", "registration": head["registration"],
            "attachment": head["public_seat_attachment"], "source": source, "plan": plan_ref}}
        spec = daily._step_spec(bound)
        files = daily.command_child.paths(operation / "commands", step["id"])
        request = {"schema": daily.command_child.REQUEST_SCHEMA, "day": E, "step": spec,
            "argv": daily._command(spec["command"]), "cwd": str(daily.ROOT), "binding": bound["binding"],
            **daily._command_flags(spec)}
        request_ref = write(files["request"], request)
        claim_ref = write(files["started"], {"schema": daily.command_child.START_SCHEMA,
            "request_ref": request_ref, "supervisor_pid": 33441, **daily._command_flags(spec)})
        write(files["terminal"], {"schema": daily.command_child.TERMINAL_SCHEMA,
            "request_ref": request_ref, "started_ref": claim_ref, "day": E, "step": spec,
            "argv": request["argv"], "supervisor_pid": 33441, "status": "completed", "exit_code": 0,
            "execution_progress_unknown": False, "output_refs": daily._outputs(spec, E), **daily.FLAGS})
        write(operation / "commands" / (step["id"] + ".json"), {"step": step["id"], "day": E,
            "status": "failed_or_interrupted_no_automatic_retry", "command": step["command"],
            "argv": request["argv"], "expected_files": spec["expected_files"], "child_request_ref": request_ref,
            "child_pid": 33441, "execution_progress_unknown": True, **daily.FLAGS})
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("No command runner"))
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: pytest.fail("No child"))
    monkeypatch.setattr(daily, "_opening", lambda *a, **k: pytest.fail("No opening rebuild"))
    command_bytes = {path: path.read_bytes() for path in (operation / "commands").iterdir()}
    result = recovery.recover_route(saved.directory, E, "public_seat")
    assert result["status"] == "recovered_saved_route_partial" and result["consumer_complete"] is False
    assert result["saved_branch_observations"]["old_seat"]["consumer_complete"] is True
    assert result["saved_branch_observations"]["append_seat"]["consumer_complete"] is False
    updated = daily._read(saved.directory / "head.json")
    adopted = updated["public_seat_profile"]
    assert adopted["last_source_day"] == E and adopted["operations"] == [str(out / "receipt.json")]
    assert adopted["old_seat"]["last_processed_execution_day"] == E
    assert adopted["append_seat"]["last_processed_execution_day"] is None
    assert adopted["append_seat"]["publications"][-1] == str(publication)
    updated["public_seat_profile"] = profile
    assert updated == head
    assert Path(result["original_route_receipt_backup"]["file"]).read_bytes() == original_route_bytes
    assert all(path.read_bytes() == old for path, old in command_bytes.items())
    monkeypatch.setattr(seat, "run_profile", lambda *a, **k: pytest.fail("Exact-after repeat cannot rerun recipe"))
    repeated = recovery.recover_route(saved.directory, E, "public_seat")
    assert repeated["reused_saved_recovery"] is True and repeated["consumer_complete"] is False


@pytest.mark.parametrize("conflict", ["missing_terminal_changed_request", "failed_terminal_changed_binding"])
def test_partial_consumer_existing_binding_conflict_rejects_without_adoption(tmp_path, monkeypatch, conflict):
    f = fixture(tmp_path, monkeypatch)
    files = daily.command_child.paths(f.operation / "commands", "entry_comparison_forward")
    if conflict == "missing_terminal_changed_request":
        files["terminal"].unlink()
        request = daily._read(files["request"])
        request["binding"]["source"]["day"] = F
        write(files["request"], request)
    else:
        terminal = daily._read(files["terminal"])
        terminal.update(status="failed_command_no_automatic_retry", exit_code=7)
        terminal["binding"] = {**daily._read(files["request"])["binding"], "source": {"day": F}}
        write(files["terminal"], terminal)
    protected = {path: path.read_bytes() for path in f.saved.directory.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="binding"):
        recovery.recover_route(f.saved.directory, E, "entry_comparison")
    assert all(path.read_bytes() == old for path, old in protected.items())
    assert not (f.saved.directory / "route_recoveries").exists()


def test_partial_consumer_missing_terminal_cannot_change_original_child_pid(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    files = daily.command_child.paths(f.operation / "commands", "entry_comparison_forward")
    files["terminal"].unlink()
    claim = daily._read(files["started"])
    claim["supervisor_pid"] += 1
    write(files["started"], claim)
    protected = {path: path.read_bytes() for path in f.saved.directory.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="child claim.*binding"):
        recovery.recover_route(f.saved.directory, E, "entry_comparison")
    assert all(path.read_bytes() == old for path, old in protected.items())
    assert not (f.saved.directory / "route_recoveries").exists()
