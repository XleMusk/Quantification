import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from a_stock_committee.engine import (
    create_empty_thesis,
    create_sample_thesis,
    evaluate_red_flags,
    generate_review_markdown,
)
from a_stock_committee.market_data import normalize_symbol
from a_stock_committee.position_sizing import calculate_a_share_position
from a_stock_committee.radar_config import small_account_config
from a_stock_committee.screeners import analyze_stock
from a_stock_committee.skill_catalog import get_skill, list_skills
from a_stock_committee.trader_memory import close_thesis, list_theses, register_thesis


class CommitteeEngineTest(unittest.TestCase):
    def test_empty_thesis_blocks_without_stop(self):
        thesis = create_empty_thesis()
        thesis["stock_name"] = "测试股份"
        thesis["code"] = "600000.SSE"
        flags = evaluate_red_flags(thesis)
        self.assertTrue(any("没有预设止损" in item for item in flags))
        self.assertTrue(any("没有写清" in item for item in flags))

    def test_sample_review_contains_core_sections(self):
        report = generate_review_markdown(create_sample_thesis())
        self.assertIn("六席投委会审查", report)
        self.assertIn("仓位与风险预算", report)
        self.assertIn("A股执行与风控席", report)

    def test_skill_catalog_discovers_position_sizer(self):
        names = {item.name for item in list_skills()}
        self.assertIn("position-sizer", names)
        item = get_skill("position-sizer")
        self.assertTrue((item.path / "SKILL.md").exists())

    def test_a_share_position_sizer_rounds_lots(self):
        result = calculate_a_share_position(
            account_equity=100000,
            entry_price=10,
            stop_pct=8,
            risk_pct=0.5,
            max_position_pct=5,
        )
        self.assertEqual(result.final_shares, 500)
        self.assertAlmostEqual(result.final_risk_pct, 0.4)

    def test_trader_memory_register_and_close(self):
        with TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "theses"
            item = register_thesis(create_sample_thesis(), state_dir=state_dir)
            self.assertEqual(item["status"], "IDEA")
            self.assertEqual(len(list_theses(state_dir)), 1)
            closed = close_thesis(
                item["thesis_id"],
                exit_price=10.5,
                reason="test close",
                state_dir=state_dir,
                lessons=["按计划退出"],
            )
            self.assertEqual(closed["status"], "CLOSED")
            self.assertEqual(list_theses(state_dir)[0]["status"], "CLOSED")

    def test_symbol_normalization(self):
        self.assertEqual(normalize_symbol("600000").vt_symbol, "600000.SSE")
        self.assertEqual(normalize_symbol("000001.SZ").vt_symbol, "000001.SZSE")
        self.assertEqual(normalize_symbol("SH600519").baostock_code, "sh.600519")
        self.assertEqual(normalize_symbol("159509").security_type, "fund")

    def test_analyze_stock_with_synthetic_uptrend(self):
        dates = pd.date_range("2024-01-01", periods=240, freq="B")
        close = pd.Series(range(240), dtype="float64") / 10 + 10
        df = pd.DataFrame(
            {
                "date": dates,
                "code": "sh.600000",
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "preclose": close.shift(1).fillna(close.iloc[0]),
                "volume": 10_000_000,
                "amount": 200_000_000,
                "adjustflag": "2",
                "turn": 1.0,
                "tradestatus": 1,
                "pctChg": 0.5,
                "peTTM": 18,
                "pbMRQ": 2,
                "psTTM": 1,
                "pcfNcfTTM": 10,
                "isST": 0,
                "vt_symbol": "600000.SSE",
                "qlib_code": "SH600000",
                "source": "test",
            }
        )
        decision = analyze_stock(df)
        self.assertGreaterEqual(decision.metrics["trend_score"], 70)
        self.assertIn("trend-watch", decision.strategy_tags)

    def test_external_risk_radar_block_overrides_uptrend(self):
        dates = pd.date_range("2024-01-01", periods=240, freq="B")
        close = pd.Series(range(240), dtype="float64") / 10 + 10
        df = pd.DataFrame(
            {
                "date": dates,
                "code": "sh.600745",
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "preclose": close.shift(1).fillna(close.iloc[0]),
                "volume": 10_000_000,
                "amount": 200_000_000,
                "adjustflag": "2",
                "turn": 1.0,
                "tradestatus": 1,
                "pctChg": 0.5,
                "peTTM": 18,
                "pbMRQ": 2,
                "psTTM": 1,
                "pcfNcfTTM": 10,
                "isST": 0,
                "vt_symbol": "600745.SSE",
                "qlib_code": "SH600745",
                "security_type": "stock",
                "source": "test",
            }
        )

        decision = analyze_stock(
            df,
            external_risk_flags=["BLOCK: 风险雷达红灯[新增负面事件] 退市风险警示"],
        )

        self.assertEqual(decision.strategy_tags, [])
        self.assertEqual(decision.label, "红灯：不进入买入流程")
        self.assertTrue(any("风险雷达红灯" in flag for flag in decision.risk_flags))

    def test_small_account_blocks_expensive_lot(self):
        dates = pd.date_range("2024-01-01", periods=240, freq="B")
        close = pd.Series(range(240), dtype="float64") / 10 + 100
        df = pd.DataFrame(
            {
                "date": dates,
                "code": "sh.600519",
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "preclose": close.shift(1).fillna(close.iloc[0]),
                "volume": 10_000_000,
                "amount": 2_000_000_000,
                "adjustflag": "2",
                "turn": 1.0,
                "tradestatus": 1,
                "pctChg": 0.5,
                "peTTM": 25,
                "pbMRQ": 5,
                "psTTM": 1,
                "pcfNcfTTM": 10,
                "isST": 0,
                "vt_symbol": "600519.SSE",
                "qlib_code": "SH600519",
                "source": "test",
            }
        )
        decision = analyze_stock(df, config=small_account_config(10_000))
        self.assertTrue(any("超过小资金价格上限" in flag for flag in decision.risk_flags))
        self.assertTrue(any("超过单票预算" in flag for flag in decision.risk_flags))


if __name__ == "__main__":
    unittest.main()
