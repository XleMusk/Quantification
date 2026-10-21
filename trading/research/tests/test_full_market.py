import unittest
from hashlib import sha256
import tempfile
from pathlib import Path

import pandas as pd

from trading.research.full_market import inspect_rows, month_intervals, verify_chunks
from trading.research.full_market_audit import compare_paired
from trading.research.full_market_screen import combined, evaluate
from trading.research.full_market_etf import etf_candidates, normalize_history
from trading.research.stockdb_2026_inventory import physical_inventory, summarize_chunks


class FullMarketTest(unittest.TestCase):
    def test_month_intervals_are_closed(self):
        self.assertEqual(list(month_intervals("2025-09-24", "2025-10-02")), [
            ("2025-09", "20250924", "20250930"),
            ("2025-10", "20251001", "20251002"),
        ])

    def test_inspection_preserves_missing_provider_fields(self):
        row = ["510300", 20250924, 4.0, 4.2, 3.9, 4.1, 100, 410, None, None, None]
        frame, checks = inspect_rows([row], "5", "20250924", "20250925")
        self.assertEqual(len(frame), 1)
        self.assertEqual(checks["missing_pct_chg"], 1)
        self.assertEqual(checks["missing_pre_close"], 1)
        self.assertEqual(checks["invalid_ohlc"], 0)

    def test_reject_out_of_range_day(self):
        row = ["600519", 20251001, 4.0, 4.2, 3.9, 4.1, 100, 410, 0.1, False, 4.0]
        with self.assertRaises(ValueError):
            inspect_rows([row], "6", "20250901", "20250930")

    def test_paired_compare_exposes_adjusted_pre_close(self):
        reference = pd.DataFrame([{"code": "000001", "date": 20240102,
            "open_ref": 9.39, "high_ref": 9.42, "low_ref": 9.21, "close_ref": 9.21,
            "volume_ref": 100, "amount_ref": 921, "pct_chg_ref": -1.917, "pre_close_ref": 9.39}])
        source = pd.DataFrame([{"code": "000001", "date": 20240102,
            "open": 9.39, "high": 9.42, "low": 9.21, "close": 9.21,
            "volume": 100, "amount": 921, "pct_chg": -1.917, "pre_close": 7.881}])
        result = compare_paired(source, reference)
        self.assertEqual(result["paired_rows"], 1)
        self.assertEqual(result["differences"]["pre_close"]["mismatch_rows"], 1)
        self.assertEqual(result["differences"]["pct_chg"]["mismatch_rows"], 0)

    def test_factor_evaluation_drops_unknown_forward_labels(self):
        codes = [f"{index:06d}" for index in range(110)]
        score = pd.Series(range(110), index=codes)
        label = pd.Series(range(110), index=codes, dtype=float) / 100
        label.iloc[-1] = float("nan")
        result = evaluate(score, label, pd.Series(True, index=codes))
        self.assertEqual(result["pairs"], 109)
        self.assertEqual(result["missing_forward_labels"], 1)
        self.assertGreater(result["rank_ic"], .99)

    def test_fixed_combination_weights(self):
        score = pd.Series([1., 2., 3.], index=["a", "b", "c"])
        choices = combined({"momentum_60_skip5": score, "momentum_120_skip5": score,
                            "breakout_20": score, "reversal_5": score,
                            "low_volatility_20": score})
        self.assertEqual(set(choices), {"trend_defensive", "trend_breakout", "trend_reversal"})
        self.assertAlmostEqual(choices["trend_defensive"]["c"], 1.)

    def test_current_etf_marker_excludes_lof(self):
        self.assertEqual(etf_candidates([["510300", "300ETF"], ["160119", "500ETF LOF"],
                                         ["510300", "300ETF"], ["166401", None]]), ["510300"])

    def test_adjusted_etf_history_normalization(self):
        frame = pd.DataFrame([["2025-09-24", 4.0, 4.1, 4.2, 3.9, 100, 410,
                               2.0, 1.0, .1, 3.0]], columns=list(range(11)))
        normalized = normalize_history(frame, "510300")
        self.assertEqual(normalized.date.iloc[0], "20250924")
        self.assertEqual(normalized.close.iloc[0], 4.1)

    def test_snapshot_hash_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "one.parquet"
            path.write_bytes(b"good")
            manifest = {"status": "complete_inventory_only", "chunks_expected": 1,
                        "chunks": [{"parquet_file": path.name,
                                    "parquet_sha256": sha256(b"good").hexdigest()}]}
            verify_chunks(folder, manifest)
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verify_chunks(folder, manifest)

    def test_2026_summary_groups_pinned_chunks(self):
        chunks = [{"month": "2026-01", "prefix": "5", "rows": 2,
                   "missing_pct_chg": 2, "missing_pre_close": 2,
                   "invalid_ohlc": 0, "first_date": 20260105, "last_date": 20260106},
                  {"month": "2026-02", "prefix": "5", "rows": 1,
                   "missing_pct_chg": 0, "missing_pre_close": 0,
                   "invalid_ohlc": 0, "first_date": 20260202, "last_date": 20260202}]
        result = summarize_chunks(chunks)
        self.assertEqual(result["total"]["rows"], 3)
        self.assertEqual(result["by_month"]["2026-01"]["missing_pct_chg"], 2)
        self.assertEqual(result["by_prefix"]["5"]["last_date"], 20260202)

    def test_physical_inventory_ignores_rotated_leveldb_logs(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("data", "data1"):
                folder = root / name
                folder.mkdir()
                (folder / "one.ldb").write_bytes(b"data")
                (folder / ".sync_manifest.json").write_text(json.dumps({"files": [
                    {"path": "one.ldb", "size": 4},
                    {"path": "rotated.log", "size": 0}]}), encoding="utf-8")
            result = physical_inventory(root)
            self.assertEqual(result["segments"]["data"]["missing_manifest_ldb"], [])
            self.assertEqual(result["segments"]["data1"]["size_mismatch_ldb"], [])


if __name__ == "__main__":
    unittest.main()
