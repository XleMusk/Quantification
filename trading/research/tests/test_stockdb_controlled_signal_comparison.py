import json

import numpy as np
import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.research import stockdb_controlled_signal_comparison as comparison


def inputs():
    calendar = pd.bdate_range("2025-01-02", periods=45).strftime("%Y-%m-%d").tolist()
    symbols = ["600000.SSE", "000001.SZSE"]
    closes = pd.DataFrame({s: 10 + np.arange(45) * (0.1 + i * 0.02) for i, s in enumerate(symbols)}, index=calendar)
    raw = pd.DataFrame([{"date": day, "symbol": symbol, "open": closes.at[day, symbol],
        "high": closes.at[day, symbol] * 1.01, "low": closes.at[day, symbol] * .99,
        "close": closes.at[day, symbol], "preclose": closes.iloc[max(0, i - 1)][symbol],
        "volume": 100000000., "amount": 1000000000., "tradestatus": 1, "isST": 0, "adjustflag": "3"}
        for i, day in enumerate(calendar) for symbol in symbols])
    days = calendar[-4:-1]
    scores = pd.DataFrame([{"date": d, "symbol": s, "rank_mean": float(2 - i),
        "score_seed17": float(2 - i), "score_seed29": float(2 - i), "score_seed43": float(2 - i)}
        for d in days for i, s in enumerate(symbols)])
    scores = comparison.repair_ranks(scores)
    instruments = {s: {"ipoDate": "2000-01-01", "outDate": ""} for s in symbols}
    config = StrategyConfig(lookback=21, skip=1, volatility_window=10, rebalance_sessions=1,
        holdings=1, retain_rank=1, min_listing_days=1, min_amount=0).validate()
    return raw, closes, scores, instruments, calendar, config, days, {}


def test_missing_later_quote_is_not_a_selection_filter_and_stops_before_mutation():
    raw, closes, scores, instruments, calendar, config, days, _ = inputs()
    day, execution = days[:2]
    raw = raw.loc[~(raw.date.eq(execution) & raw.symbol.eq("600000.SSE"))]
    feed = comparison.ControlledFeed(raw, closes, scores, instruments, calendar, config, arm="lgbm_day_local")
    assert "600000.SSE" in {r["symbol"] for r in feed.pool(day, config)}
    state = new_state(config.initial_cash)
    context = RunContext("missing-future-test", TradingMode.REPLAY)
    advance(state, feed, day, config, 0, context=context)
    before = json.dumps(state, sort_keys=True)
    with pytest.raises(ValueError, match="Unknown required raw quote"):
        advance(state, feed, execution, config, 1, context=context)
    assert json.dumps(state, sort_keys=True) == before


def test_three_arms_use_one_common_pool_and_write_actual_engine_accounts(tmp_path, monkeypatch):
    fixture = inputs()
    monkeypatch.setattr(comparison, "_sources", lambda *args: fixture)
    out = tmp_path / "run"
    result = comparison.run("unused", "unused", "unused", out)
    assert result["status"] == "completed"
    assert [r["arm"] for r in result["results"]] == list(comparison.ARMS)
    assert all(r["sessions"] == 3 and r["orders"] > 0 and r["fills"] > 0 for r in result["results"])
    assert all(r["metrics"] is not None and r["pending_targets"] for r in result["results"])
    assert result["method"]["symbol_cap"] is None
    assert result["method"]["forward_path_screening"] is False
    assert all(r["common_eligible"] == 2 for r in result["rank_impact"])
    assert result["execution_admitted"] is False
    assert result["strategy_validated"] is False
    assert len(pd.read_parquet(out / "common_pool.parquet")) == 6
    assert all((out / arm / "account.json").is_file() for arm in comparison.ARMS)


def test_native_gap_keeps_original_values_and_unknown_halt_quantity():
    raw = pd.DataFrame([{"date": "2026-10-08", "symbol": "600000.SSE", "open": 10.0, "volume": 100.0}])
    native = pd.DataFrame([{"date": "2026-10-08", "symbol": symbol, "open": "18.6900", "high": "18.6900",
        "low": "18.6900", "close": "18.6900", "preclose": "18.6900", "volume": "", "amount": "",
        "tradestatus": "0", "isST": "0", "adjustflag": "3"} for symbol in ("600000.SSE", "000668.SZSE")])
    result, originals = comparison.append_native_gaps(raw, native)
    assert result.loc[result.symbol.eq("600000.SSE"), "open"].item() == 10.0
    halt = result.loc[result.symbol.eq("000668.SZSE")].iloc[0]
    assert halt.tradestatus == 0 and halt.isST == 0 and halt.close == 18.69
    assert pd.isna(halt.volume) and pd.isna(halt.amount)
    assert len(originals) == 1 and originals[0]["volume"] == ""
