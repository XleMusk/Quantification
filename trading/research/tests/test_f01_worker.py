import json
from pathlib import Path

import pytest

from trading.config import StrategyConfig
from trading import store
from trading.research.f01_worker import attempt_directory, prepare_attempt, recover_attempt, run_attempt
from trading.research.tests.test_f01_signal import CONTEXT, F01ReplayFeed, result, signal_frame


def _fixture():
    days = ("2026-10-21", "2026-10-22")
    frames = {day: signal_frame({"600001.SSE": result("600001.SSE", 60, 40, 1000),
                                 "600002.SSE": result("600002.SSE", 50, 40, 1000)}, day, CONTEXT)
              for day in days}
    feed = F01ReplayFeed(frames)
    results = {day: {symbol: result(symbol, 60 if symbol == "600001.SSE" else 50, 40, 1000)
                     for symbol in ("600001.SSE", "600002.SSE")} for day in days}
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, gross_limit=.8,
                            single_limit=.5, slippage_bps=0)
    identity = {
        "study_id": "f01-worker-v1", "code_version": "worker-test", "snapshot_id": "synthetic",
        "calendar_id": "calendar-test", "rules_id": "rules-test", "benchmark_config_id": "benchmark-test",
        "initial_account_ref": "paper-test", "study_config": {"delay": 0},
    }
    return feed, results, config, identity


def test_f01_worker_publishes_one_attempt_and_recovers_terminal_bundle(tmp_path):
    feed, results, config, identity = _fixture()
    terminal = run_attempt(feed, results, config, CONTEXT, tmp_path, identity)
    assert terminal["status"] == "COMPLETED" and terminal["execution_admitted"] is False
    recovered = recover_attempt(tmp_path, identity)
    assert recovered["status"] == "COMPLETED" and recovered["recovered"] is True
    repeated = run_attempt(feed, results, config, CONTEXT, tmp_path, identity)
    assert repeated["status"] == "COMPLETED" and repeated["attempt_key"] == terminal["attempt_key"]


def test_f01_worker_classifies_request_without_terminal_as_unknown_no_retry(tmp_path):
    _, _, _, identity = _fixture()
    directory = prepare_attempt(tmp_path, identity, 3)
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    assert request["retry_permitted"] is False
    recovered = recover_attempt(tmp_path, identity, 3)
    assert recovered["status"] == "UNKNOWN_INTERRUPTED_NO_RETRY"
    assert recovered["execution_progress_unknown"] is True


def test_f01_worker_binds_request_and_terminal_to_existing_store(tmp_path, monkeypatch):
    from trading import settings
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "trading" / "trading.sqlite3")
    store.initialize()
    feed, results, config, identity = _fixture()
    terminal = run_attempt(feed, results, config, CONTEXT, tmp_path / "attempts", identity,
                           persistent_store=store)
    attempt = store.research_attempt(terminal["attempt_key"])
    assert attempt["status"] == "COMPLETED"
    assert attempt["request"]["attempt_key"] == terminal["attempt_key"]
    assert attempt["terminal"]["execution_admitted"] is False
