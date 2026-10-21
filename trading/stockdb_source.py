"""Read-only inventory and cross-source checks for the local StockDB service."""

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import urlopen

import msgpack
import pandas as pd

from .market_dataset import write_json


DEFAULT_STOCKDB = Path(
    r"C:\Users\13944\Desktop\free-stockdb-windows-v0.3.5-more-power\stockdb"
)
DEFAULT_REFERENCE = Path(
    r"D:\QuantificationData\trading\datasets\ashare_2023_20260924"
)
ENDPOINT = "http://127.0.0.1:7899/"
SYMBOL_PATTERN = re.compile(r"^(?P<code>\d{6})\.(?:SSE|SZSE)$")
FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "amount")
SNAPSHOT_SCHEMA = "stockdb_research_snapshot_v1"


def local_get(key):
    url = ENDPOINT + "?" + urlencode({"cmd": "get", "t": key})
    with urlopen(url, timeout=5) as response:
        if response.headers.get_content_type() != "application/x-msgpack":
            raise ValueError("Unexpected StockDB response type")
        payload = response.read(1_000_001)
    if len(payload) > 1_000_000:
        raise ValueError("StockDB response exceeds the audit limit")
    return msgpack.unpackb(payload, raw=False)


def source_bar(symbol, day, *, _get=None):
    match = SYMBOL_PATTERN.fullmatch(symbol)
    if not match:
        raise ValueError(f"Expected six-digit SSE/SZSE symbol: {symbol}")
    compact = pd.Timestamp(day).strftime("%Y%m%d")
    value = (_get or local_get)(f"日k:{match.group('code')}:{compact}")
    if value == []:
        return None
    if not isinstance(value, dict) or value.get("code") != match.group("code") or value.get("date") != int(compact):
        raise ValueError(f"Unexpected StockDB daily bar: {symbol} {day}")
    return value


def source_minute(symbol, day, time="150000", *, _get=None):
    match = SYMBOL_PATTERN.fullmatch(symbol)
    if not match or not re.fullmatch(r"\d{6}", time):
        raise ValueError("Expected SSE/SZSE symbol and HHMMSS time")
    compact = pd.Timestamp(day).strftime("%Y%m%d") + time
    value = (_get or local_get)(f"分钟k:{match.group('code')}:{compact}")
    if value == []:
        return None
    if not isinstance(value, dict) or value.get("code") != match.group("code") or value.get("date") != int(compact):
        raise ValueError(f"Unexpected StockDB minute bar: {symbol} {compact}")
    return value


def reference_bars(root, symbol, dates):
    path = root / "raw" / f"{symbol}.parquet"
    if not path.exists():
        return {}
    frame = pd.read_parquet(path)
    frame = frame.loc[frame.date.isin(dates)]
    if frame.date.duplicated().any():
        raise ValueError(f"Duplicate reference dates for {symbol}")
    return {row["date"]: row for row in frame.to_dict("records")}


def bar_consistency(source):
    if source is None:
        return {"status": "unavailable"}
    fields = ("low", "high", "volume", "amount")
    if any(source.get(key) is None for key in fields):
        return {"status": "incomplete", "missing_fields": [key for key in fields if source.get(key) is None]}
    values = {key: float(source[key]) for key in fields}
    if (not all(math.isfinite(value) for value in values.values())
            or values["low"] <= 0 or values["high"] < values["low"]
            or values["volume"] < 0 or values["amount"] < 0):
        return {"status": "invalid_values"}
    if values["volume"] == 0:
        return {"status": "inactive_no_vwap" if values["amount"] == 0 else "invalid_values"}
    vwap = values["amount"] / values["volume"]
    within = values["low"] - .011 <= vwap <= values["high"] + .011
    return {"status": "within_ohlc_not_unit_verification" if within else "amount_volume_outside_ohlc",
            "amount_divided_by_volume": vwap, "assumption": "amount RMB / volume shares",
            "unit_verified": False, "automatic_conversion": False}


