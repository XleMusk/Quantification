import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from trading.market_dataset import (BAOSTOCK_FIELDS, bar_quality, collect, discover,
                                    prioritized, provider_frame, select_universe, summarize, write_frame, write_json)


class MarketDatasetTests(unittest.TestCase):
    def test_priority_changes_order_without_removing_delisted_or_other_boards(self):
        universe, _ = select_universe(self.master(), "2023-01-01", "2026-09-24")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "priority.csv"
            write_frame(path, pd.DataFrame({"symbol": ["300001.SZSE", "600000.SSE"]}))
            records = prioritized(universe, path)
            self.assertEqual([r["symbol"] for r in records[:2]], ["300001.SZSE", "600000.SSE"])
            self.assertEqual({r["symbol"] for r in records}, set(universe.symbol))
            self.assertTrue(any(r["status"] == "0" for r in records))
            write_frame(path, pd.DataFrame({"symbol": ["unknown"]}))
            with self.assertRaisesRegex(ValueError, "outside discovery"):
                prioritized(universe, path)

    def test_empty_provider_result_keeps_declared_columns_without_inventing_rows(self):
        response = SimpleNamespace(error_code="0", error_msg="success", fields=["code", "dividOperateDate"],
                                   get_data=lambda: pd.DataFrame())
        frame = provider_frame(response)
        self.assertEqual(list(frame.columns), response.fields)
        self.assertTrue(frame.empty)

    def master(self):
        rows = [
            ("sh.600000", "2000-01-01", "", "1"),
            ("sh.600070", "2000-01-01", "2025-04-30", "0"),
            ("sz.000001", "2000-01-01", "2022-12-31", "0"),
            ("sz.300001", "2024-06-01", "", "1"),
            ("sh.688001", "2019-01-01", "", "1"),
            ("sh.689009", "2020-01-01", "", "1"),
            ("bj.920001", "2020-01-01", "", "1"),
            ("sh.600001", "2027-01-01", "", "1"),
        ]
        return pd.DataFrame([{ "code": c, "code_name": "fixture", "ipoDate": i, "outDate": o,
                              "type": "1", "status": s} for c, i, o, s in rows])

    def bars(self, flag="3"):
        row = dict.fromkeys(BAOSTOCK_FIELDS.split(","), "0")
        row.update(date="2024-01-02", code="sh.600000", open="10", high="11", low="9",
                   close="10", preclose="10", volume="1000", amount="10000", adjustflag=flag,
                   tradestatus="1", isST="0")
        return pd.DataFrame([row])

    def fixture(self, root):
        master = self.master().iloc[:1]
        universe, _ = select_universe(master, "2024-01-01", "2024-01-03")
        write_frame(root / "universe.csv", universe)
        write_frame(root / "calendar.csv", pd.DataFrame({"date": ["2024-01-02", "2024-01-03"]}))
        write_json(root / "collection_plan.json", {"source": "BaoStock", "start": "2024-01-01",
            "end": "2024-01-03", "fields": BAOSTOCK_FIELDS, "limitations": []})

    def test_historical_universe_keeps_delisted_and_new_listings(self):
        selected, excluded = select_universe(self.master(), "2023-01-01", "2026-09-24")
        self.assertEqual(set(selected.code), {"sh.600000", "sh.600070", "sz.300001", "sh.688001"})
        self.assertEqual(selected.loc[selected.code.eq("sh.600070"), "status"].iloc[0], "0")
        self.assertEqual(len(excluded), 4)
        broken = pd.concat([self.master(), self.master().iloc[:1]])
        with self.assertRaises(ValueError):
            select_universe(broken, "2023-01-01", "2026-09-24")

    def test_quality_preserves_gaps_and_rejects_mixed_price_units(self):
        instrument = self.master().iloc[0].to_dict()
        bars = self.bars()
        quality = bar_quality(bars, instrument, ["2024-01-02", "2024-01-03"], "3")
        self.assertEqual(quality["missing_calendar_dates"], ["2024-01-03"])
        self.assertEqual(quality["invalid_price_dates"], [])
        bars.loc[0, "close"] = "99"
        self.assertEqual(bar_quality(bars, instrument, ["2024-01-02"], "3")["invalid_price_dates"], ["2024-01-02"])
        with self.assertRaises(ValueError):
            bar_quality(self.bars("2"), instrument, ["2024-01-02"], "3")

    def test_existing_plan_cannot_change_period(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with self.assertRaisesRegex(ValueError, "new folder"):
                discover(root, "2023-01-01", "2024-01-03")

    def test_resume_uses_completed_tasks_and_does_not_admit_to_execution(self):
        with tempfile.TemporaryDirectory() as folder, patch("trading.market_dataset.baostock_session") as session, patch("trading.market_dataset.checked", side_effect=lambda frame: frame), patch("trading.market_dataset.time.sleep"):
            root = Path(folder)
            self.fixture(root)
            bs = session.return_value.__enter__.return_value
            bs.query_history_k_data_plus.side_effect = lambda *args, **kwargs: self.bars(kwargs["adjustflag"])
            result = collect(root, 1, "bars")
            self.assertEqual(bs.query_history_k_data_plus.call_count, 2)
            self.assertEqual(result["status"], "partial_backfill")
            self.assertFalse(result["execution_admitted"])
            collect(root, 1, "bars")
            self.assertEqual(bs.query_history_k_data_plus.call_count, 2)
            bs.query_dividend_data.return_value = pd.DataFrame(columns=["code", "dividOperateDate"])
            result = collect(root, 1, "actions")
            self.assertEqual(result["complete_symbols"], 1)
            self.assertEqual(result["symbols_with_quality_issues"], 1)
            self.assertFalse(result["execution_admitted"])
            (root / "raw/600000.SSE.parquet").unlink()
            self.assertEqual(summarize(root)["complete_symbols"], 0)


if __name__ == "__main__":
    unittest.main()
