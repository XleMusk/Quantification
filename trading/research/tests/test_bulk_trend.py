from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.research.bulk_trend import BulkTrendFeed, trend_prices
from trading.research.seat_trend import donchian, donchian_matrices


class BulkTrendTests(unittest.TestCase):
    def test_matrix_rule_matches_prior_channels_and_is_prefix_causal(self):
        days = pd.bdate_range("2025-01-02", periods=48)
        close = pd.DataFrame({"600000.SSE": [10.] * 21 + list(np.linspace(11, 17, 20)) + [8.] * 7,
                              "300001.SZSE": [np.nan] * 3 + [20.] * 18 + list(np.linspace(22, 34, 20)) + [16.] * 7}, index=days)
        high, low = close + .1, close - .1
        high.iloc[29, 1] = low.iloc[29, 1] = close.iloc[29, 1] = np.nan
        actual = donchian_matrices(high, low, close)
        self.assertFalse(actual["trend_active"][20, 0])
        self.assertTrue(actual["trend_active"][21, 0])
        self.assertAlmostEqual(actual["entry_channel"][21, 0], 10.1)
        self.assertFalse(actual["trend_active"][41, 0])
        self.assertTrue(np.isnan(actual["atr"][30:, 1]).all())
        for symbol in close:
            panel = pd.DataFrame({"date": days, "symbol": symbol, "high": high[symbol],
                                  "low": low[symbol], "close": close[symbol]})
            old = donchian(panel, days.strftime("%Y-%m-%d").tolist())
            index = close.columns.get_loc(symbol)
            for field in actual:
                np.testing.assert_allclose(actual[field][:, index], old[field].to_numpy(), equal_nan=True)
        altered = close.copy()
        altered.iloc[32:] *= 100
        later = donchian_matrices(altered + .1, altered - .1, altered)
        first = donchian_matrices(close + .1, close - .1, close)
        for field in first:
            np.testing.assert_allclose(first[field][:32], later[field][:32], equal_nan=True)

    def test_dated_bulk_pool_and_declared_signal_override(self):
        calendar = pd.bdate_range("2025-01-02", periods=48).strftime("%Y-%m-%d").tolist()
        symbols = ["600000.SSE", "600001.SSE", "300001.SZSE", "689009.SSE"]
        close = pd.DataFrame(np.tile(np.r_[[10.] * 21, np.linspace(11, 17, 27)], (4, 1)).T,
                             index=calendar, columns=symbols)
        base = SimpleNamespace(calendar=calendar, day_index={d: i for i, d in enumerate(calendar)}, symbols=symbols,
            amounts=pd.DataFrame(21000000., index=calendar, columns=symbols),
            instruments={s: {"ipoDate": "2000-01-01" if s != "300001.SZSE" else "", "outDate": ""} for s in symbols},
            unsupported_instruments={"689009.SSE": "CDR"}, manifest={"selection_date": "2023-01-01"},
            rows={calendar[-1]: {s: {"tradestatus": 1, "isST": int(s == "600001.SSE"), "volume": 10000000} for s in symbols}})
        with patch("trading.research.bulk_trend.trend_prices", return_value={"high": close + .1, "low": close - .1, "close": close}):
            feed = BulkTrendFeed(base)
        context = RunContext("test_replay", TradingMode.REPLAY)
        frame = feed.signal_frame(calendar[-1], StrategyConfig(), context)
        self.assertEqual([s.security.symbol for s in frame.signals], ["600000.SSE"])
        self.assertEqual(frame.source, "rule:donchian_trend20_10_atr20")
        self.assertEqual(feed.audit[calendar[-1]]["supported_symbols"], 3)
        self.assertEqual(feed.audit[calendar[-1]]["candidates"], 1)
        base.rows[calendar[-1]]["600000.SSE"]["tradestatus"] = 0
        self.assertEqual(feed.scores(calendar[-1], StrategyConfig()), [])

    def test_pinned_price_reader_masks_halts_and_refuses_different_close(self):
        with tempfile.TemporaryDirectory() as folder:
            days, symbol = ["2025-01-02", "2025-01-03"], "600000.SSE"
            frame = pd.DataFrame({"date": days, "code": "sh.600000", "adjustflag": "2", "high": "11",
                                  "low": "9", "close": "10", "volume": ["1000", "0"], "tradestatus": ["1", "0"]})
            path = Path(folder) / "canonical_daily.parquet"
            with pq.ParquetWriter(path, pa.Table.from_pandas(frame).schema) as writer:
                for i in range(2):
                    writer.write_table(pa.Table.from_pandas(frame.iloc[i:i + 1], preserve_index=False))
            base = SimpleNamespace(folder=Path(folder), calendar=days, symbols=[symbol], codes=["sh.600000"],
                day_members=[np.array([0]), np.array([0])],
                closes=pd.DataFrame(10., index=days, columns=[symbol]),
                volumes=pd.DataFrame([1000., 0.], index=days, columns=[symbol]))
            quotes = trend_prices(base)
            self.assertEqual(quotes["close"].iloc[0, 0], 10)
            self.assertTrue(np.isnan(quotes["close"].iloc[1, 0]))
            base.closes.iloc[0, 0] = 12
            with self.assertRaisesRegex(ValueError, "do not match"):
                trend_prices(base)


if __name__ == "__main__":
    unittest.main()
