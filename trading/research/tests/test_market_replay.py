import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading.market_snapshot import FIXED_SCOPE, INGESTION_SCOPE
from trading.research import market_replay


class MarketReplayTests(unittest.TestCase):
    def feed(self, root):
        return SimpleNamespace(folder=root, symbols=["600000.SSE"], calendar=["2024-01-02"],
            manifest={"scope": FIXED_SCOPE, "start": "2024-01-02", "end": "2024-01-02",
                      "selection": {"source_files_complete": True, "snapshot_validated": True,
                                    "expected_symbols": 1, "complete_symbols": 1, "missing": []}})

    def test_fixed_comparison_labels_and_identical_portfolio_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = self.feed(root)
            calls = []
            def fake_replay(supplied_feed, config, dates, destination):
                self.assertIs(supplied_feed, feed)
                self.assertEqual(dates, ["2024-01-02"])
                calls.append(config.to_dict())
                return {"net_return": 0., "max_drawdown": 0., "stale_valuation_days": 0}
            with patch.object(market_replay, "ROOT", root), patch.object(market_replay, "DATA_ROOT", root / "data"), \
                    patch.object(market_replay, "HistoricalFeed", return_value=feed), \
                    patch.object(market_replay, "replay", side_effect=fake_replay):
                out = market_replay.run(root)
            record = json.loads((out / "run.json").read_text())
            self.assertEqual(record["status"], "completed_fixed_rule_comparison")
            self.assertTrue(record["protocol"]["selection_is_point_in_time"])
            self.assertFalse(record["protocol"]["fresh_holdout_claimed"])
            self.assertFalse(record["strategy_validated"])
            self.assertEqual(len(calls), 3)
            self.assertEqual({c.pop("signal") for c in calls}, {"risk_adjusted_momentum", "momentum", "equal_weight"})
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(calls[1], calls[2])

    def test_fixed_incomplete_snapshot_stops_before_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = self.feed(root)
            feed.manifest["selection"]["expected_symbols"] = 2
            with patch.object(market_replay, "HistoricalFeed", return_value=feed), \
                    patch.object(market_replay, "replay") as replay:
                with self.assertRaisesRegex(ValueError, "every expected member"):
                    market_replay.run(root)
                replay.assert_not_called()

    def test_case_restart_requires_bulk_continuation_and_does_not_change_fixed_signals(self):
        for kwargs in ({"restart_cases": ["momentum"]},
                       {"restart_cases": ["momentum"], "price_view": "same"},
                       {"restart_cases": ["new_signal"], "price_view": "same", "resume_run": "same"}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "Case restart"):
                market_replay.run(**kwargs)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / "run.json").write_text(json.dumps({"status": "completed_full_source_diagnostic"}), encoding="utf-8")
            with patch.object(market_replay, "run", return_value=out) as run:
                self.assertEqual(market_replay.main(["--price-view", "price", "--resume-run", "prior", "--restart-case", "momentum"]), 0)
            self.assertEqual(run.call_args.kwargs["restart_cases"], ["momentum"])

    def test_only_existing_signal_does_not_repeat_other_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = self.feed(root)
            with patch.object(market_replay, "ROOT", root), patch.object(market_replay, "DATA_ROOT", root / "data"), \
                    patch.object(market_replay, "HistoricalFeed", return_value=feed), \
                    patch.object(market_replay, "replay", return_value={
                        "net_return": 0., "max_drawdown": 0., "stale_valuation_days": 0}) as replay:
                out = market_replay.run(root, only_signals=["equal_weight"])
            self.assertEqual(replay.call_count, 1)
            record = json.loads((out / "run.json").read_text())
            self.assertEqual(record["protocol"]["signals"], ["equal_weight"])
            self.assertEqual([row["signal"] for row in record["results"]], ["equal_weight"])
        for selected in (["new_signal"], ["momentum", "momentum"]):
            with self.assertRaisesRegex(ValueError, "distinct existing"):
                market_replay.run(only_signals=selected)

    def test_partial_scope_remains_regression_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = self.feed(root)
            feed.manifest["scope"] = "acquired_mainboard_ingestion_regression_not_strategy_validation"
            with patch.object(market_replay, "ROOT", root), patch.object(market_replay, "DATA_ROOT", root / "data"), \
                    patch.object(market_replay, "HistoricalFeed", return_value=feed), \
                    patch.object(market_replay, "replay", side_effect=ValueError("unresolved holding")):
                out = market_replay.run(root)
            record = json.loads((out / "run.json").read_text())
            self.assertEqual(record["status"], "failed_ingestion_regression")
            self.assertFalse(record["protocol"]["selection_is_point_in_time"])
            self.assertEqual(len(record["results"]), 3)
            self.assertTrue(all(r["error"] == "ValueError: unresolved holding" for r in record["results"]))

    def test_requested_window_and_cash_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = self.feed(root)
            feed.calendar = ["2024-01-02", "2024-01-03", "2024-01-04"]
            feed.manifest["end"] = "2024-01-04"
            def fake_replay(_feed, config, dates, _destination):
                self.assertEqual(dates, ["2024-01-03", "2024-01-04"])
                self.assertEqual(config.initial_cash, 140000)
                return {"net_return": 0., "max_drawdown": 0., "stale_valuation_days": 0}
            with patch.object(market_replay, "ROOT", root), patch.object(market_replay, "DATA_ROOT", root / "data"), \
                    patch.object(market_replay, "HistoricalFeed", return_value=feed), \
                    patch.object(market_replay, "replay", side_effect=fake_replay):
                out = market_replay.run(root, "2024-01-03", "2024-01-04", 140000)
            record = json.loads((out / "run.json").read_text())
            self.assertEqual((record["start"], record["end"], record["sessions"]),
                             ("2024-01-03", "2024-01-04", 2))
            with patch.object(market_replay, "HistoricalFeed", return_value=feed):
                with self.assertRaisesRegex(ValueError, "exchange sessions"):
                    market_replay.run(root, "2024-01-01", "2024-01-04", 140000)


    def test_multiboard_ingestion_cannot_be_promoted_to_strategy_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = self.feed(root)
            feed.symbols = ["600000.SSE", "300001.SZSE", "688001.SSE"]
            feed.manifest["scope"] = INGESTION_SCOPE
            with patch.object(market_replay, "ROOT", root), patch.object(market_replay, "DATA_ROOT", root / "data"), \
                    patch.object(market_replay, "HistoricalFeed", return_value=feed), \
                    patch.object(market_replay, "replay", return_value={
                        "net_return": 0., "max_drawdown": 0., "stale_valuation_days": 0}):
                out = market_replay.run(root)
            record = json.loads((out / "run.json").read_text())
            self.assertEqual(record["status"], "completed_ingestion_regression")
            self.assertEqual(record["board_counts"], {"main": 1, "chinext": 1, "star": 1})
            self.assertFalse(record["strategy_validated"])
            self.assertFalse(record["live_ready"])
            self.assertFalse(record["protocol"]["selection_is_point_in_time"])
            self.assertFalse(record["protocol"]["tuning"])
            self.assertIn("Donchian is not part of this run.", (out / "REPORT.md").read_text())


