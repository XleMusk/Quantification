"""Read-only inventory of local StockDB files and the pinned 2026 daily slice."""

import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import msgpack
import pandas as pd

from ..settings import ROOT
from ..stockdb_source import DEFAULT_STOCKDB, ENDPOINT
from .experiment import write_json
from .full_market_etf import etf_candidates


DEFAULT_SNAPSHOT = Path(r"D:\QuantificationData\trading\research\full_market\20260928_011314_1aadfbef")
MINUTE_DAYS = ("20260105", "20260630", "20260701", "20260901", "20260924")
PREFIX_END = {"0": "1", "1": "2", "3": "4", "5": "6", "6": "7", "9": "N"}
FIELD_SAMPLE_DAYS = ("20260105", "20260701", "20260924")
FIELD_SAMPLE = ("code", "name", "pb", "pe_ttm", "float_mv", "turnover", "pct_chg")


def request(params, limit=5_000_000, endpoint=ENDPOINT):
    with urlopen(endpoint + "?" + urlencode(params), timeout=25) as response:
        if response.headers.get_content_type() != "application/x-msgpack":
            raise ValueError("Unexpected StockDB response type")
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("StockDB response exceeded inventory limit")
    return msgpack.unpackb(payload, raw=False)


def physical_inventory(root):
    files = [path for path in root.rglob("*") if path.is_file()]
    extensions = Counter(path.suffix.lower() or "(none)" for path in files)
    segments = {}
    for name in ("data", "data1"):
        folder = root / name
        manifest = json.loads((folder / ".sync_manifest.json").read_text(encoding="utf-8"))
        expected = {item["path"]: item["size"] for item in manifest["files"] if item["path"].endswith(".ldb")}
        present = {path.name: path.stat().st_size for path in folder.glob("*.ldb")}
        segments[name] = {
            "ldb_files": len(present),
            "ldb_bytes": sum(present.values()),
            "manifest_ldb_files": len(expected),
            "missing_manifest_ldb": sorted(expected.keys() - present.keys()),
            "unlisted_ldb": sorted(present.keys() - expected.keys()),
            "size_mismatch_ldb": sorted(name for name in expected.keys() & present.keys()
                                        if expected[name] != present[name]),
        }
    return {"file_count": len(files), "bytes": sum(path.stat().st_size for path in files),
            "extensions": dict(sorted(extensions.items())), "segments": segments}


def year_chunks(snapshot):
    manifest_path = snapshot / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("status") != "complete_inventory_only" or len(manifest["chunks"]) != manifest["chunks_expected"]:
        raise ValueError("Incomplete pinned source manifest")
    chunks = [item for item in manifest["chunks"] if item["month"].startswith("2026-")]
    if not chunks:
        raise ValueError("Pinned source has no 2026 chunks")
    for item in chunks:
        path = (snapshot / item["parquet_file"]).resolve()
        path.relative_to(snapshot.resolve())
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != item["parquet_sha256"]:
            raise ValueError(f"2026 chunk hash mismatch: {item['parquet_file']}")
    return sha256(manifest_bytes).hexdigest(), chunks


def summarize_chunks(chunks):
    def total(items):
        return {"chunks": len(items), "rows": sum(item["rows"] for item in items),
                "missing_pct_chg": sum(item["missing_pct_chg"] for item in items),
                "missing_pre_close": sum(item["missing_pre_close"] for item in items),
                "invalid_ohlc": sum(item["invalid_ohlc"] for item in items),
                "first_date": min(item["first_date"] for item in items if item["rows"]),
                "last_date": max(item["last_date"] for item in items if item["rows"])}
    return {"total": total(chunks),
            "by_month": {month: total([item for item in chunks if item["month"] == month])
                         for month in sorted({item["month"] for item in chunks})},
            "by_prefix": {prefix: total([item for item in chunks if item["prefix"] == prefix])
                          for prefix in sorted({item["prefix"] for item in chunks})}}


