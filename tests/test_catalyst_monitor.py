from datetime import datetime, timedelta
import unittest

from a_stock_committee.catalyst_monitor import (
    NewsItem,
    affected_targets,
    freshness_score,
    scan_catalysts,
    score_news_item,
    score_text,
    source_quality_score,
)


class CatalystMonitorTests(unittest.TestCase):
    def test_scores_nvl72_bom_morgan_report(self):
        text = (
            "摩根士丹利披露英伟达Rubin VR200 NVL72机柜BOM成本拆解，"
            "PCB、MLCC、ABF载板、电源、液冷和CPO价值量提升。"
        )

        score, matched = score_text(text)

        self.assertGreaterEqual(score, 7)
        self.assertIn("摩根士丹利", matched)
        self.assertIn("NVL72", matched)
        self.assertIn("PCB", matched)

    def test_scan_maps_to_consumer_electronics_and_communication(self):
        items = [
            NewsItem(
                title="大摩NVL72机柜物料清单引发AI硬件链重估",
                summary="PCB、MLCC、CPO、铜连接、消费电子和AI服务器ODM环节价值量提升。",
                source="unit-test",
                published_at="2026-05-22 09:00:00",
            )
        ]

        hits = scan_catalysts(items, threshold=7)

        self.assertEqual(len(hits), 1)
        targets = ",".join(hits[0].affected_targets)
        self.assertIn("消费电子ETF华夏", targets)
        self.assertIn("通信ETF国泰", targets)

    def test_affected_targets_for_nvl72_components(self):
        targets = affected_targets("PCB MLCC CPO 铜连接 消费电子 AI服务器", "nvl72_bom")
        joined = ",".join(targets)

        self.assertIn("消费电子ETF华夏", joined)
        self.assertIn("通信ETF国泰", joined)
        self.assertIn("电子50ETF", joined)

    def test_source_and_freshness_boost_high_quality_recent_items(self):
        now = datetime(2026, 5, 22, 10, 0, 0)
        item = NewsItem(
            title="摩根士丹利NVL72机柜BOM成本拆解",
            summary="PCB、CPO、铜连接价值量提升。",
            source="财联社",
            published_at="2026-05-22 09:45:00",
        )

        score, matched, source_score, freshness = score_news_item(item, now=now)

        self.assertIn("摩根士丹利", matched)
        self.assertGreaterEqual(source_score, 3.0)
        self.assertEqual(freshness, 1.5)
        self.assertGreater(score, sum([3.0, 3.0, 2.5]))

    def test_old_low_quality_mentions_are_penalized(self):
        now = datetime(2026, 5, 22, 10, 0, 0)
        item = NewsItem(
            title="股吧小作文称NVL72 PCB涨价",
            summary="未证实消息。",
            source="股吧",
            published_at=(now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
        )

        self.assertLess(source_quality_score(item), 0)
        self.assertLess(freshness_score(item.published_at, now=now), 0)

    def test_confirmation_boost_marks_multiple_sources(self):
        items = [
            NewsItem(
                title="财联社：大摩NVL72机柜BOM引发AI硬件链重估",
                summary="PCB、CPO、铜连接、消费电子、AI服务器ODM价值量提升。",
                source="财联社",
                published_at="2026-05-22 09:30:00",
            ),
            NewsItem(
                title="东方财富：摩根士丹利NVL72物料清单受关注",
                summary="PCB、CPO、铜连接、消费电子、AI服务器ODM价值量提升。",
                source="东方财富",
                published_at="2026-05-22 09:35:00",
            ),
        ]

        hits = scan_catalysts(items, threshold=7)

        self.assertEqual(len(hits), 2)
        self.assertGreaterEqual(hits[0].confirmation_count, 2)
        self.assertIn("财联社", hits[0].confirmation_sources)
        self.assertIn("东方财富", hits[0].confirmation_sources)


if __name__ == "__main__":
    unittest.main()
