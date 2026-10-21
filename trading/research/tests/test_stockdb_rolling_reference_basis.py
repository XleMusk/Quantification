"""Synthetic real-Engine checks for the old rolling mixed-reference guard."""
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.research import stockdb_rolling_low_volatility as rolling


A, B = "600000.SSE", "000001.SZSE"


def rows(calendar, symbols=(A,)):
    return pd.DataFrame([{"date": day, "symbol": symbol, "code": symbol.split(".")[0],
        "open": 10 + i * .01, "high": 11 + i * .01, "low": 9 + i * .01,
        "close": 10 + i * .01 + .002 * np.sin(i), "pre_close": 10 + max(0, i - 1) * .01,
        "volume": 100_000_000., "amount": 1_000_000_000., "is_st": False}
        for i, day in enumerate(calendar) for symbol in symbols])


def test_mixed_positive_pending_stops_before_engine_writes_but_signal_warmup_is_allowed():
    calendar = ["2026-01-28", "2026-01-29", "2026-01-30", "2026-02-02"]
    source = rows(calendar[:-1])
    feed = rolling.RollingStockDBFeed(source, calendar[-2], variant=rolling.VARIANTS[1], fixed_calendar=calendar)
    config, context = rolling.portfolio(), RunContext("synthetic-old-mixed", TradingMode.REPLAY)
    assert feed.rows.get(calendar[-2])[A]["execution_reference_basis"] == "mixed_stockdb_pre_close"
    assert isinstance(feed.signal_frame(calendar[-2], config, context), SignalUnavailable)
    assert feed.audit[-1]["decision"] == "252_session_warmup"
    saved = new_state(config.initial_cash)
    saved["pending_targets"] = {A: .04}
    before = deepcopy(saved)
    with pytest.raises(ValueError, match="Mixed StockDB execution reference basis"):
        advance(saved, feed, calendar[-2], config, 0, context=context)
    assert saved == before


def test_explicit_native_halt_keeps_empty_volume_and_original_monthly_ranking_path():
    calendar = pd.bdate_range("2025-01-01", "2026-02-02").strftime("%Y-%m-%d").tolist()
    day, previous = "2026-01-30", "2026-01-29"
    source = rows(calendar[:-1], symbols=(A, B))
    source["tradestatus"], source["adjustflag"] = pd.NA, pd.NA
    source.loc[source.date.eq(day), ["tradestatus", "adjustflag"]] = ["1", "3"]
    halt = source.date.eq(day) & source.symbol.eq(B)
    source.loc[halt, "tradestatus"] = "0"
    source.loc[halt, ["open", "volume", "amount"]] = [0., np.nan, np.nan]
    feed = rolling.RollingStockDBFeed(source, day, variant=rolling.VARIANTS[1], fixed_calendar=calendar)
    config, context = rolling.portfolio(), RunContext("synthetic-native-rolling", TradingMode.REPLAY)
    assert feed.rows.get(previous)[A]["execution_reference_basis"] == "mixed_stockdb_pre_close"
    assert feed.rows.get(day)[B]["execution_reference_basis"] == "native_raw"
    assert np.isnan(feed.rows.get(day)[B]["volume"])
    signal = feed.signal_frame(day, config, context)
    assert isinstance(signal, SignalFrame)
    assert [r["symbol"] for r in signal.ranked_candidates()] == [A]
    saved = new_state(config.initial_cash)
    saved.update(day=previous, positions={B: [{"qty": 100, "available": previous}]}, prices={B: 1000})
    result = advance(saved, feed, day, config, 0, context=context)
    assert result["stale_symbols"] == [B]
    assert sum(lot["qty"] for lot in saved["positions"][B]) == 100
    assert saved["cash"] == round(config.initial_cash * 100) and saved["orders"] == {} and saved["fills"] == {}
    assert saved["pending_targets"] == {A: .04}
