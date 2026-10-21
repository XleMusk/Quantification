from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from trading.market_dataset import write_frame, write_json
from trading.research.factors import catalog
from trading.research.market_indices import (INDICES, build, collect, compute, create, extend,
                                            feature_names, index_symbol, join_stock_features,
                                            load, normalize, single_catalog, source_formulas)


class MarketIndicesTests(unittest.TestCase):
    def bars(self, code, days):
        values = []
        for n, day in enumerate(days):
            close = 100 + n * n
            values.append(dict(date=day, code=code, open=close, high=close + 1, low=close - 1,
                               close=close, preclose=100 + max(0, n - 1) ** 2,
                               volume=1000 + n, amount=10000 + n * 200, pctChg=0))
        return pd.DataFrame(values)

    @contextmanager
    def source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            days = pd.bdate_range("2024-01-02", periods=8).strftime("%Y-%m-%d").tolist()
            write_json(root / "collection_plan.json", {"source": "BaoStock", "start": days[0], "end": days[-1]})
            write_frame(root / "calendar.csv", pd.DataFrame({"date": days}))
            write_frame(root / "provider_instruments.csv", pd.DataFrame([
                {"code": code, "type": "2", "ipoDate": "2000-01-01", "outDate": "", "status": "1"}
                for code in INDICES]))
            formulas = [f'Mask({f}, "{c.replace(".", "")}")' for c in INDICES for f in single_catalog().formula]
            recipe = root / "README.md"
            recipe.write_text("```python\n" + repr(formulas) + "\n```\n", encoding="utf-8")
            with patch("trading.research.market_indices.RECIPE_HASH", sha256(recipe.read_bytes()).hexdigest()):
                folder = create(root, Path(directory) / "indices", recipe)
                yield folder, days

    def test_source_list_and_order_are_checked_without_executing_code(self):
        formulas = [f'Mask({f}, "{c.replace(".", "")}")' for c in INDICES for f in single_catalog().formula]
        text = "```python\n" + repr(formulas) + ",\n['Mean$ typo']\n```"
        self.assertEqual(source_formulas(text), formulas)
        with self.assertRaisesRegex(ValueError, "formulas/order"):
            source_formulas("```python\n" + repr(formulas[::-1]) + "\n```")
        self.assertEqual(len(feature_names()), 63)

    def test_index_point_validation_is_not_equity_vwap_validation(self):
        days = ["2024-01-02", "2024-01-03"]
        frame = self.bars(INDICES[0], days)
        valid = normalize(frame, INDICES[0], days)
        self.assertEqual(valid.symbol.unique().tolist(), [index_symbol(INDICES[0])])
        # Aggregate index amount / constituent shares is not an index point price.
        self.assertLess(frame.amount.iloc[0] / frame.volume.iloc[0], frame.low.iloc[0])
        for field, value in (("amount", 0), ("high", 1), ("volume", -1), ("close", np.inf)):
            bad = frame.copy()
            bad[field] = bad[field].astype(float)
            bad.loc[0, field] = value
            with self.assertRaises(ValueError):
                normalize(bad, INDICES[0], days)
        with self.assertRaisesRegex(ValueError, "calendar gap"):
            normalize(frame.iloc[:1], INDICES[0], days)
        with self.assertRaises(ValueError):
            normalize(frame, INDICES[1], days)

    def test_collection_resumes_completed_receipts_and_hash_rejects_tampering(self):
        with self.source() as (folder, days), patch("trading.research.market_indices.baostock_session") as session:
            provider = session.return_value.__enter__.return_value
            provider.query_history_k_data_plus.side_effect = lambda code, *args, **kw: self.bars(code, days)
            with patch("trading.research.market_indices.provider_frame", side_effect=lambda response: response):
                result = collect(folder)
                self.assertEqual(result["status"], "raw_complete")
                self.assertEqual(provider.query_history_k_data_plus.call_count, 3)
                collect(folder)
                self.assertEqual(provider.query_history_k_data_plus.call_count, 3)
            self.assertFalse(result["execution_admitted"])
            (folder / "calendar.csv").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "calendar/master changed"):
                load(folder)

    def test_provider_failure_preserves_completed_index_and_stops(self):
        with self.source() as (folder, days), patch("trading.research.market_indices.baostock_session") as session:
            provider = session.return_value.__enter__.return_value
            provider.query_history_k_data_plus.side_effect = [self.bars(INDICES[0], days), RuntimeError("source stopped")]
            with patch("trading.research.market_indices.provider_frame", side_effect=lambda response: response):
                with self.assertRaisesRegex(RuntimeError, "source stopped"):
                    collect(folder)
            manifest, _ = load(folder)
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(list(manifest["receipts"]), [INDICES[0]])
            self.assertEqual(provider.query_history_k_data_plus.call_count, 2)
            with self.assertRaisesRegex(ValueError, "All three"):
                build(folder)

    def test_raw_extension_queries_only_new_dates_and_preserves_original(self):
        with self.source() as (folder, days), patch("trading.research.market_indices.baostock_session") as session:
            provider = session.return_value.__enter__.return_value
            provider.query_history_k_data_plus.side_effect = lambda code, *args, **kw: self.bars(code, days)
            with patch("trading.research.market_indices.provider_frame", side_effect=lambda response: response):
                collect(folder)
                original = (folder / "manifest.json").read_bytes()
                feature = folder / "market_features.parquet"
                feature.write_bytes(b"existing feature archive")
                tail = pd.bdate_range(pd.Timestamp(days[-1]) + pd.Timedelta(days=1), periods=3).strftime("%Y-%m-%d").tolist()
                calendar = folder.parent / "extended_calendar.csv"
                write_frame(calendar, pd.DataFrame({"date": days + tail}))
                provider.query_history_k_data_plus.reset_mock()
                provider.query_history_k_data_plus.side_effect = lambda code, *args, **kw: self.bars(code, tail)
                new, result = extend(folder, calendar, folder.parent / "extended")
            self.assertEqual(result["status"], "raw_complete")
            self.assertEqual(load(new)[1], days + tail)
            self.assertEqual((folder / "manifest.json").read_bytes(), original)
            self.assertEqual(feature.read_bytes(), b"existing feature archive")
            self.assertFalse((new / feature.name).exists())
            for call in provider.query_history_k_data_plus.call_args_list:
                self.assertEqual(call.kwargs["start_date"], tail[0])
                self.assertEqual(call.kwargs["end_date"], tail[-1])
            self.assertEqual(provider.query_history_k_data_plus.call_count, 3)
            self.assertTrue(all(row["appended_rows"] == 3 for row in result["receipts"].values()))

    def test_raw_extension_rejects_changed_prefix_and_preserves_failed_tail(self):
        with self.source() as (folder, days), patch("trading.research.market_indices.baostock_session") as session:
            provider = session.return_value.__enter__.return_value
            provider.query_history_k_data_plus.side_effect = lambda code, *args, **kw: self.bars(code, days)
            calendar = folder.parent / "extended_calendar.csv"
            with patch("trading.research.market_indices.provider_frame", side_effect=lambda response: response):
                collect(folder)
                write_frame(calendar, pd.DataFrame({"date": days[1:] + ["2024-01-12", "2024-01-15"]}))
                with self.assertRaisesRegex(ValueError, "unchanged calendar prefix"):
                    extend(folder, calendar, folder.parent / "bad_prefix")
                write_frame(calendar, pd.DataFrame({"date": days + ["2024-01-12", "2024-01-15"]}))
                provider.query_history_k_data_plus.side_effect = lambda code, *args, **kw: self.bars(code, ["2024-01-12"])
                with self.assertRaisesRegex(ValueError, "calendar gap"):
                    extend(folder, calendar, folder.parent / "missing_tail")
            result, _ = load(folder.parent / "missing_tail")
            self.assertEqual(result["status"], "failed_raw_extension")
            self.assertEqual(load(folder)[0]["status"], "raw_complete")

    def test_native_qlib_formulas_and_no_future_feature_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            days = pd.bdate_range("2024-01-02", periods=8).strftime("%Y-%m-%d").tolist()
            panel = pd.concat([normalize(self.bars(code, days), code, days) for code in INDICES], ignore_index=True)
            panel.date = pd.to_datetime(panel.date)
            first = compute(panel, days, Path(directory) / "a")
            self.assertEqual(first.shape, (8, 63))
            self.assertTrue(pd.isna(first.iloc[0, 0]))
            close = self.bars(INDICES[0], days).close.to_numpy(dtype=float)
            ret = close[1:] / close[:-1] - 1
            self.assertAlmostEqual(first.iloc[5, 1], ret[:5].mean(), places=6)
            self.assertAlmostEqual(first.iloc[5, 2], ret[:5].std(ddof=1), places=6)
            amount = self.bars(INDICES[0], days).amount.to_numpy(dtype=float)
            self.assertAlmostEqual(first.iloc[5, 3], amount[1:6].mean() / amount[5], places=6)
            panel.loc[panel.date.gt(pd.Timestamp(days[5])), ["close", "amount"]] *= 5
            second = compute(panel, days, Path(directory) / "b")
            pd.testing.assert_frame_equal(first.iloc[:6], second.iloc[:6])

    def test_join_preserves_every_stock_and_rejects_missing_market_days(self):
        dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
        index = pd.MultiIndex.from_tuples([(dates[0], "600000.SSE"), (dates[0], "600001.SSE"),
                                           (dates[1], "600000.SSE")], names=["date", "symbol"])
        stock = pd.DataFrame(1., index=index, columns=catalog().name.tolist())
        stock.iloc[-1, 0] = np.nan
        market = pd.DataFrame(2., index=dates, columns=feature_names())
        market.index.name = "date"
        joined = join_stock_features(stock, market)
        self.assertEqual(joined.shape, (3, 221))
        self.assertEqual(joined.index.tolist(), stock.index.tolist())
        self.assertTrue(pd.isna(joined.iloc[-1, 0]))
        with self.assertRaisesRegex(ValueError, "do not silently drop"):
            join_stock_features(stock, market.iloc[:1])
        with self.assertRaises(ValueError):
            join_stock_features(pd.concat([stock, stock]), market)


if __name__ == "__main__":
    unittest.main()
