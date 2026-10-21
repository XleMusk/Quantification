import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from a_stock_committee.quant_foundation import (
    DEFAULT_CONFIG, FACTORS, LABEL, build_signal, execution_settings, factor_frame,
    backtest_blockers, inspect_panel, membership_mask, portfolio_metrics, read_calendar, split_diagnostics,
    validate_config,
)


def fixture():
    calendar = pd.bdate_range("2018-01-02", periods=85, name="date")
    records = []
    for i, date in enumerate(calendar):
        for j in range(8):
            price = 10 + j + i * (0.02 + j * 0.005)
            records.append(dict(date=date, symbol=f"SH60000{j}", open=price, high=price * 1.02,
                                low=price * .98, close=price * 1.005, volume=10000 + i * (j + 1), factor=1.0))
    membership = pd.DataFrame({"symbol": [f"SH60000{j}" for j in range(8)],
                               "start": calendar[0], "end": calendar[-1]})
    return pd.DataFrame(records), calendar, membership


class QuantFoundationTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        self.calendar = pd.bdate_range("2005-01-04", "2021-06-11", name="date")

    def test_default_config_valid(self):
        validate_config(self.config, self.calendar)

    def test_reject_stale_provider_as_current_data(self):
        self.config["segments"]["test"][1] = "2026-09-25"
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_config(self.config, self.calendar)

    def test_reject_touching_segments(self):
        self.config["segments"]["validation"][0] = "2018-12-31"
        with self.assertRaisesRegex(ValueError, "non-overlapping"):
            validate_config(self.config, self.calendar)

    def test_reject_invalid_portfolio_settings(self):
        for key, value in (("capital", -1), ("risk_degree", 2), ("open_cost", -1),
                           ("slippage_bps", float("nan")), ("n_drop", 100), ("topk", 1.5)):
            config = copy.deepcopy(self.config)
            config["portfolio"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_config(config, self.calendar)

    def test_calendar_rejects_unsorted_or_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "calendars"
            folder.mkdir()
            for values in ("2020-01-03\n2020-01-02\n", "2020-01-02\n2020-01-02\n"):
                (folder / "day.txt").write_text(values)
                with self.assertRaises(ValueError):
                    read_calendar(directory)

    def test_membership_interval_endpoints_and_reentry(self):
        index = pd.MultiIndex.from_product([pd.date_range("2020-01-01", periods=5), ["SH600000"]], names=["date", "symbol"])
        membership = pd.DataFrame({"symbol": ["SH600000", "SH600000"],
                                   "start": pd.to_datetime(["2020-01-02", "2020-01-05"]),
                                   "end": pd.to_datetime(["2020-01-03", "2020-01-05"])})
        self.assertEqual(membership_mask(index, membership).tolist(), [False, True, True, False, True])

    def test_duplicate_bars_fail(self):
        panel, _, _ = fixture()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            inspect_panel(pd.concat([panel, panel.iloc[:1]]))

    def test_invalid_prices_volume_and_factors_are_counted(self):
        panel, _, _ = fixture()
        panel.loc[0, "open"] = 0
        panel.loc[1, "volume"] = 0
        panel.loc[2, "high"] = 1
        panel.loc[3, "factor"] = np.nan
        usable, quality = inspect_panel(panel)
        self.assertFalse(usable.iloc[:3].any())
        self.assertEqual(quality["invalid_factor_rows_with_close"], 1)
        self.assertEqual(quality["invalid_price_rows_with_close"], 1)

    def test_future_changes_do_not_change_earlier_factors(self):
        panel, calendar, membership = fixture()
        original, _ = factor_frame(panel, calendar, membership)
        panel.loc[panel.date > calendar[40], ["open", "high", "low", "close"]] *= 3
        modified, _ = factor_frame(panel, calendar, membership)
        mask = original.date <= calendar[40]
        pd.testing.assert_frame_equal(original.loc[mask, list(FACTORS)], modified.loc[mask, list(FACTORS)])

    def test_missing_session_does_not_compress_label_horizon(self):
        panel, calendar, membership = fixture()
        panel = panel.loc[~((panel.date == calendar[31]) & (panel.symbol == "SH600000"))]
        result, _ = factor_frame(panel, calendar, membership)
        row = result.loc[(result.date == calendar[30]) & (result.symbol == "SH600000")].iloc[0]
        self.assertTrue(pd.isna(row[LABEL]))
        self.assertEqual(row.entry_date, calendar[31])
        self.assertEqual(row.exit_date, calendar[36])

    def test_label_exit_is_not_masked_by_future_index_membership(self):
        panel, calendar, membership = fixture()
        membership.loc[membership.symbol == "SH600000", "end"] = calendar[31]
        result, _ = factor_frame(panel, calendar, membership)
        row = result.loc[(result.date == calendar[30]) & (result.symbol == "SH600000")].iloc[0]
        self.assertTrue(row.in_universe)
        self.assertTrue(np.isfinite(row[LABEL]))
        later = result.loc[(result.date == calendar[35]) & (result.symbol == "SH600000")].iloc[0]
        self.assertFalse(later.signal_eligible)

    def test_future_missing_label_does_not_delete_signal(self):
        panel, calendar, membership = fixture()
        result, _ = factor_frame(panel, calendar, membership)
        signal = build_signal(result, "momentum_20")
        self.assertIn((calendar[-1], "SH600000"), signal.index)
        self.assertTrue(result.loc[result.date == calendar[-1], LABEL].isna().all())

    def test_split_boundary_labels_are_purged(self):
        panel, calendar, membership = fixture()
        result, _ = factor_frame(panel, calendar, membership)
        config = copy.deepcopy(self.config)
        config["min_pairs"] = 5
        config["segments"] = {name: [str(calendar[start].date()), str(calendar[end].date())]
                              for name, start, end in (("development", 20, 40), ("validation", 41, 60), ("test", 61, 84))}
        _, daily, _, counts = split_diagnostics(result, config)
        boundary = daily.loc[(daily.segment == "development") & (daily.date > calendar[34])]
        self.assertTrue(boundary.n_pairs.eq(0).all())
        self.assertEqual(counts[0]["purged_boundary_rows"], 6 * 8)

    def test_execution_uses_open_and_lagged_volume(self):
        settings = execution_settings(self.config["portfolio"], ["SH600000"])
        self.assertEqual(settings["trade_unit"], 100)
        self.assertTrue(all("$open" in expr and "$change" not in expr for expr in settings["limit_threshold"]))
        self.assertIn("Ref($volume, 1)", settings["volume_threshold"][1])
        self.assertIn("1.001", settings["deal_price"][0])
        self.assertIn("0.999", settings["deal_price"][1])

    def test_metrics_include_first_day_drawdown_and_costs(self):
        report = pd.DataFrame({"return": [-.1, 0], "cost": [.01, 0], "bench": [0, 0],
                               "cash": [50, 50], "account": [89, 89], "turnover": [.1, 0]})
        metrics = portfolio_metrics(report)
        self.assertAlmostEqual(metrics["net_total_return"], -.11)
        self.assertAlmostEqual(metrics["max_drawdown"], -.11)

    def test_backtest_blocks_bad_data_and_missing_constituents(self):
        quality = {"inconsistent_ohlc_rows": 2, "missing_feature_symbols": ["SH600000"]}
        self.assertEqual(len(backtest_blockers(quality)), 2)
        self.assertEqual(backtest_blockers({"inconsistent_ohlc_rows": 0}), [])

    def test_negative_cash_and_missing_benchmark_fail(self):
        report = pd.DataFrame({"return": [.1], "cost": [0], "bench": [0],
                               "cash": [-1], "account": [100], "turnover": [.1]})
        with self.assertRaisesRegex(ValueError, "cash"):
            portfolio_metrics(report)
        report["cash"] = 1
        report["bench"] = np.nan
        with self.assertRaisesRegex(ValueError, "non-finite"):
            portfolio_metrics(report)


if __name__ == "__main__":
    unittest.main()