def fund_coverage(snapshot, chunks, etf_codes):
    parts = []
    for item in chunks:
        if item["prefix"] in {"1", "5"}:
            part = pd.read_parquet(snapshot / item["parquet_file"], columns=["code", "date", "pct_chg"])
            part["pct_chg"] = pd.to_numeric(part["pct_chg"], errors="coerce")
            parts.append(part)
    frame = pd.concat(parts, ignore_index=True)
    marked = frame.loc[frame.code.isin(etf_codes)].copy()
    return {"fund_code_group_rows": len(frame), "fund_code_group_symbols": int(frame.code.nunique()),
            "etf_name_marker_date": "20260924", "etf_name_marker_codes": len(etf_codes),
            "marked_etf_2026_symbols": int(marked.code.nunique()),
            "marked_etf_2026_rows": len(marked),
            "marked_etf_missing_pct_chg": int(marked.pct_chg.isna().sum()),
            "marked_etf_by_month": {
                str(month): {"rows": len(part), "symbols": int(part.code.nunique()),
                             "missing_pct_chg": int(part.pct_chg.isna().sum())}
                for month, part in marked.groupby(marked.date // 100)
            }}


def adjustment_coverage(etf_codes):
    events = []
    for prefix in ("1", "5"):
        rows = request({"cmd": "get", "t": "复权", "k1": f"fwd:{prefix},{PREFIX_END[prefix]}",
                        "k2": "fwd:20260101,20260928"})
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 2 for row in rows):
            raise ValueError("Unexpected adjustment event rows")
        events.extend(rows)
    codes = [key.split(":")[1] for key, _ in events]
    marked = [code for code in codes if code in etf_codes]
    return {"fund_code_events": len(events), "fund_code_event_symbols": len(set(codes)),
            "marked_etf_events": len(marked), "marked_etf_event_symbols": len(set(marked)),
            "examples": [{"key": key, "value": value} for key, value in events
                         if key.split(":")[1] in {"510300", "510500"}]}


def minute_coverage():
    samples = {}
    for day in MINUTE_DAYS:
        samples[day] = {}
        for prefix, upper in PREFIX_END.items():
            keys = request({"cmd": "keys", "t": "分钟k", "k1": f"fwd:{prefix},{upper}",
                            "k2": f"key:{day}145900"}, limit=1_000_000)
            if not isinstance(keys, list):
                raise ValueError("Unexpected minute-key response")
            samples[day][prefix] = len(keys)
    return samples


def field_coverage():
    samples = {}
    for day in FIELD_SAMPLE_DAYS:
        samples[day] = {}
        for prefix, upper in PREFIX_END.items():
            rows = request({"cmd": "vals", "t": "日k", "k1": f"fwd:{prefix},{upper}",
                            "k2": f"key:{day}", "ap": "get." + ",".join(FIELD_SAMPLE)})
            if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(FIELD_SAMPLE)
                                                 for row in rows):
                raise ValueError(f"Unexpected field projection: {prefix} {day}")
            samples[day][prefix] = {"rows": len(rows), "missing": {
                field: sum(row[index] is None for row in rows)
                for index, field in enumerate(FIELD_SAMPLE) if field != "code"}}
    return samples


