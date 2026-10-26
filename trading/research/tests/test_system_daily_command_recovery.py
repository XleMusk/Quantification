"""Saved-command recovery only: isolated child processes, no market or Engine."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from trading.research import system_daily_command_child as child
from trading.research import system_daily_refresh as m
from trading.research import daily_refresh_recipe as recipe
from trading.research.tests.test_system_daily_refresh import D, E, F, saved_system, write


def _small_real_child(monkeypatch, output, *, exit_code=0, payload=None, logical_command=None):
    """Replace only the research command with a real, tiny Python process."""
    marker = output.parent / "actual_child_calls.txt"
    payload = {"synthetic_output": True} if payload is None else payload
    code = (
        "from pathlib import Path; import json; "
        f"p=Path({str(output)!r}); p.parent.mkdir(parents=True,exist_ok=True); "
        f"Path({str(marker)!r}).open('a',encoding='utf-8').write('called\\n'); "
        f"p.write_text(json.dumps({payload!r}),encoding='utf-8'); "
        f"raise SystemExit({exit_code})"
    )
    argv = [sys.executable, "-B", "-X", "utf8", "-c", code]
    original_command, real_popen = m._command, m.subprocess.Popen

    def command(logical):
        production = original_command(logical)  # Keep the production command allow-list.
        return list(argv) if logical_command is None or list(logical) == logical_command else production

    def popen(actual, **options):
        if actual == argv:
            return real_popen(actual, **options)
        assert actual[4:6] == ["-m", "trading.research.system_daily_command_child"]
        request = actual[actual.index("--request") + 1]
        pin = actual[actual.index("--request-sha256") + 1]
        terminal = child.run_request(request, pin)
        return SimpleNamespace(pid=os.getpid(), wait=lambda:
            0 if terminal["status"] == "completed" else 1)

    monkeypatch.setattr(m, "_command", command)
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    return marker


def _core_operation(saved):
    operation = saved.directory / "operations" / E.replace("-", "")
    output = operation / "artifacts" / "environment" / "result.json"
    step = {"id": "environment", "command": ["leader-environment", "--out", str(output.parent)],
        "expected_files": [str(output)], "requires": []}
    source = {"day": E, "source_folder": str(saved.package[E].resolve()), "synthetic_complete_close": True}
    write(operation / "receipt.json", {"schema": m.SCHEMA, "day": E, "source": source,
        "steps": {}, **m.FLAGS})
    profile = deepcopy(saved.head["profile"])
    profile.update(last_source_day=E, source_days=[D, E])
    plan = {"schema": recipe.PLAN_SCHEMA, "source_day": E, "execution_day": F,
        "package": str(saved.package[E].resolve()), "out": str(operation.resolve()),
        "steps": [step], "next_profile": profile, **m.FLAGS}
    write(operation / "plan.json", plan)
    step["binding"] = {"kind": "core", "registration": m._ref(saved.directory / "registration.json"),
        "source": source, "plan": m._ref(operation / "plan.json")}
    return operation, step, output


def _unclaimed_parent(operation, step, *, status="started"):
    path = operation / "commands" / (step["id"] + ".json")
    receipt = m._read(path)
    receipt.update(status=status, execution_progress_unknown=True)
    for key in ("output_refs", "exit_code", "supervisor_exit_code", "terminal_observed_at",
                "child_terminal_ref"):
        receipt.pop(key, None)
    write(path, receipt)
    return path, path.read_bytes()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_independent_terminal_records_real_child_exit_without_retry(tmp_path, monkeypatch, exit_code):
    saved = saved_system(tmp_path, monkeypatch)
    operation, step, output = _core_operation(saved)
    marker = _small_real_child(monkeypatch, output, exit_code=exit_code)
    result = m._run_command(step, operation / "commands", E)
    files = child.paths(operation / "commands", step["id"])
    terminal, request = m._read(files["terminal"]), m._read(files["request"])
    assert terminal["exit_code"] == exit_code
    assert terminal["execution_progress_unknown"] is False
    assert terminal["request_ref"] == m._ref(files["request"])
    assert request["day"] == E and request["step"] == m._step_spec(step)
    assert marker.read_text(encoding="utf-8").splitlines() == ["called"]
    if exit_code:
        assert result["status"] == "failed_no_automatic_retry"
        with pytest.raises(ValueError, match="exit zero"):
            m.recover_step(saved.directory, E, step["id"])
    else:
        assert result["status"] == "completed"
        assert terminal["output_refs"] == [m._ref(output)]
    assert m._run_command(step, operation / "commands", E) == result
    assert marker.read_text(encoding="utf-8").splitlines() == ["called"]


@pytest.mark.parametrize("original_status", ["started", "failed_or_interrupted_no_automatic_retry"])
def test_recover_accepts_saved_terminal_preserves_attempt_and_never_runs_producer(
        tmp_path, monkeypatch, original_status):
    saved = saved_system(tmp_path, monkeypatch)
    operation, step, output = _core_operation(saved)
    marker = _small_real_child(monkeypatch, output)
    assert m._run_command(step, operation / "commands", E)["status"] == "completed"
    attempt, original_bytes = _unclaimed_parent(operation, step, status=original_status)
    snapshot = {path: path.read_bytes() for path in
        (saved.directory / "head.json", saved.directory / "registration.json", operation / "plan.json")}
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: pytest.fail("Recovery cannot launch a child"))
    recovered = m.recover_step(saved.directory, E, step["id"])
    assert recovered["status"] == "recovered_saved_command"
    assert m.recover_step(saved.directory, E, step["id"])["status"] == "already_recovered"
    adopted = m._run_command(step, operation / "commands", E)
    assert adopted["status"] == "completed" and adopted["recovered_saved_child"] is True
    assert adopted["producer_repeated"] is False
    assert attempt.read_bytes() == original_bytes
    assert all(path.read_bytes() == content for path, content in snapshot.items())
    assert marker.read_text(encoding="utf-8").splitlines() == ["called"]


def test_missing_terminal_is_unknown_and_changed_or_cross_bound_evidence_is_rejected(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    operation, step, output = _core_operation(saved)
    _small_real_child(monkeypatch, output)
    assert m._run_command(step, operation / "commands", E)["status"] == "completed"
    attempt, original_bytes = _unclaimed_parent(operation, step)
    files = child.paths(operation / "commands", step["id"])
    terminal_bytes = files["terminal"].read_bytes()
    files["terminal"].unlink()
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: pytest.fail("Unknown cannot retry"))
    assert m.recover_step(saved.directory, E, step["id"])["status"] == "unknown_no_successful_child_terminal"
    assert attempt.read_bytes() == original_bytes
    files["terminal"].write_bytes(terminal_bytes)
    original_output = output.read_bytes()
    output.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="bytes"):
        m.recover_step(saved.directory, E, step["id"])
    output.write_bytes(original_output)
    for field, changed in (("day", F), ("argv", ["changed-command"])):
        terminal = json.loads(terminal_bytes)
        terminal[field] = changed
        write(files["terminal"], terminal)
        with pytest.raises(ValueError, match="different daily inputs"):
            m.recover_step(saved.directory, E, step["id"])
    files["terminal"].write_bytes(terminal_bytes)
    terminal = json.loads(terminal_bytes)
    terminal["exit_code"] = False  # bool equals zero, but is not a process exit integer.
    write(files["terminal"], terminal)
    with pytest.raises(ValueError, match="exit zero"):
        m.recover_step(saved.directory, E, step["id"])
    files["terminal"].write_bytes(terminal_bytes)
    request_bytes = files["request"].read_bytes()
    request = json.loads(request_bytes)
    request["binding"]["source"]["day"] = F
    write(files["request"], request)
    with pytest.raises(ValueError, match="different daily inputs"):
        m.recover_step(saved.directory, E, step["id"])
    files["request"].write_bytes(request_bytes)
    receipt = m._read(attempt)
    receipt["child_terminal_ref"] = m._ref(files["terminal"])
    write(attempt, receipt)
    terminal = json.loads(terminal_bytes)
    terminal["recorded_at"] = "changed-after-parent-pin"
    write(files["terminal"], terminal)
    with pytest.raises(ValueError, match="different daily inputs"):
        m.recover_step(saved.directory, E, step["id"])
    files["terminal"].write_bytes(terminal_bytes)
    attempt.write_bytes(original_bytes)
    assert not files["recovery"].exists()
    assert attempt.read_bytes() == original_bytes


def test_tampered_saved_recovery_is_rejected_without_producer_or_state_mutation(tmp_path, monkeypatch):
    """A changed recovery record must fail closed and never launch a producer."""
    saved = saved_system(tmp_path, monkeypatch)
    operation, step, output = _core_operation(saved)
    marker = _small_real_child(monkeypatch, output)
    assert m._run_command(step, operation / "commands", E)["status"] == "completed"
    attempt, original_bytes = _unclaimed_parent(operation, step)
    before = {path: path.read_bytes() for path in
              (saved.directory / "head.json", saved.directory / "registration.json", operation / "plan.json")}
    assert m.recover_step(saved.directory, E, step["id"])["status"] == "recovered_saved_command"
    recovery = child.paths(operation / "commands", step["id"])["recovery"]
    recovery_value = m._read(recovery)
    recovery_value["producer_repeated"] = True
    write(recovery, recovery_value)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: pytest.fail("Tampered recovery cannot launch a producer"))
    with pytest.raises(ValueError, match="changed or belongs"):
        m.recover_step(saved.directory, E, step["id"])
    assert attempt.read_bytes() == original_bytes
    assert all(path.read_bytes() == content for path, content in before.items())
    assert marker.read_text(encoding="utf-8").splitlines() == ["called"]


def test_successful_child_exit_cannot_recover_native_old_head(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    operation, _, _ = _core_operation(saved)
    account = Path(saved.head["native_profile"]["forward_templates"]["v1"]["run_dir"]) / "head.json"
    head = deepcopy(saved.head)
    head["native_profile"]["source_day"] = E
    source = m._read(operation / "receipt.json")["source"]
    registration = m._read(saved.directory / "registration.json")
    plan_file, _ = m._native_command_plan(head, registration, saved.head["native_profile"],
        operation, E, source)
    step = m._native_forward_step(head["native_profile"], "v1", E, saved.package[E], operation)
    step["binding"] = {"kind": "native", "registration": head["registration"],
        "source": source, "plan": m._ref(plan_file)}
    _small_real_child(monkeypatch, account,
        payload={"cases": {kind: {"state": {"day": D}} for kind in ("lr", "lgbm")}},
        logical_command=step["command"])
    result = m._run_command(step, operation / "commands", E)
    terminal = m._read(child.paths(operation / "commands", step["id"])["terminal"])
    assert terminal["exit_code"] == 0
    assert terminal["status"] == "failed_or_interrupted_no_automatic_retry"
    assert "exact requested" in terminal["error"]
    assert result["status"] != "completed"
    with pytest.raises(ValueError, match="exit zero"):
        m.recover_step(saved.directory, E, step["id"])


def test_recover_step_cli_routes_only_to_explicit_saved_recovery(tmp_path, monkeypatch, capsys):
    calls = []
    def recover(run_dir, day, step_id):
        calls.append((run_dir, day, step_id))
        return {"status": "unknown_no_successful_child_terminal"}
    monkeypatch.setattr(m, "recover_step", recover)
    monkeypatch.setattr(m, "run", lambda *a, **k: pytest.fail("Recovery CLI cannot run the daily driver"))
    assert m.main(["recover-step", "--run-dir", str(tmp_path), "--day", E,
        "--step", "environment"]) == 1
    assert calls == [(tmp_path, E, "environment")]
    assert json.loads(capsys.readouterr().out)["status"] == "unknown_no_successful_child_terminal"


def test_clean_supervisor_cli_and_real_allowlisted_command_can_be_recovered(tmp_path, monkeypatch):
    from trading.research.tests.test_leader_identity_registry import registered

    saved = saved_system(tmp_path, monkeypatch)
    operation, _, output = _core_operation(saved)
    fixture = tmp_path / "tiny_registry"
    fixture.mkdir()
    _, _, registry = registered(fixture)
    step = {"id": "episodes", "command": ["leader-identity-episodes", "--registry", str(registry),
        "--out", str(output)], "expected_files": [str(output)], "requires": []}
    plan_file = operation / "plan.json"
    plan = m._read(plan_file)
    plan["steps"] = [step]
    write(plan_file, plan)
    step["binding"] = {"kind": "core", "registration": m._ref(saved.directory / "registration.json"),
        "source": m._read(operation / "receipt.json")["source"], "plan": m._ref(plan_file)}

    # Both the supervisor and its allow-listed CLI run in clean processes.
    # No Popen or _command replacement is installed in this integration test.
    completed = m._run_command(step, operation / "commands", E)
    files = child.paths(operation / "commands", step["id"])
    terminal = m._read(files["terminal"])
    assert completed["status"] == "completed" and completed["supervisor_exit_code"] == 0
    assert terminal["exit_code"] == 0 and terminal["status"] == "completed"
    assert terminal["supervisor_pid"] == completed["child_pid"] != os.getpid()
    assert terminal["command_pid"] not in (os.getpid(), terminal["supervisor_pid"])
    assert terminal["output_refs"] == completed["output_refs"] == [m._ref(output)]
    assert m._read(output)["member_count"] == 2
    attempt, original_bytes = _unclaimed_parent(operation, step)
    artifact_bytes, terminal_bytes = output.read_bytes(), files["terminal"].read_bytes()
    assert m.recover_step(saved.directory, E, step["id"])["status"] == "recovered_saved_command"
    assert m._run_command(step, operation / "commands", E)["recovered_saved_child"] is True
    assert attempt.read_bytes() == original_bytes
    assert output.read_bytes() == artifact_bytes and files["terminal"].read_bytes() == terminal_bytes


def test_native_success_recovers_only_original_registered_run_and_head(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    operation, _, _ = _core_operation(saved)
    source = m._read(operation / "receipt.json")["source"]
    registration = m._read(saved.directory / "registration.json")
    plan_file, _ = m._native_command_plan(saved.head, registration, saved.head["native_profile"],
        operation, E, source)
    step = m._native_forward_step(saved.head["native_profile"], "v1", E, saved.package[E], operation)
    step["binding"] = {"kind": "native", "registration": saved.head["registration"],
        "source": source, "plan": m._ref(plan_file)}
    account = Path(step["expected_files"][0])
    marker = _small_real_child(monkeypatch, account,
        payload={"cases": {kind: {"state": {"day": E}} for kind in ("lr", "lgbm")}},
        logical_command=step["command"])
    assert m._run_command(step, operation / "commands", E)["status"] == "completed"
    attempt, original_bytes = _unclaimed_parent(operation, step)
    original_plan = plan_file.read_bytes()
    plan = json.loads(original_plan)
    plan["profile"]["forward_templates"]["v1"]["run_dir"] = str(tmp_path / "another_account")
    write(plan_file, plan)
    with pytest.raises(ValueError, match="registered account"):
        m.recover_step(saved.directory, E, step["id"])
    plan_file.write_bytes(original_plan)
    plan = json.loads(original_plan)
    plan["steps"][0]["expected_files"] = [str(tmp_path / "another_account" / "head.json")]
    write(plan_file, plan)
    with pytest.raises(ValueError, match="exact registered run/head"):
        m.recover_step(saved.directory, E, step["id"])
    plan_file.write_bytes(original_plan)
    assert m.recover_step(saved.directory, E, step["id"])["status"] == "recovered_saved_command"
    assert m._run_command(step, operation / "commands", E)["status"] == "completed"
    assert attempt.read_bytes() == original_bytes and plan_file.read_bytes() == original_plan
    assert marker.read_text(encoding="utf-8").splitlines() == ["called"]
