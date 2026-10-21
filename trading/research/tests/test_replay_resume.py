from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import pandas as pd

from trading.config import StrategyConfig
from trading.research.experiment import load_replay_checkpoint, replay
from trading.research.market_replay import resume_case_source, validate_resume_inputs
from trading.research.baseline_replay import resume_boundary


class ReplayResumeTests(unittest.TestCase):
    def feed(self, blocked=None):
        days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"]
        symbol = "600000.SSE"
        feed = SimpleNamespace(calendar=days, manifest={"replay_only": True}, actions=[],
            instruments={symbol: {"ipoDate": "1999-11-10", "outDate": ""}},
            volumes=pd.DataFrame({symbol: [1000000.] * len(days)}, index=days),
            rows={day: {symbol: {"open": 10. + i / 10, "high": 11., "low": 9., "close": 10. + i / 10,
                                "preclose": 10. + max(0, i - 1) / 10, "tradestatus": 1, "isST": 0, "adjustflag": "3"}}
                  for i, day in enumerate(days)},
            scores=lambda day, config: [{"symbol": symbol, "score": 1.}],
            next_day=lambda day: days[days.index(day) + 1] if day != days[-1] else "2024-01-11")
        def validate(state, day):
            if day == blocked:
                raise ValueError("deliberate source gap")
        feed.validate_session = validate
        return feed

    def test_saved_model_boundary_preserves_dates_prices_costs_and_seed_plan(self):
        config = StrategyConfig(initial_cash=140000).to_dict()
        manifest = {key: "same" for key in ("price_view", "raw_view", "event_view", "reference_view", "listing_master")}
        manifest.update(scope="full_source_sh_sz_research_only", source_codes=1, canonical_codes=1, unsupported_instruments={})
        saved = {"status": "failed_ledger_comparison", "dataset_manifest": manifest,
                 "data_quality": {"symbols": 1}, "portfolio_config": config, "config": {"seeds": [17, 29, 43]}}
        dates = self.feed().calendar
        before = resume_boundary(saved, dates)
        validate_resume_inputs(before, resume_boundary(saved, dates), dates[2])
        with self.assertRaisesRegex(ValueError, "Resume input changed"):
            validate_resume_inputs(before, resume_boundary({**saved, "dataset_manifest": {**manifest, "price_view": "changed"}}, dates), dates[2])
        for changed in ({**saved, "portfolio_config": {**config, "slippage_bps": 0}},
                        {**saved, "config": {"seeds": [17, 29, 44]}}):
            with self.assertRaisesRegex(ValueError, "same terminal"):
                validate_resume_inputs(before, resume_boundary(changed, dates), dates[2])
        with self.assertRaisesRegex(ValueError, "same terminal"):
            validate_resume_inputs(before, resume_boundary(saved, dates[1:]), dates[2])

    def test_cash_only_inception_does_not_infer_entitlements_for_existing_accounts(self):
        config = StrategyConfig(initial_cash=140000)
        feed = self.feed()
        past = {"kind": "restructuring_holder_distribution", "symbol": "600000.SSE",
                "date": "2023-12-29", "registration_date": "2023-12-28"}
        right = {**past, "kind": "rights_issue", "date": "2024-01-03"}
        feed.manifest.update(start="2023-01-01", historical_price_adjustments=[past, right])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = replay(feed, config, feed.calendar, root / "cash_only")
            self.assertEqual(result["sessions"], len(feed.calendar))
            state = json.loads((root / "cash_only/account.json").read_text(encoding="utf-8"))
            self.assertEqual(state["entitlements"]["600000.SSE:2023-12-29"], 0)
            self.assertEqual(state["entitlements"]["rights:600000.SSE:2024-01-03"], 0)
            receipt = json.loads((root / "cash_only/replay.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["opening_basis"], "fresh_cash_only_no_prior_holdings")
            self.assertEqual(feed.manifest["start"], "2023-01-01")

            from trading.ledger import new_state
            imported = new_state(config.initial_cash)
            checkpoint = {"state": imported, "daily": [], "config": config.to_dict(), "source": "existing"}
            with self.assertRaisesRegex(ValueError, "registered quantity=None"):
                replay(feed, config, feed.calendar, root / "unknown_existing", checkpoint=checkpoint)
            imported["entitlements"]["600000.SSE:2023-12-29"] = 100
            with self.assertRaisesRegex(ValueError, "Skipped holder-distribution"):
                replay(feed, config, feed.calendar, root / "held_existing", checkpoint=checkpoint)

            future = {**past, "registration_date": feed.calendar[1], "date": feed.calendar[2]}
            feed.manifest["historical_price_adjustments"] = [future]
            feed.actions = [{"symbol": future["symbol"], "dividRegistDate": future["registration_date"],
                             "dividOperateDate": future["date"], "action_kind": "restricted_restructuring"}]
            with self.assertRaisesRegex(ValueError, "Unresolved holder eligibility"):
                replay(feed, config, feed.calendar, root / "held_after_inception")

    def test_continuation_matches_uninterrupted_ledger_and_original_rebalance_phase(self):
        config = StrategyConfig(initial_cash=140000)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            full = self.feed()
            expected = replay(full, config, full.calendar, root / "full")
            gap = self.feed(full.calendar[3])
            with self.assertRaisesRegex(ValueError, "source gap"):
                replay(gap, config, gap.calendar, root / "prefix")
            checkpoint = load_replay_checkpoint(root / "prefix", config, full.calendar)
            actual = replay(full, config, full.calendar, root / "continued", checkpoint=checkpoint)
            self.assertEqual(actual["reused_sessions"], 3)
            self.assertEqual(actual["newly_completed_sessions"], 4)
            for key in ("sessions", "net_return", "max_drawdown", "fees", "turnover_value", "orders", "fills", "stale_valuation_days"):
                self.assertAlmostEqual(actual[key], expected[key])
            account = lambda name: json.loads((root / name / "account.json").read_text(encoding="utf-8"))
            self.assertEqual(account("continued"), account("full"))
            pd.testing.assert_frame_equal(pd.read_csv(root / "continued/daily.csv"), pd.read_csv(root / "full/daily.csv"))
            self.assertEqual([event["day"] for event in account("continued")["events"] if event["kind"] == "targets_generated"],
                             [full.calendar[0], full.calendar[5]])
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                load_replay_checkpoint(root / "prefix", replace(config, holdings=4), full.calendar)

    def test_only_uninvolved_formula_corrections_may_reuse_a_past_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            before, after = root / "before", root / "after"
            before.mkdir()
            after.mkdir()
            for directory in (before, after):
                (directory / "reviewed_special_references.json").write_text("[]", encoding="utf-8")
            reference = {"code": "sh.600000", "date": "2024-01-05", "close": 10., "preclose": 10.,
                         "tradestatus": 1, "previous_observed_close": 10.1, "reference_delta": -0.1,
                         "has_reported_distribution": True, "has_reviewed_special_reference": False,
                         "source_year_queried": True, "dividCashPsBeforeTax": "0.1",
                         "reference_formula_status": "reference_mismatch", "reference_formula_basis": "reported",
                         "expected_reference_rmb": "9.9", "reference_error_rmb": -0.1}
            corrected = {**reference, "reference_formula_status": "reference_match", "reference_formula_basis": "reviewed",
                         "expected_reference_rmb": "10.0", "reference_error_rmb": 0.}
            pd.DataFrame([reference]).to_parquet(before / "raw_reference_changes.parquet")
            pd.DataFrame([corrected]).to_parquet(after / "raw_reference_changes.parquet")
            inputs = {"event_view": "same", "reference_view": str(before), "price_view": "same", "raw_view": "same",
                      "listing_master": "same", "source_codes": 1, "canonical_codes": 1, "unsupported_instruments": {}}
            previous = {"status": "failed_full_source_diagnostic", "purpose": "full_source_rule_ledger_diagnostic",
                        "protocol": {"portfolio": StrategyConfig().to_dict(), "signals": ["momentum"]},
                        "scope": "full_source_sh_sz_research_only", "start": "2024-01-02", "end": "2024-01-10",
                        "sessions": 7, "symbols": 1, "board_counts": {"main": 1}, "inputs": inputs}
            current = {**previous, "inputs": {**inputs, "reference_view": str(after)}}
            empty = {"positions": {}, "orders": {}, "fills": {}, "events": [], "entitlements": {}, "receivables": {}}
            self.assertEqual(validate_resume_inputs(previous, current, "2024-01-08", empty), [("sh.600000", "2024-01-05")])
            involved = [None, {**empty, "positions": {"600000.SSE": {}}},
                        {**empty, "targets": {"600000.SSE": 0.1}},
                        {**empty, "pending_targets": {"600000.SSE": 0.1}},
                        {**empty, "orders": {"order": {"symbol": "600000.SSE"}}},
                        {**empty, "fills": {"fill": {"symbol": "600000.SSE"}}},
                        {**empty, "events": [{"targets": {"600000.SSE": 0.1}}]},
                        {**empty, "events": [{"symbol": "600000.SSE"}]},
                        {**empty, "entitlements": {"600000.SSE:2024-01-04": 100}},
                        {**empty, "receivables": {"rights:600000.SSE:2024-01-04": 100}}]
            for state in involved:
                with self.subTest(state=state), self.assertRaisesRegex(ValueError, "Past price-reference"):
                    validate_resume_inputs(previous, current, "2024-01-08", state)
            pd.DataFrame([{**corrected, "preclose": 10.01}]).to_parquet(after / "raw_reference_changes.parquet")
            with self.assertRaisesRegex(ValueError, "Past raw price"):
                validate_resume_inputs(previous, current, "2024-01-08", empty)
            pd.DataFrame([{**corrected, "dividCashPsBeforeTax": "0.2"}]).to_parquet(after / "raw_reference_changes.parquet")
            with self.assertRaisesRegex(ValueError, "Past price-reference"):
                validate_resume_inputs(previous, current, "2024-01-08", empty)

    def test_case_source_follows_a_premutation_failure_but_not_a_broken_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            protocol = {"portfolio": StrategyConfig().to_dict(), "signals": ["momentum"]}
            inputs = {key: "same" for key in ("price_view", "raw_view", "listing_master", "source_codes", "canonical_codes", "unsupported_instruments")}
            source = {"status": "completed_full_source_diagnostic", "purpose": "full_source_rule_ledger_diagnostic",
                      "protocol": protocol, "scope": "same", "start": "2024-01-02", "end": "2024-01-10",
                      "sessions": 7, "symbols": 1, "board_counts": {"main": 1}, "inputs": inputs,
                      "artifacts": str(root / "old_artifacts"), "results": [{"signal": "momentum", "status": "completed"}]}
            old = root / "old_artifacts/momentum"
            old.mkdir(parents=True)
            (root / "run.json").write_text(json.dumps(source), encoding="utf-8")
            failed = {**source, "status": "failed_full_source_diagnostic", "artifacts": str(root / "new_artifacts"),
                      "resume_run": str(root), "results": [{"signal": "momentum", "status": "failed"}]}
            prior, path = resume_case_source(failed, source, "momentum")
            self.assertEqual(prior, source)
            self.assertEqual(path, old)
            broken = root / "new_artifacts/momentum"
            broken.mkdir(parents=True)
            self.assertEqual(resume_case_source(failed, source, "momentum")[1], broken)
            with self.assertRaises((ValueError, FileNotFoundError)):
                load_replay_checkpoint(broken, StrategyConfig(), self.feed().calendar)
            broken.rmdir()
            bad = {**source, "protocol": {**protocol, "portfolio": {**protocol["portfolio"], "holdings": 4}}}
            (root / "run.json").write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "same terminal"):
                resume_case_source(failed, source, "momentum")
            (root / "run.json").write_text(json.dumps(failed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Cyclic"):
                resume_case_source(failed, source, "momentum")

    def test_only_future_action_terms_may_change_during_continuation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            before, after = root / "before", root / "after"
            before.mkdir()
            after.mkdir()
            action = {"symbol": "600000.SSE", "code": "sh.600000", "dividOperateDate": "2024-01-05",
                      "dividRegistDate": "2024-01-04", "dividPayDate": "2024-01-05", "dividStockMarketDate": "",
                      "dividCashPsBeforeTax": "0.1", "dividStocksPs": "0", "dividReserveToStockPs": ""}
            pd.DataFrame([action]).to_parquet(before / "canonical_actions.parquet")
            pd.DataFrame([{**action, "dividReserveToStockPs": "0"}]).to_parquet(after / "canonical_actions.parquet")
            inputs = {"event_view": str(before), "reference_view": "unchanged", "price_view": "same",
                      "raw_view": "same", "listing_master": "same", "source_codes": 1,
                      "canonical_codes": 1, "unsupported_instruments": {}}
            previous = {"status": "failed_full_source_diagnostic", "purpose": "full_source_rule_ledger_diagnostic",
                        "protocol": {"portfolio": StrategyConfig().to_dict(), "signals": ["momentum"]},
                        "scope": "full_source_sh_sz_research_only", "start": "2024-01-02", "end": "2024-01-10",
                        "sessions": 7, "symbols": 1, "board_counts": {"main": 1}, "inputs": inputs}
            current = {**previous, "inputs": {**inputs, "event_view": str(after)}}
            validate_resume_inputs(previous, current, "2024-01-04")
            with self.assertRaisesRegex(ValueError, "Past economic action"):
                validate_resume_inputs(previous, current, "2024-01-08")
            state = {"positions": {}, "orders": {}, "fills": {}, "events": [], "entitlements": {}, "receivables": {}}
            self.assertEqual(validate_resume_inputs(previous, current, "2024-01-08", state), [("sh.600000", "2024-01-05")])
            state["events"] = [{"kind": "targets_generated", "targets": {"600000.SSE": 0.1}}]
            with self.assertRaisesRegex(ValueError, "Past economic action"):
                validate_resume_inputs(previous, current, "2024-01-08", state)
            pd.DataFrame([{**action, "dividRegistDate": "2024-01-03", "dividReserveToStockPs": "0"}]).to_parquet(after / "canonical_actions.parquet")
            with self.assertRaisesRegex(ValueError, "Past entitlement"):
                validate_resume_inputs(previous, current, "2024-01-04")


if __name__ == "__main__":
    unittest.main()
