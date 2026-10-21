from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.research.bulk_model_inputs import dated_eligibility, provider_labels
from trading.research.factors import LABEL, qcode
from quant_learning_starter.factor_lab import compute_factors_and_labels


def test_cached_provider_labels_match_existing_calendar_function(tmp_path):
    days = pd.date_range("2024-01-02", periods=15, freq="B")
    symbols = ["600000.SSE", "000001.SZSE"]
    panels = []
    for offset, symbol in enumerate(symbols):
        values = {"open": np.arange(10 + offset, 25 + offset, dtype=np.float32),
                  "close": np.arange(11 + offset, 26 + offset, dtype=np.float32),
                  "volume": np.full(15, 1000, dtype=np.float32)}
        for value in values.values():
            value[5] = np.nan
        folder = tmp_path / "features" / qcode(symbol).lower()
        folder.mkdir(parents=True)
        for name, value in values.items():
            np.r_[0, value].astype("<f4").tofile(folder / (name + ".day.bin"))
        panels.append(pd.DataFrame({"date": days, "symbol": symbol, **{k: v.astype(float) for k, v in values.items()}}))
    expected = compute_factors_and_labels(pd.concat(panels), days).set_index(["date", "symbol"])[[LABEL, "entry_date", "exit_date"]]
    pd.testing.assert_frame_equal(provider_labels(tmp_path, symbols, days), expected)
    assert pd.isna(expected.at[(days[4], symbols[0]), LABEL])
    (tmp_path / "features" / "sh600000" / "open.day.bin").write_bytes(b"short")
    with pytest.raises(ValueError, match="calendar/header"):
        provider_labels(tmp_path, symbols, days)


def test_dated_pool_uses_raw_status_liquidity_and_listing_not_future_events():
    days = pd.date_range("2024-01-02", periods=24, freq="B").strftime("%Y-%m-%d").tolist()
    symbols = ["600000.SSE", "000001.SZSE", "689009.SSE"]
    common = {"tradestatus": 1, "isST": 0, "volume": 1000}
    rows = {day: {s: dict(common) for s in symbols} for day in days}
    rows[days[20]][symbols[0]]["isST"] = 1
    rows[days[21]][symbols[0]]["tradestatus"] = 0
    rows[days[22]][symbols[0]]["volume"] = 0
    feed = SimpleNamespace(calendar=days, symbols=symbols, rows=rows,
                           unsupported_instruments={symbols[2]: "CDR"},
                           manifest={"selection_date": "2023-01-01"},
                           amounts=pd.DataFrame(30_000_000, index=days, columns=symbols),
                           instruments={s: {"ipoDate": "2020-01-01", "outDate": ""} for s in symbols})
    feed.instruments[symbols[1]]["outDate"] = days[23]
    pool = dated_eligibility(feed, StrategyConfig())
    assert not pool.loc[pd.Timestamp(days[18])].any()
    assert pool.at[(pd.Timestamp(days[19]), symbols[0])]
    assert not pool.xs(symbols[2], level="symbol").any()
    assert not pool.loc[pd.to_datetime(days[20:23]), symbols[0]].any()
    assert not pool.at[(pd.Timestamp(days[23]), symbols[1])]
    subset = dated_eligibility(feed, StrategyConfig(), dates=days[20:24])
    pd.testing.assert_series_equal(subset, pool.loc[pd.to_datetime(days[20:24])])
    with pytest.raises(ValueError, match="distinct pinned"):
        dated_eligibility(feed, StrategyConfig(), dates=[days[20], days[20]])
