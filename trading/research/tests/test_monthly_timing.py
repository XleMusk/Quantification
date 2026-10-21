from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.market_snapshot import INGESTION_SCOPE
from trading.research import monthly_timing as timing


def calendar():
    return pd.bdate_range("2023-01-02", "2024-03-05").strftime("%Y-%m-%d").tolist()


def base_feed():
    days, symbol = calendar(), "600000.SSE"
    row = {"open": 10., "close": 10., "preclose": 10., "isST": 0, "tradestatus": 1}
    return SimpleNamespace(calendar=days, symbols=[symbol],
        rows={d: {symbol: dict(row)} for d in days},
        instruments={symbol: {"ipoDate": "2000-01-01", "outDate": ""}}, actions=[],
        volumes=pd.DataFrame(1000000., index=days, columns=[symbol]),
        manifest={"start": "2024-01-02", "end": days[-1], "scope": INGESTION_SCOPE},
        next_day=lambda d: days[days.index(d) + 1],
        scores=lambda day, config: [{"symbol": symbol, "score": 1.}])


def table():
    return pd.DataFrame([{"date": "2024-01-31", "index_close": 110., "sma_10_months": 100., "gate": "risk_on"},
                         {"date": "2024-02-29", "index_close": 90., "sma_10_months": 100., "gate": "risk_off"}])


CONFIG = replace(StrategyConfig(), signal="momentum", rebalance_sessions=1, initial_cash=10000,
                 holdings=1, retain_rank=2, gross_limit=.8, single_limit=.8).validate()
CONTEXT = RunContext("monthly-test", TradingMode.REPLAY)


def test_month_ends_do_not_invent_last_or_partial_month_end():
    assert timing.month_ends(["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-09"], "2024-02-09") == ["2024-01-31"]
    assert timing.month_ends(calendar(), "2024-01-30")[-1] == "2023-12-29"
    with pytest.raises(ValueError, match="chronological"):
        timing.month_ends(["2024-01-31", "2024-01-30"], "2024-01-31")


def test_ten_complete_months_required_no_future_price_or_daily_mean():
    days = calendar()
    closes = pd.Series(100., index=days)
    ends = timing.month_ends(days, "2024-02-29")
    for count, day in enumerate(ends):
        closes.at[day] = 100. + count
    result = timing.trend_table(closes, days, "2024-02-29").set_index("date")
    assert result.loc[ends[:9], "gate"].eq("warmup").all()
    assert result.at[ends[9], "sma_10_months"] == pytest.approx(104.5)
    assert result.at[ends[9], "gate"] == "risk_on"
    changed = closes.copy()
    changed.loc[changed.index > "2023-10-31"] = 1e9
    earlier = timing.trend_table(changed, days, "2023-10-31").set_index("date")
    pd.testing.assert_frame_equal(result.loc[:"2023-10-31"], earlier)
    changed.at["2023-05-31"] = np.nan
    with pytest.raises(ValueError, match="month-end index close"):
        timing.trend_table(changed, days, "2023-10-31")


def test_equal_is_no_change_and_not_forced_cash():
    days = calendar()
    actual = timing.trend_table(pd.Series(100., index=days), days, "2024-02-29")
    assert actual.loc[actual.date.eq("2024-01-31"), "gate"].item() == "equal_no_change"
    feed = timing.MonthlyFeed(base_feed(), actual, timing.VARIANTS[1], "2024-02-29")
    result = feed.signal_frame("2024-01-31", CONFIG, CONTEXT)
    assert isinstance(result, SignalUnavailable) and result.reason == "equal_no_change"


def test_missing_batch_and_non_month_end_are_not_liquidations():
    feed = timing.MonthlyFeed(base_feed(), table().iloc[:1], timing.VARIANTS[1], "2024-02-29")
    assert feed.signal_frame("2024-01-30", CONFIG, CONTEXT).reason == "not_month_end"
    assert feed.signal_frame("2024-02-29", CONFIG, CONTEXT).reason == "index_gate_batch_missing"
    feed.base.scores = lambda day, config: SignalUnavailable("stock_batch_missing")
    assert feed.signal_frame("2024-01-31", CONFIG, CONTEXT).reason == "stock_batch_missing"


def test_typed_target_bearish_empty_batch_and_cached_research_boundary():
    feed = timing.MonthlyFeed(base_feed(), table(), timing.VARIANTS[1], "2024-02-29")
    bullish = feed.signal_frame("2024-01-31", CONFIG, CONTEXT)
    assert isinstance(bullish, SignalFrame) and bullish.signals[0].security.symbol == "600000.SSE"
    bearish = feed.signal_frame("2024-02-29", CONFIG, CONTEXT)
    assert isinstance(bearish, SignalFrame) and bearish.signals == ()
    with pytest.raises(ValueError, match="paper/live"):
        feed.signal_frame("2024-01-31", CONFIG, RunContext("paper", TradingMode.PAPER))
    with pytest.raises(ValueError, match="cutoff"):
        feed.signal_frame("2024-03-01", CONFIG, CONTEXT)
    with pytest.raises(ValueError, match="fixed momentum"):
        feed.signal_frame("2024-01-31", replace(CONFIG, signal="equal_weight"), CONTEXT)


