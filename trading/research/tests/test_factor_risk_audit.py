import unittest

import pandas as pd

from trading.research.factor_risk_audit import (daily_metrics, max_gross_for_shock,
                                                no_exit_shock, normalize_dates)


class FactorRiskAuditTests(unittest.TestCase):
    def test_daily_risk_uses_account_equity_and_cash_start(self):
        daily = pd.DataFrame({"date": ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
                              "equity": [100., 100., 90., 99.]})
        result = daily_metrics(daily, initial_cash=100)
        self.assertAlmostEqual(result["return"], -.01)
        self.assertAlmostEqual(result["worst_day"], -.10)
        self.assertAlmostEqual(result["max_drawdown"], -.10)
        self.assertIsNone(result["worst_5_session_window"])

    def test_no_exit_two_day_shock_and_exposure_cap(self):
        self.assertAlmostEqual(no_exit_shock(.8, -.2, 2), .288)
        self.assertAlmostEqual(max_gross_for_shock(.10, -.2, 2), .10 / .36)
        with self.assertRaises(ValueError):
            no_exit_shock(1.1, -.2, 2)

    def test_source_date_formats_align(self):
        self.assertEqual(normalize_dates(pd.Series([20260323])).tolist(), ["2026-03-23"])
        self.assertEqual(normalize_dates(pd.Series(["2026-03-23"])).tolist(), ["2026-03-23"])


if __name__ == "__main__":
    unittest.main()
