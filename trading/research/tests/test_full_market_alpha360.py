import unittest

import pandas as pd

from trading.research.full_market_alpha360 import (catalog360, dual_positive_count,
                                                    vectorized_scores)


class FullMarketAlpha360Tests(unittest.TestCase):
    def test_installed_catalog_has_360_unique_definitions(self):
        definitions = catalog360()
        self.assertEqual(len(definitions), 360)
        self.assertEqual(definitions.name.nunique(), 360)
        self.assertFalse(definitions.formula.isna().any())

    def test_dual_positive_requires_both_metrics_in_both_segments(self):
        summary = pd.DataFrame([
            {"factor": "A", "direction": "high", "segment": "selection",
             "mean_rank_ic": .02, "mean_top_minus_universe": .01},
            {"factor": "A", "direction": "high", "segment": "later_check",
             "mean_rank_ic": .01, "mean_top_minus_universe": .01},
            {"factor": "B", "direction": "low", "segment": "selection",
             "mean_rank_ic": .02, "mean_top_minus_universe": .01},
            {"factor": "B", "direction": "low", "segment": "later_check",
             "mean_rank_ic": -.01, "mean_top_minus_universe": .01},
            {"factor": "C", "direction": "high", "segment": "selection",
             "mean_rank_ic": .02, "mean_top_minus_universe": .01},
            {"factor": "C", "direction": "high", "segment": "later_check",
             "mean_rank_ic": .01, "mean_top_minus_universe": -.01},
        ])
        self.assertEqual(dual_positive_count(summary), 1)

    def test_vectorized_lags_cannot_wrap_before_first_session(self):
        calendar = pd.DataFrame(index=range(60))
        with self.assertRaisesRegex(ValueError, "59 prior sessions"):
            vectorized_scores(None, calendar, None, None, [(30, "selection")],
                              None, None, 8)


if __name__ == "__main__":
    unittest.main()