def compare_bar(source, reference):
    consistency = bar_consistency(source)
    if source is None or reference is None:
        return {"source_available": source is not None, "reference_available": reference is not None,
                "stockdb_internal_consistency": consistency}
    aliases = {"pre_close": "preclose"}
    differences, missing, invalid = {}, [], []
    for field in FIELDS:
        a, b = source.get(field), reference.get(aliases.get(field, field))
        if a is None or b is None:
            missing.append(field)
            continue
        a, b = float(a), float(b)
        if not math.isfinite(a) or not math.isfinite(b):
            invalid.append(field)
            continue
        tolerance = 0.02 if field in ("open", "high", "low", "close", "pre_close") else 0 if field == "volume" else max(1000, abs(b) * 0.001)
        if abs(a - b) > tolerance:
            differences[field] = {"stockdb": a, "baostock": b, "difference": round(a - b, 4)}
    return {"source_available": True, "reference_available": True, "differences": differences,
            "missing_fields": missing, "invalid_fields": invalid, "stockdb_internal_consistency": consistency}


def audit(stockdb_root, reference_root, symbols, dates):
    code_master = local_get("股票代码")
    if not isinstance(code_master, dict) or not all(isinstance(v, list) for v in code_master.values()):
        raise ValueError("Unexpected StockDB code catalog")
    folders = {}
    for name in ("data", "data1"):
        path = stockdb_root / name / ".sync_manifest.json"
        if path.exists():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            folders[name] = {"file_count": len(manifest["files"]), "generated_at_unix": manifest["generated_at"]}
    observations = []
    minute_samples = []
    for symbol in symbols:
        references = reference_bars(reference_root, symbol, dates)
        for day in dates:
            source = source_bar(symbol, day)
            comparison = compare_bar(source, references.get(day))
            observations.append({"symbol": symbol, "date": day, **comparison})
        minute = source_minute(symbol, max(dates))
        minute_samples.append({"symbol": symbol, "date": max(dates), "time": "15:00:00",
                               "available": minute is not None, "row": minute})
    paired = [row for row in observations if row["source_available"] and row["reference_available"]]
    return {
        "source": "local StockDB HTTP/MessagePack",
        "observed_at": datetime.now().astimezone().isoformat(),
        "stockdb_root": str(stockdb_root),
        "reference_root": str(reference_root),
        "local_endpoint": ENDPOINT,
        "catalog_entries_by_prefix": {k: len(v) for k, v in code_master.items()},
        "catalog_entries_include_non_A_instruments": True,
        "sync_manifests": folders,
        "comparison_tolerances": {"price_rmb": 0.02, "volume_shares": 0, "amount_rmb": "max(1000, 0.1%)"},
        "summary": {
            "paired_daily_rows": len(paired),
            "rows_with_ohlc_difference": sum(any(k in row["differences"] for k in ("open", "high", "low", "close")) for row in paired),
            "rows_with_pre_close_difference": sum("pre_close" in row["differences"] for row in paired),
            "rows_with_volume_difference": sum("volume" in row["differences"] for row in paired),
            "rows_with_missing_comparison_fields": sum(bool(row["missing_fields"]) for row in paired),
            "rows_with_invalid_comparison_fields": sum(bool(row["invalid_fields"]) for row in paired),
            "rows_with_amount_volume_outside_ohlc": sum(
                row["stockdb_internal_consistency"]["status"] == "amount_volume_outside_ohlc" for row in observations),
            "minute_rows_found": sum(row["available"] for row in minute_samples),
        },
        "observations": observations,
        "minute_samples": minute_samples,
        "execution_admitted": False,
        "limitations": [
            "StockDB pre_close may be adjusted while OHLC is raw; do not use it for execution or returns",
            "Volume units can differ across dates; amount/volume diagnostics never apply automatic scaling",
            "The catalog count is not a point-in-time A-share universe",
            "These sampled comparisons do not validate full history, corporate actions or provider rights",
            "No StockDB fields are promoted to HistoricalFeed or a live account",
        ],
    }


def _snapshot_row(symbol, day, value):
    row = {"symbol": symbol, "date": pd.Timestamp(day).strftime("%Y-%m-%d")}
    row.update({field: value.get(field) for field in FIELDS})
    row["code"] = value["code"]
    return row


