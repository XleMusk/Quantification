import json
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from trading.feed import HistoricalFeed
from trading.config import StrategyConfig
from trading.engine import advance, corporate_actions
from trading.ledger import Ledger, new_state
from trading.rules import restructuring_ex_price
from trading.market_dataset import BAOSTOCK_FIELDS, write_frame, write_json
from trading.market_snapshot import (FIXED_SCOPE, build, fixed_coverage, fixed_membership, load_action_resolutions,
                                     documented_price_adjustments, prestart_rights_adjustments, reconcile_provider_actions,
                                     supplement_actions, validate_pair)


class MarketSnapshotTests(unittest.TestCase):
    def item(self):
        return {"symbol": "600000.SSE", "code": "sh.600000", "board": "main",
                "ipoDate": "2000-01-01", "outDate": "", "status": "1"}

    def bar(self, flag):
        row = dict.fromkeys(BAOSTOCK_FIELDS.split(","), "0")
        row.update(symbol="600000.SSE", code="sh.600000", date="2024-01-02",
                   open="10", high="11", low="9", close="10", preclose="10",
                   volume="1000", amount="10000", tradestatus="1", isST="0", adjustflag=flag)
        return pd.DataFrame([row])

    def test_boundary_not_imputed_but_in_listing_gap_is_error(self):
        item = {**self.item(), "outDate": "2024-01-03"}
        days = ["2024-01-02", "2024-01-03"]
        self.assertEqual(validate_pair(self.bar("3"), self.bar("2"), item, days), [])
        self.assertTrue(validate_pair(self.bar("3"), self.bar("2"), self.item(), days))

    def test_raw_amount_units_and_pair_alignment(self):
        raw, adjusted = self.bar("3"), self.bar("2")
        raw["amount"] = "1000000"
        adjusted["date"] = "2024-01-03"
        problems = validate_pair(raw, adjusted, self.item(), ["2024-01-02", "2024-01-03"])
        self.assertTrue(any("date-symbol mismatch" in p for p in problems))
        self.assertTrue(any("outside OHLC" in p for p in problems))

    def fixture(self, root):
        write_json(root / "collection_plan.json", {"source": "fixture_only", "start": "2024-01-01",
            "end": "2024-01-02", "last_calendar_session": "2024-01-02", "limitations": []})
        write_frame(root / "universe.csv", pd.DataFrame([self.item(), {
            **self.item(), "symbol": "688001.SSE", "code": "sh.688001", "board": "star"}]))
        write_frame(root / "calendar.csv", pd.DataFrame({"date": ["2024-01-02"]}))
        write_frame(root / "raw/600000.SSE.parquet", self.bar("3"))
        write_frame(root / "adjusted/600000.SSE.parquet", self.bar("2"))
        write_frame(root / "actions/600000.SSE/2024.csv", pd.DataFrame(columns=["symbol", "dividOperateDate"]))
        tasks = {kind: {"path": f"{kind}/600000.SSE.parquet"} for kind in ("raw", "adjusted")}
        tasks["actions_2024"] = {"path": "actions/600000.SSE/2024.csv"}
        write_json(root / "receipts/600000.SSE.json", {"symbol": "600000.SSE", "tasks": tasks})

    def test_snapshot_is_separate_and_pins_only_supported_acquired_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            path = build(root, Path(directory) / "snapshot")
            feed = HistoricalFeed(path)
            self.assertEqual(feed.symbols, ["600000.SSE"])
            self.assertFalse(feed.manifest["selection_is_point_in_time"])
            self.assertFalse(feed.manifest["live_ready"])
            self.assertFalse((root / "manifest.json").exists())
            with self.assertRaises(FileExistsError):
                build(root, path)

    def test_nonfixed_snapshot_includes_acquired_star_and_chinext_without_scope_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            items = [self.item(), {**self.item(), "symbol": "688001.SSE", "code": "sh.688001", "board": "star"},
                     {**self.item(), "symbol": "300001.SZSE", "code": "sz.300001", "board": "chinext"}]
            write_frame(root / "universe.csv", pd.DataFrame(items))
            for item in items[1:]:
                symbol = item["symbol"]
                for kind, flag in (("raw", "3"), ("adjusted", "2")):
                    write_frame(root / f"{kind}/{symbol}.parquet",
                                self.bar(flag).assign(symbol=symbol, code=item["code"]))
                write_frame(root / f"actions/{symbol}/2024.csv", pd.DataFrame(columns=["symbol", "dividOperateDate"]))
                tasks = {kind: {"path": f"{kind}/{symbol}.parquet"} for kind in ("raw", "adjusted")}
                tasks["actions_2024"] = {"path": f"actions/{symbol}/2024.csv"}
                write_json(root / f"receipts/{symbol}.json", {"symbol": symbol, "tasks": tasks})
            feed = HistoricalFeed(build(root, Path(directory) / "snapshot"))
            self.assertEqual(set(feed.symbols), {item["symbol"] for item in items})
            self.assertEqual(feed.manifest["acquired_board_counts"], {"main": 1, "star": 1, "chinext": 1})
            self.assertFalse(feed.manifest["selection_is_point_in_time"])
            self.assertFalse(feed.manifest["strategy_validated"])
            self.assertFalse(feed.manifest["live_ready"])
            feed.instruments["688001.SSE"]["board"] = "main"
            with self.assertRaisesRegex(ValueError, "Board/code metadata conflict"):
                feed.validate()

    def test_failed_validation_never_drops_bad_stock_or_claims_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            destination = Path(directory) / "bad"
            with patch("trading.market_snapshot.HistoricalFeed", side_effect=ValueError("unexplained action")):
                with self.assertRaisesRegex(ValueError, "unexplained action"):
                    build(root, destination)
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["symbols"], ["600000.SSE"])
            self.assertTrue((destination / "raw.parquet").exists())

    def test_feed_rejects_duplicate_dividend_and_wrong_adjustment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            path = build(root, Path(directory) / "snapshot")
            feed = HistoricalFeed(path)
            action = {"symbol": "600000.SSE", "dividOperateDate": "2024-01-02"}
            feed.actions = [action, dict(action)]
            with self.assertRaisesRegex(ValueError, "Duplicate corporate-action"):
                feed.validate()
            feed.actions = []
            feed.adjusted["adjustflag"] = "3"
            with self.assertRaisesRegex(ValueError, "forward-adjusted"):
                feed.validate()

    def test_duplicate_provider_descriptions_keep_one_economic_event_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            first = {"code": "sh.600000", "symbol": "600000.SSE", "dividRegistDate": "2024-01-01",
                     "dividOperateDate": "2024-01-02", "dividPayDate": "2024-01-02",
                     "dividStockMarketDate": "", "dividCashPsBeforeTax": "0.30",
                     "dividStocksPs": "0", "dividReserveToStockPs": "0", "dividPlanDate": ""}
            second = {**first, "dividCashPsBeforeTax": "0.3", "dividPlanDate": "2023-12-20"}
            source = root / "actions/600000.SSE/2024.csv"
            write_frame(source, pd.DataFrame([first, second]))
            original = source.read_bytes()
            path = build(root, Path(directory) / "snapshot")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(len(HistoricalFeed(path).actions), 1)
            audit = json.loads((path / "provider_action_duplicates.json").read_text())
            self.assertEqual(len(audit), 1)
            self.assertEqual(len(audit[0]["source_rows"]), 2)

    def test_conflicting_provider_action_amounts_stop_snapshot(self):
        first = {"code": "sh.600000", "symbol": "600000.SSE", "dividRegistDate": "2024-01-01",
                 "dividOperateDate": "2024-01-02", "dividPayDate": "2024-01-02",
                 "dividStockMarketDate": "", "dividCashPsBeforeTax": "0.3",
                 "dividStocksPs": "0", "dividReserveToStockPs": "0", "_cache_file": "first.csv"}
        with self.assertRaisesRegex(ValueError, "Conflicting provider action amounts"):
            reconcile_provider_actions(pd.DataFrame([first, {**first, "dividCashPsBeforeTax": "0.4"}]))

    def test_reviewed_cash_correction_pins_pdf_and_preserves_both_source_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            first = {"code": "sh.600000", "symbol": "600000.SSE", "dividRegistDate": "2024-01-01",
                     "dividOperateDate": "2024-01-02", "dividPayDate": "2024-01-02",
                     "dividStockMarketDate": "", "dividCashPsBeforeTax": "0.3",
                     "dividStocksPs": "0", "dividReserveToStockPs": "0"}
            frame = pd.DataFrame([first, {**first, "dividCashPsBeforeTax": "0.4"}])
            source = root / "actions/600000.SSE/2024.csv"
            write_frame(source, frame)
            original = source.read_bytes()
            pdf = root / "supplements/correction.pdf"
            pdf.parent.mkdir()
            pdf.write_bytes(b"%PDF-reviewed correction fixture")
            resolution = {**{key: first[key] for key in ("symbol", "code", "dividRegistDate",
                          "dividOperateDate", "dividPayDate", "dividCashPsBeforeTax")},
                          "kind": "issuer_cash_correction", "expected_provider_cash_amounts": ["0.3", "0.4"],
                          "source_date": "2023-12-20", "source_url": "https://example.org/issuer.pdf",
                          "source_file": "supplements/correction.pdf", "source_sha256": sha256(pdf.read_bytes()).hexdigest()}
            write_json(root / "supplements/provider_action_resolutions.json", [resolution])
            path = build(root, Path(directory) / "snapshot")
            self.assertEqual(source.read_bytes(), original)
            audit = json.loads((path / "provider_action_duplicates.json").read_text())
            self.assertEqual(audit[0]["status"], "issuer_corrected_cash_event")
            self.assertEqual(len(audit[0]["source_rows"]), 2)
            self.assertEqual((path / "supplements/correction.pdf").read_bytes(), pdf.read_bytes())
            self.assertEqual(pd.read_csv(path / "actions.csv").dividCashPsBeforeTax.tolist(), [.3])
            frame["_cache_file"] = "source.csv"
            frame.loc[1, "dividCashPsBeforeTax"] = "0.5"
            with self.assertRaisesRegex(ValueError, "Conflicting provider action amounts"):
                reconcile_provider_actions(frame, [resolution])
            with self.assertRaisesRegex(ValueError, "no longer matches"):
                reconcile_provider_actions(frame.iloc[:1], [resolution])
            pdf.write_bytes(b"%PDF-tampered")
            with self.assertRaisesRegex(ValueError, "hash/PDF changed"):
                load_action_resolutions(root, ["600000.SSE"])

    def combined_cash_fixture(self, root, amounts=("0.286", "0.091")):
        first = {"code": "sh.601966", "symbol": "601966.SSE", "dividRegistDate": "2024-06-13",
                 "dividOperateDate": "2024-06-14", "dividPayDate": "2024-06-14",
                 "dividStockMarketDate": "", "dividStocksPs": "0.000000", "dividReserveToStockPs": ""}
        frame = pd.DataFrame([{**first, "dividCashPsBeforeTax": value, "_cache_file": f"source-{i}.csv"}
                              for i, value in enumerate(amounts)])
        pdf = root / "supplements/combined.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"%PDF-test-only-combined-cash")
        resolution = {**{key: first[key] for key in ("symbol", "code", "dividRegistDate", "dividOperateDate", "dividPayDate")},
                      "kind": "issuer_combined_cash_event", "expected_provider_cash_amounts": list(amounts),
                      "dividCashPsBeforeTax": str(sum(map(Decimal, amounts))),
                      "dividStocksPs": "0", "dividReserveToStockPs": "0", "source_date": "2024-06-07",
                      "source_url": "https://example.test/combined.pdf", "source_file": "supplements/combined.pdf",
                      "source_sha256": sha256(pdf.read_bytes()).hexdigest()}
        write_json(root / "supplements/provider_action_resolutions.json", [resolution])
        return frame, resolution

    def test_combined_cash_requires_exact_issuer_components_and_retains_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, resolution = self.combined_cash_fixture(root)
            original = frame.copy(deep=True)
            reviewed = load_action_resolutions(root, ["601966.SSE"])
            canonical, audit = reconcile_provider_actions(frame, reviewed)
            self.assertTrue(frame.equals(original))
            self.assertEqual(canonical.dividCashPsBeforeTax.tolist(), ["0.377"])
            self.assertEqual(canonical.dividReserveToStockPs.tolist(), ["0"])
            self.assertEqual(audit[0]["status"], "issuer_combined_cash_event")
            self.assertEqual(audit[0]["source_rows"], original.to_dict("records"))
            with self.assertRaisesRegex(ValueError, "Conflicting provider action amounts"):
                reconcile_provider_actions(frame)
            with self.assertRaisesRegex(ValueError, "components changed"):
                reconcile_provider_actions(pd.concat([frame, frame.iloc[:1]], ignore_index=True), reviewed)
            with self.assertRaisesRegex(ValueError, "components changed"):
                reconcile_provider_actions(frame, [{**resolution, "dividCashPsBeforeTax": "0.37444"}])
            for changes in ({"dividPayDate": "2024-06-17"}, {"dividStocksPs": "0.1"}):
                with self.assertRaisesRegex(ValueError, "Conflicting provider action"):
                    reconcile_provider_actions(frame.assign(**changes), reviewed)

    def test_equal_combined_cash_components_are_not_ordinary_duplicate_descriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = self.combined_cash_fixture(root, ("0.3", "0.3"))
            ordinary, _ = reconcile_provider_actions(frame)
            self.assertEqual(ordinary.dividCashPsBeforeTax.tolist(), ["0.3"])
            combined, audit = reconcile_provider_actions(frame, load_action_resolutions(root, ["601966.SSE"]))
            self.assertEqual(combined.dividCashPsBeforeTax.tolist(), ["0.6"])
            self.assertEqual(audit[0]["status"], "issuer_combined_cash_event")

    def test_combined_cash_loader_rejects_bad_sum_dates_terms_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, resolution = self.combined_cash_fixture(root)
            for changes in ({"dividCashPsBeforeTax": "0.286"}, {"source_date": "2024-06-15"},
                            {"dividReserveToStockPs": ""}, {"source_sha256": "0" * 64}):
                write_json(root / "supplements/provider_action_resolutions.json", [{**resolution, **changes}])
                with self.assertRaises(ValueError):
                    load_action_resolutions(root, ["601966.SSE"])

    def test_combined_cash_uses_sole_engine_ledger_once_not_virtual_reference_amount(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = self.combined_cash_fixture(root)
            canonical, _ = reconcile_provider_actions(frame, load_action_resolutions(root, ["601966.SSE"]))
            action = canonical.iloc[0].to_dict()
            action["reference_adjustment_cash_ps"] = "0.37444"
            symbol = action["symbol"]
            days = ["2024-06-13", "2024-06-14"]
            quote = {"open": 18.31, "close": 18.31, "preclose": 18.31, "tradestatus": 1, "isST": 0}
            feed = SimpleNamespace(calendar=days, actions=[action], manifest={"replay_only": True},
                                   instruments={symbol: {"ipoDate": "2016-07-06", "outDate": ""}},
                                   rows={day: {symbol: quote} for day in days})
            state = new_state(0)
            state["positions"][symbol] = [{"qty": 1000, "available": "2024-01-02"}]
            config = StrategyConfig()
            advance(state, feed, days[0], config, session_number=1)
            result = advance(state, feed, days[1], config, session_number=2)
            self.assertEqual(result["cash"], 301.6)  # Gross377, existing20% reserve; not virtual374.44.
            self.assertEqual(Ledger(state, config).quantity(symbol), 1000)
            self.assertEqual(len([row for row in state["events"] if row["kind"] == "dividend_paid"]), 1)
            resumed = json.loads(json.dumps(state))
            with self.assertRaisesRegex(ValueError, "processed twice"):
                advance(resumed, feed, days[1], config, session_number=2)
            self.assertEqual(resumed, state)

    def reserve_correction_fixture(self, root):
        source = root / "supplements/reserve_fixture.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-fixture only")
        original = {"symbol": "001323.SZSE", "code": "sz.001323", "dividRegistDate": "2025-08-20",
                    "dividOperateDate": "2025-08-21", "dividPayDate": "", "dividStockMarketDate": "2025-08-21",
                    "dividCashPsBeforeTax": "1", "dividCashPsAfterTax": "", "dividStocksPs": "0.000000",
                    "dividReserveToStockPs": "", "dividCashStock": "10-transfer-1", "_cache_file": "original.csv"}
        expected = {key: value for key, value in original.items()
                    if key not in {"symbol", "dividOperateDate", "_cache_file"}}
        resolution = {**original, "kind": "issuer_reserve_bonus_correction", "expected_provider_terms": expected,
                      "dividCashPsBeforeTax": "0", "dividStocksPs": "0", "dividReserveToStockPs": "0.1",
                      "source_date": "2025-08-15", "source_url": "https://example.test/reserve_fixture.pdf",
                      "source_file": "supplements/reserve_fixture.pdf", "source_sha256": sha256(source.read_bytes()).hexdigest()}
        write_json(root / "supplements/provider_action_resolutions.json", [resolution])
        return pd.DataFrame([original]), resolution

    def cash_only_fixture(self, root):
        frame, row = self.reserve_correction_fixture(root)
        frame.loc[0, "dividStockMarketDate"] = ""
        frame.loc[0, "dividPayDate"] = "2025-08-21"
        frame.loc[0, "dividCashPsAfterTax"] = "0.9 or 1"
        original = frame.iloc[0].to_dict()
        row = {**row, **original, "kind": "issuer_confirmed_cash_only_terms", "dividStocksPs": "0", "dividReserveToStockPs": "0",
               "expected_provider_terms": {key: original[key] for key in row["expected_provider_terms"]},
               "review_status": "manual_fulltext_and_pages_reviewed",
               "source_url": "https://static.cninfo.com.cn/finalpage/2025-08-15/123.PDF"}
        write_json(root / "supplements/provider_action_resolutions.json", [row])
        return frame, row

    def test_cash_only_completion_keeps_cash_tax_known_zero_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = self.cash_only_fixture(root)
            original = frame.copy(deep=True)
            rows = load_action_resolutions(root, ["001323.SZSE"])
            canonical, audit = reconcile_provider_actions(frame, rows)
            pd.testing.assert_frame_equal(frame, original)
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "1")
            self.assertEqual(canonical.iloc[0]["dividCashPsAfterTax"], "0.9 or 1")
            self.assertEqual(canonical.iloc[0]["dividStocksPs"], "0.000000")
            self.assertEqual(audit[0]["canonical_overrides"], {"dividReserveToStockPs": "0"})
            self.assertEqual(audit[0]["status"], "issuer_completed_cash_only_terms")
            self.assertEqual(audit[0]["source_rows"][0]["dividReserveToStockPs"], "")
            for field in rows[0]["expected_provider_terms"]:
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "source terms changed"):
                    changed = frame.copy()
                    changed.loc[0, field] = "changed"
                    reconcile_provider_actions(changed, rows)
            with self.assertRaisesRegex(ValueError, "source terms changed"):
                reconcile_provider_actions(pd.concat([frame, frame], ignore_index=True), rows)

    def test_cash_only_linked_plan_preserves_sources_and_rejects_unknown_or_conflicting_terms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.cash_only_fixture(root)
            plan = root / "supplements/approved_plan.pdf"
            plan.write_bytes(b"%PDF-approved-plan-fixture only")
            row.update(plan_source_file="supplements/approved_plan.pdf",
                       plan_source_url="https://static.cninfo.com.cn/finalpage/2025-08-10/124.PDF",
                       plan_source_sha256=sha256(plan.read_bytes()).hexdigest(), plan_source_date="2025-08-10",
                       plan_approval_date="2025-08-08", plan_source_pages=[1, 2],
                       plan_review_status="manual_fulltext_and_pages_reviewed", plan_symbol=row["symbol"],
                       plan_reference="same reviewed distribution plan", plan_cash_per_share="1",
                       plan_bonus_per_share="0", plan_reserve_per_share="0")
            path = root / "supplements/provider_action_resolutions.json"
            write_json(path, [row])
            reviewed = load_action_resolutions(root, [row["symbol"]])
            canonical, _ = reconcile_provider_actions(frame, reviewed)
            self.assertEqual(reviewed[0]["plan_source_file"], "supplements/approved_plan.pdf")
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "1")
            write_json(path, [{**row, "plan_source_file": str(plan.resolve())}])
            self.assertEqual(load_action_resolutions(root, [row["symbol"]])[0]["plan_source_file"],
                             "supplements/approved_plan.pdf")
            for change in ({"plan_symbol": "000001.SZSE"}, {"plan_review_status": "machine_candidate"},
                           {"plan_cash_per_share": "0.9"}, {"plan_bonus_per_share": "0.1"},
                           {"plan_reserve_per_share": "NaN"}, {"plan_source_date": "2025-08-16"},
                           {"plan_approval_date": "2025-08-11"}, {"plan_reference": ""},
                           {"plan_source_file": "missing.pdf"}, {"plan_source_pages": []}):
                write_json(path, [{**row, **change}])
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])
            write_json(path, [{key: value for key, value in row.items() if key != "plan_cash_per_share"}])
            with self.assertRaisesRegex(ValueError, "Complete reviewed"):
                load_action_resolutions(root, [row["symbol"]])
            write_json(path, [row])
            plan.write_bytes(b"%PDF-changed-approved-plan")
            with self.assertRaisesRegex(ValueError, "Approved-plan source"):
                load_action_resolutions(root, [row["symbol"]])

    def test_reviewed_treasury_cash_adjustment_binds_plan_without_rewriting_holder_terms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.cash_only_fixture(root)
            frame.loc[0, "dividCashPsBeforeTax"] = "1.344811"
            frame.loc[0, "dividCashStock"] = "10\u6d3e13.448110\u5143"
            row.update(dividCashPsBeforeTax="1.344811", dividCashStock=frame.loc[0, "dividCashStock"],
                       expected_provider_terms={key: frame.loc[0, key] for key in row["expected_provider_terms"]})
            plan = root / "supplements/approved_plan.pdf"
            plan.write_bytes(b"%PDF-reviewed-interim-plan-fixture")
            row.update(plan_source_file="supplements/approved_plan.pdf",
                       plan_source_url="https://static.cninfo.com.cn/finalpage/2025-08-10/124.PDF",
                       plan_source_sha256=sha256(plan.read_bytes()).hexdigest(), plan_source_date="2025-08-10",
                       plan_approval_date="2025-08-08", plan_source_pages=[1],
                       plan_review_status="manual_fulltext_and_pages_reviewed", plan_symbol=row["symbol"],
                       plan_reference="reviewed explicit zero-share interim plan; unchanged adjusted implementation",
                       plan_cash_per_share="1.344", plan_bonus_per_share="0", plan_reserve_per_share="0",
                       plan_cash_adjustment={"basis": "fixed_total_cash_treasury_exclusion", "plan_period": "2025H1",
                           "implementation_period": "2025H1", "plan_total_shares": "639826824",
                           "treasury_shares": "385869", "participating_shares": "639440955",
                           "declared_cash_total": "859927251.46", "issuer_cash_per_ten": "13.448110",
                           "reference": "explicit final fixed-total treasury-exclusion formula, not exact rounded payout equality"})
            path = root / "supplements/provider_action_resolutions.json"
            write_json(path, [row])
            reviewed = load_action_resolutions(root, [row["symbol"]])
            canonical, audit = reconcile_provider_actions(frame, reviewed)
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "1.344811")
            self.assertEqual(canonical.iloc[0]["dividCashPsAfterTax"], "0.9 or 1")
            self.assertEqual(reviewed[0]["plan_cash_per_share"], "1.344")
            self.assertEqual(audit[0]["canonical_overrides"], {"dividReserveToStockPs": "0"})
            changes = ({"plan_period": "2024H1"}, {"implementation_period": "2025H2"},
                       {"participating_shares": "639440956"}, {"treasury_shares": "0"},
                       {"declared_cash_total": "859927252.46"}, {"issuer_cash_per_ten": "13.448111"},
                       {"issuer_cash_per_ten": "NaN"}, {"basis": "guessed_cash_only"}, {"reference": ""})
            for change in changes:
                write_json(path, [{**row, "plan_cash_adjustment": {**row["plan_cash_adjustment"], **change}}])
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])
            for change in ({"plan_cash_per_share": "1.345"}, {"plan_reserve_per_share": "NaN"},
                           {"plan_source_date": "2025-08-16"}, {"plan_review_status": "machine_candidate"}):
                write_json(path, [{**row, **change}])
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])
            write_json(path, [{key: value for key, value in row.items() if key != "plan_source_file"}])
            with self.assertRaisesRegex(ValueError, "complete reviewed"):
                load_action_resolutions(root, [row["symbol"]])

    def test_treasury_precision_requires_exact_arithmetic_and_reviewed_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.cash_only_fixture(root)
            frame.loc[0, "dividCashPsBeforeTax"] = "1.27299"
            frame.loc[0, "dividCashStock"] = "10\u6d3e12.729896\u5143"
            row.update(kind="issuer_cash_only_precision_correction", dividCashPsBeforeTax="1.2729896",
                       issuer_cash_per_ten="12.729896", expected_provider_terms={
                           key: frame.iloc[0][key] for key in row["expected_provider_terms"]})
            plan = root / "supplements/approved_plan.pdf"
            plan.write_bytes(b"%PDF-reviewed-annual-treasury-plan-fixture")
            row.update(plan_source_file="supplements/approved_plan.pdf",
                       plan_source_url="https://static.cninfo.com.cn/finalpage/2025-03-18/124.PDF",
                       plan_source_sha256=sha256(plan.read_bytes()).hexdigest(), plan_source_date="2025-03-18",
                       plan_approval_date="2025-03-14", plan_source_pages=[1, 2],
                       plan_review_status="manual_fulltext_and_pages_reviewed", plan_symbol=row["symbol"],
                       plan_reference="approved annual fixed-total plan with explicit zero stock terms",
                       shareholder_approval_date="2025-05-20", plan_cash_per_share="1.27",
                       plan_bonus_per_share="0", plan_reserve_per_share="0",
                       plan_cash_adjustment={"basis": "fixed_total_cash_treasury_exclusion", "plan_period": "2024FY",
                           "implementation_period": "2024FY", "plan_total_shares": "643976824",
                           "treasury_shares": "1512400", "participating_shares": "642464424",
                           "declared_cash_total": "817850566.48", "issuer_cash_per_ten": "12.729896",
                           "reference": "literal final six-decimal amount and unchanged approved total"})
            path = root / "supplements/provider_action_resolutions.json"
            write_json(path, [row])
            reviewed = load_action_resolutions(root, [row["symbol"]])
            canonical, audit = reconcile_provider_actions(frame, reviewed)
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "1.2729896")
            self.assertEqual(canonical.iloc[0]["dividCashPsAfterTax"], frame.iloc[0]["dividCashPsAfterTax"])
            self.assertEqual(audit[0]["canonical_overrides"], {"dividCashPsBeforeTax": "1.2729896", "dividReserveToStockPs": "0"})
            for change in ({"plan_period": "2025FY"}, {"implementation_period": "2024H1"},
                           {"declared_cash_total": "817850567.48"}, {"participating_shares": "642464423"},
                           {"issuer_cash_per_ten": "12.729895"}):
                write_json(path, [{**row, "plan_cash_adjustment": {**row["plan_cash_adjustment"], **change}}])
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])
            for change in ({"shareholder_approval_date": "2025-03-17"}, {"plan_bonus_per_share": "0.1"},
                           {"plan_cash_per_share": "1.28"}, {"plan_review_status": "machine_candidate"}):
                write_json(path, [{**row, **change}])
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])
            for missing in ("plan_cash_adjustment", "shareholder_approval_date", "plan_source_file"):
                write_json(path, [{key: value for key, value in row.items() if key != missing}])
                with self.subTest(missing=missing), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])
            frame.loc[0, "dividCashPsBeforeTax"] = "1.270092"
            frame.loc[0, "dividCashStock"] = "10\u6d3e12.700919\u5143"
            frame.loc[0, "dividRegistDate"] = "2025-09-02"
            frame.loc[0, "dividPayDate"] = "2025-09-03"
            frame.loc[0, "dividOperateDate"] = "2025-09-03"
            interim = {**row, "source_date": "2025-08-27", "dividRegistDate": "2025-09-02",
                "dividOperateDate": "2025-09-03", "dividPayDate": "2025-09-03",
                "dividCashPsBeforeTax": "1.2700919", "issuer_cash_per_ten": "12.700919",
                "plan_source_date": "2025-08-22", "plan_approval_date": "2025-08-20",
                "plan_cash_per_share": "1.269", "approval_basis": "shareholder_authorized_interim_board",
                "shareholder_authorization_date": "2025-05-20",
                "expected_provider_terms": {key: frame.iloc[0][key] for key in row["expected_provider_terms"]},
                "plan_cash_adjustment": {**row["plan_cash_adjustment"], "plan_period": "2025H1",
                    "implementation_period": "2025H1", "treasury_shares": "553661",
                    "participating_shares": "643423163", "declared_cash_total": "817206589.66",
                    "issuer_cash_per_ten": "12.700919"}}
            write_json(path, [interim])
            canonical, _ = reconcile_provider_actions(frame, load_action_resolutions(root, [row["symbol"]]))
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "1.2700919")
            for change in ({"approval_basis": "assumed_cash_only"}, {"shareholder_authorization_date": "2025-08-21"},
                           {"plan_source_date": "2025-08-19"}):
                write_json(path, [{**interim, **change}])
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])

    def test_reserve_only_completion_preserves_known_bonus_without_claiming_plan_bonus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.cash_only_fixture(root)
            plan = root / "supplements/approved_plan.pdf"
            plan.write_bytes(b"%PDF-reserve-only-plan-fixture")
            row.update(kind="issuer_confirmed_reserve_zero_term", confirmed_fields=["dividReserveToStockPs"],
                       known_terms_basis="provider_cash_and_bonus_preserved",
                       plan_source_file="supplements/approved_plan.pdf",
                       plan_source_url="https://static.cninfo.com.cn/finalpage/2025-08-10/124.PDF",
                       plan_source_sha256=sha256(plan.read_bytes()).hexdigest(), plan_source_date="2025-08-10",
                       plan_approval_date="2025-08-08", plan_source_pages=[1, 2],
                       plan_review_status="manual_fulltext_and_pages_reviewed", plan_symbol=row["symbol"],
                       plan_reference="same reviewed plan confirms reserve only", plan_cash_per_share="1",
                       plan_reserve_per_share="0")
            path = root / "supplements/provider_action_resolutions.json"
            write_json(path, [row])
            reviewed = load_action_resolutions(root, [row["symbol"]])
            canonical, audit = reconcile_provider_actions(frame, reviewed)
            self.assertNotIn("plan_bonus_per_share", reviewed[0])
            self.assertEqual(canonical.iloc[0]["dividStocksPs"], "0.000000")
            self.assertEqual(canonical.iloc[0]["dividCashPsAfterTax"], "0.9 or 1")
            self.assertEqual(audit[0]["canonical_overrides"], {"dividReserveToStockPs": "0"})
            self.assertEqual(audit[0]["status"], "issuer_completed_reserve_zero_term")
            for change in ({"plan_cash_per_share": "0.9"}, {"plan_reserve_per_share": "0.1"},
                           {"plan_bonus_per_share": "0"}, {"confirmed_fields": ["dividStocksPs"]},
                           {"known_terms_basis": "assumed_cash_only"}):
                write_json(path, [{**row, **change}])
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])
            for field, value in (("dividStocksPs", ""), ("dividStocksPs", "0.1"),
                                 ("dividReserveToStockPs", "0")):
                changed = {**row, "expected_provider_terms": {**row["expected_provider_terms"], field: value}}
                write_json(path, [changed])
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])

    def test_reserve_bonus_correction_allows_issuer_confirmed_no_cash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "supplements/reserve_fixture.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-fixture only")
            original = {"symbol": "001311.SZSE", "code": "sz.001311", "dividRegistDate": "2026-05-25",
                        "dividOperateDate": "2026-05-26", "dividPayDate": "", "dividStockMarketDate": "2026-05-26",
                        "dividCashPsBeforeTax": "", "dividCashPsAfterTax": "", "dividStocksPs": "0.000000",
                        "dividCashStock": "10-transfer-3", "dividReserveToStockPs": "0.300000", "_cache_file": "original.csv"}
            expected = {key: value for key, value in original.items()
                        if key not in {"symbol", "dividOperateDate", "_cache_file"}}
            resolution = {**original, "kind": "issuer_reserve_bonus_correction", "expected_provider_terms": expected,
                          "dividCashPsBeforeTax": "0", "dividStocksPs": "0", "dividReserveToStockPs": "0.3",
                          "no_cash_confirmed": True, "source_date": "2026-05-19",
                          "source_url": "https://example.test/reserve_fixture.pdf",
                          "source_file": "supplements/reserve_fixture.pdf",
                          "source_sha256": sha256(source.read_bytes()).hexdigest()}
            write_json(root / "supplements/provider_action_resolutions.json", [resolution])
            frame = pd.DataFrame([original])
            rows = load_action_resolutions(root, [original["symbol"]])
            canonical, audit = reconcile_provider_actions(frame, rows)
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "0")
            self.assertEqual(canonical.iloc[0]["dividStocksPs"], "0")
            self.assertEqual(canonical.iloc[0]["dividReserveToStockPs"], "0.3")
            self.assertEqual(audit[0]["status"], "issuer_corrected_reserve_bonus_event")

    def test_single_cash_precision_uses_final_per_ten_without_changing_raw_terms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.cash_only_fixture(root)
            frame.loc[0, "dividCashPsBeforeTax"] = "0.015926"
            frame.loc[0, "dividCashStock"] = "10\u6d3e0.159264\u5143"
            frame.loc[0, "dividCashPsAfterTax"] = "0.0143338 or 0.0159264"
            row.update(kind="issuer_cash_only_precision_correction", dividCashPsBeforeTax="0.0159264",
                       issuer_cash_per_ten="0.159264", expected_provider_terms={
                           key: frame.iloc[0][key] for key in row["expected_provider_terms"]})
            path = root / "supplements/provider_action_resolutions.json"
            write_json(path, [row])
            original = frame.copy(deep=True)
            rows = load_action_resolutions(root, ["001323.SZSE"])
            canonical, audit = reconcile_provider_actions(frame, rows)
            pd.testing.assert_frame_equal(frame, original)
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "0.0159264")
            self.assertEqual(canonical.iloc[0]["dividCashPsAfterTax"], "0.0143338 or 0.0159264")
            self.assertEqual(canonical.iloc[0]["dividStocksPs"], "0.000000")
            self.assertEqual(audit[0]["status"], "issuer_corrected_cash_only_precision")
            self.assertEqual(audit[0]["canonical_overrides"], {
                "dividCashPsBeforeTax": "0.0159264", "dividReserveToStockPs": "0"})
            with self.assertRaisesRegex(ValueError, "source terms changed"):
                reconcile_provider_actions(pd.concat([frame, frame], ignore_index=True), rows)
            for changes in ({"dividCashPsBeforeTax": "0.016", "issuer_cash_per_ten": "0.16"},
                            {"dividCashPsBeforeTax": "0.015927"}, {"issuer_cash_per_ten": "0.159263"},
                            {"kind": "issuer_confirmed_cash_only_terms"}):
                write_json(path, [{**row, **changes}])
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_action_resolutions(root, ["001323.SZSE"])

    def test_issuer_ir_precision_keeps_amount_guards_and_requires_reviewed_publication_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.cash_only_fixture(root)
            frame.loc[0, ["code", "dividRegistDate", "dividOperateDate", "dividPayDate",
                          "dividCashPsBeforeTax", "dividCashStock"]] = [
                "sz.300952", "2023-07-11", "2023-07-12", "2023-07-12", "0.149333",
                "10\u6d3e1.493334\u5143"]
            row.update(code="sz.300952", symbol="300952.SZSE", source_date="2023-07-05",
                       dividRegistDate="2023-07-11", dividOperateDate="2023-07-12", dividPayDate="2023-07-12",
                       kind="issuer_cash_only_precision_correction", dividCashPsBeforeTax="0.1493334",
                       issuer_cash_per_ten="1.493334", source_channel="issuer_linked_ir_valueonline",
                       source_notice_id="2023-054", issuer_page_url="https://www.hhglove.com/menu/investor.html",
                       issuer_ir_url="https://300952.ir-online.com.cn/",
                       source_url="https://announcement.valueonline.cn/20230705/202307051688469340005025192.pdf",
                       expected_provider_terms={key: frame.iloc[0][key] for key in row["expected_provider_terms"]})
            for prefix, body in (
                ("issuer_page", '<iframe src="https://300952.ir-online.com.cn/"></iframe>'),
                ("issuer_ir", '<iframe src="https://mf.valueonline.cn/ui/irh/index?companyCodeValue=300952"></iframe>'),
            ):
                name = prefix + ".html"
                (root / name).write_text(body, encoding="utf-8")
                row.update({prefix + "_file": name, prefix + "_sha256": sha256((root / name).read_bytes()).hexdigest()})
            path = root / "supplements/provider_action_resolutions.json"
            write_json(path, [row])
            loaded = load_action_resolutions(root, ["300952.SZSE"])
            self.assertEqual(loaded[0]["dividCashPsBeforeTax"], "0.1493334")
            for changes in ({"source_notice_id": "2023-055"}, {"source_date": "2023-07-06"},
                            {"issuer_ir_url": "https://another.test/"},
                            {"source_url": "https://announcement.valueonline.cn/unreviewed.pdf"},
                            {"dividCashPsBeforeTax": "0.15", "issuer_cash_per_ten": "1.50"},
                            {"issuer_page_sha256": "0" * 64}):
                write_json(path, [{**row, **changes}])
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_action_resolutions(root, ["300952.SZSE"])
            (root / row["issuer_page_file"]).write_text('<iframe src="https://another.test/"></iframe>', encoding="utf-8")
            row["issuer_page_sha256"] = sha256((root / row["issuer_page_file"]).read_bytes()).hexdigest()
            write_json(path, [row])
            with self.assertRaisesRegex(ValueError, "Invalid issuer cash-only completion"):
                load_action_resolutions(root, ["300952.SZSE"])

    def positive_bonus_fixture(self, root):
        frame, row = self.cash_only_fixture(root)
        frame.loc[0, "dividCashPsBeforeTax"] = "0.6"
        frame.loc[0, "dividStocksPs"] = "0.400000"
        frame.loc[0, "dividStockMarketDate"] = "2025-08-22"
        plan = root / "supplements/approved_plan.pdf"
        plan.write_bytes(b"%PDF-positive-bonus-plan-fixture")
        row.update(kind="issuer_confirmed_reserve_zero_term", confirmed_fields=["dividReserveToStockPs"],
                   known_terms_basis="provider_cash_and_bonus_preserved", dividCashPsBeforeTax="0.6",
                   dividStocksPs="0.4", dividStockMarketDate="2025-08-22",
                   expected_provider_terms={key: frame.iloc[0][key] for key in row["expected_provider_terms"]},
                   plan_source_file="supplements/approved_plan.pdf",
                   plan_source_url="https://static.cninfo.com.cn/finalpage/2025-08-10/124.PDF",
                   plan_source_sha256=sha256(plan.read_bytes()).hexdigest(), plan_source_date="2025-08-10",
                   plan_approval_date="2025-08-08", plan_source_pages=[1, 2],
                   plan_review_status="manual_fulltext_and_pages_reviewed", plan_symbol=row["symbol"],
                   plan_reference="reviewed final implements unchanged approved bonus/cash/no-transfer plan",
                   plan_cash_per_share="0.6", plan_bonus_per_share="0.4", plan_reserve_per_share="0",
                   shareholder_approval_date="2025-08-12", bonus_taxable_cash_ps="0.4",
                   bonus_nominal_value_ps="1", bonus_tax_basis_reference="final bonus tax example at stated rate")
        write_json(root / "supplements/provider_action_resolutions.json", [row])
        return frame, row

    def test_positive_bonus_reserve_completion_requires_matching_plan_and_tax_basis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.positive_bonus_fixture(root)
            original = frame.copy(deep=True)
            reviewed = load_action_resolutions(root, [row["symbol"]])
            canonical, audit = reconcile_provider_actions(frame, reviewed)
            pd.testing.assert_frame_equal(frame, original)
            self.assertEqual(canonical.iloc[0]["dividStocksPs"], "0.400000")
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "0.6")
            self.assertEqual(canonical.iloc[0]["dividCashPsAfterTax"], "0.9 or 1")
            self.assertEqual(audit[0]["canonical_overrides"], {
                "dividReserveToStockPs": "0", "bonus_taxable_cash_ps": "0.4"})
            for change in ({"plan_bonus_per_share": "0"}, {"dividStocksPs": "0"},
                           {"bonus_taxable_cash_ps": "0"}, {"bonus_taxable_cash_ps": "0.3"},
                           {"bonus_nominal_value_ps": "NaN"}, {"bonus_tax_basis_reference": ""},
                           {"shareholder_approval_date": "2025-08-16"}):
                write_json(root / "supplements/provider_action_resolutions.json", [{**row, **change}])
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])
            for missing in ("plan_bonus_per_share", "bonus_taxable_cash_ps", "shareholder_approval_date"):
                write_json(root / "supplements/provider_action_resolutions.json", [
                    {key: value for key, value in row.items() if key != missing}])
                with self.subTest(missing=missing), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])

    def test_positive_bonus_ledger_reserves_stock_tax_and_respects_listing_date(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.positive_bonus_fixture(root)
            canonical, _ = reconcile_provider_actions(frame, load_action_resolutions(root, [row["symbol"]]))
            action = canonical.iloc[0].to_dict()
            symbol, days = action["symbol"], ["2025-08-20", "2025-08-21"]
            quote = {"open": 30, "close": 30, "preclose": 30, "tradestatus": 1, "isST": 0}
            feed = SimpleNamespace(calendar=days, actions=[action], manifest={"replay_only": True},
                                   instruments={symbol: {"ipoDate": "2022-06-23", "outDate": ""}},
                                   rows={day: {symbol: quote} for day in days})
            state, config = new_state(100), StrategyConfig()
            state["positions"][symbol] = [{"qty": 1000, "available": "2024-01-02"}]
            advance(state, feed, days[0], config, session_number=1)
            advance(state, feed, days[1], config, session_number=2)
            book = Ledger(state, config)
            self.assertEqual(state["cash"], 50000)  # 100 + 600 - (600 + 400)*20% RMB.
            self.assertEqual(book.quantity(symbol), 1400)
            self.assertEqual(book.available(symbol, days[1]), 1000)
            self.assertEqual(book.available(symbol, "2025-08-22"), 1400)
            event = next(item for item in state["events"] if item["kind"] == "corporate_action")
            self.assertEqual(event["modeled_tax_reserve"], 20000)
            resumed = json.loads(json.dumps(state))
            with self.assertRaisesRegex(ValueError, "processed twice"):
                advance(resumed, feed, days[1], config, session_number=2)
            self.assertEqual(resumed, state)

    def test_single_cash_precision_accepts_confirmed_upward_rounding_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.cash_only_fixture(root)
            frame.loc[0, "dividCashPsBeforeTax"] = "0.118309"
            frame.loc[0, "dividCashStock"] = "10\u6d3e1.183089\u5143"
            row.update(kind="issuer_cash_only_precision_correction", dividCashPsBeforeTax="0.1183089",
                       issuer_cash_per_ten="1.183089", expected_provider_terms={
                           key: frame.iloc[0][key] for key in row["expected_provider_terms"]})
            path = root / "supplements/provider_action_resolutions.json"
            write_json(path, [row])
            original = frame.copy(deep=True)
            canonical, _ = reconcile_provider_actions(frame, load_action_resolutions(root, ["001323.SZSE"]))
            pd.testing.assert_frame_equal(frame, original)
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "0.1183089")
            self.assertEqual(canonical.iloc[0]["dividCashPsAfterTax"], original.iloc[0]["dividCashPsAfterTax"])
            for cash, per_ten in (("0.1183081", "1.183081"), ("0.1183089", "1.18309"),
                                  ("0.1183079", "1.183079")):
                changed = {**row, "dividCashPsBeforeTax": cash, "issuer_cash_per_ten": per_ten,
                           "expected_provider_terms": {**row["expected_provider_terms"],
                                                       "dividCashStock": "10\u6d3e" + per_ten + "\u5143"}}
                write_json(path, [changed])
                with self.subTest(cash=cash), self.assertRaises(ValueError):
                    load_action_resolutions(root, ["001323.SZSE"])

    def test_cash_only_loader_rejects_unreviewed_conflicting_amounts_dates_and_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, row = self.cash_only_fixture(root)
            for changes in ({"review_status": "machine_candidate"}, {"dividCashPsBeforeTax": "0.996881"},
                            {"dividStocksPs": "0.1"}, {"dividReserveToStockPs": "NaN"},
                            {"dividPayDate": "2025-08-20"}, {"source_date": "2025-08-21"},
                            {"dividStockMarketDate": "2025-08-21"}, {"expected_provider_terms": {}},
                            {"expected_provider_terms": {**row["expected_provider_terms"], "dividReserveToStockPs": "0.1"}}):
                write_json(root / "supplements/provider_action_resolutions.json", [{**row, **changes}])
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_action_resolutions(root, ["001323.SZSE"])
            write_json(root / "supplements/provider_action_resolutions.json", [row])
            (root / row["source_file"]).write_bytes(b"changed PDF")
            with self.assertRaisesRegex(ValueError, "hash/PDF"):
                load_action_resolutions(root, ["001323.SZSE"])

    def test_coarse_share_display_requires_micro_difference_and_exact_implemented_per_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, row = self.cash_only_fixture(root)
            frame.loc[0, "dividCashPsBeforeTax"] = "0.187"
            frame.loc[0, "dividCashStock"] = "10\u6d3e1.869999\u5143"
            row.update(kind="issuer_cash_only_precision_correction", dividCashPsBeforeTax="0.1869999",
                       issuer_cash_per_ten="1.869999", expected_provider_terms={
                           key: frame.iloc[0][key] for key in row["expected_provider_terms"]})
            path = root / "supplements/provider_action_resolutions.json"
            write_json(path, [row])
            before = frame.copy(deep=True)
            canonical, audit = reconcile_provider_actions(frame, load_action_resolutions(root, [row["symbol"]]))
            pd.testing.assert_frame_equal(frame, before)
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "0.1869999")
            self.assertEqual(audit[0]["canonical_overrides"], {
                "dividCashPsBeforeTax": "0.1869999", "dividReserveToStockPs": "0"})
            for cash, per_ten in (("0.1869998", "1.869998"), ("0.1869", "1.869"),
                                  ("0.1869999", "1.87"), ("0.1869999", "NaN")):
                changed = {**row, "dividCashPsBeforeTax": cash, "issuer_cash_per_ten": per_ten,
                           "expected_provider_terms": {**row["expected_provider_terms"],
                               "dividCashStock": "10\u6d3e" + per_ten + "\u5143"}}
                write_json(path, [changed])
                with self.subTest(cash=cash, per_ten=per_ten), self.assertRaises(ValueError):
                    load_action_resolutions(root, [row["symbol"]])

    def test_cash_only_completion_credits_sole_ledger_once_without_bonus_shares(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = self.cash_only_fixture(root)
            canonical, _ = reconcile_provider_actions(frame, load_action_resolutions(root, ["001323.SZSE"]))
            action = canonical.iloc[0].to_dict()
            symbol, days = action["symbol"], ["2025-08-20", "2025-08-21"]
            quote = {"open": 30, "close": 30, "preclose": 30, "tradestatus": 1, "isST": 0}
            feed = SimpleNamespace(calendar=days, actions=[action], manifest={"replay_only": True},
                                   instruments={symbol: {"ipoDate": "2022-06-23", "outDate": ""}},
                                   rows={day: {symbol: quote} for day in days})
            state, config = new_state(100), StrategyConfig()
            state["positions"][symbol] = [{"qty": 1000, "available": "2024-01-02"}]
            advance(state, feed, days[0], config, session_number=1)
            advance(state, feed, days[1], config, session_number=2)
            self.assertEqual(state["cash"], 90000)  # Cents: initial100RMB + gross1000RMB less existing20% tax reserve.
            self.assertEqual(sum(lot["qty"] for lot in state["positions"][symbol]), 1000)
            resumed = json.loads(json.dumps(state))
            with self.assertRaisesRegex(ValueError, "processed twice"):
                advance(resumed, feed, days[1], config, session_number=2)
            self.assertEqual(resumed, state)

    def test_issuer_reserve_correction_requires_exact_single_source_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = self.reserve_correction_fixture(root)
            original = frame.copy(deep=True)
            reviewed = load_action_resolutions(root, ["001323.SZSE"])
            canonical, audit = reconcile_provider_actions(frame, reviewed)
            pd.testing.assert_frame_equal(frame, original)
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "0")
            self.assertEqual(canonical.iloc[0]["dividReserveToStockPs"], "0.1")
            self.assertEqual(canonical.iloc[0]["dividPayDate"], "")
            self.assertEqual(audit[0]["source_rows"][0]["dividCashPsBeforeTax"], "1")
            self.assertEqual(audit[0]["status"], "issuer_corrected_reserve_bonus_event")
            for changes in ({"dividCashPsBeforeTax": "1.0"}, {"dividReserveToStockPs": "0"},
                            {"dividCashStock": "different"}, {"dividRegistDate": "2025-08-19"},
                            {"dividStockMarketDate": "2025-08-22"}, {"code": "sz.001324"}):
                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "source terms changed"):
                    reconcile_provider_actions(pd.DataFrame([{**frame.iloc[0].to_dict(), **changes}]), reviewed)
            with self.assertRaisesRegex(ValueError, "source terms changed"):
                reconcile_provider_actions(pd.concat([frame, frame], ignore_index=True), reviewed)
            with self.assertRaisesRegex(ValueError, "no longer matches"):
                reconcile_provider_actions(frame.iloc[:0], reviewed)

    def test_reserve_correction_loader_rejects_cash_missing_dates_bad_terms_and_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, resolution = self.reserve_correction_fixture(root)
            for changes in ({"dividCashPsBeforeTax": "1"}, {"dividReserveToStockPs": "NaN"},
                            {"dividStocksPs": "0.1"}, {"dividPayDate": "2025-08-21"},
                            {"dividRegistDate": "2025-08-21"}, {"source_date": "2025-08-22"},
                            {"expected_provider_terms": {}}, {"dividStockMarketDate": "2025-08-19"}):
                write_json(root / "supplements/provider_action_resolutions.json", [{**resolution, **changes}])
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_action_resolutions(root, ["001323.SZSE"])
            write_json(root / "supplements/provider_action_resolutions.json", [resolution])
            (root / resolution["source_file"]).write_bytes(b"changed PDF")
            with self.assertRaisesRegex(ValueError, "hash/PDF"):
                load_action_resolutions(root, ["001323.SZSE"])

    def test_corrected_reserve_bonus_uses_sole_ledger_without_cash_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = self.reserve_correction_fixture(root)
            canonical, _ = reconcile_provider_actions(frame, load_action_resolutions(root, ["001323.SZSE"]))
            action = canonical.iloc[0].to_dict()
            symbol, days = action["symbol"], ["2025-08-20", "2025-08-21"]
            quote = {"open": 30, "close": 30, "preclose": 30, "tradestatus": 1, "isST": 0}
            feed = SimpleNamespace(calendar=days, actions=[action], manifest={"replay_only": True},
                                   instruments={symbol: {"ipoDate": "2022-06-23", "outDate": ""}},
                                   rows={day: {symbol: quote} for day in days})
            state, config = new_state(100), StrategyConfig()
            state["positions"][symbol] = [{"qty": 1000, "available": "2024-01-02"}]
            advance(state, feed, days[0], config, session_number=1)
            advance(state, feed, days[1], config, session_number=2)
            self.assertEqual(state["cash"], 10000)
            self.assertEqual(Ledger(state, config).quantity(symbol), 1100)
            self.assertFalse(any(row["kind"] == "dividend_paid" for row in state["events"]))
            resumed = json.loads(json.dumps(state))
            with self.assertRaisesRegex(ValueError, "processed twice"):
                advance(resumed, feed, days[1], config, session_number=2)
            self.assertEqual(resumed, state)

    def distribution_correction_fixture(self, root):
        source = root / "supplements/distribution_fixture.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-fixture only")
        common = {"code": "sz.003001", "dividRegistDate": "2025-07-04", "dividPayDate": "2025-07-07",
                  "dividStockMarketDate": "2025-07-07", "dividStocksPs": "0.000000"}
        old = {**common, "dividCashPsBeforeTax": "0.281614", "dividReserveToStockPs": "0.399452",
               "dividCashPsAfterTax": "old tax choices", "dividCashStock": "initial distribution"}
        final = {**common, "dividCashPsBeforeTax": "0.28149", "dividReserveToStockPs": "0.399277",
                 "dividCashPsAfterTax": "final tax choices", "dividCashStock": "corrected distribution"}
        resolution = {**common, "symbol": "003001.SZSE", "dividOperateDate": "2025-07-07",
                      "kind": "issuer_distribution_correction", "expected_provider_rows": [old, final],
                      "selected_provider_terms": final, "dividCashPsBeforeTax": "0.2814904",
                      "dividStocksPs": "0", "dividReserveToStockPs": "0.3992772", "source_date": "2025-07-05",
                      "source_url": "https://example.test/distribution_fixture.pdf",
                      "source_file": "supplements/distribution_fixture.pdf",
                      "source_sha256": sha256(source.read_bytes()).hexdigest()}
        write_json(root / "supplements/provider_action_resolutions.json", [resolution])
        frame = pd.DataFrame([{**item, "symbol": resolution["symbol"], "dividOperateDate": resolution["dividOperateDate"],
                               "_cache_file": "original.csv"} for item in [old, final]])
        return frame, resolution

    def test_distribution_correction_requires_exact_multiset_and_retains_raw_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = self.distribution_correction_fixture(root)
            original = frame.copy(deep=True)
            reviewed = load_action_resolutions(root, ["003001.SZSE"])
            canonical, audit = reconcile_provider_actions(frame.iloc[::-1], reviewed)
            pd.testing.assert_frame_equal(frame, original)
            self.assertEqual(len(canonical), 1)
            self.assertEqual(canonical.iloc[0]["dividCashPsBeforeTax"], "0.2814904")
            self.assertEqual(canonical.iloc[0]["dividReserveToStockPs"], "0.3992772")
            self.assertEqual(canonical.iloc[0]["dividCashPsAfterTax"], "")
            self.assertEqual(audit[0]["status"], "issuer_corrected_distribution_event")
            self.assertEqual(len(audit[0]["source_rows"]), 2)
            for bad in (frame.iloc[:1], pd.concat([frame, frame.iloc[:1]], ignore_index=True)):
                with self.assertRaisesRegex(ValueError, "source terms changed"):
                    reconcile_provider_actions(bad, reviewed)
            for field in ("code", "dividCashPsAfterTax", "dividReserveToStockPs", "dividCashStock", "dividRegistDate"):
                bad = frame.copy()
                bad.at[0, field] = "changed"
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "source terms changed"):
                    reconcile_provider_actions(bad, reviewed)
            with self.assertRaisesRegex(ValueError, "no longer matches"):
                reconcile_provider_actions(frame.iloc[:0], reviewed)

    def test_distribution_correction_loader_allows_postrecord_notice_but_rejects_invalid_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, row = self.distribution_correction_fixture(root)
            self.assertEqual(len(load_action_resolutions(root, ["003001.SZSE"])), 1)
            for changes in ({"source_date": "2025-07-08"}, {"dividRegistDate": "2025-07-07"},
                            {"dividPayDate": "2025-07-04"}, {"dividCashPsBeforeTax": "NaN"},
                            {"dividReserveToStockPs": "0"}, {"selected_provider_terms": {}},
                            {"expected_provider_rows": [row["selected_provider_terms"]] * 2}):
                write_json(root / "supplements/provider_action_resolutions.json", [{**row, **changes}])
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_action_resolutions(root, ["003001.SZSE"])
            write_json(root / "supplements/provider_action_resolutions.json", [row])
            (root / row["source_file"]).write_bytes(b"changed PDF")
            with self.assertRaisesRegex(ValueError, "hash/PDF"):
                load_action_resolutions(root, ["003001.SZSE"])

    def combined_cash_correction_fixture(self, root):
        source = root / "supplements/combined_precision.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-fixture only")
        common = {"code": "sz.301308", "dividRegistDate": "2026-06-01", "dividPayDate": "2026-06-02",
                  "dividStockMarketDate": "", "dividStocksPs": "0.000000", "dividReserveToStockPs": ""}
        components = [{**common, "dividCashPsBeforeTax": cash, "dividCashPsAfterTax": tax,
                       "dividCashStock": description} for cash, tax, description in
                      (("0.34676", "annual tax choices", "annual dividend"),
                       ("0.643984", "interim tax choices", "interim dividend"))]
        row = {**common, "symbol": "301308.SZSE", "dividOperateDate": "2026-06-02",
               "kind": "issuer_combined_cash_correction", "expected_provider_rows": components,
               "selected_provider_terms": components[0], "dividCashPsBeforeTax": "0.9907442",
               "dividStocksPs": "0", "dividReserveToStockPs": "0", "source_date": "2026-05-26",
               "source_url": "https://example.test/combined_precision.pdf",
               "source_file": "supplements/combined_precision.pdf", "source_sha256": sha256(source.read_bytes()).hexdigest()}
        write_json(root / "supplements/provider_action_resolutions.json", [row])
        frame = pd.DataFrame([{**item, "symbol": row["symbol"], "dividOperateDate": row["dividOperateDate"],
                               "_cache_file": "original.csv"} for item in components])
        return frame, row

    def test_combined_cash_precision_uses_issuer_total_not_sum_of_truncated_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = self.combined_cash_correction_fixture(root)
            original = frame.copy(deep=True)
            reviewed = load_action_resolutions(root, ["301308.SZSE"])
            canonical, audit = reconcile_provider_actions(frame.iloc[::-1], reviewed)
            pd.testing.assert_frame_equal(frame, original)
            self.assertEqual(canonical.dividCashPsBeforeTax.tolist(), ["0.9907442"])
            self.assertNotEqual(Decimal(canonical.iloc[0].dividCashPsBeforeTax), sum(frame.dividCashPsBeforeTax.map(Decimal)))
            self.assertEqual(canonical.dividCashPsAfterTax.tolist(), [""])
            self.assertEqual(audit[0]["status"], "issuer_combined_cash_event")
            self.assertEqual(len(audit[0]["source_rows"]), 2)
            for bad in (frame.iloc[:1], pd.concat([frame, frame.iloc[:1]], ignore_index=True)):
                with self.assertRaisesRegex(ValueError, "source terms changed"):
                    reconcile_provider_actions(bad, reviewed)
            for field in ("code", "dividCashPsBeforeTax", "dividCashPsAfterTax", "dividCashStock",
                          "dividRegistDate", "dividPayDate", "dividReserveToStockPs"):
                bad = frame.copy()
                bad.at[0, field] = "changed"
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "source terms changed"):
                    reconcile_provider_actions(bad, reviewed)
            with self.assertRaisesRegex(ValueError, "no longer matches"):
                reconcile_provider_actions(frame.iloc[:0], reviewed)

    def test_combined_cash_precision_loader_rejects_bad_terms_dates_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, row = self.combined_cash_correction_fixture(root)
            for changes in ({"source_date": "2026-06-02"}, {"dividRegistDate": "2026-06-02"},
                            {"dividPayDate": "2026-06-01"}, {"dividStockMarketDate": "2026-06-02"},
                            {"dividCashPsBeforeTax": "NaN"}, {"dividCashPsBeforeTax": "0"},
                            {"dividReserveToStockPs": "0.1"}, {"dividStocksPs": "0.1"},
                            {"selected_provider_terms": {}}, {"source_sha256": "0" * 64},
                            {"expected_provider_rows": [row["selected_provider_terms"]] * 2}):
                write_json(root / "supplements/provider_action_resolutions.json", [{**row, **changes}])
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_action_resolutions(root, ["301308.SZSE"])
            for changes in ({"dividCashPsBeforeTax": "0"}, {"dividReserveToStockPs": "0.1"}):
                first = {**row["expected_provider_rows"][0], **changes}
                bad = {**row, "expected_provider_rows": [first, row["expected_provider_rows"][1]], "selected_provider_terms": first}
                write_json(root / "supplements/provider_action_resolutions.json", [bad])
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_action_resolutions(root, ["301308.SZSE"])
            write_json(root / "supplements/provider_action_resolutions.json", [row])
            source = root / row["source_file"]
            source.write_bytes(b"not a PDF")
            with self.assertRaisesRegex(ValueError, "hash/PDF"):
                load_action_resolutions(root, ["301308.SZSE"])

    def test_combined_cash_precision_reuses_sole_ledger_and_repeat_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = self.combined_cash_correction_fixture(root)
            canonical, _ = reconcile_provider_actions(frame, load_action_resolutions(root, ["301308.SZSE"]))
            action = canonical.iloc[0].to_dict()
            symbol, days = action["symbol"], ["2026-06-01", "2026-06-02"]
            quote = {"open": 10, "close": 10, "preclose": 10, "tradestatus": 1, "isST": 0}
            feed = SimpleNamespace(calendar=days, actions=[action], manifest={"replay_only": True},
                                   instruments={symbol: {"ipoDate": "2022-08-05", "outDate": ""}},
                                   rows={day: {symbol: quote} for day in days})
            state, config = new_state(0), StrategyConfig()
            state["positions"][symbol] = [{"qty": 1000000, "available": "2024-01-02"}]
            advance(state, feed, days[0], config, session_number=1)
            advance(state, feed, days[1], config, session_number=2)
            self.assertEqual(state["cash"], 79259536)  # Cents, existing20% reserve; synthetic precision probe only.
            self.assertEqual(Ledger(state, config).quantity(symbol), 1000000)
            resumed = json.loads(json.dumps(state))
            with self.assertRaisesRegex(ValueError, "processed twice"):
                advance(resumed, feed, days[1], config, session_number=2)
            self.assertEqual(resumed, state)

    def membership_fixture(self, root):
        folder = root / "universes/hs300_20231229"
        members = pd.DataFrame([
            {"code": f"sh.{600000 + i}", "symbol": f"{600000 + i}.SSE", "updateDate": "2023-12-25"}
            for i in range(250)] + [
            {"code": f"sh.{688000 + i}", "symbol": f"{688000 + i}.SSE", "updateDate": "2023-12-25"}
            for i in range(50)])
        metadata = {"source": "BaoStock.query_hs300_stocks", "requested_date": "2023-12-29",
                    "members": 300, "mainboard_members": 250, "exchange_independently_verified": False}
        write_json(folder / "manifest.json", metadata)
        write_frame(folder / "all_members.csv", members)
        write_frame(folder / "mainboard_priority.csv", members.iloc[:250])
        return folder, members

    def test_fixed_membership_validates_source_date_and_complete_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder, members = self.membership_fixture(root)
            self.assertEqual(len(fixed_membership(root)[0]), 250)
            for field, value, message in (("updateDate", "2024-01-02", "comparison period"),
                                          ("symbol", "600999.SSE", "mismatch")):
                bad = members.copy()
                bad.loc[0, field] = value
                write_frame(folder / "all_members.csv", bad)
                with self.assertRaisesRegex(ValueError, message):
                    fixed_membership(root)
            write_frame(folder / "all_members.csv", members)
            write_frame(folder / "mainboard_priority.csv", members.iloc[:249])
            with self.assertRaisesRegex(ValueError, "Priority list"):
                fixed_membership(root)

    def test_fixed_universe_never_drops_incomplete_delisted_member(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            delisted = {**self.item(), "symbol": "600001.SSE", "code": "sh.600001",
                        "status": "0", "outDate": "2024-01-03"}
            write_frame(root / "universe.csv", pd.DataFrame([self.item(), delisted]))
            members = pd.DataFrame({"symbol": ["600000.SSE", "600001.SSE"]})
            metadata = {"source": "fixture_only", "requested_date": "2023-12-29"}
            destination = Path(directory) / "snapshot"
            with patch("trading.market_snapshot.fixed_membership", return_value=(members, metadata)):
                coverage = fixed_coverage(root)
                self.assertEqual(coverage["complete_symbols"], 1)
                self.assertEqual(coverage["missing"][0]["symbol"], "600001.SSE")
                self.assertFalse(coverage["source_files_complete"])
                with self.assertRaisesRegex(ValueError, "Fixed universe incomplete: 1/2"):
                    build(root, destination, fixed_hs300=True)
            self.assertFalse(destination.exists())

    def test_complete_fixed_fixture_retains_delisted_member_and_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            write_frame(root / "universe.csv", pd.DataFrame([
                {**self.item(), "status": "0", "outDate": "2024-01-03"}]))
            members = pd.DataFrame({"symbol": ["600000.SSE"]})
            metadata = {"source": "fixture_only", "requested_date": "2023-12-29"}
            with patch("trading.market_snapshot.fixed_membership", return_value=(members, metadata)):
                path = build(root, Path(directory) / "snapshot", fixed_hs300=True)
            feed = HistoricalFeed(path)
            self.assertEqual(feed.symbols, ["600000.SSE"])
            self.assertEqual(feed.manifest["scope"], FIXED_SCOPE)
            self.assertTrue(feed.manifest["selection"]["snapshot_validated"])
            self.assertFalse(feed.manifest["selection"]["membership_independently_verified"])
            self.assertFalse(feed.manifest["strategy_validated"])
            self.assertTrue((path / "selection.csv").exists())

    def test_fixed_membership_listing_conflict_is_not_silently_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            write_frame(root / "universe.csv", pd.DataFrame([{**self.item(), "ipoDate": "2024-01-02"}]))
            members = pd.DataFrame({"symbol": ["600000.SSE"]})
            metadata = {"source": "fixture_only", "requested_date": "2023-12-29"}
            with patch("trading.market_snapshot.fixed_membership", return_value=(members, metadata)):
                with self.assertRaisesRegex(ValueError, "listing interval"):
                    fixed_coverage(root)

    def supplement_fixture(self, root):
        row = {"symbol": "600000.SSE", "dividRegistDate": "2024-01-01", "dividOperateDate": "2024-01-02",
               "dividPayDate": "2024-01-02", "dividCashPsBeforeTax": "0.2584", "dividStocksPs": "0",
               "dividReserveToStockPs": "0", "reference_adjustment_cash_ps": "0.2559",
               "source_date": "2023-12-25", "source_url": "https://example.test/fixture.pdf",
               "source_file": "supplements/sources/fixture.pdf"}
        write_frame(root / "supplements/corporate_actions.csv", pd.DataFrame([row]))
        source = root / row["source_file"]
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-1.7\nfixture only\n")
        return row

    def test_cash_supplement_is_pinned_without_rewriting_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            self.fixture(root)
            row = self.supplement_fixture(root)
            original = (root / "actions/600000.SSE/2024.csv").read_bytes()
            path = build(root, Path(directory) / "snapshot")
            feed = HistoricalFeed(path)
            self.assertEqual(len(feed.actions), 1)
            self.assertEqual(feed.actions[0]["dividCashPsBeforeTax"], 0.2584)
            self.assertEqual(feed.actions[0]["reference_adjustment_cash_ps"], 0.2559)
            self.assertTrue((path / row["source_file"]).exists())
            self.assertEqual((root / "actions/600000.SSE/2024.csv").read_bytes(), original)

    def test_supplement_matches_upstream_once_and_conflicts_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = self.supplement_fixture(root)
            provider = pd.DataFrame([row])
            combined, evidence = supplement_actions(root, provider, [row["symbol"]])
            self.assertEqual(len(combined), 1)
            self.assertEqual(evidence[0]["status"], "matches_provider_not_added")
            provider["dividCashPsBeforeTax"] = "0.1"
            with self.assertRaisesRegex(ValueError, "conflicts"):
                supplement_actions(root, provider, [row["symbol"]])

    def test_cash_supplement_rejects_unevidenced_or_non_cash_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = self.supplement_fixture(root)
            provider = pd.DataFrame(columns=["symbol", "dividOperateDate"])
            for field, value, message in (("dividStocksPs", "0.1", "cash-only"),
                                           ("dividPayDate", "2023-01-01", "dates"),
                                           ("source_file", "missing.pdf", "archived")):
                write_frame(root / "supplements/corporate_actions.csv", pd.DataFrame([{**row, field: value}]))
                with self.assertRaisesRegex(ValueError, message):
                    supplement_actions(root, provider, [row["symbol"]])

    def test_dividend_credit_uses_cash_not_ex_price_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self.supplement_fixture(Path(directory))
            state = new_state(0)
            state["positions"][row["symbol"]] = [{"qty": 100, "available": "2024-01-01"}]
            book = Ledger(state, StrategyConfig())
            feed = SimpleNamespace(actions=[row])
            corporate_actions(book, feed, row["dividRegistDate"], at_close=True)
            corporate_actions(book, feed, row["dividOperateDate"])
            self.assertEqual(state["cash"], 2067)  # 100 * 0.2584 RMB, with the existing 20% tax reserve.

    def test_prestart_rights_adjustment_explains_price_without_ledger_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "supplements/sources/rights.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.7\nfixture only\n")
            row = {"symbol": "601916.SSE", "date": "2023-06-27", "kind": "rights_issue",
                   "registration_date": "2023-06-14", "rights_ratio": "0.3", "subscription_price": "2.02",
                   "source_date": "2023-06-27", "source_url": "https://example.test/rights.pdf",
                   "source_file": "supplements/sources/rights.pdf"}
            write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([row]))
            evidence = prestart_rights_adjustments(root, [row["symbol"]], "2024-01-02")
            self.assertEqual(len(evidence), 1)
            feed = HistoricalFeed.__new__(HistoricalFeed)
            feed.folder = root
            feed.manifest = {"start": "2024-01-02", "historical_price_adjustments": evidence}
            feed.calendar = ["2023-06-14", "2023-06-27"]
            first = {"date": "2023-06-14", "symbol": row["symbol"], "open": 2.74,
                     "high": 2.75, "low": 2.73, "close": 2.74, "preclose": 2.74,
                     "tradestatus": 1, "isST": 0, "adjustflag": "3"}
            second = {**first, "date": "2023-06-27", "open": 2.57, "high": 2.62,
                      "low": 2.56, "close": 2.60, "preclose": 2.57}
            feed.raw = pd.DataFrame([first, second])
            feed.adjusted = feed.raw.assign(adjustflag="2")
            feed.instruments = {row["symbol"]: {}}
            feed.actions = []
            feed.validate()
            self.assertEqual(feed.actions, [])
            feed.manifest["historical_price_adjustments"] = [{**evidence[0], "rights_ratio": "0.4"}]
            with self.assertRaisesRegex(ValueError, "rights_issue ex-price"):
                feed.validate()
            feed.manifest["historical_price_adjustments"] = [{**evidence[0], "date": "2024-01-02"}]
            with self.assertRaisesRegex(ValueError, "Unsupported or unverified"):
                feed.validate()

    def test_rights_adjustment_rejects_missing_evidence_and_poststart_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {"symbol": "601916.SSE", "date": "2024-01-02", "kind": "rights_issue",
                   "registration_date": "2023-12-29", "rights_ratio": "0.3", "subscription_price": "2.02",
                   "source_date": "2024-01-02", "source_url": "https://example.test/rights.pdf",
                   "source_file": "supplements/sources/missing.pdf"}
            write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([row]))
            with self.assertRaisesRegex(ValueError, "pre-replay"):
                prestart_rights_adjustments(root, [row["symbol"]], "2024-01-02")
            write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([{**row, "date": "2023-06-27",
                "registration_date": "2023-06-14", "source_date": "2023-06-27"}]))
            with self.assertRaisesRegex(ValueError, "archived primary-source"):
                prestart_rights_adjustments(root, [row["symbol"]], "2024-01-02")


    def test_prestart_restructuring_is_price_evidence_not_cash_or_share_entitlement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "supplements/sources/restructuring.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.7\nfixture only\n")
            row = {"symbol": "300010.SZSE", "date": "2023-12-27", "kind": "restructuring_no_entitlement",
                   "registration_date": "2023-12-26", "source_date": "2023-12-27",
                   "source_url": "https://example.test/fixture.pdf", "source_file": "supplements/sources/restructuring.pdf",
                   "shares_before": "868324647", "debt_shares": "296162627", "investor_shares": "902125385",
                   "holder_shares": "0", "debt_cash_equivalent": "1776975762", "investor_cash": "1096746343.70",
                   "cash_dividend": "0"}
            write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([row]))
            evidence = prestart_rights_adjustments(root, [row["symbol"]], "2024-01-02")
            feed = HistoricalFeed.__new__(HistoricalFeed)
            feed.folder = root
            feed.calendar = ["2023-12-25", "2023-12-27"]
            feed.manifest = {"start": "2024-01-02", "historical_price_adjustments": evidence}
            first = {"date": "2023-12-25", "symbol": row["symbol"], "open": 2.68,
                     "high": 2.68, "low": 2.68, "close": 2.68, "preclose": 2.68,
                     "tradestatus": 1, "isST": 1, "adjustflag": "3"}
            second = {**first, "date": "2023-12-27", "open": 2.52, "high": 2.52,
                      "low": 2.52, "close": 2.52, "preclose": 2.52}
            feed.raw = pd.DataFrame([first, second])
            feed.adjusted = feed.raw.assign(adjustflag="2")
            feed.instruments = {row["symbol"]: {}}
            feed.actions = []
            feed.validate()
            state = new_state(0)
            state["positions"][row["symbol"]] = [{"qty": 100, "available": "2023-12-25"}]
            book = Ledger(state, StrategyConfig())
            corporate_actions(book, feed, "2023-12-27")
            self.assertEqual(book.quantity(row["symbol"]), 100)
            self.assertEqual(state["cash"], 0)
            feed.manifest["historical_price_adjustments"] = [{**evidence[0], "investor_cash": "0"}]
            with self.assertRaisesRegex(ValueError, "restructuring_no_entitlement ex-price"):
                feed.validate()
            write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([{**row, "date": "2024-01-02"}]))
            with self.assertRaisesRegex(ValueError, "pre-replay"):
                prestart_rights_adjustments(root, [row["symbol"]], "2024-01-02")


    def test_in_replay_no_holder_restructuring_preserves_account_entitlements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "supplements/sources/restructuring.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.7\nfixture only\n")
            row = {"symbol": "300091.SZSE", "date": "2026-02-12", "kind": "restructuring_no_entitlement",
                   "registration_date": "2026-02-11", "source_date": "2026-02-11",
                   "source_url": "https://example.test/fixture.pdf", "source_file": "supplements/sources/restructuring.pdf",
                   "shares_before": "1489164214", "debt_shares": "146257602", "investor_shares": "1206623251",
                   "holder_shares": "0", "debt_cash_equivalent": "842280494.14", "investor_cash": "2378584004.65",
                   "cash_dividend": "0"}
            write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([row]))
            evidence = documented_price_adjustments(root, [row["symbol"]], "2024-01-02", "2026-09-24")
            feed = HistoricalFeed.__new__(HistoricalFeed)
            feed.folder = root
            feed.calendar = ["2026-02-10", "2026-02-11", "2026-02-12"]
            feed.manifest = {"start": "2024-01-02", "end": "2026-09-24", "historical_price_adjustments": evidence}
            first = {"date": "2026-02-10", "symbol": row["symbol"], "open": 3.16,
                     "high": 3.16, "low": 3.16, "close": 3.16, "preclose": 3.16,
                     "tradestatus": 1, "isST": 1, "adjustflag": "3"}
            suspended = {**first, "date": "2026-02-11", "tradestatus": 0}
            second = {**first, "date": "2026-02-12", "open": 2.79, "high": 2.79,
                      "low": 2.79, "close": 2.79, "preclose": 2.79}
            feed.raw = pd.DataFrame([first, suspended, second])
            feed.adjusted = feed.raw.assign(adjustflag="2")
            feed.instruments = {row["symbol"]: {}}
            feed.actions = []
            feed.validate()
            state = new_state(100)
            state["positions"][row["symbol"]] = [{"qty": 100, "available": "2026-02-10"}]
            book = Ledger(state, StrategyConfig())
            book.mark_price(row["symbol"], 316, "2026-02-10")
            before = book.snapshot()
            corporate_actions(book, feed, row["date"])
            self.assertEqual(state, before)
            book.mark_price(row["symbol"], 279, row["date"])
            self.assertEqual(book.equity(), 37900)  # Dilution is a value loss, not phantom shares or cash.
            feed.manifest["historical_price_adjustments"] = [{**evidence[0], "holder_shares": "100"}]
            with self.assertRaisesRegex(ValueError, "no-holder-entitlement"):
                feed.validate()
            feed.manifest["historical_price_adjustments"] = [{**evidence[0], "investor_cash": "0"}]
            with self.assertRaisesRegex(ValueError, "ex-price"):
                feed.validate()
            feed.manifest["historical_price_adjustments"] = evidence
            source.write_bytes(b"%PDF-1.7\nchanged fixture\n")
            with self.assertRaisesRegex(ValueError, "unverified"):
                feed.validate()

    def test_restructuring_donation_reference_does_not_credit_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "supplements/sources/donation_fixture.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.7\nfixture only\n")
            row = {"symbol": "000691.SZSE", "date": "2026-01-05", "kind": "restructuring_no_entitlement",
                   "registration_date": "2025-12-31", "source_date": "2025-12-31",
                   "source_url": "https://example.test/donation_fixture.pdf",
                   "source_file": "supplements/sources/donation_fixture.pdf",
                   "shares_before": "323270000", "debt_shares": "0", "investor_shares": "161635000",
                   "holder_shares": "0", "debt_cash_equivalent": "0", "investor_cash": "403763890",
                   "investor_donation_cash": "73000000", "cash_dividend": "0"}
            write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([row]))
            evidence = documented_price_adjustments(root, [row["symbol"]], "2024-01-02", "2026-09-24")
            feed = HistoricalFeed.__new__(HistoricalFeed)
            feed.folder = root
            feed.calendar = ["2025-12-30", "2025-12-31", "2026-01-05"]
            feed.manifest = {"start": "2024-01-02", "end": "2026-09-24", "historical_price_adjustments": evidence}
            first = {"date": "2025-12-30", "symbol": row["symbol"], "open": 9.10,
                     "high": 9.10, "low": 9.10, "close": 9.10, "preclose": 9.10,
                     "tradestatus": 1, "isST": 1, "adjustflag": "3"}
            suspended = {**first, "date": "2025-12-31", "tradestatus": 0}
            ex_day = {**first, "date": row["date"], "open": 7.05, "high": 7.05,
                      "low": 7.05, "close": 7.05, "preclose": 7.05}
            feed.raw = pd.DataFrame([first, suspended, ex_day])
            feed.adjusted = feed.raw.assign(adjustflag="2")
            feed.instruments = {row["symbol"]: {}}
            feed.actions = []
            feed.validate()
            state = new_state(100)
            state["positions"][row["symbol"]] = [{"qty": 100, "available": "2025-12-30"}]
            book = Ledger(state, StrategyConfig())
            book.mark_price(row["symbol"], 910, "2025-12-30")
            before = book.snapshot()
            corporate_actions(book, feed, row["date"])
            self.assertEqual(book.snapshot(), before)
            book.mark_price(row["symbol"], 705, row["date"])
            self.assertEqual(book.quantity(row["symbol"]), 100)
            self.assertEqual(book.state["cash"], 10000)
            self.assertEqual(book.equity(), 80500)
            feed.manifest["historical_price_adjustments"] = [{**evidence[0], "investor_donation_cash": "0"}]
            with self.assertRaisesRegex(ValueError, "ex-price"):
                feed.validate()

    def float_restructuring(self):
        return {"shares_before": "1227326240", "debt_shares": "0", "investor_shares": "1536186603",
                "holder_shares": "0", "debt_cash_equivalent": "0", "investor_cash": "989465020",
                "investor_compensation_cash": "483326468", "cash_dividend": "0",
                "reference_share_base": "883566332", "reference_share_basis": "unrestricted_float",
                "reference_threshold_price": "0.96"}

    def test_documented_float_reference_and_rounded_threshold(self):
        row = self.float_restructuring()
        expected = (2.84 * 883566332 + 989465020 + 483326468) / (883566332 + 1536186603)
        self.assertAlmostEqual(float(restructuring_ex_price(2.84, row)), expected, places=12)
        # Negative control: unlimited float alone misreads the issuer's original A-share base.
        self.assertEqual(round(expected, 2), 1.65)
        self.assertEqual(float(restructuring_ex_price(.96, row)), .96)
        self.assertNotEqual(restructuring_ex_price(.96, row),
                            restructuring_ex_price(.96, {**row, "reference_threshold_price": ""}))
        self.assertLess(float(restructuring_ex_price(.97, row)), .97)
        self.assertNotAlmostEqual(float(restructuring_ex_price(2.84, row)),
                                  float(restructuring_ex_price(2.84, {**row, "reference_share_base": "",
                                                                        "reference_share_basis": ""})), places=2)
        unconditional = {**row, "reference_threshold_price": "0"}
        self.assertNotEqual(restructuring_ex_price(.95, unconditional), restructuring_ex_price(.95, row))

    def test_documented_float_reference_rejects_invalid_terms(self):
        row = self.float_restructuring()
        for change in ({"reference_share_base": "0"}, {"reference_share_base": "1227326241"},
                       {"reference_share_base": "883566332.5"}, {"reference_share_base": "NaN"},
                       {"reference_share_basis": ""}, {"reference_share_base": ""},
                       {"reference_share_basis": "total_shares"}, {"reference_threshold_price": "NaN"},
                       {"reference_threshold_price": "-1"}, {"investor_compensation_cash": "-1"},
                       {"investor_compensation_cash": "Infinity"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                restructuring_ex_price(2.84, {**row, **change})

    def test_float_restructuring_consumed_by_feed_without_account_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "supplements/sources/float_fixture.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.7\nfixture only\n")
            row = {**self.float_restructuring(), "symbol": "600666.SSE", "date": "2023-02-20",
                   "kind": "restructuring_no_entitlement", "registration_date": "2023-02-17",
                   "source_date": "2023-02-14", "source_url": "https://example.test/float_fixture.pdf",
                   "source_file": "supplements/sources/float_fixture.pdf"}
            write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([row]))
            evidence = documented_price_adjustments(root, [row["symbol"]], "2023-01-01", "2026-09-30")
            feed = HistoricalFeed.__new__(HistoricalFeed)
            feed.folder = root
            feed.calendar = ["2023-02-16", "2023-02-17", "2023-02-20"]
            feed.manifest = {"start": "2023-01-01", "end": "2026-09-30", "historical_price_adjustments": evidence}
            first = {"date": feed.calendar[0], "symbol": row["symbol"], "open": 2.84,
                     "high": 2.84, "low": 2.84, "close": 2.84, "preclose": 2.84,
                     "tradestatus": 1, "isST": 1, "adjustflag": "3"}
            suspended = {**first, "date": feed.calendar[1], "tradestatus": 0}
            ex_day = {**first, "date": row["date"], "open": 1.79, "high": 1.79,
                      "low": 1.79, "close": 1.79, "preclose": 1.79}
            feed.raw = pd.DataFrame([first, suspended, ex_day])
            feed.adjusted = feed.raw.assign(adjustflag="2")
            feed.instruments = {row["symbol"]: {}}
            feed.actions = []
            with self.assertRaisesRegex(ValueError, "ex-price"):
                feed.validate()
            # Incorrect unlimited-only interpretation must still reject the actual 1.79 reference.
            feed.raw.loc[feed.raw.date.eq(row["date"]), "preclose"] = 1.65
            feed.validate()
            state = new_state(100)
            state["positions"][row["symbol"]] = [{"qty": 100, "available": feed.calendar[0]}]
            book = Ledger(state, StrategyConfig())
            before = book.snapshot()
            corporate_actions(book, feed, row["date"])
            self.assertEqual(book.snapshot(), before)
            for change in ({"reference_share_base": "", "reference_share_basis": ""},
                           {"investor_compensation_cash": "0"}):
                feed.manifest["historical_price_adjustments"] = [{**evidence[0], **change}]
                with self.assertRaisesRegex(ValueError, "ex-price"):
                    feed.validate()

    def test_600666_original_a_share_base_includes_both_flow_classes(self):
        row = {**self.float_restructuring(), "reference_share_base": "", "reference_share_basis": ""}
        self.assertEqual(343759908 + 883566332, int(row["shares_before"]))
        expected = restructuring_ex_price(2.84, row)
        self.assertAlmostEqual(float(expected), 1.7942373679064534, places=12)
        self.assertEqual(round(float(expected), 2), 1.79)
        self.assertEqual(float(restructuring_ex_price(.96, row)), .96)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "supplements/sources/original_a_share_fixture.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.7\nfixture only\n")
            row.update(symbol="600666.SSE", date="2023-02-20", kind="restructuring_no_entitlement",
                       registration_date="2023-02-17", source_date="2023-02-14",
                       source_url="https://example.test/original_a_share_fixture.pdf",
                       source_file="supplements/sources/original_a_share_fixture.pdf")
            write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([row]))
            evidence = documented_price_adjustments(root, [row["symbol"]], "2023-01-01", "2026-09-30")
            feed = HistoricalFeed.__new__(HistoricalFeed)
            feed.folder = root
            feed.calendar = ["2023-02-16", "2023-02-17", "2023-02-20"]
            feed.manifest = {"start": "2023-01-01", "end": "2026-09-30", "historical_price_adjustments": evidence}
            first = {"date": feed.calendar[0], "symbol": row["symbol"], "open": 2.84,
                     "high": 2.84, "low": 2.84, "close": 2.84, "preclose": 2.84,
                     "tradestatus": 1, "isST": 1, "adjustflag": "3"}
            suspended = {**first, "date": feed.calendar[1], "tradestatus": 0}
            ex_day = {**first, "date": row["date"], "open": 1.79, "high": 1.79,
                      "low": 1.79, "close": 1.79, "preclose": 1.79}
            feed.raw = pd.DataFrame([first, suspended, ex_day])
            feed.adjusted = feed.raw.assign(adjustflag="2")
            feed.instruments = {row["symbol"]: {}}
            feed.actions = []
            feed.validate()
            state = new_state(100)
            state["positions"][row["symbol"]] = [{"qty": 100, "available": feed.calendar[0]}]
            book = Ledger(state, StrategyConfig())
            before = book.snapshot()
            corporate_actions(book, feed, row["date"])
            self.assertEqual(book.snapshot(), before)
            for change in ({"reference_share_base": "883566332", "reference_share_basis": "unrestricted_float"},
                           {"investor_compensation_cash": "0"}):
                feed.manifest["historical_price_adjustments"] = [{**evidence[0], **change}]
                with self.assertRaisesRegex(ValueError, "ex-price"):
                    feed.validate()

    def test_in_replay_adjustment_still_rejects_future_and_holder_distribution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "supplements/sources/restructuring.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.7\nfixture only\n")
            row = {"symbol": "000430.SZSE", "date": "2025-12-29", "kind": "restructuring_no_entitlement",
                   "registration_date": "2025-12-26", "source_date": "2025-12-27",
                   "source_url": "https://example.test/fixture.pdf", "source_file": "supplements/sources/restructuring.pdf",
                   "shares_before": "404817686", "debt_shares": "38817686", "investor_shares": "366000000",
                   "holder_shares": "0", "debt_cash_equivalent": "776353720", "investor_cash": "1586300000",
                   "cash_dividend": "0", "rights_ratio": ".3", "subscription_price": "2.02"}
            for change, message in (({"date": "2026-09-25"}, "outside"),
                                    ({"holder_shares": "100"}, "no-holder-entitlement"),
                                    ({"registration_date": row["date"]}, "chronology")):
                write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([{**row, **change}]))
                with self.assertRaisesRegex(ValueError, message):
                    documented_price_adjustments(root, [row["symbol"]], "2024-01-02", "2026-09-24")

    def rights_replay_fixture(self, root):
        source = root / "supplements/sources/rights.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-1.7\nfixture only\n")
        row = {"symbol": "300176.SZSE", "date": "2026-08-21", "kind": "rights_issue",
               "registration_date": "2026-08-12", "rights_ratio": "0.3722702", "subscription_price": "3.36",
               "source_date": "2026-08-21", "source_url": "https://example.test/rights.pdf",
               "source_file": "supplements/sources/rights.pdf"}
        write_frame(root / "supplements/price_adjustments.csv", pd.DataFrame([row]))
        evidence = documented_price_adjustments(root, [row["symbol"]], "2024-01-02", "2026-09-24")
        feed = HistoricalFeed.__new__(HistoricalFeed)
        feed.folder = root
        feed.calendar = ["2026-08-12", "2026-08-20", "2026-08-21"]
        feed.manifest = {"start": "2024-01-02", "end": "2026-09-24", "historical_price_adjustments": evidence}
        first = {"date": feed.calendar[0], "symbol": row["symbol"], "open": 5.23,
                 "high": 5.23, "low": 5.23, "close": 5.23, "preclose": 5.23,
                 "tradestatus": 1, "isST": 0, "adjustflag": "3"}
        suspended = {**first, "date": feed.calendar[1], "tradestatus": 0}
        ex_day = {**first, "date": row["date"], "open": 4.72, "high": 4.72,
                  "low": 4.72, "close": 4.72, "preclose": 4.72}
        feed.raw = pd.DataFrame([first, suspended, ex_day])
        feed.adjusted = feed.raw.assign(adjustflag="2")
        feed.instruments = {row["symbol"]: {}}
        feed.actions = []
        feed.rows = {r["date"]: {r["symbol"]: r} for r in feed.raw.to_dict("records")}
        feed.validate()
        return feed, row

    def test_in_replay_rights_reference_requires_bounded_guarded_exact_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed, row = self.rights_replay_fixture(root)
            self.assertEqual(feed.manifest["historical_price_adjustments"][0]["ledger_policy"],
                             "stop_unresolved_registered_holding")
            with self.assertRaisesRegex(ValueError, "pre-replay"):
                prestart_rights_adjustments(root, [row["symbol"]], feed.manifest["start"])
            evidence = feed.manifest["historical_price_adjustments"]
            for changes, message in (({"ledger_policy": "subscribe_automatically"}, "unverified"),
                                     ({"registration_date": "2026-08-13"}, "unverified"),
                                     ({"rights_ratio": "0.4"}, "ex-price"),
                                     ({"rights_ratio": "nan"}, "Invalid rights")):
                feed.manifest["historical_price_adjustments"] = [{**evidence[0], **changes}]
                with self.assertRaisesRegex(ValueError, message):
                    feed.validate()
            feed.manifest["historical_price_adjustments"] = evidence
            (root / row["source_file"]).write_bytes(b"%PDF-1.7\nchanged fixture\n")
            with self.assertRaisesRegex(ValueError, "unverified"):
                feed.validate()

    def test_zero_registered_rights_entitlement_advances_without_cash_or_share_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, row = self.rights_replay_fixture(Path(directory))
            state, config = new_state(140000), StrategyConfig()
            for day in feed.calendar:
                result = advance(state, feed, day, config, session_number=1)
            identifier = f"rights:{row['symbol']}:{row['date']}"
            self.assertEqual(state["entitlements"][identifier], 0)
            self.assertEqual(state["day"], row["date"])
            self.assertEqual(result["equity"], 140000)
            self.assertEqual(state["cash"], 14000000)
            for field in ("positions", "orders", "fills", "receivables"):
                self.assertEqual(state[field], {})

    def test_registered_rights_holding_stops_even_after_sale_without_account_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, row = self.rights_replay_fixture(Path(directory))
            state, config = new_state(140000), StrategyConfig()
            state["positions"][row["symbol"]] = [{"qty": 100, "available": "2026-08-11"}]
            advance(state, feed, row["registration_date"], config, session_number=1)
            identifier = f"rights:{row['symbol']}:{row['date']}"
            self.assertEqual(state["entitlements"][identifier], 100)
            state["positions"][row["symbol"]] = []
            # A pending cash dividend must not be credited before the rights guard fails.
            state["entitlements"][f"600000.SSE:{row['date']}"] = 100
            feed.actions = [{"symbol": "600000.SSE", "dividOperateDate": row["date"],
                             "dividRegistDate": row["registration_date"], "dividPayDate": row["date"],
                             "dividCashPsBeforeTax": "1"}]
            before = Ledger(state, config).snapshot()
            with self.assertRaisesRegex(ValueError, "registered quantity=100"):
                advance(state, feed, row["date"], config, session_number=1)
            self.assertEqual(state, before)

    def test_missing_registered_rights_entitlement_is_not_assumed_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, row = self.rights_replay_fixture(Path(directory))
            state, config = new_state(140000), StrategyConfig()
            before = Ledger(state, config).snapshot()
            with self.assertRaisesRegex(ValueError, "registered quantity=None"):
                advance(state, feed, row["date"], config, session_number=1)
            self.assertEqual(state, before)

    def test_skipping_rights_ex_session_cannot_bypass_the_entitlement_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, row = self.rights_replay_fixture(Path(directory))
            later = "2026-08-24"
            feed.calendar.append(later)
            feed.rows[later] = {row["symbol"]: {**feed.rows[row["date"]][row["symbol"]], "date": later}}
            state, config = new_state(140000), StrategyConfig()
            state["positions"][row["symbol"]] = [{"qty": 100, "available": "2026-08-11"}]
            advance(state, feed, row["registration_date"], config, session_number=1)
            before = Ledger(state, config).snapshot()
            with self.assertRaisesRegex(ValueError, "registered quantity=100"):
                advance(state, feed, later, config, session_number=1)
            self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()
