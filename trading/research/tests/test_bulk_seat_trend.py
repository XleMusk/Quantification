import json
import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.research import bulk_seat_trend as module
from trading.research.experiment import baseline_ledger_result
from trading.research.seat_trend import MODES, candidates_by_day, event_signals


def test_sql_reader_preserves_duplicates_conflicts_unknowns_and_source_scope(tmp_path):
    path = tmp_path / "disclosures.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE seat_observations(event_key,trade_date,symbol,seat_name,seat_code,window_kind,
            buy_cny REAL,sell_cny REAL,net_cny REAL,period_turnover_cny REAL,identity_usable,amount_valid,retrieved_at)""")
        db.execute("CREATE TABLE stock_disclosures(trade_date,symbol,window_kind)")
        common = ["2025-09-26", "600000.SSE", "exact branch", "001", "single"]
        rows = [["same", *common, 200., 10., 190., 10000., 1, 1, "2026-09-26"],
                ["same", *common, 200., 10., 190., 10000., 1, 1, "2026-09-26"],
                ["conflict", *common, 250., 10., 240., 10000., 1, 1, "2026-09-26"],
                ["valid", "2025-09-29", *common[1:], 200., 10., 190., 10000., 1, 1, "2026-09-26"],
                ["generic", "2025-09-29", "600000.SSE", "generic branch", "0", "single", None, 10., None, 10000., 0, 0, "2026-09-26"],
                ["multi", "2025-09-29", "600000.SSE", "exact branch", "001", "multi_up_to_3", 200., 10., 190., 10000., 1, 1, "2026-09-26"],
                ["outside", "2025-09-29", "920001.BJ", "exact branch", "001", "single", 200., 10., 190., 10000., 1, 1, "2026-09-26"]]
        db.executemany("INSERT INTO seat_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT INTO stock_disclosures VALUES (?,?,?)", [
            ("2025-09-26", "600000.SSE", "single"), ("2025-09-26", "600000.SSE", "single"),
            ("2025-09-29", "920001.BJ", "single")])
    before = path.stat().st_mtime_ns
    events, summaries, coverage = module.load_disclosures(path, ["600000.SSE"], "2025-09-26", "2025-09-30")
    assert path.stat().st_mtime_ns == before
    assert coverage["raw_listings_in_requested_interval"] == 7
    assert events.loc[events.event_key.eq("same"), "raw_listings"].iloc[0] == 2
    assert events.loc[events.event_key.isin(["same", "conflict"]), "amount_conflict"].all()
    assert events.loc[events.eligible, "event_key"].tolist() == ["valid"]
    assert pd.isna(events.loc[events.event_key.eq("generic"), "buy_cny"].iloc[0])
    assert len(summaries) == 1 and coverage["published_at_known"] is False


def fake_trend(days):
    symbol = "600000.SSE"
    return SimpleNamespace(calendar=days, day_index={day: i for i, day in enumerate(days)}, symbols=[symbol],
        trend={"trend_active": np.ones((len(days), 1), dtype=bool), "regime": np.ones((len(days), 1), dtype=int)},
        scores=lambda day, config: [{"symbol": symbol, "score": 1.}])


@pytest.mark.parametrize("mode", MODES)
def test_bulk_filter_matches_existing_regime_flags_and_skipped_warmup(mode):
    days = ["2025-09-25", "2025-09-26", "2025-09-29", "2025-09-30"]
    symbol = "600000.SSE"
    signals = {(days[0], symbol): {"buy": True, "lhb": True},
               (days[2], symbol): {"buy": True, "sell": True, "lhb": True}}
    trend = fake_trend(days)
    features = pd.DataFrame({"regime": 1, "trend_active": True, "score": 1.,
                              "entry_channel": 9., "exit_channel": 8., "atr": .1},
        index=pd.MultiIndex.from_product([pd.to_datetime(days), [symbol]], names=["date", "symbol"]))
    expected, _ = candidates_by_day(features, pd.Series(True, index=features.index), days, signals, mode)
    feed = module.BulkSeatFeed(trend, signals, mode, module.portfolio())
    for day in days[1:]:
        assert feed.scores(day, module.portfolio()) == expected[day]
    with pytest.raises(ValueError, match="backwards"):
        feed.scores(days[1], module.portfolio())


def test_normalized_summary_delay_and_units_stay_explicit():
    days = ["2025-09-26", "2025-09-29", "2025-09-30"]
    events = pd.DataFrame([{"trade_date": days[0], "symbol": "600000.SSE", "seat_name": "branch",
                           "eligible": True, "net_cny": 200., "period_turnover_cny": 10000.}])
    summaries = [{"trade_date": days[0], "symbol": "600000.SSE", "window_kind": "single"},
                 {"trade_date": days[0], "symbol": "600000.SSE", "window_kind": "multi_up_to_3"}]
    signals = event_signals(events, summaries, ["branch"], days, 1, module.THRESHOLD)
    assert (days[0], "600000.SSE") not in signals
    assert signals[(days[1], "600000.SSE")] == {"buy": True, "sell": False, "lhb": True}
    rows = {days[0]: {"600000.SSE": {"amount": 10000.}}}
    checks, error = module.turnover_checks(events, ["branch"], SimpleNamespace(rows=rows))
    assert error is None and checks.status.tolist() == ["matched_within_one_percent"]
    rows[days[0]]["600000.SSE"]["amount"] = 5000.
    assert module.turnover_checks(events, ["branch"], SimpleNamespace(rows=rows))[1] is not None
    checks, error = module.turnover_checks(events, ["branch"], SimpleNamespace(rows={}))
    assert error is not None and checks.status.tolist() == ["unknown_raw_turnover"]
    assert pd.isna(checks.relative_error.iloc[0])


def test_new_adapter_uses_original_ledger_and_rejects_live_signal(tmp_path):
    days = ["2025-09-26", "2025-09-29", "2025-09-30", "2025-10-09"]
    symbol = "600000.SSE"
    trend = fake_trend(days)
    trend.rows = {day: {symbol: {"open": 10., "close": 10., "preclose": 10., "isST": 0, "tradestatus": 1}} for day in days}
    trend.actions = []
    trend.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""}}
    trend.volumes = pd.DataFrame(1000000., index=days, columns=[symbol])
    trend.next_day = lambda day: days[days.index(day) + 1] if day != days[-1] else "2025-10-10"
    signals = {(days[1], symbol): {"buy": True}, (days[2], symbol): {"sell": True}}
    feed = module.BulkSeatFeed(trend, signals, "TREND_H1_H4", module.portfolio())
    with pytest.raises(ValueError, match="historical replay"):
        feed.signal_frame(days[0], module.portfolio(), RunContext("paper", TradingMode.PAPER))
    destination = tmp_path / "replay"
    result = baseline_ledger_result(feed, module.portfolio(), days, destination)
    account = json.loads((destination / "account.json").read_text(encoding="utf-8"))
    orders = list(account["orders"].values())
    assert result["status"] == "completed" and result["fees"] > 0
    assert [row["day"] for row in orders if row["side"] == "buy"] == [days[2]]
    assert [row["day"] for row in orders if row["side"] == "sell"] == [days[3]]


def test_seat_checkpoint_continuation_preserves_cash_prefix_and_filter_state(tmp_path):
    days = ["2025-09-26", "2025-09-29", "2025-09-30", "2025-10-09"]
    symbol = "600000.SSE"
    trend = fake_trend(days)
    trend.rows = {day: {symbol: {"open": 10., "close": 10., "preclose": 10., "isST": 0, "tradestatus": 1}} for day in days}
    trend.actions = []
    trend.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""}}
    trend.volumes = pd.DataFrame(1000000., index=days, columns=[symbol])
    trend.next_day = lambda day: days[days.index(day) + 1] if day != days[-1] else "2025-10-10"
    signals = {(days[1], symbol): {"buy": True}, (days[2], symbol): {"sell": True}}
    case = {"signal": "lag1_TREND_H1_H4", "mode": "TREND_H1_H4", "lag": 1}
    config = module.portfolio()
    original = module.BulkSeatFeed(trend, signals, case["mode"], config)
    folder = tmp_path / "previous" / case["signal"]
    first = baseline_ledger_result(original, config, days[:2], folder)
    module.append_signal_observations(original, folder, None)
    manifest = {key: "same" for key in ("price_view", "raw_view", "listing_master", "source_codes", "canonical_codes",
                                       "unsupported_instruments", "event_view", "reference_view")}
    protocol = {"id": module.ID, "cases": [case], "portfolio": config.to_dict()}
    previous = {"research_family": module.ID, "status": "partial_seat_bulk_diagnostic", "execution_admitted": False,
                "protocol": protocol, "artifacts": str(folder.parent), "stock_manifest": manifest,
                "results": [{**case, **first}]}
    checkpoints, unaffected = module.resume_check(previous, protocol, manifest, days, config)
    checkpoint = checkpoints[case["signal"]]
    resumed = module.BulkSeatFeed(trend, signals, case["mode"], config)
    destination = tmp_path / "continued"
    result = baseline_ledger_result(resumed, config, days, destination, checkpoint=checkpoint)
    module.append_signal_observations(resumed, destination, checkpoint)
    assert result["reused_sessions"] == 2 and result["newly_completed_sessions"] == 2
    assert unaffected[case["signal"]] == []
    assert pd.read_csv(destination / "candidate_counts.csv").date.tolist() == days
    full = module.BulkSeatFeed(trend, signals, case["mode"], config)
    control = baseline_ledger_result(full, config, days, tmp_path / "full")
    assert result["final_equity"] == control["final_equity"]
    assert json.loads((destination / "account.json").read_text())["fills"] == json.loads((tmp_path / "full/account.json").read_text())["fills"]
    with pytest.raises(ValueError, match="same terminal hypotheses"):
        module.resume_check(previous, {**protocol, "portfolio": {**config.to_dict(), "retain_rank": 30}}, manifest, days, config)
