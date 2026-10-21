import json
import tempfile
import unittest

from a_stock_committee.alpha_stack import (
    AlphaScore,
    AlphaOutcome,
    MarketSnapshot,
    append_alpha_log,
    etf_snapshot_cache_status,
    evaluate_alpha_log,
    load_cached_etf_snapshots,
    rank_event_reactions,
    save_etf_snapshot_cache,
    summarize_alpha_feedback,
)
from a_stock_committee.catalyst_monitor import NewsItem, scan_catalysts
from a_stock_committee.trend_calibration import KNOWN_TARGETS


class AlphaStackTests(unittest.TestCase):
    def test_ranks_confirmed_consumer_electronics_event(self):
        hits = scan_catalysts(
            [
                NewsItem(
                    title="摩根士丹利NVL72机柜BOM成本拆解",
                    summary="PCB、MLCC、CPO、铜连接、消费电子、AI服务器ODM价值量提升。",
                )
            ],
            threshold=7,
        )
        snapshots = [
            MarketSnapshot(
                target_id="consumer_electronics_hx_etf",
                name="消费电子ETF华夏",
                symbol="159732",
                pct_change=6.7,
                amount=640_000_000,
                open_price=1.49,
                high=1.572,
                low=1.49,
                latest=1.571,
                main_net_inflow=30_000_000,
                turnover=25,
            ),
            MarketSnapshot(
                target_id="chip_etf",
                name="芯片ETF",
                symbol="159995",
                pct_change=0.1,
                amount=990_000_000,
                open_price=2.49,
                high=2.52,
                low=2.44,
                latest=2.48,
                main_net_inflow=20_000_000,
                turnover=4,
            ),
        ]

        scores = rank_event_reactions(hits, snapshots, targets=KNOWN_TARGETS)
        score_map = {score.symbol: score for score in scores}

        self.assertGreater(score_map["159732"].total_score, score_map["159995"].total_score)
        self.assertIn(score_map["159732"].label, {"主线确认", "候选进攻", "高位拥挤，等分歧"})
        self.assertIn("不追高", score_map["159732"].playbook)

    def test_candidate_attack_gets_risk_aware_playbook(self):
        hits = scan_catalysts(
            [
                NewsItem(
                    title="摩根士丹利NVL72机柜BOM成本拆解",
                    summary="CPO、铜连接、AI服务器ODM价值量提升。",
                )
            ],
            threshold=7,
        )
        snapshots = [
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
        ]

        scores = rank_event_reactions(hits, snapshots, targets=KNOWN_TARGETS)
        score_map = {score.symbol: score for score in scores}

        self.assertIn("进攻候选", score_map["515880"].playbook)
        self.assertIn("控制单笔风险", score_map["515880"].playbook)

    def test_etf_snapshot_cache_round_trips_and_expires(self):
        snapshot = MarketSnapshot(
            target_id="communication_etf",
            name="通信ETF国泰",
            symbol="515880",
            pct_change=4.5,
            amount=2_200_000_000,
            open_price=1.4,
            high=1.5,
            low=1.39,
            latest=1.49,
            main_net_inflow=470_000_000,
            turnover=8,
            data_time="2026-05-22 14:59:00",
        )
        with tempfile.TemporaryDirectory() as tmp:
            missing = etf_snapshot_cache_status(cache_dir=tmp)
            self.assertFalse(missing["exists"])

            path = save_etf_snapshot_cache([snapshot], cache_dir=tmp)
            status = etf_snapshot_cache_status(cache_dir=tmp)
            loaded = load_cached_etf_snapshots(cache_dir=tmp, max_age_seconds=3600)

            self.assertTrue(status["exists"])
            self.assertEqual(status["count"], 1)
            self.assertGreaterEqual(status["age_seconds"], 0)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded[0].symbol, "515880")
            self.assertEqual(loaded[0].main_net_inflow, 470_000_000)

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["saved_at"] = "2000-01-01T00:00:00"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(load_cached_etf_snapshots(cache_dir=tmp, max_age_seconds=1))

    def test_append_alpha_log_writes_decision_ledger(self):
        hits = scan_catalysts(
            [NewsItem(title="摩根士丹利NVL72机柜BOM成本拆解", summary="CPO、铜连接、消费电子受益。")],
            threshold=7,
        )
        scores = [
            AlphaScore(
                target_id="communication_etf",
                name="通信ETF国泰",
                symbol="515880",
                total_score=0.86,
                catalyst_score=1.0,
                reaction_score=0.8,
                crowding_penalty=0.0,
                liquidity_score=1.0,
                label="候选进攻",
                reason="测试",
                pct_change=4.5,
                amount=2_200_000_000,
                latest=1.0,
                main_net_inflow=470_000_000,
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = append_alpha_log(scores, hits, event_text="NVL72 BOM", log_dir=tmp)
            append_alpha_log(scores, hits, event_text="NVL72   BOM", log_dir=tmp)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

            self.assertEqual(record["event_text"], "NVL72 BOM")
            self.assertIn("event_hash", record)
            self.assertEqual(record["scores"][0]["symbol"], "515880")
            self.assertEqual(record["hits"][0]["event"], "nvl72_bom")
            self.assertEqual(record["model_version"], "event_alpha_v0.3")

            current = MarketSnapshot(
                target_id="communication_etf",
                name="通信ETF国泰",
                symbol="515880",
                pct_change=3.0,
                amount=2_200_000_000,
                open_price=1.0,
                high=1.04,
                low=0.99,
                latest=1.03,
            )
            outcomes = evaluate_alpha_log([current], log_path=path, limit=1)
            self.assertEqual(outcomes[0].return_pct, 3.0)
            self.assertEqual(outcomes[0].outcome, "进攻有效")

    def test_summarize_alpha_feedback_flags_loose_attack_thresholds(self):
        outcomes = [
            AlphaOutcome(
                signal_id=str(index),
                created_at="2026-05-22T10:00:00",
                target_id="communication_etf",
                name="通信ETF国泰",
                symbol="515880",
                label="候选进攻",
                original_score=0.8,
                entry_price=1.0,
                current_price=0.98,
                return_pct=-2.0,
                original_pct_change=4.0,
                current_pct_change=-1.0,
                outcome="进攻失效",
            )
            for index in range(5)
        ]

        feedback = summarize_alpha_feedback(outcomes, min_samples=5)

        self.assertEqual(feedback[0].label, "候选进攻")
        self.assertEqual(feedback[0].verdict, "进攻偏松")
        self.assertIn("提高反应分阈值", feedback[0].suggested_adjustment)


if __name__ == "__main__":
    unittest.main()
