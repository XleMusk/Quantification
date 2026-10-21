import unittest

from a_stock_committee.catalyst_monitor import NewsItem
from a_stock_committee.risk_radar import dedupe_news_items, scan_risk_items, score_risk_text, symbol_risk_flags


class RiskRadarTests(unittest.TestCase):
    def test_flags_juli_sling_regulatory_misleading_disclosure_risk(self):
        item = NewsItem(
            title="巨力索具收到证监会立案告知书",
            summary="公司因涉嫌信息披露误导性陈述违法违规被证监会立案调查，此前公司曾发布市场传闻澄清及风险提示公告。",
            source="公告",
            published_at="2026-05-15 20:00:00",
        )

        signals = scan_risk_items([item], threshold=5)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].symbol, "002342")
        self.assertEqual(signals[0].severity, "红灯-回避/禁止买入")
        self.assertIn("监管立案/信披", signals[0].categories)
        self.assertIn("移出进攻池", signals[0].action)

    def test_registration_notice_is_hard_red_even_when_title_is_brief(self):
        item = NewsItem(
            title="巨力索具:关于收到中国证券监督管理委员会立案告知书的公告",
            summary="巨力索具 002342",
            source="公告",
            published_at="2026-05-16",
        )

        signals = scan_risk_items([item], threshold=5)

        self.assertEqual(signals[0].severity, "红灯-回避/禁止买入")

    def test_flags_st_wingtech_audit_and_delisting_risk(self):
        item = NewsItem(
            title="闻泰科技将被实施退市风险警示，简称变更为*ST闻泰",
            summary="审计机构对公司2025年度财务会计报告出具无法表示意见，内部控制审计报告亦为无法表示意见，公司叠加其他风险警示并连续跌停。",
            source="公告",
            published_at="2026-04-29 20:00:00",
        )

        signals = scan_risk_items([item], threshold=5)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].symbol, "600745")
        self.assertEqual(signals[0].severity, "红灯-回避/禁止买入")
        self.assertIn("ST/退市风险", signals[0].categories)
        self.assertIn("审计/内控非标", signals[0].categories)

    def test_filters_plain_positive_catalyst_text(self):
        text = "摩根士丹利NVL72 BOM拆解，PCB和CPO价值量提升。"
        score, matched = score_risk_text(text)

        self.assertEqual(score, 0)
        self.assertEqual(matched, [])
        self.assertEqual(scan_risk_items([NewsItem(title=text)], threshold=5), [])

    def test_dedupe_news_items_uses_title_time_and_url(self):
        items = [
            NewsItem(title="*ST闻泰:股票交易异常波动公告", published_at="2026-05-20", url="u1"),
            NewsItem(title="*ST闻泰:股票交易异常波动公告", published_at="2026-05-20", url="u1"),
            NewsItem(title="*ST闻泰:股票交易异常波动公告", published_at="2026-05-14", url="u2"),
        ]

        deduped = dedupe_news_items(items)

        self.assertEqual(len(deduped), 2)

    def test_collapses_baseline_st_status_noise_but_keeps_gate(self):
        items = [
            NewsItem(title="*ST闻泰:年度报告", summary="*ST闻泰 600745 财务报告", published_at="2026-05-20"),
            NewsItem(title="*ST闻泰:转债转股结果", summary="*ST闻泰 600745 例行公告", published_at="2026-05-21"),
        ]

        signals = scan_risk_items(items, threshold=5)
        flags = symbol_risk_flags(signals)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].risk_type, "存量风险状态")
        self.assertIn("600745", flags)
        self.assertTrue(flags["600745"][0].startswith("BLOCK: 风险雷达红灯"))

    def test_keeps_material_event_even_for_st_stock(self):
        items = [
            NewsItem(title="*ST闻泰:年度报告", summary="*ST闻泰 600745 财务报告", published_at="2026-05-20"),
            NewsItem(title="*ST闻泰:重大诉讼公告", summary="控制权受限并涉及重大诉讼 600745", published_at="2026-05-21"),
        ]

        signals = scan_risk_items(items, threshold=5)

        self.assertEqual(len(signals), 2)
        self.assertTrue(any(signal.risk_type == "新增负面事件" for signal in signals))

    def test_benign_release_phrases_do_not_create_new_event_score(self):
        item = NewsItem(
            title="*ST闻泰:关于股东部分股权解除质押的公告",
            summary="*ST闻泰 600745",
            published_at="2026-05-14",
        )

        signals = scan_risk_items([item], threshold=5)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].risk_type, "存量风险状态")
        self.assertNotIn("诉讼/冻结/质押", signals[0].categories)

    def test_standard_annual_related_party_table_is_not_governance_red(self):
        item = NewsItem(
            title="汉缆股份:2025年度非经营性资金占用及其他关联方资金往来情况汇总表",
            summary="汉缆股份 002498",
            published_at="2026-04-29",
        )

        signals = scan_risk_items([item], threshold=5)

        self.assertEqual(signals, [])


if __name__ == "__main__":
    unittest.main()
