from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.feed import HistoricalFeed


def test_low_vol_momentum_uses_fixed_cross_sectional_median_gate():
    day = "2026-01-05"
    symbols = ["000001.SZSE", "000002.SZSE", "000003.SZSE"]
    feed = HistoricalFeed.__new__(HistoricalFeed)
    feed.symbols = symbols
    feed.manifest = {"selection_date": "2025-01-01"}
    feed.instruments = {
        symbol: {"ipoDate": "2020-01-01", "outDate": ""}
        for symbol in symbols
    }
    feed.rows = {
        day: {
            symbol: {"tradestatus": 1, "isST": 0}
            for symbol in symbols
        }
    }
    feed.features = lambda config: (
        pd.DataFrame([[.30, .20, .10]], index=[day], columns=symbols),
        pd.DataFrame([[.10, .20, .30]], index=[day], columns=symbols),
        pd.DataFrame([[100., 100., 100.]], index=[day], columns=symbols),
    )

    config = StrategyConfig(
        signal="low_vol_momentum", min_amount=1, min_listing_days=1,
        holdings=2, retain_rank=2, initial_cash=10000,
    )
    result = feed.scores(day, config)

    assert [row["symbol"] for row in result] == ["000001.SZSE", "000002.SZSE"]
    assert [row["score"] for row in result] == pytest.approx([3.0, 1.0])


def test_liquid_momentum_uses_fixed_activity_median_gate():
    day = "2026-01-05"
    symbols = ["000001.SZSE", "000002.SZSE", "000003.SZSE"]
    feed = HistoricalFeed.__new__(HistoricalFeed)
    feed.symbols = symbols
    feed.instruments = {
        symbol: {"ipoDate": "2020-01-01", "outDate": "", "board": "MAIN"}
        for symbol in symbols
    }
    feed.rows = {day: {
        symbol: {"tradestatus": 1, "isST": 0} for symbol in symbols
    }}
    feed.manifest = {"selection_date": "2020-01-01"}
    feed.features = lambda config: (
        pd.DataFrame([[.30, .20, .10]], index=[day], columns=symbols),
        pd.DataFrame([[.10, .20, .30]], index=[day], columns=symbols),
        pd.DataFrame([[100., 200., 300.]], index=[day], columns=symbols),
    )
    config = StrategyConfig(
        signal="liquid_momentum", min_amount=1, min_listing_days=1,
        holdings=2, retain_rank=2, initial_cash=10000,
    )
    result = feed.scores(day, config)

    assert [row["symbol"] for row in result] == ["000002.SZSE", "000003.SZSE"]
    assert [row["score"] for row in result] == pytest.approx([1.0, 1 / 3])
