import json
import gzip
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from trading.research.seat_dataset import (PublicClient, SCHEMA, export_observations, import_media_links, ingest,
                                           params_for, publish_status, quarantine_unverified_currency)
from trading.research.tests.test_seat_trend import raw
from trading.research.seats import collect, normalize
from trading.research.seat_dataset_audit import verify_pages
from trading.research.seat_dataset_parquet import export as export_parquet, write_table


class SeatDatasetTests(unittest.TestCase):
    def test_small_collector_uses_stable_sort_and_reuses_only_matching_cache(self):
        row = {"TRADE_DATE": "2025-09-26", "SECURITY_CODE": "600000"}
        body = {"success": True, "result": {"pages": 1, "count": 1, "data": [row]}}
        with tempfile.TemporaryDirectory() as folder, patch("trading.research.seats.requests.Session") as factory, patch("trading.research.seats.time.sleep"), patch("builtins.print"):
            session = factory.return_value.__enter__.return_value
            session.get.return_value.json.return_value = body
            collect(folder, "2025-09-26", "2025-09-26", ["600000"])
            self.assertEqual(session.get.call_count, 3)
            buy_params = session.get.call_args_list[1].kwargs["params"]
            self.assertEqual(buy_params["pageSize"], 500)
            self.assertIn("TRADE_ID,OPERATEDEPT_CODE,BUY,SELL", buy_params["sortColumns"])
            collect(folder, "2025-09-26", "2025-09-26", ["600000"])
            self.assertEqual(session.get.call_count, 3)
    def test_parquet_preserves_null_and_excludes_non_a_shares(self):
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as folder, closing(sqlite3.connect(":memory:")) as db:
            db.executescript(SCHEMA)
            row = {**raw("a"), "SELL": None, "NET": None}
            doc = {"response": {"result": {"data": [row, {**row, "SECUCODE": "200001.SZ"}]}},
                   "retrieved_at": "2026-09-26"}
            ingest(db, "buy", 1, doc, Path("fixture.json.gz"))
            db.execute("CREATE TEMP TABLE export_symbols AS SELECT symbol FROM security_registry WHERE security_kind='A_share'")
            files = write_table(db, "seat_observations", Path(folder))
            self.assertEqual(sum(item["rows"] for item in files), 1)
            saved = pq.ParquetFile(files[0]["path"]).read().to_pylist()[0]
            self.assertEqual(saved["buy_cny"], 200.)
            self.assertIsNone(saved["sell_cny"])
            self.assertIsNone(saved["published_at"])
            self.assertEqual(saved["symbol"], "600000.SSE")

    def test_partial_parquet_publish_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "status.json").write_text('{"status":"partial_backfill"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Partial backfills"):
                export_parquet(root)

    def test_page_audit_reports_overlap_without_removing_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = {"requested_start": "2020-01-01", "end": "2026-09-24", "pagination_version": 2,
                    "reports": {"buy": {"rows": 501, "pages": 2}, "sell": {"rows": 1, "pages": 1}}}
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            rows = [{**raw(str(i)), "TRADE_ID": str(i)} for i in range(500)]
            for kind, page, data in (("buy", 1, rows), ("buy", 2, rows[:1]), ("sell", 1, rows[:1])):
                target = root / "raw" / kind / f"{page:05d}.json.gz"
                target.parent.mkdir(parents=True, exist_ok=True)
                doc = {"request": params_for(kind, page, plan["requested_start"], plan["end"]),
                       "response": {"result": {"count": plan["reports"][kind]["rows"],
                           "pages": plan["reports"][kind]["pages"], "data": data}}}
                with gzip.open(target, "wt", encoding="utf-8") as stream:
                    json.dump(doc, stream)
            result = verify_pages(root)
            self.assertEqual(result["kinds"]["buy"]["cross_page_repeated_facts"], 1)
            self.assertEqual(result["kinds"]["buy"]["physical_rows"], 501)
            self.assertEqual(result["kinds"]["buy"]["status"], "needs_overlap_review")
            self.assertEqual(result["kinds"]["sell"]["status"], "source_pagination_verified")
            self.assertFalse(result["private_account_completeness_proven"])
            (root / "raw" / "buy" / "00002.json.gz").unlink()
            self.assertEqual(verify_pages(root)["kinds"]["buy"]["missing_pages"], [2])

    def test_stock_views_exclude_b_shares_and_bonds(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(SCHEMA)
            codes = ["600000.SH", "300001.SZ", "920001.BJ", "113010.SH", "200001.SZ"]
            rows = [{**raw(str(i)), "SECUCODE": code} for i, code in enumerate(codes)]
            doc = {"response": {"result": {"data": rows}}, "retrieved_at": "2026-09-26"}
            ingest(db, "buy", 1, doc, Path("test_buy.json.gz"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM a_share_seat_events").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT security_kind FROM security_registry WHERE symbol='113010.SSE'").fetchone()[0], "convertible_or_exchangeable_bond")
            self.assertEqual(db.execute("SELECT security_kind FROM security_registry WHERE symbol='200001.SZSE'").fetchone()[0], "B_share")
            quarantine_unverified_currency(db)
            self.assertIsNone(db.execute("SELECT buy_cny FROM seat_observations WHERE symbol='200001.SZSE'").fetchone()[0])
            self.assertEqual(db.execute("SELECT buy_cny FROM seat_observations WHERE symbol='600000.SSE'").fetchone()[0], 200.)

    def test_generic_cross_page_review_requires_exact_multiplicity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            day = "2025-09-26"
            plan = {"requested_start": day, "end": day, "pagination_version": 2,
                    "reports": {"buy": {"rows": 501, "pages": 2}, "sell": {"rows": 1, "pages": 1}}}
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            generic = {**raw("generic", name="机构专用"), "OPERATEDEPT_CODE": "0",
                       "TRADE_ID": "repeated", "SECURITY_CODE": "600000"}
            first = [{**raw(str(i)), "TRADE_ID": str(i)} for i in range(499)] + [generic]
            for kind, page, rows in (("buy", 1, first), ("buy", 2, [generic]), ("sell", 1, first[:1])):
                target = root / "raw" / kind / f"{page:05d}.json.gz"
                target.parent.mkdir(parents=True, exist_ok=True)
                doc = {"request": params_for(kind, page, day, day), "response": {"success": True,
                       "result": {"count": plan["reports"][kind]["rows"],
                                  "pages": plan["reports"][kind]["pages"], "data": rows}}}
                with gzip.open(target, "wt", encoding="utf-8") as stream:
                    json.dump(doc, stream)
            target = root / "overlap_reviews" / "buy" / "board.json.gz"
            target.parent.mkdir(parents=True)
            request = params_for("buy", 1, day, day)
            request["filter"] += '(SECURITY_CODE="600000")(TRADE_ID="repeated")'
            for multiplicity in (1, 2, 3):
                with self.subTest(source_rows=multiplicity):
                    doc = {"review_key": [day, "600000.SH", "repeated"], "request": request,
                           "response": {"success": True, "result": {
                               "pages": 1, "count": multiplicity, "data": [generic] * multiplicity}}}
                    with gzip.open(target, "wt", encoding="utf-8") as stream:
                        json.dump(doc, stream)
                    result = verify_pages(root)["kinds"]["buy"]
                    self.assertEqual(result["physical_rows"], 501)
                    self.assertEqual(result["cross_page_repeated_facts"], 1)
                    self.assertEqual(result["resolved_cross_page_repetitions"], int(multiplicity == 2))
                    self.assertEqual(result["status"], "source_pagination_verified" if multiplicity == 2 else "needs_overlap_review")

    def test_pre_bse_disclosures_are_retained_but_excluded_from_a_share_view(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(SCHEMA)
            rows = [{**raw("a"), "SECUCODE": "920001.BJ", "TRADE_DATE": day}
                    for day in ("2021-11-12", "2021-11-15")]
            doc = {"response": {"result": {"data": rows}}, "retrieved_at": "2026-09-26"}
            ingest(db, "buy", 1, doc, Path("fixture.json.gz"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM seat_observations").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT trade_date FROM a_share_seat_observations").fetchall(), [("2021-11-15",)])

    def test_one_transport_retry_but_no_permission_retry(self):
        first, second = Mock(), Mock()
        first.get.side_effect = requests.exceptions.ChunkedEncodingError("truncated transport")
        second.get.return_value.json.return_value = {"success": True, "result": {"count": 1}}
        with patch("trading.research.seat_dataset.requests.Session", side_effect=[first, second]), patch("trading.research.seat_dataset.time.sleep"):
            result = PublicClient().fetch({"reportName": "fixture"})
        self.assertEqual(result["transport_retries"], ["ChunkedEncodingError"])
        self.assertEqual(first.get.call_count, 1)
        self.assertEqual(second.get.call_count, 1)
        denied = Mock()
        denied.get.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError("429")
        with patch("trading.research.seat_dataset.requests.Session", return_value=denied):
            with self.assertRaises(requests.exceptions.HTTPError):
                PublicClient().fetch({"reportName": "fixture"})
        self.assertEqual(denied.get.call_count, 1)
        gateway, recovered = Mock(), Mock()
        gateway.get.return_value.status_code = 504
        recovered.get.return_value.status_code = 200
        recovered.get.return_value.json.return_value = {"success": True, "result": {"count": 1}}
        with patch("trading.research.seat_dataset.requests.Session", side_effect=[gateway, recovered]), patch("trading.research.seat_dataset.time.sleep"):
            result = PublicClient().fetch({"reportName": "fixture"})
        self.assertEqual(result["transport_retries"], ["HTTP504"])

    def test_idempotent_import_direction_dedup_generic_and_publication_null(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / "disclosures.sqlite3")) as db:
                db.executescript(SCHEMA)
                normal, generic = raw("a"), raw("b", name="机构专用")
                doc = {"response": {"result": {"data": [normal, generic]}}, "retrieved_at": "2026-09-26T00:00:00Z"}
                self.assertTrue(ingest(db, "buy", 1, doc, root / "buy1.json.gz"))
                self.assertFalse(ingest(db, "buy", 1, doc, root / "buy1.json.gz"))
                self.assertTrue(ingest(db, "sell", 1, doc, root / "sell1.json.gz"))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM seat_observations").fetchone()[0], 4)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM seat_events").fetchone()[0], 3)
                row = db.execute("SELECT buy_cny,raw_listings,published_at FROM seat_events WHERE identity_usable=1").fetchone()
                self.assertEqual(row, (200., 2, None))
                import_media_links(db)
                import_media_links(db)
                seed_path = Path(__file__).parents[1] / "seat_media_links.json"
                self.assertEqual(db.execute("SELECT COUNT(*) FROM person_seat_links").fetchone()[0], len(json.loads(seed_path.read_text(encoding="utf-8"))))
                self.assertEqual(db.execute("SELECT MAX(identity_verified) FROM person_seat_links").fetchone()[0], 0)
            result = export_observations(root, root / "seat.csv", seat="Broker A branch")
            self.assertEqual(result["rows"], 1)
            self.assertIn("not_private_trades", result["type"])
            stock_result = export_observations(root, root / "stock.csv", symbol="600000.SSE")
            self.assertEqual(stock_result["rows"], 3)

    def test_same_display_name_with_distinct_codes_is_not_merged(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(SCHEMA)
            rows = [{**raw("a"), "OPERATEDEPT_CODE": "123"},
                    {**raw("b"), "OPERATEDEPT_CODE": "456"}]
            doc = {"response": {"result": {"data": rows}}, "retrieved_at": "2026-09-26"}
            ingest(db, "buy", 1, doc, Path("fixture.json.gz"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM seat_events").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_amount_conflicts").fetchone()[0], 0)
            frame = normalize(rows, [])
            self.assertEqual(len(frame), 2)
            self.assertFalse(frame.amount_conflict.any())

    def test_partial_is_not_complete_and_query_is_full_market(self):
        params = params_for("buy", 1, "1990-01-01", "2026-09-24")
        self.assertNotIn(" in ", params["filter"])
        self.assertIn("TRADE_ID,OPERATEDEPT_CODE,BUY,SELL", params["sortColumns"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / "disclosures.sqlite3")) as db:
                db.executescript(SCHEMA)
                plan = {"expected_raw_rows": 6, "reports": {k: {"rows": 2, "pages": 1} for k in ("buy", "sell", "summary")}}
                doc = {"response": {"result": {"data": [raw("a"), raw("b")]}}, "retrieved_at": "2026-09-26"}
                ingest(db, "buy", 1, doc, root / "buy1.json.gz")
                result = publish_status(db, root, plan)
                self.assertEqual(result["status"], "partial_backfill")
                self.assertEqual(result["raw_rows_downloaded"], 2)
                self.assertFalse(result["all_persons_identified"])
                self.assertEqual(json.loads((root / "status.json").read_text())["raw_rows_expected"], 6)


if __name__ == "__main__":
    unittest.main()
