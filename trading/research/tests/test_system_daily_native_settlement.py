"""Exact saved native settlement wiring; synthetic children, no Engine/network."""
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import shortline_native_preopen as preopen
from trading.research import shortline_native_preopen_forward as forward
from trading.research import system_daily_command_child as child
from trading.research import system_daily_refresh as m
from trading.research.tests.test_system_daily_refresh import D, E, F, saved_system, write


def wired(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    calendar = m._ref(head["native_profile"]["calendar"])

    def proof(day):
        return {"day": day, "source_folder": str(saved.package[day].resolve()),
            "synthetic_complete_close": True}

    for key, template in head["native_profile"]["forward_templates"].items():
        ipo = saved.directory.parent / "ipo" / (key + ".json")
        write(ipo, {"fixed_policy": key})
        template["argv"].extend(["--ipo-proof", str(ipo), "--research-master-run",
            head["native_profile"]["master_run"]])
        if key == "qualified":
            template["argv"].append("--ipo-append-only")
        account = Path(template["run_dir"]) / "head.json"
        value = m._read(account)
        value["raw_sources"] = [proof(D)]
        value["binding"] = {"method_id": "synthetic_" + key, "policy_id": "synthetic_" + key}
        write(account, value)
    registration = m._read(saved.directory / "registration.json")
    registration["initial_native_profile"] = deepcopy(head["native_profile"])
    head["registration"] = write(saved.directory / "registration.json", registration)
    write(saved.directory / "head.json", head)

    def load_plan(path):
        return {**m._read(path), "plan_ref": m._ref(path),
            "calendar": {**calendar, "byte_count": Path(calendar["file"]).stat().st_size}}

    monkeypatch.setattr(preopen, "load_preopen_plan", load_plan)
    monkeypatch.setattr(forward, "_panel_link", lambda plan, now:
        {"source_close_proof": proof(plan["source_day"])})
    calls, fail, wrong_result = [], set(), set()

    def popen(argv, **options):
        if argv[4:6] == ["-m", "trading.research.system_daily_command_child"]:
            request = argv[argv.index("--request") + 1]
            pin = argv[argv.index("--request-sha256") + 1]
            terminal = child.run_request(request, pin)
            return SimpleNamespace(pid=os.getpid(), wait=lambda: 0 if terminal["status"] == "completed" else 1)
        assert argv[5] == "shortline-native-preopen-forward"
        calls.append(argv[5:])
        run = Path(m._values(argv, "--run")[0])
        if run.name in fail:
            return SimpleNamespace(pid=os.getpid(), wait=lambda: 7)
        account = m._read(run / "head.json")
        for case in account["cases"].values():
            case["state"]["day"] = E
        account["raw_sources"] = [proof(D), proof(E)]
        write(run / "head.json", account)
        write(Path(m._values(argv, "--result-json")[0]), {
            "schema": "native_shortline_preopen_forward_result_v1",
            "status": "waiting_for_exact_complete_raw_close" if run.name in wrong_result else "advanced_isolated_native_research_books",
            "checkpoint": str(run / "head.json"), "run_directory": str(run), "case_count": 2,
            **account["binding"], "cases": [{"model_kind": kind, "last_processed_day": E} for kind in ("lr", "lgbm")]})
        return SimpleNamespace(pid=os.getpid(), wait=lambda: 0)

    def forbidden(*args, **kwargs):
        pytest.fail("Settlement cannot call a producer, opening or lowvol")

    for name in ("_native_production_refresh", "_opening", "_lowvol", "_recent_actions"):
        monkeypatch.setattr(m, name, forbidden)
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    saved.head = head
    return SimpleNamespace(saved=saved, calls=calls, fail=fail, wrong_result=wrong_result, proof=proof)


def test_two_original_templates_terminal_pins_and_exact_noop_without_signal_head_mutation(tmp_path, monkeypatch):
    state = wired(tmp_path, monkeypatch)
    saved = state.saved
    frozen = {path: path.read_bytes() for path in [saved.directory / "head.json",
        saved.directory / "registration.json", *map(Path, saved.head["native_profile"]["latest_plans"].values())]}
    result = m.settle_native(saved.directory, E, saved.package[E])
    assert result["status"] == "completed_native_settlement"
    assert result["whole_system_daily_complete"] is False and result["signals_published"] is False
    assert len(state.calls) == 2
    for key, command in zip(("v1", "qualified"), state.calls):
        original = saved.head["native_profile"]["forward_templates"][key]
        assert m._static_native_command(command) == m._static_native_command(original["argv"])
        assert m._values(command, "--plan") == original["plan_prefix"]
        assert m._values(command, "--raw-close") == [str(saved.package[D]), str(saved.package[E])]
        assert m._values(command, "--through") == [E]
        outcome = result["policies"][key]
        assert outcome["consumer_head_ref"] == m._ref(Path(original["run_dir"]) / "head.json")
        assert outcome["terminal_ref"]["file"].endswith(".child_terminal.json")
    again = m.settle_native(saved.directory, E, saved.package[E])
    assert again["status"] == "already_completed_native_settlement" and again["consumer_called"] is False
    assert len(state.calls) == 2
    assert all(path.read_bytes() == value for path, value in frozen.items())
    assert not (saved.directory / "operations").exists()


@pytest.mark.parametrize("problem", ["wrong_raw_day", "incomplete_raw", "nonadjacent"])
def test_shared_invalid_source_is_refused_before_any_consumer(tmp_path, monkeypatch, problem):
    state = wired(tmp_path, monkeypatch)
    saved = state.saved
    if problem == "incomplete_raw":
        monkeypatch.setattr(m, "_source", lambda *a, **k: (_ for _ in ()).throw(ValueError("incomplete raw")))
    day = F if problem == "nonadjacent" else E
    package = saved.package[D] if problem == "wrong_raw_day" else saved.package[E]
    with pytest.raises(ValueError):
        m.settle_native(saved.directory, day, package)
    assert not state.calls and not (saved.directory / "settlements").exists()


@pytest.mark.parametrize("problem", ["static_ipo", "wrong_plan", "wrong_head", "original_started", "original_failed", "orphan_claim"])
def test_policy_preflight_failure_preserves_bad_account_and_settles_other_policy(tmp_path, monkeypatch, problem):
    state = wired(tmp_path, monkeypatch)
    saved = state.saved
    template = saved.head["native_profile"]["forward_templates"]["v1"]
    account = Path(template["run_dir"]) / "head.json"
    original = saved.directory / "operations" / E.replace("-", "") / "commands"
    if problem == "static_ipo":
        head = m._read(saved.directory / "head.json")
        target = head["native_profile"]["forward_templates"]["v1"]
        target["argv"] = m._set(target["argv"], "--ipo-proof", ["changed.json"])
        write(saved.directory / "head.json", head)
    elif problem == "wrong_plan":
        write(Path(template["plan_prefix"][-1]), {"source_day": D, "execution_day": F})
    elif problem == "wrong_head":
        value = m._read(account)
        value["cases"]["lr"]["state"]["day"] = F
        write(account, value)
    elif problem == "orphan_claim":
        write(child.paths(original, "native_forward_v1")["request"], {"unresolved_original_claim": True})
    else:
        write(original / "native_forward_v1.json", {"status": problem.removeprefix("original_")})
    before = account.read_bytes()
    result = m.settle_native(saved.directory, E, saved.package[E])
    assert result["status"] == "partial_native_settlement_no_automatic_retry"
    assert result["policies"]["v1"]["status"] == "blocked_native_settlement_no_automatic_retry"
    assert result["policies"]["qualified"]["status"] == "completed"
    assert account.read_bytes() == before
    assert len(state.calls) == 1 and "--ipo-append-only" in state.calls[0]


@pytest.mark.parametrize("pending", [False, True])
def test_failed_attempt_never_retries_and_does_not_repeat_successful_other_policy(tmp_path, monkeypatch, pending):
    state = wired(tmp_path, monkeypatch)
    state.fail.add("v1")
    saved = state.saved
    result = m.settle_native(saved.directory, E, saved.package[E])
    assert result["status"] == "partial_native_settlement_no_automatic_retry"
    attempt = saved.directory / "settlements" / E.replace("-", "") / "commands" / "native_forward_v1.json"
    if pending:
        receipt = m._read(attempt)
        receipt["status"] = "started"
        receipt["execution_progress_unknown"] = True
        write(attempt, receipt)
    original = attempt.read_bytes()
    assert len(state.calls) == 2
    again = m.settle_native(saved.directory, E, saved.package[E])
    assert again["status"] == "partial_native_settlement_no_automatic_retry"
    assert again["consumer_called"] is False and attempt.read_bytes() == original
    assert len(state.calls) == 2


def test_same_day_different_raw_package_cannot_replace_completed_settlement(tmp_path, monkeypatch):
    state = wired(tmp_path, monkeypatch)
    saved = state.saved
    m.settle_native(saved.directory, E, saved.package[E], policy="v1")
    alternate = saved.package[E].with_name(E + "_different_source")
    write(alternate / "plan.json", {"day": E})
    with pytest.raises(ValueError, match="source or original registration changed"):
        m.settle_native(saved.directory, E, alternate, policy="v1")
    assert len(state.calls) == 1


def test_exit_zero_with_wrong_result_status_cannot_claim_settled_or_hide_other_success(tmp_path, monkeypatch):
    state = wired(tmp_path, monkeypatch)
    state.wrong_result.add("v1")
    saved = state.saved
    result = m.settle_native(saved.directory, E, saved.package[E])
    assert result["status"] == "partial_native_settlement_no_automatic_retry"
    assert result["policies"]["v1"]["status"] == "failed_native_settlement_validation_no_automatic_retry"
    assert result["policies"]["qualified"]["status"] == "completed"
    assert len(state.calls) == 2
    again = m.settle_native(saved.directory, E, saved.package[E])
    assert again["status"] == "partial_native_settlement_no_automatic_retry"
    assert again["consumer_called"] is False and len(state.calls) == 2


@pytest.mark.parametrize("problem", ["missing_terminal", "legacy_receipt", "changed_head", "changed_plan"])
def test_noop_requires_original_terminal_and_exact_unchanged_head_and_plans(tmp_path, monkeypatch, problem):
    state = wired(tmp_path, monkeypatch)
    saved = state.saved
    m.settle_native(saved.directory, E, saved.package[E], policy="v1")
    commands = saved.directory / "settlements" / E.replace("-", "") / "commands"
    files = child.paths(commands, "native_forward_v1")
    if problem == "missing_terminal":
        files["terminal"].unlink()
    elif problem == "legacy_receipt":
        receipt_file = commands / "native_forward_v1.json"
        receipt = m._read(receipt_file)
        receipt.pop("child_request_ref")
        write(receipt_file, receipt)
    elif problem == "changed_head":
        account = Path(saved.head["native_profile"]["forward_templates"]["v1"]["run_dir"]) / "head.json"
        account.write_bytes(account.read_bytes() + b" ")
    else:
        plan = Path(saved.head["native_profile"]["latest_plans"]["v1"])
        plan.write_bytes(plan.read_bytes() + b" ")
    result = m.settle_native(saved.directory, E, saved.package[E], policy="v1")
    assert result["status"] == "partial_native_settlement_no_automatic_retry"
    assert result["policies"]["v1"]["status"] == "blocked_native_settlement_no_automatic_retry"
    assert result["consumer_called"] is False and len(state.calls) == 1


def test_cli_dispatches_only_settlement_and_returns_partial_failure(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(m, "settle_native", lambda *args, **kwargs:
        (calls.append((args, kwargs)) or {"status": "partial_native_settlement_no_automatic_retry"}))
    monkeypatch.setattr(m, "run", lambda *a, **k: pytest.fail("Must not run the daily producer"))
    assert m.main(["settle-native", "--run-dir", str(tmp_path), "--day", E,
        "--package", str(tmp_path / "raw"), "--policy", "qualified"]) == 1
    assert calls == [((tmp_path, E, tmp_path / "raw"), {"policy": "qualified"})]
    assert json.loads(capsys.readouterr().out)["status"] == "partial_native_settlement_no_automatic_retry"
