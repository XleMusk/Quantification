"""Resumable public-disclosure dataset, not private account trade reconstruction."""
import argparse
from contextlib import closing
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import uuid

import pandas as pd
import requests

from .seats import DEFAULT_HOME, REPORTS, URL, iso_day, observations, period, save_json

DEFAULT_DATASET = DEFAULT_HOME / "public_history_2004_20260924_v2"
EVENT_COLUMNS = """event_key,trade_date,symbol,seat_name,seat_code,window_kind,window_start,window_end,
        GROUP_CONCAT(DISTINCT reason) AS reasons,GROUP_CONCAT(DISTINCT listed_side) AS listed_sides,
        buy_cny,sell_cny,net_cny,period_turnover_cny,identity_usable,amount_valid,
        COUNT(*) AS raw_listings,MIN(retrieved_at) AS first_retrieved_at,NULL AS published_at"""
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS pages (
 kind TEXT NOT NULL, page INTEGER NOT NULL, rows INTEGER NOT NULL,
 retrieved_at TEXT NOT NULL, raw_file TEXT NOT NULL, PRIMARY KEY(kind,page));
CREATE TABLE IF NOT EXISTS stock_disclosures (
 raw_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
 stock_name TEXT, reason TEXT, window_kind TEXT, period_turnover_cny REAL,
 buy_cny REAL, sell_cny REAL, raw_file TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seat_observations (
 raw_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, trade_date TEXT NOT NULL,
 symbol TEXT NOT NULL, seat_name TEXT, seat_code TEXT, window_kind TEXT,
 window_start TEXT, window_end TEXT, reason TEXT, listed_side TEXT,
 buy_cny REAL, sell_cny REAL, net_cny REAL, period_turnover_cny REAL,
 identity_usable INTEGER NOT NULL, amount_valid INTEGER NOT NULL,
 published_at TEXT, retrieved_at TEXT NOT NULL, raw_file TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS person_seat_links (
 link_id TEXT PRIMARY KEY, person_label TEXT NOT NULL, reported_seat_name TEXT NOT NULL,
 exact_seat_name TEXT, seat_code TEXT, source_url TEXT NOT NULL,
 source_published_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
 evidence_type TEXT NOT NULL CHECK(evidence_type IN ('media_attribution','user_document','official_disclosure')),
 identity_verified INTEGER NOT NULL DEFAULT 0,
 valid_from TEXT, valid_to TEXT, notes TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS private_trade_documents (
 document_id TEXT PRIMARY KEY, local_path TEXT NOT NULL, supplied_by TEXT NOT NULL,
 consent_reference TEXT NOT NULL, identity_status TEXT NOT NULL,
 coverage_start TEXT, coverage_end TEXT, import_status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS seat_lookup ON seat_observations(seat_name,trade_date,symbol);
CREATE INDEX IF NOT EXISTS stock_lookup ON seat_observations(symbol,trade_date);
CREATE INDEX IF NOT EXISTS event_lookup ON seat_observations(event_key);
CREATE INDEX IF NOT EXISTS summary_lookup ON stock_disclosures(symbol,trade_date);
CREATE VIEW IF NOT EXISTS seat_events AS
 SELECT {EVENT_COLUMNS}
 FROM seat_observations GROUP BY event_key;
CREATE VIEW IF NOT EXISTS seat_registry AS
 SELECT seat_name,seat_code,MIN(trade_date) AS first_disclosed,MAX(trade_date) AS last_disclosed,
        COUNT(*) AS raw_listings,COUNT(DISTINCT symbol) AS observed_stocks,
        MIN(identity_usable) AS identity_usable
 FROM seat_observations GROUP BY seat_name,seat_code;
CREATE VIEW IF NOT EXISTS seat_name_variants AS
 SELECT seat_code,COUNT(DISTINCT seat_name) AS name_count,GROUP_CONCAT(DISTINCT seat_name) AS observed_names
 FROM seat_observations WHERE identity_usable=1 GROUP BY seat_code HAVING name_count>1;
DROP VIEW IF EXISTS event_amount_conflicts;
CREATE VIEW event_amount_conflicts AS
 SELECT trade_date,symbol,seat_name,seat_code,COUNT(DISTINCT event_key) AS versions
 FROM seat_observations WHERE identity_usable=1 AND amount_valid=1 AND window_kind='single'
 GROUP BY trade_date,symbol,seat_name,seat_code HAVING versions>1;
DROP VIEW IF EXISTS security_registry;
CREATE VIEW security_registry AS
 SELECT symbol, CASE
 WHEN symbol GLOB '60[0-9][0-9][0-9][0-9].SSE' OR symbol GLOB '688[0-9][0-9][0-9].SSE'
   OR symbol GLOB '00[0-9][0-9][0-9][0-9].SZSE' OR symbol GLOB '30[0-9][0-9][0-9][0-9].SZSE'
   OR symbol GLOB '920[0-9][0-9][0-9].BJ' OR symbol GLOB '[48][0-9][0-9][0-9][0-9][0-9].BJ'
 THEN 'A_share'
 WHEN symbol GLOB '900[0-9][0-9][0-9].SSE' OR symbol GLOB '200[0-9][0-9][0-9].SZSE'
 THEN 'B_share'
 WHEN symbol GLOB '11[0-9][0-9][0-9][0-9].SSE' OR symbol GLOB '12[0-9][0-9][0-9][0-9].SZSE'
 THEN 'convertible_or_exchangeable_bond'
 WHEN symbol='689009.SSE' THEN 'CDR'
 ELSE 'unclassified_requires_review' END AS security_kind
 FROM (SELECT symbol FROM seat_observations UNION SELECT symbol FROM stock_disclosures);
CREATE VIEW IF NOT EXISTS a_share_stock_disclosures AS
 SELECT e.* FROM stock_disclosures e JOIN security_registry s ON e.symbol=s.symbol
 WHERE s.security_kind='A_share' AND NOT (e.symbol LIKE '%.BJ' AND e.trade_date<'2021-11-15');
CREATE VIEW IF NOT EXISTS a_share_seat_observations AS
 SELECT e.* FROM seat_observations e JOIN security_registry s ON e.symbol=s.symbol
 WHERE s.security_kind='A_share' AND NOT (e.symbol LIKE '%.BJ' AND e.trade_date<'2021-11-15');
DROP VIEW IF EXISTS a_share_seat_events;
CREATE VIEW a_share_seat_events AS
 SELECT e.* FROM seat_events e JOIN security_registry s ON e.symbol=s.symbol
 WHERE s.security_kind='A_share' AND NOT (e.symbol LIKE '%.BJ' AND e.trade_date<'2021-11-15');
DROP VIEW IF EXISTS a_share_seat_registry;
CREATE VIEW a_share_seat_registry AS
 SELECT e.seat_name,e.seat_code,MIN(e.trade_date) AS first_disclosed,MAX(e.trade_date) AS last_disclosed,
        COUNT(*) AS raw_listings,COUNT(DISTINCT e.symbol) AS observed_stocks,MIN(e.identity_usable) AS identity_usable
 FROM a_share_seat_observations e GROUP BY e.seat_name,e.seat_code;
"""


def stamp():
    return datetime.now().astimezone().isoformat()


def params_for(kind, page, start, end, stable=True):
    columns = "TRADE_DATE,SECURITY_CODE"
    directions = "-1,1"
    if stable and kind != "summary":
        columns += ",TRADE_ID,OPERATEDEPT_CODE,BUY,SELL"
        directions += ",1,1,1,1"
    return {"reportName": REPORTS[kind], "columns": "ALL",
            "filter": f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')",
            "pageSize": 500, "pageNumber": page,
            "sortColumns": columns, "sortTypes": directions,
            "source": "WEB", "client": "WEB"}


class PublicClient:
    def __init__(self, interval=.5):
        if interval < .5:
            raise ValueError("At most two public requests per second")
        self.interval, self.next_request = interval, 0.
        self.lock, self.local = threading.Lock(), threading.local()

    def cool_down(self):
        self.local.session.close()
        del self.local.session
        with self.lock:
            self.next_request = max(self.next_request, time.monotonic() + 5.)

    def fetch(self, params):
        transport_errors = []
        for attempt in range(2):
            with self.lock:
                delay = max(0., self.next_request - time.monotonic())
                if delay:
                    time.sleep(delay)
                self.next_request = time.monotonic() + self.interval
            if not hasattr(self.local, "session"):
                self.local.session = requests.Session()
            try:
                response = self.local.session.get(URL, params=params, timeout=(10, 45))
            except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as error:
                if attempt:
                    raise
                transport_errors.append(type(error).__name__)
                self.cool_down()
                continue
            if response.status_code in {502, 503, 504} and not attempt:
                transport_errors.append(f"HTTP{response.status_code}")
                response.close()
                self.cool_down()
                continue
            # HTTP denials/rate limits and semantic errors are never retried.
            response.raise_for_status()
            payload = response.json()
            if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
                raise ValueError(f"Source did not return a successful dataset: {payload.get('message')}")
            return {"request": params, "response": payload, "retrieved_at": stamp(), "published_at": None,
                    "transport_retries": transport_errors}


def page_file(root, kind, page):
    return root / "raw" / kind / f"{page:05d}.json.gz"


def fetch_page(client, root, kind, page, plan):
    path = page_file(root, kind, page)
    params = params_for(kind, page, plan["requested_start"], plan["end"],
                        stable=plan.get("pagination_version", 1) >= 2)
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            doc = json.load(stream)
    else:
        doc = client.fetch(params)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
            json.dump(doc, stream, ensure_ascii=False, allow_nan=False)
        temporary.replace(path)
    expected, result = plan["reports"][kind], doc["response"]["result"]
    if doc["request"] != params:
        raise ValueError("Raw page belongs to a different query")
    if int(result["count"]) != expected["rows"] or int(result["pages"]) != expected["pages"]:
        raise ValueError("Source counts changed since discovery; snapshot needs reconciliation")
    expected_rows = min(500, expected["rows"] - (page - 1) * 500)
    if len(result["data"]) != expected_rows:
        raise ValueError("Truncated page or unexpected provider pagination")
    for row in result["data"]:
        if not plan["requested_start"] <= row["TRADE_DATE"][:10] <= plan["end"]:
            raise ValueError("Historical date filter was not honored")
    return kind, page, doc, path


def discover(root, start, end, client):
    root.mkdir(parents=True, exist_ok=True)
    file = root / "plan.json"
    if file.exists():
        plan = json.loads(file.read_text(encoding="utf-8"))
        if plan["requested_start"] != start or plan["end"] != end:
            raise ValueError("Cannot reuse a snapshot directory for another interval")
        return plan
    plan = {"dataset": "A-share public LHB disclosures", "source": "Eastmoney",
            "requested_start": start, "end": end, "discovered_at": stamp(), "reports": {},
            "not_private_trade_history": True, "full_a_share_history_claim": False,
            "pagination_version": 2}
    for kind in REPORTS:
        doc = client.fetch(params_for(kind, 1, start, end))
        result = doc["response"]["result"]
        rows, pages = int(result["count"]), int(result["pages"])
        if not rows or pages != (rows + 499) // 500:
            raise ValueError("Unexpected page cap; inspect provider before scheduling")
        earliest_params = params_for(kind, 1, start, end)
        earliest_params.update(pageSize=1, sortTypes=",".join("1" for _ in earliest_params["sortColumns"].split(",")))
        earliest = client.fetch(earliest_params)
        root.joinpath("discovery").mkdir(exist_ok=True)
        save_json(root / "discovery" / f"{kind}_earliest.json", earliest)
        plan["reports"][kind] = {"rows": rows, "pages": pages,
            "earliest_returned": earliest["response"]["result"]["data"][0]["TRADE_DATE"][:10]}
        path = page_file(root, kind, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump(doc, stream, ensure_ascii=False, allow_nan=False)
    plan["expected_raw_rows"] = sum(x["rows"] for x in plan["reports"].values())
    plan["expected_pages"] = sum(x["pages"] for x in plan["reports"].values())
    save_json(file, plan)
    return plan


def ingest(connection, kind, page, doc, path):
    if connection.execute("SELECT 1 FROM pages WHERE kind=? AND page=?", (kind, page)).fetchone():
        return False
    records = doc["response"]["result"]["data"]
    if kind == "summary":
        values = []
        for n, r in enumerate(records):
            symbol = r["SECUCODE"].replace(".SH", ".SSE").replace(".SZ", ".SZSE")
            reason = r.get("EXPLANATION") or ""
            values.append((f"summary:{page}:{n}", r["TRADE_DATE"][:10], symbol, r.get("SECURITY_NAME_ABBR"),
                           reason, period(reason)[0], r.get("ACCUM_AMOUNT"), r.get("BILLBOARD_BUY_AMT"),
                           r.get("BILLBOARD_SELL_AMT"), str(path)))
        with connection:
            connection.executemany("INSERT INTO stock_disclosures VALUES (?,?,?,?,?,?,?,?,?,?)", values)
            connection.execute("INSERT INTO pages VALUES (?,?,?,?,?)", (kind, page, len(records), doc["retrieved_at"], str(path)))
    else:
        # Normalize each observation without discarding original duplicate/generic listings.
        enriched = [{**r, "_raw_id": f"{kind}:{page}:{n}", "_retrieved_at": doc["retrieved_at"]}
                    for n, r in enumerate(records)]
        values = []
        for raw in observations(enriched if kind == "buy" else [], enriched if kind == "sell" else []):
            r = SimpleNamespace(**raw)
            identity = r.seat_name if r.identity_usable and r.amount_valid else r.raw_id
            key_parts = [r.trade_date, r.symbol, r.window_kind, identity, r.seat_code, str(r.buy_cny), str(r.sell_cny),
                         str(r.period_turnover_cny), r.reason if r.window_kind != "single" else ""]
            event_key = sha256(json.dumps(key_parts, ensure_ascii=False).encode()).hexdigest()
            finite = lambda x: float(x) if pd.notna(x) else None
            values.append((r.raw_id, event_key, r.trade_date, r.symbol, r.seat_name, r.seat_code,
                           r.window_kind, r.window_start, r.window_end, r.reason, kind,
                           finite(r.buy_cny), finite(r.sell_cny), finite(r.net_cny), finite(r.period_turnover_cny),
                           int(r.identity_usable), int(r.amount_valid), None, r.retrieved_at, str(path)))
        with connection:
            connection.executemany("INSERT INTO seat_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            connection.execute("INSERT INTO pages VALUES (?,?,?,?,?)", (kind, page, len(records), doc["retrieved_at"], str(path)))
    return True


def publish_status(connection, root, plan, error=None):
    records = connection.execute("SELECT kind,COUNT(*),SUM(rows),MAX(page) FROM pages GROUP BY kind").fetchall()
    coverage = {k: {"downloaded_pages": int(p), "downloaded_rows": int(r), "highest_page": int(m),
                    "expected_pages": plan["reports"][k]["pages"], "expected_rows": plan["reports"][k]["rows"]}
                for k, p, r, m in records}
    actual = sum(r[2] for r in records)
    complete = actual == plan["expected_raw_rows"] and len(coverage) == len(REPORTS)
    status = {"status": "source_snapshot_complete" if complete else "partial_backfill",
              "updated_at": stamp(), "raw_rows_downloaded": actual, "raw_rows_expected": plan["expected_raw_rows"],
              "coverage": coverage, "error": error,
              "private_trade_documents": connection.execute("SELECT COUNT(*) FROM private_trade_documents").fetchone()[0],
              "all_persons_identified": False, "unlisted_trades_observable": False,
              "exchange_cross_check": "not_complete", "market_price_coverage": "existing_10_stock_daily_pilot_only"}
    save_json(root / "status.json", status)
    return status


def export_catalogs(connection, root):
    destination = root / "catalogs"
    destination.mkdir(exist_ok=True)
    for table in ("seat_registry", "a_share_seat_registry", "seat_name_variants", "person_seat_links", "event_amount_conflicts", "security_registry"):
        pd.read_sql_query(f"SELECT * FROM {table}", connection).to_csv(destination / f"{table}.csv", index=False, encoding="utf-8-sig")
    for group, name in (("=", "stocks_with_seat_details"), ("!=", "non_a_share_inventory")):
        table = "a_share_seat_observations" if group == "=" else "seat_observations"
        pd.read_sql_query(f"""SELECT e.symbol,s.security_kind,MIN(e.trade_date) AS first_observed,MAX(e.trade_date) AS last_observed,
           COUNT(*) AS seat_listings,COUNT(DISTINCT e.seat_name) AS observed_seat_names
           FROM {table} e JOIN security_registry s ON e.symbol=s.symbol
           WHERE s.security_kind {group} 'A_share' GROUP BY e.symbol ORDER BY e.symbol""", connection).to_csv(
               destination / f"{name}.csv", index=False, encoding="utf-8-sig")
    pd.read_sql_query("""SELECT s.symbol,d.names_as_returned,d.first_board_date,d.last_board_date,
        d.board_rows,COALESCE(e.seat_rows,0) downloaded_seat_rows,e.first_seat_date,e.last_seat_date
        FROM security_registry s LEFT JOIN
        (SELECT symbol,GROUP_CONCAT(DISTINCT stock_name) names_as_returned,MIN(trade_date) first_board_date,
         MAX(trade_date) last_board_date,COUNT(*) board_rows FROM a_share_stock_disclosures GROUP BY symbol) d ON d.symbol=s.symbol
        LEFT JOIN (SELECT symbol,COUNT(*) seat_rows,MIN(trade_date) first_seat_date,MAX(trade_date) last_seat_date
                   FROM a_share_seat_observations GROUP BY symbol) e ON e.symbol=s.symbol
        WHERE s.security_kind='A_share' AND (d.symbol IS NOT NULL OR e.symbol IS NOT NULL) ORDER BY s.symbol""", connection).to_csv(
            destination / "stock_inventory.csv", index=False, encoding="utf-8-sig")
    pd.read_sql_query("""SELECT e.seat_name,e.seat_code,
        MIN(e.trade_date) first_observed,MAX(e.trade_date) last_observed,
        COUNT(DISTINCT e.symbol) observed_stocks,
        COUNT(DISTINCT e.trade_date || ':' || e.symbol) disclosed_stock_days,
        COUNT(DISTINCT CASE WHEN e.buy_cny>0 THEN e.trade_date || ':' || e.symbol END) known_buy_stock_days,
        COUNT(DISTINCT CASE WHEN e.sell_cny>0 THEN e.trade_date || ':' || e.symbol END) known_sell_stock_days,
        COUNT(DISTINCT CASE WHEN e.window_kind='single' THEN e.trade_date || ':' || e.symbol END) single_day_stock_days
        FROM a_share_seat_observations e JOIN security_registry s ON e.symbol=s.symbol
        WHERE s.security_kind='A_share' AND e.identity_usable=1 GROUP BY e.seat_name,e.seat_code
        ORDER BY disclosed_stock_days DESC,e.seat_name""", connection).to_csv(
            destination / "seat_disclosure_profiles.csv", index=False, encoding="utf-8-sig")
    pd.read_sql_query("""SELECT kind,trade_date,COUNT(*) raw_rows,COUNT(DISTINCT symbol) instruments
        FROM (SELECT 'summary' kind,trade_date,symbol FROM stock_disclosures
              UNION ALL SELECT listed_side kind,trade_date,symbol FROM seat_observations)
        GROUP BY kind,trade_date ORDER BY trade_date,kind""", connection).to_csv(
            destination / "date_coverage.csv", index=False, encoding="utf-8-sig")
    pd.read_sql_query("""SELECT DISTINCT d.symbol,d.trade_date FROM a_share_stock_disclosures d
        JOIN security_registry s ON d.symbol=s.symbol WHERE s.security_kind='A_share'
        AND NOT EXISTS (SELECT 1 FROM seat_observations e WHERE e.symbol=d.symbol AND e.trade_date=d.trade_date)
        ORDER BY d.symbol,d.trade_date""", connection).to_csv(
            destination / "a_share_stock_days_without_details.csv", index=False, encoding="utf-8-sig")
    pd.read_sql_query("""SELECT * FROM stock_disclosures
        WHERE symbol LIKE '%.BJ' AND trade_date<'2021-11-15' ORDER BY trade_date,symbol""", connection).to_csv(
            destination / "pre_bse_disclosures.csv", index=False, encoding="utf-8-sig")


def quarantine_unverified_currency(connection):
    # Keep original B-share amounts in the raw response; do not label them as CNY.
    with connection:
        connection.execute("""UPDATE seat_observations SET buy_cny=NULL,sell_cny=NULL,net_cny=NULL,
            period_turnover_cny=NULL,amount_valid=0 WHERE symbol IN
            (SELECT symbol FROM security_registry WHERE security_kind='B_share')""")
        connection.execute("""UPDATE stock_disclosures SET buy_cny=NULL,sell_cny=NULL,period_turnover_cny=NULL
            WHERE symbol IN (SELECT symbol FROM security_registry WHERE security_kind='B_share')""")


def import_media_links(connection):
    source = Path(__file__).with_name("seat_media_links.json")
    for item in json.loads(source.read_text(encoding="utf-8")):
        identifier = sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        with connection:
            connection.execute("INSERT OR IGNORE INTO person_seat_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identifier, item["person_label"], item["reported_seat_name"], None, None,
                item["source_url"], item["source_published_at"], stamp(), "media_attribution", 0, None, None,
                "Media label, possibly a group, not a verified person. Abbreviated branch name unresolved; "
                "no account identity or effective dates inferred. " + item.get("notes", "")))


def inspect_dataset(root):
    root = Path(root)
    result = json.loads((root / "status.json").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(f"file:{(root / 'disclosures.sqlite3').as_posix()}?mode=ro", uri=True)) as connection:
        for table in ("stock_disclosures", "seat_observations", "seat_registry", "person_seat_links"):
            result[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        result["observed_security_count"] = connection.execute("SELECT COUNT(DISTINCT symbol) FROM seat_observations").fetchone()[0]
        result["observed_a_share_stock_count"] = connection.execute("SELECT COUNT(DISTINCT symbol) FROM a_share_seat_observations").fetchone()[0]
        result["a_share_seat_observations"] = connection.execute("SELECT COUNT(*) FROM a_share_seat_observations").fetchone()[0]
        result["a_share_seat_names"] = connection.execute("SELECT COUNT(DISTINCT seat_name) FROM a_share_seat_registry").fetchone()[0]
        result["a_share_summary_rows"] = connection.execute("SELECT COUNT(*) FROM a_share_stock_disclosures").fetchone()[0]
        result["a_share_summary_stocks"] = connection.execute("SELECT COUNT(DISTINCT symbol) FROM a_share_stock_disclosures").fetchone()[0]
        result["pre_bse_summary_rows"] = connection.execute("SELECT COUNT(*) FROM stock_disclosures WHERE symbol LIKE '%.BJ' AND trade_date<'2021-11-15'").fetchone()[0]
        result["downloaded_seat_date_range"] = connection.execute("SELECT MIN(trade_date),MAX(trade_date) FROM seat_observations").fetchone()
    return result


def verify_summary(root):
    root = Path(root)
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    expected = plan["reports"]["summary"]
    identities, page_signatures, overlaps, repeated_pages = set(), set(), [], []
    total = 0
    for page in range(1, expected["pages"] + 1):
        if not page_file(root, "summary", page).exists():
            raise ValueError("Stock disclosure layer still has missing pages")
        _, _, doc, _ = fetch_page(None, root, "summary", page, plan)
        keys = []
        for row in doc["response"]["result"]["data"]:
            key = (row["TRADE_DATE"], row["SECURITY_CODE"], row.get("CHANGE_TYPE"), row.get("TRADE_ID"))
            if key in identities:
                overlaps.append({"page": page, "key": key})
            identities.add(key)
            keys.append(key)
            total += 1
        signature = sha256(json.dumps(keys).encode()).hexdigest()
        if signature in page_signatures:
            repeated_pages.append(page)
        page_signatures.add(signature)
    result = {"checked_at": stamp(), "physical_rows": total, "expected_source_rows": expected["rows"],
              "unique_source_identities": len(identities), "repeated_pages": repeated_pages,
              "overlap_rows": len(overlaps), "overlap_examples": overlaps[:20],
              "status": "source_pagination_verified" if total == expected["rows"] and not overlaps and not repeated_pages else "needs_overlap_review",
              "exchange_history_completeness_proven": False}
    save_json(root / "summary_integrity.json", result)
    return result


def export_observations(root, out, seat=None, symbol=None):
    if not seat and not symbol:
        raise ValueError("Specify an exact seat name or stock symbol")
    conditions, values = [], []
    for key, value in (("seat_name", seat), ("symbol", symbol)):
        if value:
            conditions.append(f"{key}=?")
            values.append(value)
    # Every event key includes stock and exact seat identity, so filtering commutes with grouping.
    query = (f"SELECT {EVENT_COLUMNS} FROM a_share_seat_observations WHERE " + " AND ".join(conditions)
             + " GROUP BY event_key ORDER BY trade_date,symbol")
    with closing(sqlite3.connect(f"file:{(Path(root) / 'disclosures.sqlite3').as_posix()}?mode=ro", uri=True)) as connection:
        frame = pd.read_sql_query(query, connection, params=values)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out, index=False, encoding="utf-8-sig")
    return {"rows": len(frame), "path": str(out), "type": "disclosed_seat_events_not_private_trades"}


def reindex(root):
    """Offline parser migration: keep the raw pages and preserve user-supplied provenance."""
    root = Path(root)
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    current = root / "disclosures.sqlite3"
    replacement = root / f"disclosures_reindex_{identifier}.sqlite3"
    connection = sqlite3.connect(replacement)
    connection.executescript(SCHEMA)
    try:
        if current.exists():
            with closing(sqlite3.connect(current, timeout=30)) as old:
                old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                for table in ("person_seat_links", "private_trade_documents"):
                    values = old.execute(f"SELECT * FROM {table}").fetchall()
                    if values:
                        placeholders = ",".join("?" for _ in values[0])
                        with connection:
                            connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", values)
        import_media_links(connection)
        for kind in REPORTS:
            for path in sorted((root / "raw" / kind).glob("*.json.gz")):
                page = int(path.name.split(".")[0])
                k, p, doc, raw_path = fetch_page(None, root, kind, page, plan)
                ingest(connection, k, p, doc, raw_path)
        quarantine_unverified_currency(connection)
        result = publish_status(connection, root, plan)
        export_catalogs(connection, root)
    finally:
        connection.close()
    if current.exists():
        current.replace(root / f"disclosures_before_reindex_{identifier}.sqlite3")
    replacement.replace(current)
    save_json(root / "parser_version.json", {"version": 3, "reindexed_at": stamp(),
        "known_side_retained_when_other_side_missing": True,
        "incomplete_amount_rows_not_merged": True, "seat_code_part_of_event_key": True,
        "network_used": False})
    return result


def build(root=DEFAULT_DATASET, start="1990-01-01", end="2026-09-24", max_requests=120, workers=3, kinds=None):
    if not 1 <= max_requests <= 6000 or not 1 <= workers <= 8:
        raise ValueError("Bounded run requires 1..6000 pages and 1..8 workers; global rate remains <=2/s")
    start, end = iso_day(start), iso_day(end)
    if start > end:
        raise ValueError("Invalid interval")
    root = Path(root)
    kinds = list(REPORTS) if kinds is None else list(dict.fromkeys(kinds))
    if not kinds or any(k not in REPORTS for k in kinds):
        raise ValueError("Unknown source report selection")
    client = PublicClient()
    plan = discover(root, start, end, client)
    if plan.get("pagination_version", 1) < 2:
        raise ValueError("Legacy seat pagination has unstable tie ordering. Use the v2 snapshot; preserve old raw pages.")
    connection = sqlite3.connect(root / "disclosures.sqlite3", timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    import_media_links(connection)
    completed = {(k, p) for k, p in connection.execute("SELECT kind,page FROM pages")}
    # Round-robin pages keeps stock and both seat directions near the same recent interval.
    tasks = [(kind, page) for page in range(1, max(x["pages"] for x in plan["reports"].values()) + 1)
             for kind in kinds if page <= plan["reports"][kind]["pages"] and (kind, page) not in completed]
    cached = [(k, p) for k, p in tasks if page_file(root, k, p).exists()]
    tasks = cached + [(k, p) for k, p in tasks if not page_file(root, k, p).exists()][:max_requests]
    error = None
    processed = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            iterator, pending = iter(tasks), set()
            def submit():
                job = next(iterator, None)
                if job:
                    pending.add(pool.submit(fetch_page, client, root, *job, plan))
            for _ in range(workers):
                submit()
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    kind, page, doc, path = future.result()
                    ingest(connection, kind, page, doc, path)
                    processed += 1
                    if processed % 10 == 0:
                        status = publish_status(connection, root, plan)
                        print(f"Pages this run {processed}/{len(tasks)}; raw rows {status['raw_rows_downloaded']:,}/{status['raw_rows_expected']:,}", flush=True)
                    submit()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"Stopped without bypass/retry: {error}", flush=True)
    finally:
        quarantine_unverified_currency(connection)
        status = publish_status(connection, root, plan, error)
        export_catalogs(connection, root)
        connection.close()
        with (root / "runs.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"finished_at": stamp(), "pages_processed": processed,
                                     "request_budget": max_requests, "workers": workers, "kinds": kinds, **status}, ensure_ascii=False) + "\n")
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--start", default="1990-01-01", type=iso_day)
    parser.add_argument("--end", default="2026-09-24", type=iso_day)
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--kinds", nargs="+", choices=list(REPORTS))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--reindex", action="store_true")
    parser.add_argument("--verify-summary", action="store_true")
    parser.add_argument("--export-seat")
    parser.add_argument("--export-stock")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.verify_summary:
        print(json.dumps(verify_summary(args.root), ensure_ascii=False, indent=2))
        return 0
    if args.reindex:
        print(json.dumps(reindex(args.root), ensure_ascii=False, indent=2))
        return 0
    if args.status:
        print(json.dumps(inspect_dataset(args.root), ensure_ascii=False, indent=2))
        return 0
    if args.export_seat or args.export_stock:
        if not args.out:
            parser.error("Exports require --out")
        print(json.dumps(export_observations(args.root, args.out, args.export_seat, args.export_stock), ensure_ascii=False))
        return 0
    if not args.execute:
        print(json.dumps(vars(args), default=str, ensure_ascii=False))
        print("Plan only; no network or account access.")
        return 0
    result = build(args.root, args.start, args.end, args.max_requests, args.workers, args.kinds)
    return 1 if result["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
