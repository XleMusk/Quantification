"""Preserve the original 2023 BaoStock daily strings and nullable statuses.

This is a local observed-history adapter, not a canonical alias projection,
historical dated roster, adjusted-price builder or strategy admission.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from ..market_dataset import write_json
from .baostock_daily_slice import digest

SCHEMA = "baostock_native_string_history_v1"
METHOD_ID = "original_baostock_2023_daily_strings_nullable_status_v1"
RAW_FIELDS = ("date", "code", "open", "high", "low", "close", "preclose", "volume", "amount",
    "adjustflag", "turn", "tradestatus", "pctChg", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM", "isST")
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "model_refit": False, "future_labels_used_for_score": False, "provider_contacted": False,
    "network_accessed": False, "orders_generated": False, "ledger_mutated": False,
    "ths_payload_generated": False, "account_accessed": False,
    "pit_verified": False, "corporate_actions_verified": False, "historical_dated_roster_verified": False,
    "historical_publication_verified": False, "source_snapshots_modified": False,
    "raw_prices_imputed": False, "status_imputed": False, "alias_projection_applied": False,
    "adjusted_prices_generated": False, "model_scored": False, "engine_advanced": False}


def _now():
    return datetime.now().astimezone()


def _time(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("The source cache must retain its actual timezone-aware completion clock")
    return stamp


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Native history needs canonical exact source dates")
    return value


def _calendar(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["date"]:
            raise ValueError("The pinned historical calendar must retain its date-only CSV schema")
        days = [_day(row["date"]) for row in reader]
    if not days or days != sorted(set(days)):
        raise ValueError("Pinned calendar dates must be sorted and unique")
    return [day for day in days if "2023-01-01" <= day <= "2023-12-31"]


def _number(value):
    if value == "":
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _status(token, *, st=False):
    if token not in {"0", "1"}:
        return None, "unknown_missing_or_unrecognized_source_token"
    if st:
        return int(token), "known_ST" if token == "1" else "known_non_ST"
    return int(token), "known_active" if token == "1" else "known_suspended"


def build(quality_receipt, out):
    """Read only the exact 2023 files registered by the supplied saved receipt."""
    out, quality_path = Path(out).resolve(), Path(quality_receipt).resolve()
    if out.exists():
        raise FileExistsError("Native history requires a new output directory; existing artifacts are not replaced")
    started = _now()
    quality_hash = digest(quality_path)
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    if (quality.get("schema") != "stockdb_baostock_extension_quality_v1"
            or quality.get("status") != "partial_backfill" or quality.get("quality_gate") != "diagnostic_only"
            or quality.get("start") != "2023-01-01" or quality.get("end") != "2023-12-31"
            or any(quality.get(key) is not False for key in
                ("execution_admitted", "future_labels_used", "model_refit", "ledger_mutated", "ths_payload_generated"))):
        raise ValueError("Select the original unadmitted diagnostic 2023 extension quality receipt")
    root, calendar = Path(quality["source_root"]).resolve(), Path(quality["calendar"]).resolve()
    calendar_hash, days = digest(calendar), _calendar(calendar)
    if (not days or len(days) != quality["expected_sessions"] or len(days) != quality["sessions"]
            or (quality.get("first_date") is not None and days[0] != quality["first_date"])
            or (quality.get("last_date") is not None and days[-1] != quality["last_date"])):
        raise ValueError("The quality receipt and exact 2023 calendar sessions disagree")
    plan_path, receipt_path = root / "plan.json", root / "receipts.json"
    plan_hash, receipt_hash = digest(plan_path), digest(receipt_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    source_calendar = Path(plan["calendar_source"]).resolve()
    if (plan.get("execution_admitted") is not False or receipt.get("execution_admitted") is not False
            or plan.get("historical_universe_independently_verified") is not False or plan.get("kind") != "stock"
            or receipt.get("status") != "source_cache_complete"
            or digest(source_calendar) != plan["calendar_sha256"] or _calendar(source_calendar) != days):
        raise ValueError("A completed source cache and unchanged matching source/extension calendars are required")
    planned = plan["days"]
    if (planned != sorted(set(planned)) or receipt.get("expected_days") != len(planned)
            or [day for day in planned if "2023-01-01" <= day <= "2023-12-31"] != days):
        raise ValueError("Every requested 2023 session must belong to the unchanged original source plan")
    source_available = _time(receipt["updated_at"])
    if source_available > started:
        raise ValueError("The source cache completion clock is in the future")
    tasks = receipt["tasks"]
    sources = []
    for day in days:
        task = tasks.get(day)
        if (not isinstance(task, dict) or task.get("date") != day or task.get("kind") != "stock"
                or task.get("method") != "query_daily_history_k_AStock" or task.get("error_code") != "0"
                or task.get("file") != day + "_stock.csv" or not task.get("sha256")):
            raise ValueError(f"Missing or incomplete registered original source task: {day}")
        path = root / (day + "_stock.csv")
        if not path.is_file():
            raise ValueError(f"Missing exact original source file: {day} {path}")
        sources.append((day, path, task))

    native_schema = pa.schema([(field, pa.string()) for field in RAW_FIELDS + ("symbol", "source_file")])
    status_schema = pa.schema([(field, pa.string()) for field in
        ("date", "code", "symbol", "source_file", "tradestatus_raw", "isST_raw")]
        + [("tradestatus_value", pa.int8()), ("isST_value", pa.int8()),
           ("tradestatus_known", pa.bool_()), ("isST_known", pa.bool_()),
           ("tradestatus_reason", pa.string()), ("isST_reason", pa.string())])
    counts = Counter({key: 0 for key in ("rows", "known_active_rows", "known_suspended_rows",
        "unknown_trade_status_rows", "known_ST_rows", "known_non_ST_rows", "unknown_ST_rows")})
    symbols, proofs = set(), []
    alias_dates = {"sz.302132": set(), "sz.300114": set()}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + out.name + ".native-", dir=out.parent) as temporary:
        stage = Path(temporary)
        with pq.ParquetWriter(stage / "native_daily.parquet", native_schema, compression="zstd") as writer, \
                pq.ParquetWriter(stage / "nullable_status.parquet", status_schema, compression="zstd") as status_writer:
            for day, path, task in sources:
                raw = path.read_bytes()
                raw_hash = sha256(raw).hexdigest()
                if raw_hash != task["sha256"]:
                    raise ValueError(f"Original source task hash changed: {day} {path}")
                rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
                if not rows or rows[0] != list(RAW_FIELDS):
                    raise ValueError(f"Original 18-field source header changed: {day} {path}")
                native, statuses, seen = [], [], set()
                for values in rows[1:]:
                    if len(values) != len(RAW_FIELDS):
                        raise ValueError(f"Malformed original 18-field row: {day} {path}")
                    row = dict(zip(RAW_FIELDS, values))
                    code = row["code"]
                    if row["date"] != day or not re.fullmatch(r"(?:sh|sz)\.[0-9]{6}", code) or row["adjustflag"] != "3":
                        raise ValueError(f"Source date/code/raw-adjustment identity changed: {day} {code}")
                    if code in seen:
                        raise ValueError(f"Duplicate original code/date row: {day} {code}")
                    seen.add(code)
                    symbol = code[3:] + (".SSE" if code.startswith("sh.") else ".SZSE")
                    symbols.add(symbol)
                    if code in alias_dates:
                        alias_dates[code].add(day)
                    trade, trade_reason = _status(row["tradestatus"])
                    st, st_reason = _status(row["isST"], st=True)
                    native.append({**row, "symbol": symbol, "source_file": str(path)})
                    statuses.append({"date": day, "code": code, "symbol": symbol, "source_file": str(path),
                        "tradestatus_raw": row["tradestatus"], "isST_raw": row["isST"],
                        "tradestatus_value": trade, "isST_value": st,
                        "tradestatus_known": trade is not None, "isST_known": st is not None,
                        "tradestatus_reason": trade_reason, "isST_reason": st_reason})
                    counts["unknown_trade_status_rows" if trade is None else "known_active_rows" if trade else "known_suspended_rows"] += 1
                    counts["unknown_ST_rows" if st is None else "known_ST_rows" if st else "known_non_ST_rows"] += 1
                    numeric = {key: _number(row[key]) for key in
                        ("open", "high", "low", "close", "preclose", "volume", "amount", "pctChg", "turn")}
                    missing_market = any(numeric[key] is None for key in numeric if key != "turn")
                    counts["missing_or_nonfinite_market_rows"] += int(missing_market)
                    counts["known_suspended_missing_market_rows"] += int(trade == 0 and missing_market)
                    counts["missing_or_nonfinite_turn_rows"] += int(numeric["turn"] is None)
                    counts["negative_volume_values_preserved"] += int(numeric["volume"] is not None and numeric["volume"] < 0)
                    counts["negative_amount_values_preserved"] += int(numeric["amount"] is not None and numeric["amount"] < 0)
                    counts["nonpositive_price_values_preserved"] += sum(
                        int(numeric[key] is not None and numeric[key] <= 0) for key in ("open", "high", "low", "close", "preclose"))
                    counts["negative_valuation_values_preserved"] += sum(
                        int(value is not None and value < 0) for value in (_number(row[key]) for key in ("peTTM", "pbMRQ", "psTTM", "pcfNcfTTM")))
                if (not native or len(native) != task["quality"]["rows"]
                        or len(seen) != task["quality"]["symbols"]):
                    raise ValueError(f"Original source task row/symbol inventory differs: {day}")
                writer.write_table(pa.Table.from_pylist(native, schema=native_schema))
                status_writer.write_table(pa.Table.from_pylist(statuses, schema=status_schema))
                counts["rows"] += len(native)
                proofs.append({"day": day, "source_file": str(path), "sha256": raw_hash,
                    "rows": len(native), "symbols": len(seen), "source_task": task,
                    "source_available_at": source_available.isoformat(),
                    "source_clock_basis": "original_whole_cache_terminal_updated_at_conservative_not_fabricated_per_call"})
        if (counts["rows"] != quality["rows"] or counts["rows"] != quality["source_rows"]
                or len(symbols) != quality["symbols"]):
            raise ValueError("Original 2023 row/symbol totals differ from the supplied quality receipt; no alias projection is permitted")
        if (digest(quality_path) != quality_hash or digest(plan_path) != plan_hash or digest(receipt_path) != receipt_hash
                or digest(calendar) != calendar_hash or digest(source_calendar) != plan["calendar_sha256"]):
            raise ValueError("Native source, receipt or calendar changed during adaptation")
        failures = receipt.get("failures", [])
        scoped_failures = [item for item in failures if "2023-01-01" <= item.get("query", {}).get("date", "") <= "2023-12-31"]
        alias = {"mapping_description": "302132.SZSE -> 300114.SZSE is comparison-only; original source identities are retained",
            "comparison_only": True, "projection_applied": False, "deduplication_applied": False,
            "source_rows_by_code": {code: len(value) for code, value in alias_dates.items()},
            "overlap_session_count": len(alias_dates["sz.302132"] & alias_dates["sz.300114"]),
            "overlap_sessions": sorted(alias_dates["sz.302132"] & alias_dates["sz.300114"])}
        quality_report = {"schema": "baostock_native_observed_history_quality_v1", "rows": counts["rows"],
            "symbols": len(symbols), "sessions": len(days), "status_counts": dict(counts),
            "source_quality_status": quality["status"], "source_quality_receipt": quality,
            "source_failure_history": failures, "requested_range_failure_history": scoped_failures,
            "alias_comparison": alias, "missing_or_negative_values_preserved_not_repaired": True,
            "coverage_interpretation": "observed_rows_without_historical_dated_roster_or_PIT_membership",
            **FLAGS}
        write_json(stage / "quality_report.json", quality_report)
        native_hash, status_hash = digest(stage / "native_daily.parquet"), digest(stage / "nullable_status.parquet")
        created = _now()
        if created < started:
            raise ValueError("Actual native-history completion clock moved backwards")
        manifest = {"schema": SCHEMA, "method_id": METHOD_ID,
            "status": "completed_native_observed_history_not_admitted", "created_at": created.isoformat(),
            "available_at": created.isoformat(), "started_at": started.isoformat(),
            "start": quality["start"], "end": quality["end"], "first_date": days[0], "last_date": days[-1],
            "rows": counts["rows"], "symbols": len(symbols), "sessions": len(days), "days": days,
            "native_daily_file": str(out / "native_daily.parquet"), "native_daily_sha256": native_hash,
            "status_file": str(out / "nullable_status.parquet"), "status_sha256": status_hash,
            "raw_fields": list(RAW_FIELDS), "raw_fields_storage": "original_csv_strings_including_empty_tokens",
            "source_quality_receipt": str(quality_path), "source_quality_receipt_sha256": quality_hash,
            "source_quality_status": quality["status"], "source_root": str(root),
            "source_plan": str(plan_path), "source_plan_sha256": plan_hash,
            "source_receipt": str(receipt_path), "source_receipt_sha256": receipt_hash,
            "source_receipt_status": receipt["status"], "source_available_at": source_available.isoformat(),
            "quality_receipt_original_available_at": None,
            "calendar": str(calendar), "calendar_sha256": calendar_hash,
            "source_calendar": str(source_calendar), "source_calendar_sha256": plan["calendar_sha256"],
            "source_files": proofs, "source_files_read": len(proofs),
            "unrequested_source_dates_read": 0, "source_failure_history": failures,
            "requested_range_failure_history": scoped_failures, "alias_comparison": alias,
            "quality_report_file": str(out / "quality_report.json"), "quality_report_sha256": digest(stage / "quality_report.json"),
            "price_basis": "provider_adjustflag_3_unadjusted_strings_no_price_volume_or_amount_scaling",
            "corporate_action_coverage": "unknown_not_empty_or_known_zero", "status_counts": dict(counts),
            "coverage_interpretation": "observed_rows_without_historical_dated_roster_or_PIT_membership", **FLAGS}
        write_json(stage / "manifest.json", manifest)
        stage.rename(out)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality-receipt", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build(args.quality_receipt, args.out)
    print(json.dumps({key: result[key] for key in ("schema", "status", "created_at", "rows", "symbols", "sessions", "execution_admitted")}
        | {"manifest": str(args.out.resolve() / "manifest.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
