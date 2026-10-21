import unittest

import numpy as np
import pandas as pd

from trading.research.institutional_mix import WEIGHTS, formulas, score_matrix, select_on_first_period


class InstitutionalMixTests(unittest.TestCase):
    def test_fixed_formula_and_weights(self):
        definitions = formulas().set_index("name")
        self.assertEqual(definitions.at["ROC60", "formula"], "Ref($close, 60)/$close")
        self.assertEqual(definitions.at["MOM252_SKIP21", "formula"], "Ref($close,21)/Ref($close,252)-1")
        self.assertTrue(all(abs(sum(weights.values()) - 1) < 1e-12 for weights in WEIGHTS.values()))

    def test_common_eligibility_and_no_future_score_dependency(self):
        days = pd.to_datetime(["2026-01-05", "2026-01-06"])
        symbols = [f"600{i:03d}.SSE" for i in range(60)]
        index = pd.MultiIndex.from_product([days, symbols], names=["date", "symbol"])
        order = np.tile(np.arange(60), 2)
        native = pd.DataFrame({"ROC60": 1 / (1 + order / 100), "RSV20": order / 60,
                               "MOM252_SKIP21": order / 100, "RETVOL60": .1 + order / 1000}, index=index)
        raw = pd.DataFrame({"date": index.get_level_values("date"), "symbol": index.get_level_values("symbol"),
                            "open": 10., "high": 11., "low": 9., "close": 10 + order / 100})
        eligible = pd.Series(True, index=index)
        eligible.iloc[0] = False
        native.iloc[1, native.columns.get_loc("MOM252_SKIP21")] = np.nan
        scores, coverage = score_matrix(native, raw, eligible)
        self.assertEqual(coverage["minimum_eligible_symbols"], 58)
        self.assertNotIn((days[0], symbols[0]), scores.index)
        self.assertNotIn((days[0], symbols[1]), scores.index)
        first = scores.loc[days[0]].copy()
        raw.loc[raw.date.eq(days[1]), "close"] += 4
        native.loc[days[1], "MOM252_SKIP21"] += 50
        changed, _ = score_matrix(native, raw, eligible)
        pd.testing.assert_frame_equal(first, changed.loc[days[0]])
        self.assertTrue(np.isfinite(scores.to_numpy()).all())

    def test_selection_uses_return_with_drawdown_gate_and_excludes_reference(self):
        rows = [
            {"variant": "existing_rule_reference", "status": "completed", "net_return": .9,
             "max_drawdown": -.01, "stale_valuation_days": 0},
            {"variant": "mix_equal", "status": "completed", "net_return": .4,
             "max_drawdown": -.21, "stale_valuation_days": 0},
            {"variant": "mix_trend", "status": "completed", "net_return": .2,
             "max_drawdown": -.1, "stale_valuation_days": 0},
            {"variant": "mix_defensive", "status": "completed", "net_return": .3,
             "max_drawdown": -.1, "stale_valuation_days": 1},
        ]
        self.assertEqual(select_on_first_period(rows), "mix_trend")
        self.assertIsNone(select_on_first_period(rows[:2]))


if __name__ == "__main__":
    unittest.main()
