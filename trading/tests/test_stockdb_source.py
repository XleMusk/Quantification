import unittest
from unittest.mock import patch

from pathlib import Path
from tempfile import TemporaryDirectory
from trading.stockdb_source import bar_consistency, compare_bar, freeze_snapshot, source_bar, source_minute


class StockDBSourceTests(unittest.TestCase):
    def test_source_bar_checks_code_and_date(self):
        with patch("trading.stockdb_source.local_get", return_value={"code": "600519", "date": 20260924}) as get:
            self.assertEqual(source_bar("600519.SSE", "2026-09-24")["code"], "600519")
            get.assert_called_once_with("日k:600519:20260924")
        with patch("trading.stockdb_source.local_get", return_value={"code": "600519", "date": 20260923}):
            with self.assertRaisesRegex(ValueError, "Unexpected StockDB daily bar"):
                source_bar("600519.SSE", "2026-09-24")

    def test_raw_close_agreement_does_not_hide_adjusted_preclose(self):
        source = {"open": 1731.2, "high": 1738.43, "low": 1706.01, "close": 1730.01,
                  "pre_close": 1564.18, "volume": 2603380, "amount": 4487760384}
        reference = {"open": 1731.2, "high": 1738.43, "low": 1706.01, "close": 1730.01,
                     "preclose": 1727.0, "volume": 2603380, "amount": 4487760230.88}
        comparison = compare_bar(source, reference)
        self.assertEqual(list(comparison["differences"]), ["pre_close"])
        self.assertEqual(comparison["differences"]["pre_close"]["difference"], -162.82)

    def test_minute_bar_is_bound_to_requested_timestamp(self):
        with patch("trading.stockdb_source.local_get", return_value={"code": "600519", "date": 20260924150000}) as get:
            self.assertEqual(source_minute("600519.SSE", "2026-09-24")["date"], 20260924150000)
            get.assert_called_once_with("分钟k:600519:20260924150000")

    def test_recorded_etf_volume_scale_gap_is_reported_not_corrected(self):
        source = {"low": 4.838, "high": 4.919, "volume": 53201664, "amount": 25922582528.0}
        original = dict(source)
        result = bar_consistency(source)
        self.assertEqual(result["status"], "amount_volume_outside_ohlc")
        self.assertGreater(result["amount_divided_by_volume"], 480)
        self.assertFalse(result["automatic_conversion"])
        self.assertEqual(source, original)
        source["volume"] = 5320166468
        self.assertEqual(bar_consistency(source)["status"], "within_ohlc_not_unit_verification")

    def test_missing_preclose_stays_missing_in_comparison(self):
        source = {"open": 4.737, "high": 4.769, "low": 4.718, "close": 4.738,
                  "pre_close": None, "volume": 2913439864, "amount": 13792576512.0}
        reference = {**source, "preclose": 4.736}
        result = compare_bar(source, reference)
        self.assertEqual(result["missing_fields"], ["pre_close"])
        self.assertEqual(result["differences"], {})
        self.assertEqual(result["stockdb_internal_consistency"]["status"], "within_ohlc_not_unit_verification")

    def test_nonfinite_and_zero_volume_are_not_validated(self):
        source = {"low": 1.0, "high": 2.0, "volume": 0, "amount": 0}
        self.assertEqual(bar_consistency(source)["status"], "inactive_no_vwap")
        source["amount"] = float("inf")
        self.assertEqual(bar_consistency(source)["status"], "invalid_values")

    def test_freeze_snapshot_preserves_missing_rows_and_non_admission(self):
        daily = {"code": "600519", "date": 20261009, "open": 1260.0, "high": 1270.0,
                 "low": 1250.0, "close": 1263.0, "pre_close": 1261.0,
                 "volume": 100, "amount": 126300.0}
        minute = {"code": "600519", "date": 20261009150000, "open": 1263.0,
                  "high": 1263.0, "low": 1263.0, "close": 1263.0,
                  "volume": 10, "amount": 12630.0}

        def fake_get(key):
            if key == "日k:600519:20261009":
                return daily
            if key == "分钟k:600519:20261009150000":
                return minute
            return []

        with TemporaryDirectory() as temp, patch("trading.stockdb_source.local_get", side_effect=fake_get):
            result = freeze_snapshot(Path(temp), Path(temp) / "snapshot",
                                     ["600519.SSE"], ["2026-10-09", "2026-10-10"])
            self.assertTrue(result["research_only"])
            self.assertFalse(result["execution_admitted"])
            self.assertEqual(result["rows"]["missing_daily"], 1)
            self.assertIn("2026-10-10", (Path(temp) / "snapshot" / "availability.csv").read_text(encoding="utf-8-sig"))

    def test_freeze_snapshot_can_pin_effective_exchange_sessions(self):
        daily = {"code": "600519", "date": 20261009, "open": 1260.0, "high": 1270.0,
                 "low": 1250.0, "close": 1263.0, "pre_close": 1261.0,
                 "volume": 100, "amount": 126300.0}

        def fake_get(key):
            if key.startswith("日k:600519:"):
                return {**daily, "date": int(key.rsplit(":", 1)[-1])}
            return []

        with TemporaryDirectory() as temp, patch("trading.stockdb_source.local_get", side_effect=fake_get):
            result = freeze_snapshot(
                Path(temp), Path(temp) / "snapshot", ["600519.SSE"],
                ["2026-10-08", "2026-10-09", "2026-10-10"],
                session_dates=["2026-10-08", "2026-10-09"],
                calendar_basis="calendar.json",
            )
            self.assertEqual(result["rows"]["requested_daily"], 2)
            self.assertEqual(result["rows"]["missing_daily"], 0)
            self.assertEqual(result["non_trading_dates"], ["2026-10-10"])
            self.assertEqual(result["calendar_basis"], "calendar.json")


if __name__ == "__main__":
    unittest.main()
