"""Explicit fixed-runner binding and recovery, on tiny synthetic data only."""
import json
from pathlib import Path

import pytest

from trading.research import experiment
from trading.research import stockdb_rolling_low_volatility as m
from trading.target_exit_lifecycle import FULL_EXIT_LIFECYCLE
from trading.research.tests.test_stockdb_calendar_month_ends import (
    evidence, MONTH_END, NEXT, START, VARIANT, saved_account)


def execute(e, *, persistent=True, resume=None, end=MONTH_END, source=None):
    return m.run(e["old"] if source is None else source, START, end, variants=(VARIANT,),
        calendars=[e["calendar"]], resume_run=resume,
        calendar_driven_month_ends=True, persistent_full_exits=persistent)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_explicit_method_flows_through_runner_checkpoint_and_resume(evidence, monkeypatch):
    e = evidence
    out, initial = execute(e)
    account_path = saved_account(initial)
    account_bytes = account_path.read_bytes()
    initial_account = read(account_path)
    receipt_path = account_path.parent / "replay.json"
    assert read(receipt_path)["full_exit_lifecycle"] == FULL_EXIT_LIFECYCLE
    assert initial["full_exit_lifecycle"] == FULL_EXIT_LIFECYCLE
    assert read(out / "protocol.json")["full_exit_lifecycle"] == FULL_EXIT_LIFECYCLE
    assert initial_account["full_exit_lifecycle"]["latest_target"]["decided_on"] == MONTH_END
    assert initial_account["full_exit_lifecycle"]["target_version"] == 1
    checkpoint = experiment.load_replay_checkpoint(account_path.parent, m.portfolio(),
        [START, MONTH_END, NEXT], full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
    assert checkpoint["full_exit_lifecycle"] == FULL_EXIT_LIFECYCLE
    monkeypatch.setattr(experiment, "new_state", lambda *a: pytest.fail("Continuation must not reset account"))
    _, resumed = execute(e, source=e["new"], end=NEXT, resume=out / "run.json")
    result = resumed["results"][0]
    assert result["status"] == "completed"
    assert result["reused_sessions"] == 2 and result["newly_completed_sessions"] == 1
    account = read(saved_account(resumed))
    assert account["day"] == NEXT and account["fills"]
    assert account["full_exit_lifecycle"]["target_version"] == 1
    assert account_path.read_bytes() == account_bytes


@pytest.mark.parametrize("before,after", [(False, True), (True, False)])
def test_saved_runner_cannot_upgrade_or_disable_method(evidence, before, after):
    out, initial = execute(evidence, persistent=before)
    original = saved_account(initial).read_bytes()
    if not before:
        assert "full_exit_lifecycle" not in initial
        assert "full_exit_lifecycle" not in read(out / "protocol.json")
        assert "full_exit_lifecycle" not in read(saved_account(initial))
        assert "full_exit_lifecycle" not in read(saved_account(initial).parent / "replay.json")
    with pytest.raises(ValueError, match="Resume full-exit lifecycle changed"):
        execute(evidence, persistent=after, source=evidence["new"], end=NEXT, resume=out / "run.json")
    assert saved_account(initial).read_bytes() == original


def test_new_mode_requires_fixed_calendar_before_reading_data(evidence, monkeypatch):
    monkeypatch.setattr(m, "_read_snapshot", lambda *a, **kw: pytest.fail("Invalid mode must reject before I/O"))
    with pytest.raises(ValueError, match="require calendar-driven month ends"):
        m.run(evidence["old"], START, MONTH_END, persistent_full_exits=True)
    with pytest.raises(ValueError, match="explicit boolean"):
        m.run(evidence["old"], START, MONTH_END, persistent_full_exits=1)


@pytest.mark.parametrize("corrupt", ["account", "receipt"])
def test_checkpoint_binding_cannot_be_removed_or_replaced(evidence, corrupt):
    _, initial = execute(evidence)
    folder = saved_account(initial).parent
    path = folder / ("account.json" if corrupt == "account" else "replay.json")
    value = read(path)
    value["full_exit_lifecycle"] = (None if corrupt == "account" else "unregistered_method")
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="Full-exit|full-exit"):
        experiment.load_replay_checkpoint(folder, m.portfolio(), [START, MONTH_END, NEXT],
            full_exit_lifecycle=FULL_EXIT_LIFECYCLE)


def test_failed_new_day_keeps_committed_account_and_method(evidence, monkeypatch):
    _, initial = execute(evidence)
    folder = saved_account(initial).parent
    old_bytes = saved_account(initial).read_bytes()
    checkpoint = experiment.load_replay_checkpoint(folder, m.portfolio(), [START, MONTH_END, NEXT],
        full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
    feed = m.RollingStockDBFeed(evidence["frame"], NEXT, variant=VARIANT,
        fixed_calendar=initial["calendar_verification"]["fixed_open_sessions"])
    def fail_after_trial_mutation(state, feed, day, config, session_number, **options):
        assert day == NEXT and options["full_exit_lifecycle"] == FULL_EXIT_LIFECYCLE
        state["cash"] = -1
        state["pending_targets"] = None
        raise ValueError("synthetic unprocessed day failure")
    monkeypatch.setattr(experiment, "advance", fail_after_trial_mutation)
    destination = evidence["root"] / "failed_trial"
    result = experiment.baseline_ledger_result(feed, m.portfolio(), [START, MONTH_END, NEXT],
        destination, checkpoint=checkpoint, full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
    assert result["status"] == "failed" and result["completed_sessions"] == 2
    assert result["last_completed_session"] == MONTH_END and result["attempted_session"] == NEXT
    assert read(destination / "account.json") == read(folder / "account.json")
    resumed = experiment.load_replay_checkpoint(destination, m.portfolio(), [START, MONTH_END, NEXT],
        full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
    assert resumed["state"] == checkpoint["state"] and resumed["daily"] == checkpoint["daily"]
    assert read(destination / "replay.json")["full_exit_lifecycle"] == FULL_EXIT_LIFECYCLE
    assert saved_account(initial).read_bytes() == old_bytes
    with pytest.raises(ValueError, match="configuration/policy mismatch"):
        experiment.replay(feed, m.portfolio(), [START, MONTH_END, NEXT],
            evidence["root"] / "disabled", checkpoint=checkpoint)
    assert not (evidence["root"] / "disabled").exists()


def test_cli_forwards_only_explicit_new_method(monkeypatch):
    observed = []
    def run(*args, **kwargs):
        observed.append(kwargs)
        return Path("synthetic"), {"status": "completed_research_only"}
    monkeypatch.setattr(m, "run", run)
    assert m.main(["--snapshot", "synthetic", "--calendar-driven-month-ends", "--persistent-full-exits"]) == 0
    assert observed[-1]["persistent_full_exits"] is True
    assert m.main(["--snapshot", "synthetic", "--calendar-driven-month-ends"]) == 0
    assert "persistent_full_exits" not in observed[-1]
