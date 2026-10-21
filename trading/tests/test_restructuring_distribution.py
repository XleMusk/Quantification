import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import advance, corporate_actions
from trading.feed import HistoricalFeed
from trading.ledger import Ledger, new_state
from trading.market_dataset import write_json
from trading.market_snapshot import documented_price_adjustments
from trading.rules import holder_bonus_ratio_bounds, holder_distribution_action, holder_restructuring_ex_price


class HolderDistributionTests(unittest.TestCase):
    def terms(self):
        return {"symbol": "000615.SZSE", "date": "2025-12-29", "registration_date": "2025-12-26",
                "kind": "restructuring_holder_distribution", "source_date": "2025-12-27",
                "source_url": "https://example.test/price.pdf", "source_file": "price.pdf",
                "holder_source_date": "2025-12-23", "holder_source_url": "https://example.test/holder.pdf",
                "holder_source_file": "holder.pdf", "shares_before": "762979719", "debt_shares": "86518140",
                "investor_shares": "861696863", "holder_shares": "51416829", "surrender_shares": "24881142",
                "debt_cash_equivalent": "545064282.00", "investor_cash": "1536099324.68",
                "guarantee_shares": "173578005", "guarantee_price": "6.30", "guarantee_debt": "1157161547.99",
                "guarantee_provision": "1043196055.12", "trust_asset_value": "10751404.40",
                "additional_guarantee_cash": "52868712.09", "cash_dividend": "0",
                "holder_bonus_ratio": "0.1", "eligible_share_base": "514168296",
                "holder_listing_date": "2025-12-29", "holder_fraction_policy": "csdc_tail_ranked",
                "holder_eligibility_rule": "exclude_named_parties_and_successors", "excluded_holders": ["fixture party"]}

    def fixture(self, root):
        for name in ("price.pdf", "holder.pdf"):
            (root / name).write_bytes(b"%PDF-1.7\nsynthetic fixture only\n")
        row = self.terms()
        write_json(root / "supplements/restructuring_distributions.json", {"events": [row]})
        evidence = documented_price_adjustments(root, [row["symbol"]], "2024-01-02", "2026-09-24")
        feed = HistoricalFeed.__new__(HistoricalFeed)
        feed.folder = root
        feed.calendar = ["2025-12-25", "2025-12-26", "2025-12-29", "2025-12-30"]
        feed.manifest = {"start": "2024-01-02", "end": "2026-09-24", "historical_price_adjustments": evidence}
        first = {"date": feed.calendar[0], "symbol": row["symbol"], "open": 4.18,
                 "high": 4.18, "low": 4.18, "close": 4.18, "preclose": 4.18,
                 "tradestatus": 1, "isST": 0, "adjustflag": "3", "volume": 10000}
        suspended = {**first, "date": feed.calendar[1], "tradestatus": 0}
        ex_day = {**first, "date": row["date"], "open": 3.53, "high": 3.53, "low": 3.53,
                  "close": 3.53, "preclose": 3.53}
        feed.raw = pd.DataFrame([first, suspended, ex_day, {**ex_day, "date": feed.calendar[3]}])
        feed.adjusted = feed.raw.assign(adjustflag="2")
        feed.instruments = {row["symbol"]: {}}
        feed.actions = [holder_distribution_action(evidence[0])]
        feed.rows = {r["date"]: {r["symbol"]: r} for r in feed.raw.to_dict("records")}
        feed.validate()
        return feed, evidence[0]

    def registered(self, feed, reference, quantity, context=None):
        state, config = new_state(140000), StrategyConfig()
        if quantity:
            state["positions"][reference["symbol"]] = [{"qty": quantity, "available": "2025-12-24"}]
        advance(state, feed, reference["registration_date"], config, session_number=1, context=context)
        return state, config

    def receipt(self, reference, quantity, **changes):
        return {"account_id": "standalone_replay", "mode": TradingMode.REPLAY.value,
                "registered_quantity": quantity, "reference_sha256": reference["source_sha256"],
                "holder_source_sha256": reference["holder_source_sha256"], "eligibility": "eligible",
                "source_kind": "explicit_replay_assumption", "source_ref": "synthetic test assumption", **changes}

    def attach(self, state, reference, receipt):
        state["corporate_action_reconciliations"] = {f"{reference['symbol']}:{reference['date']}": receipt}

    def test_geometry_is_not_the_ordinary_holder_bonus_ratio(self):
        from decimal import Decimal
        self.assertEqual(holder_restructuring_ex_price("4.18", self.terms()).quantize(Decimal(".01")), Decimal("3.53"))
        self.assertEqual(holder_restructuring_ex_price("2", self.terms()), Decimal("2"))
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            self.assertEqual(feed.actions[0]["dividReserveToStockPs"], "0.1")
            self.assertEqual(reference["ledger_policy"], "reconcile_registered_holder_distribution")

    def test_bad_formula_or_distribution_terms_are_rejected(self):
        for changes in ({"guarantee_debt": "nan"}, {"holder_shares": "0"}, {"cash_dividend": "1"},
                        {"guarantee_provision": "9999999999"}, {"surrender_shares": "1.1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                holder_restructuring_ex_price("4.18", {**self.terms(), **changes})
        with tempfile.TemporaryDirectory() as directory:
            _, reference = self.fixture(Path(directory))
            for changes in ({"holder_bonus_ratio": "1.34278"}, {"holder_fraction_policy": "floor"},
                            {"excluded_holders": []}, {"holder_listing_date": "2025-12-25"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    holder_distribution_action({**reference, **changes})

    def test_feed_rejects_unverified_terms_and_conflicting_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed, reference = self.fixture(root)
            for field, value in (("dividReserveToStockPs", "1.34278"), ("dividStocksPs", "1"),
                                 ("dividRegistDate", "2025-12-25")):
                saved = dict(feed.actions[0])
                feed.actions[0][field] = value
                with self.assertRaisesRegex(ValueError, "pinned reference"):
                    feed.validate()
                feed.actions[0] = saved
            feed.manifest["historical_price_adjustments"] = [{**reference, "ledger_policy": "assume_eligible"}]
            with self.assertRaisesRegex(ValueError, "unverified"):
                feed.validate()
            feed.manifest["historical_price_adjustments"] = [reference]
            (root / "holder.pdf").write_bytes(b"%PDF-1.7\nchanged fixture\n")
            with self.assertRaisesRegex(ValueError, "unverified"):
                feed.validate()

    def test_zero_registered_holder_advances_without_phantom_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            state, config = self.registered(feed, reference, 0)
            advance(state, feed, reference["date"], config, session_number=1)
            self.assertEqual(state["cash"], 14000000)
            self.assertEqual(state["positions"], {})

    def test_eligible_integer_allocation_uses_registered_not_current_quantity(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            state, config = self.registered(feed, reference, 100)
            state["positions"][reference["symbol"]] = []
            self.attach(state, reference, self.receipt(reference, 100))
            advance(state, feed, reference["date"], config, session_number=1)
            self.assertEqual(state["positions"][reference["symbol"]], [{"qty": 10, "available": reference["date"]}])
            self.assertEqual(state["cash"], 14000000)
            event = next(e for e in state["events"] if e["kind"] == "corporate_action")
            self.assertEqual(event["allocated_shares"], 10)
            advance(state, feed, "2025-12-30", config, session_number=1)
            self.assertEqual(Ledger(state, config).quantity(reference["symbol"]), 10)

    def test_excluded_holder_receives_no_shares(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            state, config = self.registered(feed, reference, 100)
            self.attach(state, reference, self.receipt(reference, 100, eligibility="excluded"))
            advance(state, feed, reference["date"], config, session_number=1)
            self.assertEqual(Ledger(state, config).quantity(reference["symbol"]), 100)

    def test_unknown_holder_and_missing_registration_fail_before_any_cash_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            feed.actions.append({"symbol": "600000.SSE", "dividOperateDate": reference["date"],
                                 "dividRegistDate": reference["registration_date"],
                                 "dividPayDate": reference["date"], "dividCashPsBeforeTax": "1"})
            for registered in (True, False):
                state, config = self.registered(feed, reference, 100) if registered else (new_state(140000), StrategyConfig())
                state["entitlements"][f"600000.SSE:{reference['date']}"] = 100
                before = Ledger(state, config).snapshot()
                with self.assertRaisesRegex(ValueError, "Unresolved holder"):
                    advance(state, feed, reference["date"], config, session_number=1)
                self.assertEqual(state, before)

    def test_fractional_allocation_cannot_be_guessed_from_replay_assumption(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            state, config = self.registered(feed, reference, 101)
            self.attach(state, reference, self.receipt(reference, 101, allocated_shares=10))
            before = Ledger(state, config).snapshot()
            with self.assertRaisesRegex(ValueError, "fractional holder allocation"):
                advance(state, feed, reference["date"], config, session_number=1)
            self.assertEqual(state, before)

    def test_broker_reconciled_fraction_allows_either_actual_tail_result(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            for allocation in (10, 11):
                state, config = self.registered(feed, reference, 101)
                self.attach(state, reference, self.receipt(reference, 101, source_kind="broker_account_record",
                                                          source_ref="synthetic broker receipt", allocated_shares=allocation))
                advance(state, feed, reference["date"], config, session_number=1)
                self.assertEqual(Ledger(state, config).quantity(reference["symbol"]), 101 + allocation)

    def test_wrong_account_terms_and_impossible_allocations_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            for changes in ({"account_id": "other"}, {"mode": TradingMode.PAPER.value}, {"registered_quantity": 200},
                            {"reference_sha256": "other"}, {"holder_source_sha256": "other"}, {"source_ref": ""},
                            {"eligibility": "unknown"}, {"allocated_shares": 134}, {"allocated_shares": True}):
                state, config = self.registered(feed, reference, 100)
                self.attach(state, reference, self.receipt(reference, 100, **changes))
                before = Ledger(state, config).snapshot()
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    advance(state, feed, reference["date"], config, session_number=1)
                self.assertEqual(state, before)

    def test_skipped_ex_session_cannot_lose_a_registered_distribution(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            state, config = self.registered(feed, reference, 100)
            self.attach(state, reference, self.receipt(reference, 100))
            before = Ledger(state, config).snapshot()
            with self.assertRaisesRegex(ValueError, "Skipped holder-distribution"):
                advance(state, feed, "2025-12-30", config, session_number=1)
            self.assertEqual(state, before)

    def test_paper_context_cannot_use_a_historical_eligibility_assumption(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            context = RunContext("paper", TradingMode.PAPER)
            state, config = self.registered(feed, reference, 100, context)
            self.attach(state, reference, self.receipt(reference, 100, account_id="paper", mode=context.mode.value))
            before = Ledger(state, config, context).snapshot()
            with self.assertRaisesRegex(ValueError, "account-specific receipt"):
                advance(state, feed, reference["date"], config, session_number=1, context=context)
            self.assertEqual(state, before)

    def test_reconciled_state_survives_json_recovery_and_does_not_recredit(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            state, config = self.registered(feed, reference, 100)
            self.attach(state, reference, self.receipt(reference, 100))
            restored = json.loads(json.dumps(state))
            advance(restored, feed, reference["date"], config, session_number=1)
            restored_again = json.loads(json.dumps(restored))
            advance(restored_again, feed, "2025-12-30", config, session_number=1)
            self.assertEqual(Ledger(restored_again, config).quantity(reference["symbol"]), 110)
            self.assertEqual(len([e for e in restored_again["events"] if e["kind"] == "corporate_action"]), 1)


class ApproximateHolderDistributionTests(unittest.TestCase):
    def terms(self):
        row = HolderDistributionTests().terms()
        row.update(symbol="603007.SSE", date="2024-12-26", registration_date="2024-12-25",
                   source_date="2024-12-25", holder_source_date="2024-12-14",
                   shares_before="406847052", debt_shares="49781729", investor_shares="377065323",
                   holder_shares="43201997", surrender_shares="0", debt_cash_equivalent="497817290",
                   investor_cash="409725470.08", funds_occupation_impairment_adjustment="90414975.24",
                   reference_threshold_price="2.12", holder_bonus_ratio="0.155346",
                   holder_bonus_ratio_kind="rounded_approximate", eligible_share_base="278102052",
                   holder_listing_date="2024-12-26", holder_fraction_policy="account_record_required",
                   holder_eligibility_rule="exclude_named_parties")
        for field in ("guarantee_shares", "guarantee_price", "guarantee_debt", "guarantee_provision",
                      "trust_asset_value", "additional_guarantee_cash"):
            row[field] = "0"
        return row

    def fixture(self, root):
        for name in ("price.pdf", "holder.pdf"):
            (root / name).write_bytes(b"%PDF-1.7\nsynthetic approximate-allocation fixture only\n")
        row = self.terms()
        write_json(root / "supplements/restructuring_distributions.json", {"events": [row]})
        evidence = documented_price_adjustments(root, [row["symbol"]], "2024-01-02", "2026-09-30")
        feed = HistoricalFeed.__new__(HistoricalFeed)
        feed.folder, feed.calendar = root, ["2024-12-24", "2024-12-25", "2024-12-26", "2024-12-27"]
        feed.manifest = {"start": "2024-01-02", "end": "2026-09-30", "historical_price_adjustments": evidence}
        first = {"date": feed.calendar[0], "symbol": row["symbol"], "open": 15.39,
                 "high": 15.39, "low": 15.39, "close": 15.39, "preclose": 15.39,
                 "tradestatus": 1, "isST": 0, "adjustflag": "3", "volume": 10000}
        ex_day = {**first, "date": row["date"], "open": 8.28, "high": 8.28, "low": 8.28,
                  "close": 8.28, "preclose": 8.28}
        feed.raw = pd.DataFrame([first, {**first, "date": feed.calendar[1], "tradestatus": 0},
                                 ex_day, {**ex_day, "date": feed.calendar[3]}])
        feed.adjusted = feed.raw.assign(adjustflag="2")
        feed.instruments = {row["symbol"]: {}}
        feed.actions = [holder_distribution_action(evidence[0])]
        feed.rows = {r["date"]: {r["symbol"]: r} for r in feed.raw.to_dict("records")}
        feed.validate()
        return feed, evidence[0]

    def registered(self, feed, reference, quantity, context=None):
        state, config = new_state(140000), StrategyConfig()
        if quantity:
            state["positions"][reference["symbol"]] = [{"qty": quantity, "available": "2024-12-24"}]
        advance(state, feed, reference["registration_date"], config, session_number=1, context=context)
        return state, config

    def attach(self, state, reference, quantity, **changes):
        receipt = HolderDistributionTests().receipt(reference, quantity, **changes)
        HolderDistributionTests().attach(state, reference, receipt)

    def test_primary_reference_keeps_impairment_separate_from_holder_cash(self):
        from decimal import Decimal
        row = self.terms()
        self.assertEqual(holder_restructuring_ex_price("15.39", row).quantize(Decimal(".01")), Decimal("8.28"))
        self.assertEqual(holder_restructuring_ex_price("2.12", row), Decimal("2.12"))
        self.assertNotEqual(holder_restructuring_ex_price("2.1201", row), Decimal("2.1201"))
        self.assertEqual(holder_bonus_ratio_bounds(row), (Decimal(".155345"), Decimal(".155347")))
        for changes in ({"funds_occupation_impairment_adjustment": "nan"},
                        {"funds_occupation_impairment_adjustment": "-1"}, {"reference_threshold_price": "inf"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                holder_restructuring_ex_price("15.39", {**row, **changes})

    def test_published_ratio_is_not_force_fitted_or_a_tail_allocation_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            self.assertEqual(feed.actions[0]["dividReserveToStockPs"], "0.155346")
            self.assertEqual(feed.actions[0]["holder_fraction_policy"], "account_record_required")
            for changes in ({"holder_bonus_ratio_kind": ""}, {"holder_bonus_ratio_kind": "guessed"},
                            {"holder_bonus_ratio": "1.155346"}, {"holder_bonus_ratio": "0.15"},
                            {"holder_fraction_policy": "csdc_tail_ranked"}, {"holder_shares": "50000000"},
                            {"eligible_share_base": "500000000"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    holder_distribution_action({**reference, **changes})
            feed.actions[0]["holder_fraction_policy"] = "csdc_tail_ranked"
            with self.assertRaisesRegex(ValueError, "pinned reference"):
                feed.validate()

    def test_no_holding_needs_no_receipt_and_never_receives_phantom_shares(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            state, config = self.registered(feed, reference, 0)
            advance(state, feed, reference["date"], config, session_number=1)
            self.assertEqual(Ledger(state, config).quantity(reference["symbol"]), 0)
            self.assertEqual(state["cash"], 14000000)

    def test_rounded_integer_product_still_requires_explicit_actual_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            for changes in ({"allocated_shares": 155346}, {"source_kind": "broker_account_record"},
                            {"source_kind": "broker_account_record", "allocated_shares": 155400},
                            {"source_kind": "broker_account_record", "allocated_shares": True}):
                state, config = self.registered(feed, reference, 1000000)
                self.attach(state, reference, 1000000, **changes)
                before = Ledger(state, config).snapshot()
                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "approximate holder allocation"):
                    advance(state, feed, reference["date"], config, session_number=1)
                self.assertEqual(state, before)

    def test_actual_record_bounds_credit_the_existing_ledger_once(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            for allocation in (155345, 155346, 155347):
                state, config = self.registered(feed, reference, 1000000)
                self.attach(state, reference, 1000000, source_kind="broker_account_record",
                            source_ref="synthetic account allocation fixture", allocated_shares=allocation)
                restored = json.loads(json.dumps(state))
                advance(restored, feed, reference["date"], config, session_number=1)
                self.assertEqual(Ledger(restored, config).quantity(reference["symbol"]), 1000000 + allocation)
                self.assertEqual(restored["cash"], 14000000)
                advance(restored, feed, "2024-12-27", config, session_number=1)
                self.assertEqual(len([e for e in restored["events"] if e["kind"] == "corporate_action"]), 1)

    def test_excluded_and_paper_accounts_cannot_use_replay_eligibility_assumptions(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, reference = self.fixture(Path(directory))
            for context in (None, RunContext("paper_fixture", TradingMode.PAPER)):
                state, config = self.registered(feed, reference, 100, context)
                extra = {"account_id": context.account_id, "mode": context.mode.value} if context else {}
                self.attach(state, reference, 100, eligibility="excluded", allocated_shares=0, **extra)
                before = Ledger(state, config, context).snapshot()
                with self.assertRaises(ValueError):
                    advance(state, feed, reference["date"], config, session_number=1, context=context)
                self.assertEqual(state, before)
                self.attach(state, reference, 100, eligibility="excluded", allocated_shares=0,
                            source_kind="broker_account_record", source_ref="synthetic excluded-holder record", **extra)
                advance(state, feed, reference["date"], config, session_number=1, context=context)
                self.assertEqual(Ledger(state, config, context).quantity(reference["symbol"]), 100)


if __name__ == "__main__":
    unittest.main()
