import unittest

import pandas as pd

from trading.research.full_market_alpha158 import (decision_dates, evaluate_both,
                                                   select_candidates)


class FullMarketAlpha158Tests(unittest.TestCase):
    def test_direction_metrics_share_same_pairs(self):
        scores = pd.Series(range(1, 201), index=range(200))
        labels = scores * .01
        allowed = pd.Series(True, index=scores.index)
        high, low = evaluate_both(scores, labels, allowed)
        self.assertEqual(high[:3], ("high", 200, 0))
        self.assertAlmostEqual(high[3], 1)
        self.assertAlmostEqual(low[3], -1)
        self.assertGreater(high[4], 0)
        self.assertLess(low[4], 0)

    def test_selection_rejects_later_only_winner(self):
        summary = pd.DataFrame([
            {"factor": "A", "direction": "high", "segment": "selection",
             "evaluated_dates": 20, "min_pairs": 100, "mean_rank_ic": .08,
             "mean_top_minus_universe": .01},
            {"factor": "B", "direction": "high", "segment": "selection",
             "evaluated_dates": 20, "min_pairs": 100, "mean_rank_ic": -.01,
             "mean_top_minus_universe": -.01},
            {"factor": "B", "direction": "high", "segment": "later_check",
             "evaluated_dates": 12, "min_pairs": 100, "mean_rank_ic": .5,
             "mean_top_minus_universe": .1},
        ])
        self.assertEqual(select_candidates(summary), [{"factor": "A", "direction": "high"}])

    def test_selection_label_must_exit_before_segment_end(self):
        calendar = [20260629, 20260630, 20260701, 20260702, 20260703,
                    20260706, 20260707, 20260708, 20260709, 20260710,
                    20260713, 20260714]
        self.assertEqual(decision_dates(calendar), [(20260706, "later_check")])


if __name__ == "__main__":
    unittest.main()
