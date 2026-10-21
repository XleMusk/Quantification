import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import a_stock_committee.trend_calibration as trend_calibration
from a_stock_committee.trend_calibration import (
    TrendTarget,
    add_trend_features,
    apply_event_overlay,
    backfill_trend_history,
    cached_calibrate_params,
    ensure_target_history,
    evaluate_target_frame,
    signal_from_score,
    trend_history_cache_summary,
)


class TrendCalibrationTests(unittest.TestCase):
    def test_signal_thresholds(self):
        self.assertEqual(signal_from_score(0.10, 0.05), "看多")
        self.assertEqual(signal_from_score(-0.10, 0.05), "看空")
        self.assertEqual(signal_from_score(0.01, 0.05), "震荡")

    def test_evaluate_target_frame_uses_prior_bar_for_evaluation(self):
        dates = pd.bdate_range("2025-01-01", periods=220)
        trend = np.linspace(10, 14, len(dates))
        wiggle = np.sin(np.linspace(0, 18, len(dates))) * 0.12
        close = trend + wiggle
        frame = pd.DataFrame(
            {
                "date": dates,
                "open": close * 0.997,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": np.linspace(1_000_000, 1_500_000, len(dates)),
                "amount": close * np.linspace(1_000_000, 1_500_000, len(dates)),
            }
        )
        features = add_trend_features(frame)
        target = TrendTarget("synthetic", "合成指数", "index", "syn000001")
        eval_date = features.iloc[-1]["date"].strftime("%Y-%m-%d")

        result = evaluate_target_frame(target, features, evaluate_date=eval_date, lookback=120)

        self.assertEqual(result.prediction_date, eval_date)
        self.assertEqual(result.latest_date, eval_date)
        self.assertEqual(result.diagnostics["pred_base_date"], features.iloc[-2]["date"].strftime("%Y-%m-%d"))
        self.assertIsInstance(result.correct, bool)
        self.assertGreaterEqual(result.train_accuracy, 0)
        self.assertLessEqual(result.train_accuracy, 1)

    def test_nvl72_event_boosts_consumer_electronics(self):
        row = pd.Series(
            {
                "ret_1": 0.02,
                "upper_shadow": 0.002,
            }
        )
        target = TrendTarget(
            "consumer_electronics_hx_etf",
            "消费电子ETF华夏",
            "etf",
            "159732",
            ("consumer_electronics", "electronics_manufacturing", "ai_hardware"),
        )

        adjusted, event_adjustment = apply_event_overlay(0.0, row, target, "nvl72_bom")

        self.assertIsNotNone(event_adjustment)
        self.assertGreater(adjusted, 0)
        self.assertIn("consumer_electronics", event_adjustment["matched_tags"])

    def test_trend_history_cache_reuses_recent_years(self):
        dates = pd.bdate_range("2023-01-02", periods=520)
        frame = pd.DataFrame(
            {
                "date": dates,
                "open": np.linspace(10, 12, len(dates)),
                "high": np.linspace(10.1, 12.1, len(dates)),
                "low": np.linspace(9.9, 11.9, len(dates)),
                "close": np.linspace(10, 12, len(dates)),
                "volume": np.linspace(1_000_000, 2_000_000, len(dates)),
                "amount": np.linspace(10_000_000, 20_000_000, len(dates)),
                "target_id": "synthetic",
                "target_name": "合成ETF",
                "symbol": "159999",
            }
        )
        target = TrendTarget("synthetic", "合成ETF", "etf", "159999")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("a_stock_committee.trend_calibration.fetch_target_history", return_value=frame) as fetch:
                first = ensure_target_history(
                    target,
                    start_date="2023-01-02",
                    end_date="2024-12-31",
                    cache_dir=tmp,
                )
                second = ensure_target_history(
                    target,
                    start_date="2023-06-01",
                    end_date="2024-06-30",
                    cache_dir=tmp,
                )

            self.assertEqual(fetch.call_count, 1)
            self.assertGreater(len(first), len(second))
            self.assertGreaterEqual(second["date"].min(), pd.Timestamp("2023-06-01"))
            self.assertLessEqual(second["date"].max(), pd.Timestamp("2024-06-30"))

            entries = trend_history_cache_summary(cache_dir=tmp, targets=[target])
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].rows, len(frame))
            self.assertEqual(entries[0].start, "2023-01-02")

    def test_backfill_trend_history_reports_fetch_errors(self):
        target = TrendTarget("broken", "坏数据", "etf", "159998")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("a_stock_committee.trend_calibration.fetch_target_history", side_effect=RuntimeError("boom")):
                entries = backfill_trend_history(
                    targets=[target],
                    start_date="2023-01-01",
                    end_date="2024-01-01",
                    cache_dir=tmp,
                )

        self.assertEqual(len(entries), 1)
        self.assertIn("RuntimeError", entries[0].error)

    def test_cached_calibrate_params_reuses_grid_result(self):
        dates = pd.bdate_range("2025-01-01", periods=120)
        close = np.linspace(10, 11, len(dates))
        frame = pd.DataFrame(
            {
                "date": dates,
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.98,
                "close": close,
                "volume": np.linspace(1_000_000, 1_200_000, len(dates)),
                "amount": close * np.linspace(1_000_000, 1_200_000, len(dates)),
            }
        )
        train = add_trend_features(frame).tail(80)
        target = TrendTarget("synthetic", "合成ETF", "etf", "159999")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("a_stock_committee.trend_calibration.calibrate_params", wraps=trend_calibration.calibrate_params) as calibrate:
                first_params, first_acc = cached_calibrate_params(target, train, lookback=80, cache_dir=tmp)
                second_params, second_acc = cached_calibrate_params(target, train, lookback=80, cache_dir=tmp)

            self.assertEqual(calibrate.call_count, 1)
            self.assertEqual(first_params, second_params)
            self.assertEqual(first_acc, second_acc)


if __name__ == "__main__":
    unittest.main()
