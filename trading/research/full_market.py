"""Pin a read-only StockDB daily-bar inventory for broad-universe research.

This is a source audit, not a HistoricalFeed or an executable backtest dataset.
"""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
import uuid

import msgpack
import pandas as pd

from ..settings import DATA_ROOT
from ..stockdb_source import ENDPOINT


PREFIX_END = {"0": "1", "1": "2", "3": "4", "5": "6", "6": "7", "9": "N"}
FIELDS = ("code", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "is_st", "pre_close")
VALUATION_FIELDS = ("pb", "pe_ttm", "float_mv", "total_mv", "turnover")
MAX_RESPONSE_BYTES = 30_000_000


def month_intervals(start, end):
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    if first > last:
        raise ValueError("start must not follow end")
    for period in pd.period_range(first, last, freq="M"):
        lower = max(first, period.start_time).strftime("%Y%m%d")
        upper = min(last, period.end_time).strftime("%Y%m%d")
        yield str(period), lower, upper


def query(prefix, lower, upper, endpoint=ENDPOINT, fields=FIELDS):
    if prefix not in PREFIX_END or len(lower) != 8 or len(upper) != 8 or lower > upper:
        raise ValueError("Invalid StockDB daily-bar query")
    params = {"cmd": "vals", "t": "日k", "k1": f"fwd:{prefix},{PREFIX_END[prefix]}",
              "k2": f"fwd:{lower},{upper}", "ap": "get." + ",".join(fields)}
    url = endpoint + "?" + urlencode(params)
    with urlopen(url, timeout=45) as response:
        if response.headers.get_content_type() != "application/x-msgpack":
            raise ValueError("Unexpected StockDB response type")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("StockDB response exceeded per-chunk limit")
    rows = msgpack.unpackb(payload, raw=False)
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(fields) for row in rows):
        raise ValueError("Unexpected StockDB projected-row shape")
    return payload, rows


def inspect_rows(rows, prefix, lower, upper, fields=FIELDS):
    frame = pd.DataFrame(rows, columns=fields)
    if frame.empty:
        return frame, {"rows": 0, "symbols": 0, "sessions": 0, "invalid_ohlc": 0,
                       "missing_pct_chg": 0, "missing_pre_close": 0, "nonpositive_volume": 0}
    code = frame.code.astype(str)
    if not code.str.fullmatch(r"\d{6}").all() or not code.str.startswith(prefix).all():
        raise ValueError("StockDB returned a code outside its requested prefix")
    dates = pd.to_numeric(frame.date, errors="raise").astype("int64")
    if not dates.between(int(lower), int(upper)).all():
        raise ValueError("StockDB returned a date outside its requested month")
    if frame.duplicated(["code", "date"]).any():
        raise ValueError("Duplicate code/date rows in a StockDB chunk")
    prices = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    valid = prices.notna().all(axis=1) & prices.gt(0).all(axis=1)
    valid &= prices.high.ge(prices[["open", "low", "close"]].max(axis=1))
    valid &= prices.low.le(prices[["open", "high", "close"]].min(axis=1))
    volume = pd.to_numeric(frame.volume, errors="coerce")
    amount = pd.to_numeric(frame.amount, errors="coerce")
    valid &= volume.ge(0) & amount.ge(0)
    return frame, {"rows": len(frame), "symbols": code.nunique(), "sessions": dates.nunique(),
                   "first_date": int(dates.min()), "last_date": int(dates.max()),
                   "invalid_ohlc": int((~valid).sum()),
                   "missing_pct_chg": int(frame.pct_chg.isna().sum()),
                   "missing_pre_close": int(frame.pre_close.isna().sum()),
                   "nonpositive_volume": int(volume.le(0).sum())}