if __name__ == "__main__":
    unittest.main()


def test_flat_start_zero_completion_requires_strictly_later_first_use(tmp_path):
    import pandas as pd
    import pytest
    symbol, day = "603065.SSE", "2026-06-11"
    before_root, after_root = tmp_path / "before", tmp_path / "after"
    before_root.mkdir()
    after_root.mkdir()
    action = {"symbol": symbol, "dividOperateDate": day, "code": "sh.603065",
              "dividRegistDate": "2026-06-10", "dividPayDate": day, "dividStockMarketDate": "",
              "dividCashPsBeforeTax": "0.2", "dividStocksPs": "0.000000", "dividReserveToStockPs": ""}
    pd.DataFrame([action]).to_parquet(before_root / "canonical_actions.parquet")
    corrected = {**action, "dividReserveToStockPs": "0"}
    pd.DataFrame([corrected]).to_parquet(after_root / "canonical_actions.parquet")
    before = {key: "same" for key in ("price_view", "raw_view", "listing_master", "source_codes",
              "canonical_codes", "unsupported_instruments", "reference_view")}
    before["event_view"] = str(before_root)
    after = {**before, "event_view": str(after_root)}
    state = {"positions": {}, "targets": {}, "pending_targets": {}, "orders": {}, "fills": {},
             "entitlements": {}, "receivables": {}, "events": [
                 {"day": "2026-06-12", "targets": {symbol: 0.16}}]}
    with pytest.raises(ValueError, match="Past economic action"):
        market_replay.validate_prefix_inputs(before, after, "2026-09-24", state)
    assert market_replay.validate_prefix_inputs(before, after, "2026-09-24", state,
        flat_start_first_use={symbol: "2026-06-12"}) == [("sh.603065", day)]
    for first in ("2026-06-10", day, ""):
        with pytest.raises(ValueError, match="Past economic action"):
            market_replay.validate_prefix_inputs(before, after, "2026-09-24", state,
                                                 flat_start_first_use={symbol: first})
    pd.DataFrame([{**corrected, "dividCashPsBeforeTax": "0.19"}]).to_parquet(after_root / "canonical_actions.parquet")
    with pytest.raises(ValueError, match="Past economic action"):
        market_replay.validate_prefix_inputs(before, after, "2026-09-24", state,
                                             flat_start_first_use={symbol: "2026-06-12"})


def test_replay_failures_are_structured_without_relaxing_the_gate():
    blocker = market_replay.classify_replay_error(
        "ValueError: Unresolved held corporate action: 603121.SSE 2025-06-27; "
        "missing=dividReserveToStockPs")
    assert blocker == {
        "kind": "held_corporate_action",
        "code": "missing_corporate_action_terms",
        "message": "ValueError: Unresolved held corporate action: 603121.SSE 2025-06-27; "
                    "missing=dividReserveToStockPs",
        "symbol": "603121.SSE",
        "decision_date": "2025-06-27",
        "missing_fields": ["dividReserveToStockPs"],
        "requires_source_evidence": True,
        "resume_policy": "requires_reviewed_source_evidence_and_replay",
    }
    reference = market_replay.classify_replay_error(
        "ValueError: Unresolved held/execution price reference: 605488.SSE "
        "2025-05-30; reference_mismatch")
    assert reference["code"] == "unresolved_price_reference"
    assert reference["requires_source_evidence"] is True
