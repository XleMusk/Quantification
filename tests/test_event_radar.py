import tempfile
import unittest

from a_stock_committee.alpha_stack import MarketSnapshot
from a_stock_committee.catalyst_monitor import NewsItem, scan_catalysts
from a_stock_committee.event_radar import (
    build_event_radar,
    event_radar_markdown,
    regime_adjusted_playbook,
    save_event_radar_report,
)
from a_stock_committee.intraday_breadth import IntradayBreadth
from a_stock_committee.market_regime import MarketRegime


class EventRadarTests(unittest.TestCase):
    def test_build_event_radar_links_catalyst_to_alpha_scores(self):
        hits = scan_catalysts(
            [
                NewsItem(
                    title="财联社：大摩NVL72机柜BOM引发AI硬件链重估",
                    summary="PCB、CPO、铜连接、消费电子、AI服务器ODM价值量提升。",
                    source="财联社",
                    published_at="刚刚",
                )
            ],
            threshold=7,
        )
        snapshots = [
            MarketSnapshot(
                target_id="communication_etf",
                name="通信ETF国泰",
                symbol="515880",
                pct_change=4.2,
                amount=2_500_000_000,
                open_price=1.40,
                high=1.50,
                low=1.39,
                latest=1.49,
                main_net_inflow=420_000_000,
                turnover=8,
            ),
            MarketSnapshot(
                target_id="consumer_electronics_hx_etf",
                name="消费电子ETF华夏",
                symbol="159732",
                pct_change=6.8,
                amount=640_000_000,
                open_price=1.49,
                high=1.57,
                low=1.49,
                latest=1.56,
                main_net_inflow=-90_000_000,
                turnover=23,
            ),
        ]

        selected_hits, scores = build_event_radar(hits, snapshots, top_events=1)
        score_map = {score.symbol: score for score in scores}

        self.assertEqual(len(selected_hits), 1)
        self.assertIn("515880", score_map)
        self.assertIn("159732", score_map)
        self.assertGreater(score_map["515880"].total_score, score_map["159732"].total_score)

    def test_save_event_radar_report_writes_markdown_and_csvs(self):
        hits = scan_catalysts(
            [
                NewsItem(
                    title="摩根士丹利NVL72 BOM成本拆解",
                    summary="CPO、铜连接、消费电子价值量提升。",
                    source="财联社",
                    published_at="刚刚",
                )
            ],
            threshold=7,
        )
        snapshots = [
            MarketSnapshot(
                target_id="communication_etf",
                name="通信ETF国泰",
                symbol="515880",
                pct_change=4.2,
                amount=2_500_000_000,
                open_price=1.40,
                high=1.50,
                low=1.39,
                latest=1.49,
            )
        ]
        selected_hits, scores = build_event_radar(hits, snapshots, top_events=1)

        with tempfile.TemporaryDirectory() as tmp:
            hits_csv, scores_csv, md_path = save_event_radar_report(selected_hits, scores, output_dir=tmp)
            text = md_path.read_text(encoding="utf-8")

            self.assertTrue(hits_csv.exists())
            self.assertTrue(scores_csv.exists())
            self.assertIn("A股事件雷达", text)
            self.assertIn("通信ETF国泰", text)

    def test_event_radar_markdown_includes_market_regime_gate(self):
        regime = MarketRegime(
            label="弱修复",
            score=0.24,
            broad_score=-0.06,
            tech_score=0.61,
            broad_positive_count=1,
            tech_positive_count=6,
            risk_gate="降低追涨权限，优先等分歧低吸",
            execution_bias="强催化可观察，避免大涨后追高",
            diagnostics={},
        )

        text = event_radar_markdown([], [], regime=regime)

        self.assertIn("市场环境闸门", text)
        self.assertIn("弱修复", text)
        self.assertIn("降低追涨权限", text)

    def test_event_radar_markdown_includes_intraday_breadth(self):
        breadth = IntradayBreadth(
            label="强修复但高拥挤",
            total=12,
            positive_count=12,
            strong_count=6,
            hot_count=3,
            negative_flow_count=4,
            positive_ratio=1.0,
            avg_pct_change=3.58,
            median_pct_change=3.52,
            total_amount=10_000_000_000,
            main_net_inflow=800_000_000,
            risk_gate="强度够但拥挤高，禁止追高，等分歧换手",
            diagnostics={},
        )

        text = event_radar_markdown([], [], breadth=breadth)

        self.assertIn("盘中修复强度", text)
        self.assertIn("强修复但高拥挤", text)
        self.assertIn("禁止追高", text)

    def test_regime_adjusted_playbook_downgrades_attack_in_weak_repair(self):
        regime = MarketRegime(
            label="弱修复",
            score=0.24,
            broad_score=-0.06,
            tech_score=0.61,
            broad_positive_count=1,
            tech_positive_count=6,
            risk_gate="降低追涨权限，优先等分歧低吸",
            execution_bias="强催化可观察，避免大涨后追高",
            diagnostics={},
        )
        hits = scan_catalysts(
            [
                NewsItem(
                    title="摩根士丹利NVL72机柜BOM成本拆解",
                    summary="CPO、铜连接、AI服务器ODM价值量提升。",
                )
            ],
            threshold=7,
        )
        selected_hits, scores = build_event_radar(
            hits,
            [
                MarketSnapshot(
                    target_id="communication_etf",
                    name="通信ETF国泰",
                    symbol="515880",
                    pct_change=3.2,
                    amount=2_500_000_000,
                    open_price=1.40,
                    high=1.48,
                    low=1.39,
                    latest=1.47,
                    main_net_inflow=350_000_000,
                    turnover=8,
                )
            ],
            top_events=1,
        )

        self.assertEqual(len(selected_hits), 1)
        text = regime_adjusted_playbook(scores[0], regime)
        self.assertIn("进攻降一档", text)
        self.assertIn("回踩承接", text)


if __name__ == "__main__":
    unittest.main()