def write_manifest(path, manifest):
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def verify_chunks(folder, manifest, raw=False):
    if manifest.get("status") != "complete_inventory_only" or len(manifest.get("chunks", [])) != manifest.get("chunks_expected"):
        raise ValueError("Incomplete full-market inventory")
    for item in manifest["chunks"]:
        names = (("parquet_file", "parquet_sha256"), ("raw_file", "raw_sha256")) if raw else (("parquet_file", "parquet_sha256"),)
        for path_key, hash_key in names:
            path = (folder / item[path_key]).resolve()
            path.relative_to(folder.resolve())
            if not path.is_file() or sha256(path.read_bytes()).hexdigest() != item[hash_key]:
                raise ValueError(f"Snapshot chunk hash mismatch: {item[path_key]}")


def collect(start, end, destination=None, prefixes=None, endpoint=ENDPOINT, *, valuation_fields=False):
    prefixes = tuple(prefixes or PREFIX_END)
    if not prefixes or any(prefix not in PREFIX_END for prefix in prefixes) or len(set(prefixes)) != len(prefixes):
        raise ValueError("Invalid code-prefix selection")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    folder = Path(destination) if destination else DATA_ROOT / "research" / "full_market" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    intervals = list(month_intervals(start, end))
    fields = FIELDS + VALUATION_FIELDS if valuation_fields else FIELDS
    manifest = {"status": "running", "source": "local StockDB HTTP/MessagePack, read-only",
                "endpoint": endpoint, "created_at": datetime.now().astimezone().isoformat(),
                "start": start, "end": end, "prefixes": prefixes, "fields": fields,
                "chunks_expected": len(prefixes) * len(intervals), "chunks": [],
                "execution_admitted": False, "strategy_validated": False,
                "limitations": [
                    "Source coverage and instrument classification are not independently exchange-reconciled",
                    "Historical membership, delistings and corporate actions are not validated",
                    "pre_close may be adjusted inconsistently with raw OHLC; do not use it for execution",
                    "pct_chg and ETF distributions/splits require cross-source validation before returns are claimed",
                    "Fund code groups include ETFs, LOFs and other products; ETF identity is not established by prefix",
                    "This inventory must not be promoted to HistoricalFeed or live trading automatically",
                    "Optional valuation/size/turnover are retrieval-time provider fields, not revision-safe financial PIT",
                ]}
    write_manifest(folder / "manifest.json", manifest)
    try:
        for period, lower, upper in intervals:
            for prefix in prefixes:
                payload, rows = query(prefix, lower, upper, endpoint, fields)
                frame, checks = inspect_rows(rows, prefix, lower, upper, fields)
                stem = f"{period}_{prefix}"
                raw_path = folder / f"{stem}.msgpack"
                parquet_path = folder / f"{stem}.parquet"
                raw_path.write_bytes(payload)
                frame.to_parquet(parquet_path, index=False)
                manifest["chunks"].append({"month": period, "prefix": prefix,
                                           "lower_inclusive": lower, "upper_inclusive": upper,
                                           "raw_file": raw_path.name, "raw_sha256": sha256(payload).hexdigest(),
                                           "parquet_file": parquet_path.name,
                                           "parquet_sha256": sha256(parquet_path.read_bytes()).hexdigest(),
                                           **checks})
                write_manifest(folder / "manifest.json", manifest)
                print(f"{stem}: {checks['rows']} rows, {checks['symbols']} symbols, {checks['invalid_ohlc']} invalid", flush=True)
        manifest["summary"] = {
            "rows": sum(chunk["rows"] for chunk in manifest["chunks"]),
            "invalid_ohlc": sum(chunk["invalid_ohlc"] for chunk in manifest["chunks"]),
            "missing_pct_chg": sum(chunk["missing_pct_chg"] for chunk in manifest["chunks"]),
            "missing_pre_close": sum(chunk["missing_pre_close"] for chunk in manifest["chunks"]),
            "chunks": len(manifest["chunks"]),
        }
        manifest["status"] = "complete_inventory_only"
    except Exception as error:
        manifest["status"] = "failed_partial_inventory"
        manifest["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        write_manifest(folder / "manifest.json", manifest)
    return folder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-09-24")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--prefixes", default=",".join(PREFIX_END))
    parser.add_argument("--valuation-fields", action="store_true")
    args = parser.parse_args(argv)
    print(collect(args.start, args.end, args.destination, args.prefixes.split(","),
                  valuation_fields=args.valuation_fields), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
