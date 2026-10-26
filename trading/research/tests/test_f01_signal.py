from datetime import date

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.research.f01_financial_factor import FinancialFact, compute_f01
from trading.research.f01_account_replay import load_bundle, publish_bundle, replay_f01, restore_bundle
from trading.research.f01_signal import signal_frame, signal_provenance
from trading.research.financial_factor_contracts import TimeWindow, publication_interval


BEFORE = TimeWindow.exact("2026-10-19T15:00:00+08:00")
EVENT = publication_interval("2026-10-20")
CONTEXT = RunContext("f01-signal-test", TradingMode.REPLAY)


def fact(fid, metric, value, end):
    period_kind = "point_in_time" if metric == "total_assets" else "single_quarter"
    return FinancialFact(fid, "issuer", metric, date.fromisoformat(end), date.fromisoformat(end),
                         period_kind, "consolidated", "CNY", "yuan", value, BEFORE)


def result(symbol, current, prior, assets):
    return compute_f01(fact(symbol + "-cur", "parent_net_profit", current, "2026-09-30"),
                       fact(symbol + "-prior", "parent_net_profit", prior, "2025-09-30"),
                       fact(symbol + "-assets", "total_assets", assets, "2026-09-30"), EVENT)


def test_f01_signal_is_sorted_stably_and_carries_fact_provenance():
    frame = signal_frame({"600002.SSE": result("600002.SSE", 60, 40, 1000),
                          "600001.SSE": result("600001.SSE", 50, 40, 1000)}, "2026-10-21", CONTEXT)
    assert isinstance(frame, SignalFrame)
    assert [row["symbol"] for row in frame.ranked_candidates()] == ["600002.SSE", "600001.SSE"]
    provenance = signal_provenance({"600002.SSE": result("600002.SSE", 60, 40, 1000),
                                    "600001.SSE": result("600001.SSE", 50, 40, 1000)},
                                   accepted_symbols=[row["symbol"] for row in frame.ranked_candidates()])
    assert provenance["600002.SSE"]["fact_ids"] == ["600002.SSE-cur", "600002.SSE-prior", "600002.SSE-assets"]


def test_f01_signal_does_not_turn_unready_results_into_zero_or_empty_success():
    blocked = result("600001.SSE", 50, 40, 0)
    output = signal_frame({"600001.SSE": blocked}, "2026-10-21", CONTEXT)
    assert isinstance(output, SignalUnavailable)
    assert output.reason == "f01_no_ready_candidates"


class F01ReplayFeed:
    """Tiny in-memory feed used only to prove the existing Engine hook."""

    def __init__(self, frames):
        self.calendar = ["2026-10-21", "2026-10-22"]
        self.frames = frames
        symbols = tuple(frames[self.calendar[0]].ranked_candidates()[i]["symbol"] for i in range(2))
        self.rows = {day: {symbol: {"open": price, "close": price, "preclose": price,
                                    "tradestatus": 1, "isST": 0, "adjustflag": "3"}
                           for symbol, price in zip(symbols, (10., 20.))} for day in self.calendar}
        self.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""} for symbol in symbols}
        self.actions = []
        self.volumes = pd.DataFrame(1_000_000., index=self.calendar, columns=symbols)

    def next_day(self, day):
        return self.calendar[self.calendar.index(day) + 1] if day != self.calendar[-1] else "9999-12-31"

    def signal_frame(self, day, config, context):
        return self.frames[day]


def test_f01_signal_uses_the_existing_engine_and_ledger_path():
    frames = {day: signal_frame({"600001.SSE": result("600001.SSE", 60, 40, 1000),
                                 "600002.SSE": result("600002.SSE", 50, 40, 1000)}, day, CONTEXT)
              for day in ("2026-10-21", "2026-10-22")}
    feed = F01ReplayFeed(frames)
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, gross_limit=.8,
                            single_limit=.5, slippage_bps=0)
    state = new_state(config.initial_cash)
    first = advance(state, feed, "2026-10-21", config, 0, context=CONTEXT)
    assert state["pending_targets"] and not state["orders"]
    second = advance(state, feed, "2026-10-22", config, 1, context=CONTEXT)
    assert state["orders"] and state["fills"]
    assert state["cash"] < config.initial_cash * 100
    assert any(event["kind"] == "targets_generated" for event in state["events"])
    assert all(order["symbol"] in {"600001.SSE", "600002.SSE"} for order in state["orders"].values())


