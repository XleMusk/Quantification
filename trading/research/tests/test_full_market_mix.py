import unittest

import pandas as pd

from trading.research.full_market_mix import proxy_replay


class FullMarketMixTests(unittest.TestCase):
    def fixture(self):
        dates = [20260105, 20260106, 20260107]
        symbols = [f"60000{i}" for i in range(5)]
        returns = pd.DataFrame(.1, index=dates, columns=symbols)
        tradable = pd.DataFrame(True, index=dates, columns=symbols)
        choices = {dates[0]: {"mix_equal": symbols}}
        return dates, symbols, returns, tradable, choices

    def test_close_signal_does_not_earn_next_session_return_before_entry(self):
        dates, _, returns, tradable, choices = self.fixture()
        result, daily = proxy_replay(dates, choices, returns, tradable, "mix_equal", 0, 0)
        self.assertAlmostEqual(result["proxy_return_after_cost"], .08)
        self.assertEqual(daily.equity.iloc[0], 140000)
        self.assertEqual(daily.equity.iloc[1], 140000)
        self.assertEqual(daily.holdings.tolist(), [0, 5, 5])
        returns.loc[dates[1]] = -.2
        changed, _ = proxy_replay(dates, choices, returns, tradable, "mix_equal", 0, 0)
        self.assertAlmostEqual(changed["proxy_return_after_cost"], .08)

    def test_cost_and_untradable_rebalance_are_explicit(self):
        dates, symbols, returns, tradable, choices = self.fixture()
        result, _ = proxy_replay(dates, choices, returns, tradable, "mix_equal")
        self.assertAlmostEqual(result["proxy_return_after_cost"], (1 - .8 * .0013) * 1.08 - 1)
        tradable.at[dates[1], symbols[0]] = False
        blocked, daily = proxy_replay(dates, choices, returns, tradable, "mix_equal")
        self.assertEqual(blocked["blocked_rebalances"], 1)
        self.assertEqual(blocked["proxy_return_after_cost"], 0)
        self.assertEqual(daily.holdings.iloc[-1], 0)

    def test_missing_held_bar_is_flagged_as_stale_not_a_realized_loss(self):
        dates, symbols, returns, tradable, choices = self.fixture()
        returns.at[dates[2], symbols[0]] = float("nan")
        tradable.at[dates[2], symbols[0]] = False
        result, daily = proxy_replay(dates, choices, returns, tradable, "mix_equal", 0, 0)
        self.assertEqual(result["stale_mark_days"], 1)
        self.assertEqual(result["stale_symbol_days"], 1)
        self.assertEqual(result["unpriced_end_holdings"], [symbols[0]])
        self.assertEqual(daily.stale_symbols.iloc[-1], symbols[0])

    def test_approximate_limit_veto_keeps_optimistic_result_separate(self):
        dates, symbols, returns, tradable, choices = self.fixture()
        buyable = tradable.copy()
        sellable = tradable.copy()
        buyable.at[dates[1], symbols[0]] = False
        cautious, _ = proxy_replay(dates, choices, returns, tradable, "mix_equal", 0, 0,
                                   buyable=buyable, sellable=sellable)
        optimistic, _ = proxy_replay(dates, choices, returns, tradable, "mix_equal", 0, 0)
        self.assertEqual(cautious["approximate_queue_vetoes"], 1)
        self.assertEqual(cautious["proxy_return_after_cost"], 0)
        self.assertAlmostEqual(optimistic["proxy_return_after_cost"], .08)


if __name__ == "__main__":
    unittest.main()