def test_real_shared_engine_only_trades_after_close_and_cash_exit_next_session():
    feed = timing.MonthlyFeed(base_feed(), table(), timing.VARIANTS[1], "2024-03-01")
    state = new_state(10000)
    days = [d for d in feed.calendar if "2024-01-31" <= d <= "2024-03-01"]
    for count, day in enumerate(days):
        advance(state, feed, day, CONFIG, count)
        if day == "2024-01-31":
            assert not state["fills"] and state["pending_targets"] == {"600000.SSE": .8}
        if day == "2024-02-01":
            assert len(state["orders"]) == 1 and state["pending_targets"] is None
        if day == "2024-02-29":
            assert len(state["orders"]) == 1 and state["pending_targets"] == {}
    assert len(state["orders"]) == 2
    assert state["orders"]["2024-03-01:600000.SSE:sell"]["status"] == "filled"
    assert not any(state["positions"].values())


def test_monthly_no_change_does_not_disable_daily_st_exit():
    feed = timing.MonthlyFeed(base_feed(), table(), timing.VARIANTS[1], "2024-02-29")
    state = new_state(10000)
    for count, day in enumerate(["2024-01-31", "2024-02-01", "2024-02-02"]):
        if day == "2024-02-02":
            feed.base.rows[day]["600000.SSE"]["isST"] = 1
        advance(state, feed, day, CONFIG, count)
    assert len(state["orders"]) == 2 and not any(state["positions"].values())


def test_blocked_cash_exit_is_retried_without_using_new_month_prices():
    base = base_feed()
    base.rows["2024-03-01"]["600000.SSE"].update(open=9., close=9.)
    base.rows["2024-03-04"]["600000.SSE"].update(open=9.1, close=9.1, preclose=9.)
    feed = timing.MonthlyFeed(base, table(), timing.VARIANTS[1], "2024-03-04")
    state = new_state(10000)
    days = [d for d in feed.calendar if "2024-01-31" <= d <= "2024-03-04"]
    for count, day in enumerate(days):
        advance(state, feed, day, CONFIG, count)
        if day == "2024-03-01":
            assert any(state["positions"].values()) and state["pending_targets"] == {}
            assert state["events"][-1]["kind"] != "fill"
    blocked = [e for e in state["events"] if e["kind"] == "order_blocked"]
    assert any(e["reason"] == "limit_down_queue_unknown" for e in blocked)
    assert state["orders"]["2024-03-04:600000.SSE:sell"]["status"] == "filled"
    assert not any(state["positions"].values())
    assert feed.audit[-1]["decision"] == "carry_cash_target"


def test_cash_carry_is_cancelled_by_valid_risk_on_not_a_future_gate():
    actual = table()
    actual.loc[1, "gate"] = "risk_on"
    actual.loc[0, "gate"] = "risk_off"
    feed = timing.MonthlyFeed(base_feed(), actual, timing.VARIANTS[1], "2024-03-01")
    assert feed.signal_frame("2024-02-01", CONFIG, CONTEXT).signals == ()
    assert feed.audit[-1]["decision"] == "carry_cash_target"
    assert len(feed.signal_frame("2024-02-29", CONFIG, CONTEXT).signals) == 1
    assert feed.signal_frame("2024-03-01", CONFIG, CONTEXT).reason == "not_month_end"


def test_inspected_2026_rejected_before_loading_any_feed():
    with patch.object(timing, "HistoricalFeed") as load:
        with pytest.raises(ValueError, match="2024-2025"):
            timing.run("unused", "unused", end="2026-09-24")
        load.assert_not_called()


def test_protocol_fixed_before_replay_and_negative_results_kept(tmp_path):
    base = base_feed()
    base.folder = tmp_path / "snapshot"
    base.folder.mkdir()
    for name in ("manifest.json", "raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv"):
        (base.folder / name).write_text("source receipt", encoding="utf-8")
    calls = []
    def consume(feed, config, dates, destination):
        paths = list((tmp_path / "outputs/market_replay").glob("*/protocol.json"))
        assert len(paths) == 1
        saved = json.loads(paths[0].read_text())
        assert saved["signals"] == list(timing.VARIANTS)
        assert saved["tuning"] is False and saved["fresh_holdout_claimed"] is False
        assert dates == ["2024-01-31", "2024-02-01"]
        calls.append(config.to_dict())
        for day in dates:
            feed.signal_frame(day, config, CONTEXT)
        return {"status": "completed", "completed_sessions": len(dates), "net_return": -.1,
                "max_drawdown": -.2, "sharpe_zero_rf_252": -1., "fees": 5., "orders": 2}
    with patch.object(timing, "ROOT", tmp_path), patch.object(timing, "DATA_ROOT", tmp_path / "data"), \
            patch.object(timing, "HistoricalFeed", return_value=base), \
            patch.object(timing, "index_inputs", return_value=(table(), {"price_cutoff": "2024-02-01"})), \
            patch.object(timing, "implementation_hashes", return_value={"fixture": "fixed-source-hash"}), \
            patch.object(timing, "baseline_ledger_result", side_effect=consume):
        out = timing.run(base.folder, tmp_path / "indices", "2024-01-31", "2024-02-01")
    record = json.loads((out / "run.json").read_text())
    assert record["status"] == "completed_monthly_adaptation_diagnostic"
    assert [r["net_return"] for r in record["results"]] == [-.1, -.1]
    assert calls[0] == calls[1]
    assert record["strategy_validated"] is False and record["live_ready"] is False
    assert "not a fresh holdout" in (out / "REPORT.md").read_text()


