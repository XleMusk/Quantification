import unittest

from a_stock_committee.market_regime import classify_market_regime
from a_stock_committee.trend_calibration import TrendResult


def trend_result(target_id: str, score: float, signal: str) -> TrendResult:
    return TrendResult(
        target_id=target_id,
        name=target_id,
        symbol=target_id,
        latest_date="2026-05-22",
        prediction_date=None,
        score=score,
        signal=signal,
        actual_ret_pct=None,
        correct=None,
        train_accuracy=0.6,
        params={},
        diagnostics={},
    )


class MarketRegimeTests(unittest.TestCase):
    def test_classifies_strong_repair(self):
        results = [
            trend_result("overall", 0.12, "看多"),
            trend_result("chinext", 0.14, "看多"),
            trend_result("star50", 0.13, "看多"),
            trend_result("communication_etf", 0.16, "看多"),
            trend_result("ai_etf", 0.15, "看多"),
            trend_result("consumer_electronics_hx_etf", 0.14, "看多"),
            trend_result("electronics_etf", 0.13, "看多"),
        ]

        regime = classify_market_regime(results)

        self.assertEqual(regime.label, "强修复")
        self.assertIn("允许小仓位进攻", regime.risk_gate)

    def test_classifies_weak_repair(self):
        results = [
            trend_result("overall", -0.03, "震荡"),
            trend_result("chinext", 0.01, "震荡"),
            trend_result("star50", 0.02, "震荡"),
            trend_result("communication_etf", 0.08, "看多"),
            trend_result("ai_etf", 0.07, "看多"),
            trend_result("consumer_electronics_hx_etf", 0.06, "看多"),
            trend_result("electronics_etf", 0.03, "震荡"),
        ]

        regime = classify_market_regime(results)

        self.assertEqual(regime.label, "弱修复")
        self.assertIn("降低追涨权限", regime.risk_gate)

    def test_classifies_defensive_tape(self):
        results = [
            trend_result("overall", -0.12, "看空"),
            trend_result("chinext", -0.10, "看空"),
            trend_result("star50", -0.08, "看空"),
            trend_result("communication_etf", -0.07, "看空"),
            trend_result("ai_etf", -0.06, "看空"),
        ]

        regime = classify_market_regime(results)

        self.assertEqual(regime.label, "退潮防守")
        self.assertIn("防守为主", regime.risk_gate)

    def test_refuses_to_classify_when_data_is_insufficient(self):
        results = [
            TrendResult(
                target_id="overall",
                name="上证指数",
                symbol="sh000001",
                latest_date="2026-05-22",
                prediction_date=None,
                score=0.0,
                signal="数据失败",
                actual_ret_pct=None,
                correct=None,
                train_accuracy=0.0,
                params={},
                diagnostics={},
                error="not enough bars",
            )
        ]

        regime = classify_market_regime(results)

        self.assertEqual(regime.label, "数据不足")
        self.assertIn("先补足历史数据", regime.risk_gate)


if __name__ == "__main__":
    unittest.main()
