"""Existing PAPER openings close through the saved daily command contract only."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from trading import native_intraday_paper as paper
from trading.research import native_paper_daily_close as daily_close
from trading.research import system_daily_refresh as daily
from trading.tests.test_native_intraday_paper import inputs, read, ref, write
from trading.tests.test_published_close_intent import D, E, F


def _prepare_fixture(inputs, monkeypatch):
    original = paper.prepare_inputs

    def prepared(plan, prior_package, **kwargs):
        result = original(plan, prior_package, **kwargs)
        result.proof["source_close_proof"]["source_folder"] = str(Path(prior_package).resolve())
        result.proof["qualification_inputs"] = {}
        return result

    monkeypatch.setattr(paper, "prepare_inputs", prepared)
    return original, prepared


def _postclose(inputs):
    inputs.clock["now"] = datetime.fromisoformat(E + "T15:05:00+08:00")
    return inputs.clock["now"]


def _source(inputs):
    return {"day": E, "source_folder": str(inputs.folder / "new_E_close_package")}


def _command_options(step):
    command = step["command"]
    assert command[0] == "native-intraday-paper"
    assert len(command[1:]) % 2 == 0
    options = dict(zip(command[1::2], command[2::2]))
    assert options["--phase"] == "closing"
    return {key[2:].replace("-", "_"): value for key, value in options.items()}


def test_existing_opening_closes_original_D_plan_with_real_two_model_accounts(inputs, monkeypatch, tmp_path):
    _prepare_fixture(inputs, monkeypatch)
    inputs.call(inputs.first, "opening")
    before = read(inputs.run_dir / "head.json")
    original_plans = {key: Path(bundle["plan"]).read_bytes()
                      for key, bundle in (("original", inputs.first), ("next", inputs.second))}
    now = _postclose(inputs)
    step = daily_close.prepare_step(inputs.run_dir, inputs.first["closing"], E, now=now)
    assert step["source_day"] == D and step["execution_day"] == E
    assert step["opening_inputs"] == before["inputs"]
    assert step["step"]["command"][step["step"]["command"].index("--plan") + 1] == str(inputs.first["plan"].resolve())
    executions = []

    def execute(spec, directory, day):
        executions.append(deepcopy(spec))
        assert day == E and directory.name == "commands"
        options = _command_options(spec)
        assert options["plan"] == str(inputs.first["plan"].resolve())
        assert options["prior_package"] == str(inputs.first["prior_package"].resolve())
        assert options["plan"] != str(inputs.second["plan"].resolve())
        result = paper.run(**options, now=now)
        return {"status": "completed", "paper_result": result}

    result = daily_close.refresh([(inputs.run_dir, inputs.first["closing"])], E, _source(inputs),
        tmp_path / "operation", {"fixture_registration": True}, execute=execute, now=now)
    assert result["daily_complete"] is True and len(executions) == 1
    after = read(inputs.run_dir / "head.json")
    assert after["phase"] == "closing" and after["sequence"] == 2
    assert after["source_day"] == D and after["execution_day"] == E
    assert after["inputs"] == before["inputs"] and len(after["cases"]) == 2
    for kind, case in after["cases"].items():
        previous = before["cases"][kind]["state"]
        assert case["state"]["day"] == E and case["completed_sessions"] == 1
        for key in ("cash", "fills", "orders", "positions"):
            assert case["state"][key] == previous[key]
        assert case["daily"]["equity"] is not None
    assert original_plans == {"original": Path(inputs.first["plan"]).read_bytes(),
                              "next": Path(inputs.second["plan"]).read_bytes()}
    already = daily_close.prepare_step(inputs.run_dir, inputs.first["closing"], E, now=now)
    assert already["complete"] is True and already["step"] is None


def test_absent_and_bad_sessions_do_not_create_accounts_or_hide_valid_close(inputs, monkeypatch, tmp_path):
    original, enriched = _prepare_fixture(inputs, monkeypatch)
    valid = inputs.run_dir
    wrong_day, missing_source = tmp_path / "wrong_day_run", tmp_path / "missing_source_run"
    missing_quote, bad_quote = tmp_path / "missing_quote_run", tmp_path / "bad_quote_run"
    absent = tmp_path / "absent_run"
    inputs.call(inputs.first, "opening", run_dir=valid)
    inputs.call(inputs.second, "opening", run_dir=wrong_day)
    inputs.call(inputs.first, "opening", run_dir=missing_quote)
    inputs.call(inputs.first, "opening", run_dir=bad_quote)
    monkeypatch.setattr(paper, "prepare_inputs", original)
    inputs.call(inputs.first, "opening", run_dir=missing_source)
    monkeypatch.setattr(paper, "prepare_inputs", enriched)
    invalid = deepcopy(read(inputs.first["closing"]))
    invalid["date"] = F
    bad_quote_file = write(inputs.folder / "wrong_session_closing.json", invalid)
    untouched = {str(folder): (folder / "head.json").read_bytes()
                 for folder in (wrong_day, missing_source, missing_quote, bad_quote)}
    now = _postclose(inputs)
    calls = []

    def execute(spec, directory, day):
        calls.append(spec["command"])
        try:
            paper.run(**_command_options(spec), now=now)
        except ValueError as error:
            return {"status": "failed_no_automatic_retry", "error": str(error)}
        return {"status": "completed"}

    result = daily_close.refresh([
        (absent, inputs.first["closing"]), (wrong_day, inputs.second["closing"]),
        (missing_quote, inputs.folder / "absent_quote.json"),
        (missing_source, inputs.first["closing"]), (bad_quote, bad_quote_file),
        (valid, inputs.first["closing"])], E, _source(inputs), tmp_path / "mixed_operation",
        {}, execute=execute, now=now)
    assert result["daily_complete"] is False
    rows = {row["run_dir"]: row for row in result["rows"]}
    assert rows[str(absent.resolve())]["status"] == "not_created_no_close_required"
    assert not absent.exists()
    assert rows[str(wrong_day.resolve())]["status"] == "paper_session_day_differs_no_roll_forward"
    assert rows[str(missing_quote.resolve())]["status"] == "paper_closing_input_unavailable"
    assert rows[str(missing_source.resolve())]["status"] == "paper_closing_input_unavailable"
    assert rows[str(bad_quote.resolve())]["status"] == "failed_no_automatic_retry"
    assert rows[str(valid.resolve())]["complete"] is True
    assert len(calls) == 2  # Only the explicit bad quote and the independent valid opening.
    assert read(valid / "head.json")["phase"] == "closing"
    for folder, payload in untouched.items():
        assert (Path(folder) / "head.json").read_bytes() == payload
    empty_operation = tmp_path / "empty_operation"
    assert daily_close.refresh([], E, _source(inputs), empty_operation, {}, execute=execute, now=now) is None
    assert not empty_operation.exists()


def test_saved_close_plan_is_immutable_and_delegates_completed_or_failed_deduplication(inputs, monkeypatch, tmp_path):
    _prepare_fixture(inputs, monkeypatch)
    inputs.call(inputs.first, "opening")
    now = _postclose(inputs)
    source, operation = _source(inputs), tmp_path / "saved_operation"
    requests = [(inputs.run_dir, inputs.first["closing"])]
    receipts, launched, invoked = {}, [], []

    def execute(spec, directory, day):
        invoked.append(spec["id"])
        if spec["id"] not in receipts:
            launched.append(deepcopy(spec))
            receipts[spec["id"]] = {"status": "completed", "command": deepcopy(spec["command"])}
        assert receipts[spec["id"]]["command"] == spec["command"]
        return receipts[spec["id"]]

    first = daily_close.refresh(requests, E, source, operation, {}, execute=execute, now=now)
    saved_plan_file = operation / "native_paper_closing_plan.json"
    saved_bytes = saved_plan_file.read_bytes()
    second = daily_close.refresh(requests, E, source, operation, {}, execute=execute, now=now)
    assert first == second and len(launched) == 1 and len(invoked) == 2
    assert saved_plan_file.read_bytes() == saved_bytes
    command = launched[0]["command"]
    assert daily._command(command)[-len(command):] == command
    for phase in ("opening", "status"):
        forbidden = list(command)
        forbidden[forbidden.index("--phase") + 1] = phase
        with pytest.raises(ValueError, match="only close"):
            daily._command(forbidden)
    different_quote = write(inputs.folder / "different_close.json", read(inputs.first["closing"]))
    with pytest.raises(ValueError, match="cannot change"):
        daily_close.refresh([(inputs.run_dir, different_quote)], E, source, operation, {}, execute=execute, now=now)
    quote_bytes = inputs.first["closing"].read_bytes()
    inputs.first["closing"].write_bytes(quote_bytes + b" ")
    with pytest.raises(ValueError, match="input changed"):
        daily_close.refresh(requests, E, source, operation, {}, execute=execute, now=now)
    inputs.first["closing"].write_bytes(quote_bytes)
    plan_bytes = inputs.first["plan"].read_bytes()
    inputs.first["plan"].write_bytes(plan_bytes + b" ")
    with pytest.raises(ValueError, match="input changed"):
        daily_close.refresh(requests, E, source, operation, {}, execute=execute, now=now)
    inputs.first["plan"].write_bytes(plan_bytes)
    assert len(invoked) == 2 and saved_plan_file.read_bytes() == saved_bytes

    # A saved failed supervisor receipt is returned; the helper never launches a retry.
    failed_operation = tmp_path / "failed_operation"
    attempts, failed_receipts = [], {}

    def failed_execute(spec, directory, day):
        if spec["id"] not in failed_receipts:
            attempts.append(spec["command"])
            failed_receipts[spec["id"]] = {"status": "failed_no_automatic_retry"}
        return failed_receipts[spec["id"]]

    for _ in range(2):
        failed = daily_close.refresh(requests, E, source, failed_operation, {}, execute=failed_execute, now=now)
        assert failed["daily_complete"] is False
        assert failed["rows"][0]["status"] == "failed_no_automatic_retry"
    assert len(attempts) == 1 and read(inputs.run_dir / "head.json")["phase"] == "opening"


def test_daily_cli_forwards_only_explicit_close_pairs_without_running_driver(monkeypatch, tmp_path, capsys):
    calls = []

    def saved_driver(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "fixture_no_producer_called"}

    monkeypatch.setattr(daily, "run", saved_driver)
    system_run, package = tmp_path / "system", tmp_path / "close_package"
    pairs = [(tmp_path / "paper_a", tmp_path / "quote_a.json"),
             (tmp_path / "paper_b", tmp_path / "quote_b.json")]
    command = ["run", "--run-dir", str(system_run), "--day", E, "--package", str(package)]
    for run_dir, quote in pairs:
        command += ["--native-paper-close", str(run_dir), str(quote)]
    assert daily.main(command) == 0
    assert len(calls) == 1 and calls[0][0] == (system_run, E, package)
    assert calls[0][1]["native_paper_closes"] == [[str(a), str(b)] for a, b in pairs]
    assert not system_run.exists() and not package.exists()
    assert "fixture_no_producer_called" in capsys.readouterr().out


def test_child_output_requires_verified_paper_close_for_the_exact_execution_day(inputs, monkeypatch):
    _prepare_fixture(inputs, monkeypatch)
    inputs.call(inputs.first, "opening")
    spec = daily_close.prepare_step(inputs.run_dir, inputs.first["closing"], E,
        now=_postclose(inputs))["step"]
    with pytest.raises(ValueError, match="did not finish"):
        daily._outputs(spec, E)
    inputs.call(inputs.first, "closing")
    assert daily._outputs(spec, E) == [daily._ref(inputs.run_dir / "head.json")]
    with pytest.raises(ValueError, match="did not finish"):
        daily._outputs(spec, F)
    closed = read(inputs.run_dir / "head.json")
    closed["binding"]["mode"] = "historical_replay"
    write(inputs.run_dir / "head.json", closed)
    with pytest.raises(ValueError, match="incompatible head"):
        daily._outputs(spec, E)