def test_f01_account_adapter_records_all_shared_pipeline_stages():
    frames = {day: signal_frame({"600001.SSE": result("600001.SSE", 60, 40, 1000),
                                 "600002.SSE": result("600002.SSE", 50, 40, 1000)}, day, CONTEXT)
              for day in ("2026-10-21", "2026-10-22")}
    feed = F01ReplayFeed(frames)
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, gross_limit=.8,
                            single_limit=.5, slippage_bps=0)
    identity = {
        "study_id": "f01-v1", "code_version": "local-test", "snapshot_id": "synthetic",
        "calendar_id": "calendar-test", "rules_id": "rules-test", "benchmark_config_id": "benchmark-test",
        "initial_account_ref": "research-account-test", "study_config": {"delay": 0},
    }
    result_bundle = replay_f01(
        feed,
        {day: {symbol: result(symbol, 60 if symbol == "600001.SSE" else 50, 40, 1000)
               for symbol in ("600001.SSE", "600002.SSE")} for day in feed.calendar},
        config, CONTEXT, identity=identity, attempt=0)
    assert result_bundle["account_replay"] is True
    assert result_bundle["execution_admitted"] is False
    assert result_bundle["run_key"] and result_bundle["attempt_key"].endswith(":0")
    assert result_bundle["stages"] == ["Data", "Signal", "Target Portfolio", "Orders", "BrokerAdapter", "Fill", "Cash/Position"]
    comparison = result_bundle["comparison_contract"]
    assert comparison["benchmark_config_id"] == "benchmark-test"
    assert comparison["benchmark_binding_status"] == "BOUND"
    assert comparison["cost_model_id"] == "cn_a_share_strategy_config_fees_v1"
    assert comparison["cost_model"]["commission"] == config.commission
    assert comparison["observation_window"] == {
        "first_session": "2026-10-21", "last_session": "2026-10-22",
        "session_count": 2, "calendar_id": "calendar-test",
    }
    assert any(row["orders"] for row in result_bundle["daily"])
    assert any(row["fills"] for row in result_bundle["daily"])
    assert result_bundle["final_state"]["cash"] < config.initial_cash * 100


def test_f01_account_bundle_is_published_atomically_and_hash_checked(tmp_path):
    frames = {day: signal_frame({"600001.SSE": result("600001.SSE", 60, 40, 1000),
                                 "600002.SSE": result("600002.SSE", 50, 40, 1000)}, day, CONTEXT)
              for day in ("2026-10-21", "2026-10-22")}
    feed = F01ReplayFeed(frames)
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, gross_limit=.8,
                            single_limit=.5, slippage_bps=0)
    bundle = replay_f01(feed, {day: {symbol: result(symbol, 60 if symbol == "600001.SSE" else 50, 40, 1000)
                                     for symbol in ("600001.SSE", "600002.SSE")} for day in feed.calendar}, config, CONTEXT)
    destination = publish_bundle(bundle, tmp_path / "bundle")
    loaded = load_bundle(destination)
    assert loaded["manifest"]["status"] == "PUBLISHED"
    assert loaded["run"]["published"] is True
    (destination / "daily.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_bundle(destination)


def test_f01_account_bundle_restores_into_empty_directory(tmp_path):
    frames = {day: signal_frame({"600001.SSE": result("600001.SSE", 60, 40, 1000),
                                 "600002.SSE": result("600002.SSE", 50, 40, 1000)}, day, CONTEXT)
              for day in ("2026-10-21", "2026-10-22")}
    feed = F01ReplayFeed(frames)
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, gross_limit=.8,
                            single_limit=.5, slippage_bps=0)
    bundle = replay_f01(feed, {day: {symbol: result(symbol, 60 if symbol == "600001.SSE" else 50, 40, 1000)
                                     for symbol in ("600001.SSE", "600002.SSE")} for day in feed.calendar}, config, CONTEXT)
    source = publish_bundle(bundle, tmp_path / "source")
    restored = restore_bundle(source, tmp_path / "empty_restore")
    assert restored["manifest"] == load_bundle(source)["manifest"]
    assert restored["final_state"] == bundle["final_state"]