def inventory(stockdb_root, snapshot):
    manifest_hash, chunks = year_chunks(snapshot)
    names = {}
    for prefix in ("1", "5"):
        rows = request({"cmd": "vals", "t": "日k", "k1": f"fwd:{prefix},{PREFIX_END[prefix]}",
                        "k2": "key:20260924", "ap": "get.code,name"})
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 2 for row in rows):
            raise ValueError("Unexpected 2026 fund-name response")
        names.update({code: name for code, name in rows if isinstance(code, str)})
    etf_codes = set(etf_candidates(list(names.items())))
    catalog = request({"cmd": "get", "t": "股票代码"})
    boards = request({"cmd": "keys", "t": "板块*"}, limit=2_000_000)
    delisted = request({"cmd": "keys", "t": "退市*"}, limit=1_000_000)
    if not isinstance(catalog, dict) or not isinstance(boards, list) or not isinstance(delisted, list):
        raise ValueError("Unexpected local catalog response")
    table_probes = {}
    for name in ("财务", "龙虎榜", "营业部", "基金"):
        keys = request({"cmd": "keys", "t": name + "*", "num": "1"}, limit=10_000)
        if not isinstance(keys, list):
            raise ValueError(f"Unexpected table probe response: {name}")
        table_probes[name] = {"first_key": keys[0] if keys else None,
                              "meaning": "prefix probe only; absence does not prove no differently named table"}
    daily_field_samples = {}
    for code, day in (("600519", "20260105"), ("510300", "20260105"),
                      ("510300", "20260924")):
        row = request({"cmd": "get", "t": f"日k:{code}:{day}"}, limit=10_000)
        if not isinstance(row, dict):
            raise ValueError(f"Expected daily sample is missing: {code} {day}")
        daily_field_samples[f"{code}:{day}"] = {
            "fields": sorted(row), "null_fields": sorted(key for key, value in row.items() if value is None)}
    return {"status": "complete_read_only_inventory", "observed_at": datetime.now().astimezone().isoformat(),
            "stockdb_root": str(stockdb_root), "snapshot": str(snapshot),
            "snapshot_manifest_sha256": manifest_hash, "physical": physical_inventory(stockdb_root),
            "daily_2026": summarize_chunks(chunks),
            "current_catalog_prefix_counts": {key: len(value) for key, value in catalog.items()},
            "board_records": len(boards), "delisted_sequence_records": len(delisted),
            "other_table_prefix_probes": table_probes, "daily_field_samples": daily_field_samples,
            "funds_2026": fund_coverage(snapshot, chunks, etf_codes),
            "adjustments_2026": adjustment_coverage(etf_codes),
            "minute_1459_samples": minute_coverage(), "field_samples": field_coverage(),
            "execution_admitted": False, "returns_validated": False,
            "limitations": [
                "The pinned 2026 snapshot ends 2026-09-24; later daily bars are outside this audit",
                "Current ETF name marker is not point-in-time membership or exchange certification",
                "Local adjustment events have not been independently reconciled with fund distributions and splits",
                "Minute samples are cross-sectional spot checks, not a full minute-data census",
                "This inventory does not validate ETF total returns or authorize account orders",
            ]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stockdb-root", type=Path, default=DEFAULT_STOCKDB)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs" / "full_market" / "stockdb_2026_inventory.json")
    args = parser.parse_args(argv)
    try:
        result = inventory(args.stockdb_root, args.snapshot)
    except (URLError, TimeoutError, ConnectionError) as error:
        # Keep a read-only audit failure explicit and machine-readable.  A
        # missing local service is a data blocker, never an empty inventory or
        # permission to fall back to a different source.
        result = {
            "status": "stockdb_service_unavailable",
            "observed_at": datetime.now().astimezone().isoformat(),
            "stockdb_root": str(args.stockdb_root.resolve()),
            "snapshot": str(args.snapshot.resolve()),
            "error_type": type(error).__name__,
            "error": str(error),
            "execution_admitted": False,
            "returns_validated": False,
            "limitations": [
                "StockDB service could not be reached; no inventory or coverage is inferred",
                "No fallback provider, snapshot promotion or order path was used",
            ],
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.out, result)
    if result["status"] == "stockdb_service_unavailable":
        print(json.dumps({"out": str(args.out), "status": result["status"],
                          "error_type": result["error_type"],
                          "execution_admitted": False}, ensure_ascii=False))
        return 2
    print(json.dumps({"out": str(args.out), "rows_2026": result["daily_2026"]["total"]["rows"],
                      "marked_etf_codes": result["funds_2026"]["marked_etf_2026_symbols"],
                      "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
