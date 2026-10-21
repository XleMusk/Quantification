import unittest

from a_stock_committee.alpha_stack import MarketSnapshot
from a_stock_committee.intraday_breadth import classify_intraday_breadth


def snapshot(symbol: str, pct: float, flow: float | None = 100_000_000) -> MarketSnapshot:
    return MarketSnapshot(
        target_id=symbol,
        name=symbol,
        symbol=symbol,
        pct_change=pct,
        amount=500_000_000,
        open_price=1.0,
        high=1.0 + max(pct, 0) / 100,
        low=0.99,
        latest=1.0 + pct / 100,
        main_net_inflow=flow,
        turnover=5,
    )


class IntradayBreadthTests(unittest.TestCase):
    def test_classifies_strong_repair(self):
        snapshots = [
            snapshot("a", 4.0),
            snapshot("b", 3.5),
            snapshot("c", 3.2),
            snapshot("d", 2.8),
            snapshot("e", 2.5),
            snapshot("f", 1.5),
        ]

        breadth = classify_intraday_breadth(snapshots)

        self.assertEqual(breadth.label, "强修复")
        self.assertGreaterEqual(breadth.positive_ratio, 0.75)

    def test_classifies_weak_repair(self):
        snapshots = [
            snapshot("a", 3.1),
            snapshot("b", 2.2),
            snapshot("c", 1.0),
            snapshot("d", 0.5),
            snapshot("e", -0.2),
            snapshot("f", -0.4),
        ]

        breadth = classify_intraday_breadth(snapshots)

        self.assertEqual(breadth.label, "弱修复")
        self.assertIn("弱修复", breadth.risk_gate)

    def test_classifies_overheated_repair(self):
        snapshots = [
            snapshot("a", 6.0, -100_000_000),
            snapshot("b", 5.5, -50_000_000),
            snapshot("c", 5.2),
            snapshot("d", 3.8),
            snapshot("e", 2.6),
            snapshot("f", 1.8),
        ]

        breadth = classify_intraday_breadth(snapshots)

        self.assertEqual(breadth.label, "强修复但高拥挤")
        self.assertIn("禁止追高", breadth.risk_gate)


if __name__ == "__main__":
    unittest.main()
