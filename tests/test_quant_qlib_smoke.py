"""Native Qlib integration on disposable synthetic data, never alpha evidence."""

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from a_stock_committee.quant_foundation import DEFAULT_CONFIG, run_portfolio


class NativeQlibSmokeTests(unittest.TestCase):
    def test_native_engine_lag_lots_cash_and_costs(self):
        import qlib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = root / "provider"
            (provider / "calendars").mkdir(parents=True)
            (provider / "instruments").mkdir()
            calendar = pd.bdate_range("2019-12-02", periods=90)
            (provider / "calendars/day.txt").write_text("\n".join(calendar.strftime("%Y-%m-%d")) + "\n")
            codes = ["SH600000", "SH600001", "SH000300"]
            (provider / "instruments/all.txt").write_text("".join(
                f"{code}\t{calendar[0].date()}\t{calendar[-1].date()}\n" for code in codes))
            for j, code in enumerate(codes):
                folder = provider / "features" / code.lower()
                folder.mkdir(parents=True)
                opening = 10 + j + np.arange(len(calendar)) * .01
                close = opening * 1.001
                values = {"open": opening, "high": opening * 1.01, "low": opening * .99,
                          "close": close, "volume": np.full(len(calendar), 1000000.),
                          "factor": np.full(len(calendar), 2.),
                          "change": np.r_[0., close[1:] / close[:-1] - 1]}
                for name, series in values.items():
                    np.r_[0., series].astype("<f4").tofile(folder / f"{name}.day.bin")
            qlib.init(provider_uri=str(provider), region="cn", expression_cache=None,
                      dataset_cache=None, joblib_backend="threading", kernels=1)
            config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            config["segments"]["test"] = ["2020-01-03", "2020-02-28"]
            config["portfolio"].update(topk=1, n_drop=1, capital=100000)
            rows = []
            for i, date in enumerate(calendar[calendar >= "2020-01-06"]):
                for j, code in enumerate(codes[:2]):
                    rows.append({"date": date, "symbol": code, "signal_eligible": True,
                                 "momentum_20": 1. if (i // 4) % 2 == j else 0.})
            out = root / "result"
            out.mkdir()
            result = run_portfolio(pd.DataFrame(rows), config, {}, out)
            report = pd.read_csv(out / "portfolio_daily.csv")
            holdings = pd.read_csv(out / "holdings.csv")
            self.assertEqual(result["status"], "historical_execution_proxy_only")
            self.assertGreater(report["cost"].sum(), 0)
            self.assertTrue(report["cash"].ge(0).all())
            self.assertEqual(holdings.date.min(), "2020-01-07")
            self.assertEqual(report.loc[report.date.le("2020-01-06"), "turnover"].sum(), 0)
            self.assertTrue(holdings.adjusted_shares.gt(0).all())
            real_lots = holdings.adjusted_shares * 2 / 100
            np.testing.assert_allclose(real_lots, np.round(real_lots), atol=1e-5)
            self.assertLessEqual(holdings.groupby("date").size().max(), 1)


if __name__ == "__main__":
    unittest.main()