@pytest.mark.parametrize("removed", [None, "2024-02-28", "2024-03-01"])
def test_index_calendar_cutoff_match_not_irrelevant_later_tail(tmp_path, removed):
    stock_days = calendar()
    index_days = [day for day in stock_days if day != "2024-03-05" and day != removed]
    frame = pd.DataFrame({"date": index_days, "code": "sh.000300", "open": 100.,
                          "high": 100., "low": 100., "close": 100., "preclose": 100.,
                          "volume": 100000., "amount": 10000000., "pctChg": 0.})
    frame.to_parquet(tmp_path / "index.parquet", index=False)
    (tmp_path / "raw.csv").write_text("synthetic source", encoding="utf-8")
    (tmp_path / "manifest.json").write_text("synthetic manifest", encoding="utf-8")
    receipt = {"file": "index.parquet", "raw_file": "raw.csv",
               "sha256": sha256((tmp_path / "index.parquet").read_bytes()).hexdigest(),
               "raw_sha256": sha256((tmp_path / "raw.csv").read_bytes()).hexdigest()}
    with patch.object(timing, "load_indices", return_value=(
            {"receipts": {"sh.000300": receipt}, "calendar_sha256": "fixture"}, index_days)):
        if removed:
            with pytest.raises(ValueError, match="cutoff and its next session"):
                timing.index_inputs(tmp_path, stock_days, "2024-02-29")
        else:
            result, proof = timing.index_inputs(tmp_path, stock_days, "2024-02-29")
            assert result.date.max() == "2024-02-29"
            assert proof["price_cutoff"] == "2024-02-29"
            assert proof["price_rows_consumed"] == sum(day <= "2024-02-29" for day in index_days)


def test_bulk_monthly_uses_all_feed_symbols_and_same_fixed_variants(tmp_path):
    base = base_feed()
    base.folder = tmp_path / "bulk"
    base.folder.mkdir()
    (base.folder / "result.json").write_text("synthetic bulk receipt", encoding="utf-8")
    base.symbols.append("689009.SSE")
    base.unsupported_instruments = {"689009.SSE": "unsupported"}
    base.manifest.update(scope=timing.BULK_SCOPE, price_view="price", event_view="events",
                         reference_view="references", raw_view="raw", listing_master="master",
                         source_codes=2, canonical_codes=2, unsupported_instruments=base.unsupported_instruments,
                         limitations=["not independent PIT"])
    calls = []
    def consume(feed, config, dates, destination):
        assert feed.symbols == base.symbols
        calls.append(config.to_dict())
        return {"status": "completed", "completed_sessions": len(dates), "net_return": -.1,
                "max_drawdown": -.2, "sharpe_zero_rf_252": -1., "fees": 5., "orders": 2}
    with patch.object(timing, "ROOT", tmp_path), patch.object(timing, "DATA_ROOT", tmp_path / "data"), \
            patch.object(timing, "BulkResearchFeed", return_value=base) as bulk, \
            patch.object(timing, "HistoricalFeed") as old, \
            patch.object(timing, "index_inputs", return_value=(table(), {"price_cutoff": "2024-02-01"})), \
            patch.object(timing, "implementation_hashes", return_value={}), \
            patch.object(timing, "baseline_ledger_result", side_effect=consume):
        out = timing.run(None, "indices", "2024-01-31", "2024-02-01",
                         price_view="price", event_view="events", reference_view="references", listing_master="master")
        bulk.assert_called_once_with("price", "events", "references", "master")
        old.assert_not_called()
    record = json.loads((out / "run.json").read_text())
    assert record["symbols"] == 2 and record["board_counts"] == {"main": 1, "unsupported": 1}
    assert calls[0] == calls[1]
    assert record["protocol"]["signals"] == list(timing.VARIANTS)
    assert record["protocol"]["bulk_inputs"]["canonical_codes"] == 2
    assert not record["strategy_validated"] and not record["live_ready"]
    assert "full source-covered" in (out / "REPORT.md").read_text()