def _sha256(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_snapshot(stockdb_root, output_root, symbols, dates, minute_time="150000",
                    *, session_dates=None, calendar_basis=None, catalog_prefixes=None):
    """Persist a point-in-time StockDB extract without promoting it to a feed.

    The snapshot keeps missing rows explicit and preserves raw provider fields.
    It is intentionally not compatible with HistoricalFeed admission: source
    semantics, corporate actions and PIT coverage still require separate review.
    """
    stockdb_root = Path(stockdb_root).resolve()
    output_root = Path(output_root).resolve()
    if output_root.exists():
        raise FileExistsError(f"Snapshot output must be a new directory: {output_root}")
    if minute_time and not re.fullmatch(r"\d{6}", minute_time):
        raise ValueError("minute_time must be HHMMSS")
    symbols = list(dict.fromkeys(symbols))
    requested_dates = [pd.Timestamp(day).strftime("%Y-%m-%d") for day in dates]
    dates = requested_dates if session_dates is None else [
        pd.Timestamp(day).strftime("%Y-%m-%d") for day in session_dates
    ]
    if any(day not in requested_dates for day in dates):
        raise ValueError("session_dates must be a subset of dates")
    for symbol in symbols:
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"Expected six-digit SSE/SZSE symbol: {symbol}")

    prefixes = list(dict.fromkeys(catalog_prefixes or []))
    if catalog_prefixes is not None and not prefixes:
        raise ValueError("At least one explicit catalog prefix is required")
    output_root.mkdir(parents=True, exist_ok=False)
    daily_path = output_root / "daily.csv"
    availability_path = output_root / "availability.csv"
    minute_path = output_root / "minute.csv"
    decoded_path = output_root / "decoded_daily_records.jsonl"
    minute_decoded_path = output_root / "decoded_minute_records.jsonl"
    receipt_path = output_root / "capture_receipt.json"
    daily_rows, availability, minute_rows = [], [], []
    representation = "decoded_messagepack_payload_json_not_original_transport_bytes"
    receipt = {
        "schema": "stockdb_snapshot_capture_receipt_v1", "status": "capturing",
        "capture_started_at": datetime.now().astimezone().isoformat(), "completed_at": None,
        "snapshot_published": False, "endpoint": ENDPOINT, "requested_symbols": symbols,
        "requested_dates": requested_dates, "effective_session_dates": dates,
        "catalog_prefixes": prefixes if catalog_prefixes is not None else None,
        "decoded_daily_records": 0, "decoded_minute_records": 0, "decoded_catalog_records": 0,
        "transport_failures": 0, "last_query": None, "automatic_retry": False,
        "payload_representation": representation, "original_msgpack_bytes_preserved": False,
        "catalog_is_dated_PIT_roster": False, "execution_admitted": False,
    }
    write_json(receipt_path, receipt)

    def write_tables():
        # The legacy projection and its original units remain unchanged. On a
        # failed capture these are explicitly partial and have no snapshot.json.
        pd.DataFrame(daily_rows, columns=["symbol", "date", "code", *FIELDS]).to_csv(
            daily_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(availability, columns=["symbol", "date", "status"]).to_csv(
            availability_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(minute_rows, columns=["symbol", "date", "time", "code",
                                           "open", "high", "low", "close", "volume", "amount"]).to_csv(
            minute_path, index=False, encoding="utf-8-sig")

    def capture_get(key, kind, stream=None, symbol=None, day=None):
        record = {"kind": kind, "query_key": key, "symbol": symbol, "date": day,
                  "request_started_at": datetime.now().astimezone().isoformat(),
                  "payload_representation": representation}
        receipt["last_query"] = {k: v for k, v in record.items() if k != "payload_representation"}
        try:
            value = local_get(key)
        except (Exception, KeyboardInterrupt) as error:
            record.update(status="request_failed_no_retry", response_decoded_at=None,
                          request_finished_at=datetime.now().astimezone().isoformat(),
                          error_type=type(error).__name__, error=str(error))
            receipt["transport_failures"] += 1
            if stream is not None:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
            else:
                write_json(output_root / "catalog_capture.json", record)
            raise
        record.update(status="decoded", response_decoded_at=datetime.now().astimezone().isoformat(),
                      payload=value)
        if stream is not None:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
        else:
            write_json(output_root / "catalog_capture.json", record)
        receipt["decoded_" + kind + "_records"] += 1
        receipt["last_query"]["response_decoded_at"] = record["response_decoded_at"]
        return value

    catalog_capture = None
    try:
        with decoded_path.open("x", encoding="utf-8", newline="\n") as decoded, \
                minute_decoded_path.open("x", encoding="utf-8", newline="\n") as minute_decoded:
            if catalog_prefixes is not None:
                catalog = capture_get("股票代码", "catalog")
                if (not isinstance(catalog, dict) or not all(isinstance(v, list) for v in catalog.values())
                        or any(prefix not in catalog for prefix in prefixes)):
                    raise ValueError("catalog-prefixes require the original StockDB code catalog")
                codes = [code for prefix in prefixes for code in catalog[prefix]]
                if any(not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) for code in codes):
                    raise ValueError("Catalog security codes must remain six original string digits")
                symbols = list(dict.fromkeys(code + (".SSE" if code.startswith("6") else ".SZSE")
                                            for code in codes))
                receipt["requested_symbols"] = symbols
                catalog_capture = {
                    "path": "catalog_capture.json", "sha256": _sha256(output_root / "catalog_capture.json"),
                    "entries_by_prefix": {key: len(value) for key, value in catalog.items()},
                    "selected_prefixes": prefixes, "selected_entries": len(codes),
                    "selected_unique_symbols": len(symbols), "dated_PIT_roster_verified": False,
                    "payload_representation": representation,
                }
                write_json(receipt_path, receipt)
            for symbol in symbols:
                for day in dates:
                    value = source_bar(symbol, day, _get=lambda key: capture_get(key, "daily", decoded, symbol, day))
                    status = "available" if value is not None else "missing"
                    availability.append({"symbol": symbol, "date": day, "status": status})
                    if value is not None:
                        daily_rows.append(_snapshot_row(symbol, day, value))
                        minute = source_minute(symbol, day, minute_time,
                            _get=lambda key: capture_get(key, "minute", minute_decoded, symbol, day)) if minute_time else None
                        if minute is not None:
                            minute_rows.append({"symbol": symbol, "date": day, "time": minute_time,
                                "code": minute["code"], **{key: minute.get(key) for key in
                                    ("open", "high", "low", "close", "volume", "amount")}})
                    if len(availability) % 250 == 0:
                        write_json(receipt_path, receipt)
                        print(json.dumps({"stockdb_snapshot_progress": len(availability),
                            "requested": len(symbols) * len(dates), "available": len(daily_rows),
                            "actual_at": datetime.now().astimezone().isoformat()}), flush=True)
        write_tables()
        manifests = {}
        for name in ("data", "data1"):
            path = stockdb_root / name / ".sync_manifest.json"
            if path.exists():
                manifests[name] = json.loads(path.read_text(encoding="utf-8"))
        captured_at = datetime.now().astimezone().isoformat()
        metadata = {
        "schema": SNAPSHOT_SCHEMA,
        "captured_at": captured_at,
        "source": "local StockDB HTTP/MessagePack",
        "endpoint": ENDPOINT,
        "stockdb_root": str(stockdb_root),
        "requested_symbols": symbols,
        "requested_dates": requested_dates,
        "effective_session_dates": dates,
        "non_trading_dates": [day for day in requested_dates if day not in dates],
        "calendar_basis": str(calendar_basis) if calendar_basis else None,
        "minute_time": minute_time or None,
        "rows": {
            "requested_daily": len(availability),
            "available_daily": len(daily_rows),
            "missing_daily": sum(row["status"] == "missing" for row in availability),
            "available_minute": len(minute_rows),
        },
        "files": {
            "daily": {"path": "daily.csv", "sha256": _sha256(daily_path)},
            "availability": {"path": "availability.csv", "sha256": _sha256(availability_path)},
            "minute": {"path": "minute.csv", "sha256": _sha256(minute_path)},
            "decoded_daily": {"path": decoded_path.name, "sha256": _sha256(decoded_path),
                              "records": receipt["decoded_daily_records"]},
            "decoded_minute": {"path": minute_decoded_path.name, "sha256": _sha256(minute_decoded_path),
                               "records": receipt["decoded_minute_records"]},
        },
        "capture_started_at": receipt["capture_started_at"],
        "capture_receipt": "capture_receipt.json", "capture_complete": True,
        "decoded_payload_representation": representation,
        "original_msgpack_bytes_preserved": False, "catalog_capture": catalog_capture,
        "sync_manifests": {
            name: {"generated_at": value.get("generated_at"), "file_count": len(value.get("files", []))}
            for name, value in manifests.items()
        },
        "research_only": True,
        "execution_admitted": False,
        "historical_feed_promoted": False,
        "limitations": [
            "Raw StockDB fields are preserved; no automatic unit conversion is applied",
            "pre_close, corporate actions and PIT semantics require independent review",
            "The requested symbol set is not a point-in-time A-share universe",
            "This snapshot does not provide broker/account/order/fill evidence",
        ],
        }
        receipt.update(status="completed_snapshot_capture", completed_at=captured_at,
                       snapshot_published=True, rows=metadata["rows"])
        write_json(receipt_path, receipt)
        # This is the successful publication boundary; partial folders have no
        # snapshot.json and must not be consumed as completed snapshots.
        write_json(output_root / "snapshot.json", metadata)
        return metadata
    except (Exception, KeyboardInterrupt) as error:
        receipt.update(status="partial_snapshot_capture_stopped_no_retry", snapshot_published=False,
                       completed_at=datetime.now().astimezone().isoformat(),
                       error_type=type(error).__name__, error=str(error),
                       rows={"available_daily": len(daily_rows), "queried_daily": len(availability),
                             "missing_daily": sum(row["status"] == "missing" for row in availability)})
        write_tables()
        write_json(receipt_path, receipt)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only local StockDB sample audit")
    parser.add_argument("--stockdb-root", type=Path, default=DEFAULT_STOCKDB)
    parser.add_argument("--reference-root", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--symbols", default="600519.SSE,000001.SZSE")
    parser.add_argument("--dates", default="2023-01-03,2023-12-20,2024-01-02,2025-01-02,2026-09-24")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--minute-time", default="150000", help="Minute sample HHMMSS; use empty to skip")
    parser.add_argument("--session-dates", help="Optional effective exchange-session dates, comma-separated")
    parser.add_argument("--calendar-basis", help="Optional calendar manifest or source path recorded in the snapshot")
    parser.add_argument("--catalog-prefixes", help="Freeze codes from the local catalog, e.g. 0,3,6")
    parser.add_argument("--freeze", action="store_true", help="Write a research-only frozen snapshot")
    args = parser.parse_args(argv)
    prefixes = ([item.strip() for item in args.catalog_prefixes.split(",") if item.strip()]
                if args.catalog_prefixes is not None else None)
    if args.catalog_prefixes and not args.freeze:
        catalog = local_get("股票代码")
        if not prefixes or any(prefix not in catalog for prefix in prefixes):
            raise ValueError("catalog-prefixes must name prefixes present in the local StockDB catalog")
        symbols = [code + (".SSE" if code.startswith("6") else ".SZSE")
                   for prefix in prefixes for code in catalog[prefix]]
    elif args.catalog_prefixes:
        symbols = []  # The one catalog call is captured inside the new snapshot.
    else:
        symbols = [item.strip() for item in args.symbols.split(",")]
    dates = [pd.Timestamp(item.strip()).strftime("%Y-%m-%d") for item in args.dates.split(",")]
    if args.freeze:
        output_root = args.out
        if output_root is None:
            output_root = (Path(r"D:\QuantificationData\trading\research\stockdb_snapshots") /
                           ("stockdb_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")))
        session_dates = ([item.strip() for item in args.session_dates.split(",") if item.strip()]
                         if args.session_dates else None)
        report = freeze_snapshot(args.stockdb_root, output_root, symbols, dates, args.minute_time,
                                 session_dates=session_dates, calendar_basis=args.calendar_basis,
                                 catalog_prefixes=prefixes)
    else:
        report = audit(args.stockdb_root, args.reference_root, symbols, dates)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out and not args.freeze:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
