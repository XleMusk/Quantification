"""One bounded anonymous BaoStock historical status-list probe, never a feed.

The returned tradeStatus is provider evidence for that exact requested date.
It cannot create suspended-price bars, establish exchange/PIT membership, or
resolve a different session's missing quote. No retry or installation occurs.
"""

import argparse
import csv
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import socket
import sys
import time

import pandas as pd

from a_stock_committee.market_data import baostock_session
from .daily_source_probe import WHEEL_SHA256, select_sdk
from ..settings import DATA_ROOT


EXPECTED_FIELDS = ["code", "tradeStatus", "code_name"]
MISSING_JAN05 = ["sz.002055", "sz.300091", "sz.300658"]
TARGETS = [*MISSING_JAN05, "sz.000670", "sz.300087"]


def probe(day: str, sdk_wheel: Path, destination: Path, *, _sdk_info=None, _provider_documentation=None) -> dict:
    day = date.fromisoformat(day).isoformat()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    receipt = {
        "schema": "baostock_historical_all_stock_status_probe_v1",
        "status": "running", "day": day,
        "method": "query_all_stock", "kwargs": {"day": day},
        "logical_query_attempts": 0, "socket_timeout_seconds": 30, "retries": 0,
        "anonymous_market_data_session_only": True, "broker_login_attempted": False,
        "official_sdk_expected_fields": EXPECTED_FIELDS,
        "requested_missing_same_day_codes": MISSING_JAN05 if day == "2026-01-05" else [],
        "additional_same_day_observations_for_other_session_missing_codes": ["sz.000670", "sz.300087"],
        "source_sdk_function": "baostock.metadata.stock_metadata.query_all_stock",
        "source_documentation_status": "official SDK inspected; online method-specific field semantics were not independently recovered",
        "trade_status_semantics_verified_with_exchange": False,
        "source_verified": False, "pit_verified": False, "price_bars_generated": False,
        "stockdb_modified": False, "ledger_mutated": False, "model_refit": False,
        "signal_admitted": False, "execution_admitted": False,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    receipt_path = destination / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows, fields, response_day = [], [], None
    old_timeout = socket.getdefaulttimeout()
    started = time.monotonic()
    try:
        receipt["sdk"] = _sdk_info if _sdk_info is not None else select_sdk(sdk_wheel)
        if receipt["sdk"]["sha256"] != WHEEL_SHA256:
            raise ValueError("Batch SDK metadata does not identify the pinned official wheel")
        socket.setdefaulttimeout(30)
        with baostock_session() as provider:
            if not callable(getattr(provider, "query_all_stock", None)):
                raise ValueError("Pinned SDK lacks query_all_stock")
            receipt["logical_query_attempts"] = 1
            result = provider.query_all_stock(day=day)
            receipt.update(provider_error_code=result.error_code, provider_error_msg=result.error_msg)
            fields = list(result.fields)
            response_day = getattr(result, "day", None)
            if result.error_code != "0":
                raise RuntimeError(f"BaoStock status list query failed: {result.error_code} {result.error_msg}")
            while result.next():
                rows.append(result.get_row_data())
            if result.error_code != "0":
                raise RuntimeError(f"BaoStock status list pagination failed: {result.error_code} {result.error_msg}")
        receipt["status"] = "completed_provider_status_list"
    except (OSError, RuntimeError, ValueError) as error:
        receipt.update(status="provider_probe_failed_no_retry", error_type=type(error).__name__, error=str(error))
    finally:
        socket.setdefaulttimeout(old_timeout)
    raw_path = destination / f"query_all_stock_{day}.csv"
    # Preserve malformed partial rows too; analysis must not leave a failed
    # acquisition looking like a running receipt.
    with raw_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields if fields else EXPECTED_FIELDS)
        writer.writerows(rows)
    issues = []
    try:
        raw = pd.DataFrame(rows, columns=fields if fields else EXPECTED_FIELDS)
    except ValueError as error:
        raw = pd.DataFrame(columns=EXPECTED_FIELDS)
        issues.append("malformed_row_width_raw_preserved")
        receipt.update(status="provider_probe_failed_no_retry", error_type=type(error).__name__, error=str(error))
    if set(fields) != set(EXPECTED_FIELDS):
        issues.append("response_schema_unknown_or_changed")
    if response_day != day:
        issues.append("response_day_mismatch")
    if raw.empty:
        issues.append("empty_status_list")
    if set(EXPECTED_FIELDS) <= set(raw):
        if raw["code"].duplicated().any():
            issues.append("duplicate_provider_code")
        if not raw["tradeStatus"].isin(["0", "1"]).all():
            issues.append("unknown_trade_status_values")
    query_ok = receipt["status"] == "completed_provider_status_list" and not issues
    observations = []
    for code in TARGETS:
        matches = raw.loc[raw["code"].eq(code)] if "code" in raw else raw.iloc[:0]
        unique = query_ok and len(matches) == 1
        value = str(matches.iloc[0]["tradeStatus"]) if unique else None
        observations.append({
            "day": day, "code": code, "provider_row_observed": bool(unique),
            "tradeStatus_raw": value,
            "code_name_raw": str(matches.iloc[0]["code_name"]) if unique else None,
            "provider_zero_status_observed": value == "0" if unique else None,
            "provider_one_status_observed": value == "1" if unique else None,
            "explains_same_day_stockdb_missing": "provider_status_only_no_synthetic_bar_or_exchange_claim" if unique else "unknown",
            "other_date_status_inferred": False,
        })
    receipt.update(
        completed_at=datetime.now().astimezone().isoformat(),
        elapsed_seconds=round(time.monotonic() - started, 3), fields_returned=fields,
        response_day=response_day, rows_preserved=len(rows), raw_csv=str(raw_path),
        raw_sha256=sha256(raw_path.read_bytes()).hexdigest(),
        csv_empty_header_only=raw.empty, csv_header_basis="returned SDK fields" if fields else "empty diagnostic header; provider returned no schema",
        quality_issues=issues, target_observations=observations,
        historical_bounded_collection_feasible=query_ok,
        historical_collection_status="single_day_only_feasibility_not_all_history_coverage" if query_ok else "provider_failed_or_unusable_do_not_start_bulk_retry_loop",
        future_interface="One explicit historical day per protected anonymous session using pinned SDK; persist raw lists/status receipts; stop on provider/schema/session failure. Do not infer current-list membership backward.",
        limitations=[
            "BaoStock is the market-data provider, not an exchange-approved historical eligibility master.",
            "Status list code/name/tradeStatus does not provide OHLC, share actions, T+1 availability or fill evidence.",
            "Status0 alone does not authorize filling a missing StockDB bar; raw nontrading marking needs an explicit reviewed adapter later.",
            "A2026-01-05 response does not establish2026-01-06 status for000670/300087/300658.",
            "A single successful day cannot prove complete historical membership or stable service capacity.",
        ],
    )
    if _provider_documentation is not None:
        receipt.update(provider_documentation=_provider_documentation, provider_semantics_verified=True,
                       source_documentation_status="official method documentation pinned separately;1 normal trading/0 suspended, provider-level only")
    else:
        receipt["provider_semantics_verified"] = False
    if receipt["status"] == "completed_provider_status_list" and issues:
        receipt["status"] = "completed_status_list_with_quality_issues_not_usable"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (destination / "target_status_observations.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def load_cached_day(folder: Path, day: str, *, allow_legacy_existing_evidence=False) -> tuple[dict, pd.DataFrame, dict]:
    """Resume exact successful schema/date/hash; corrupt or failed data is not reacquired."""
    folder = Path(folder).resolve()
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    if (receipt.get("schema") != "baostock_historical_all_stock_status_probe_v1"
            or receipt.get("day") != day or receipt.get("response_day") != day
            or receipt.get("method") != "query_all_stock" or receipt.get("kwargs") != {"day": day}
            or receipt.get("status") != "completed_provider_status_list" or receipt.get("quality_issues") != []
            or receipt.get("provider_error_code") != "0" or receipt.get("fields_returned") != EXPECTED_FIELDS
            or receipt.get("sdk", {}).get("sha256") != WHEEL_SHA256
            or receipt.get("execution_admitted") is not False or receipt.get("price_bars_generated") is not False):
        raise ValueError("Existing receipt schema/day/source/status is not an exact successful status batch")
    raw_path = Path(receipt["raw_csv"]).resolve()
    if raw_path.parent != folder:
        raise ValueError("Existing raw CSV is outside its declared day folder")
    actual_hash = sha256(raw_path.read_bytes()).hexdigest()
    expected_hash = receipt.get("raw_sha256")
    if expected_hash is None and not allow_legacy_existing_evidence:
        raise ValueError("Existing receipt has no acquisition raw hash; refuse resume/reacquisition")
    if expected_hash is not None and actual_hash != expected_hash:
        raise ValueError("Existing raw CSV hash mismatch; refuse resume/reacquisition")
    raw = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
    if (list(raw) != EXPECTED_FIELDS or len(raw) != receipt["rows_preserved"] or raw.empty
            or raw["code"].duplicated().any() or not raw["tradeStatus"].isin(["0", "1"]).all()):
        raise ValueError("Existing raw CSV schema/count/status validation failed")
    verification = {
        "day": day, "original_receipt": str(folder / "receipt.json"), "raw_csv": str(raw_path),
        "verified_current_raw_sha256": actual_hash,
        "raw_hash_origin": "acquisition_receipt_match" if expected_hash else "post_acquisition_pin_now_original_receipt_unmodified",
        "original_acquisition_hash_verified": expected_hash is not None,
        "verified_at": datetime.now().astimezone().isoformat(), "rows": len(raw),
        "reacquired": False, "execution_admitted": False,
    }
    return receipt, raw, verification


def _documentation_pin(folder: Path | None) -> dict | None:
    if folder is None:
        return None
    folder = Path(folder).resolve()
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    path = folder / "StockBasicInfoAPI.md"
    if sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
        raise ValueError("Official provider documentation hash mismatch")
    compact = "".join(path.read_text(encoding="utf-8").split())
    if "tradeStatus|交易状态，其中1：正常交易，0：停牌" not in compact:
        raise ValueError("Pinned official documentation does not carry the reviewed tradeStatus semantics")
    return {"public_url": receipt["source"], "retrieval_endpoint": receipt["retrieval_endpoint"],
            "sha256": receipt["sha256"], "local_document": str(path),
            "tradeStatus": {"1": "normal_trading", "0": "suspended"},
            "provider_semantics_verified": True, "exchange_semantics_verified": False, "pit_verified": False}


def _provider_code(symbol: str) -> str:
    symbol = str(symbol).upper()
    if symbol.endswith(".SSE"):
        return "sh." + symbol.split(".")[0]
    if symbol.endswith(".SZSE"):
        return "sz." + symbol.split(".")[0]
    raise ValueError(f"Unexpected raw-missing symbol: {symbol}")


def reconcile_missing(day: str, raw: pd.DataFrame, exact: dict | None, preview: dict | None) -> tuple[list[dict], dict]:
    missing = exact["missing_symbols"] if exact is not None else (preview.get("missing_symbols_first20", "").split("|") if preview is not None else [])
    missing = [symbol for symbol in missing if symbol]
    first20 = [symbol for symbol in preview.get("missing_symbols_first20", "").split("|") if symbol] if preview else []
    if preview and exact is not None:
        if int(preview["missing_count"]) != len(missing) or first20 != missing[:20]:
            raise ValueError(f"Truncated preview/exact dated missing coverage disagree on {day}")
    source = raw.set_index("code")
    rows = []
    for symbol in missing:
        code = _provider_code(symbol)
        observed = code in source.index
        value = source.loc[code, "tradeStatus"] if observed else None
        rows.append({
            "day": day, "symbol": symbol, "provider_code": code, "provider_row_observed": observed,
            "tradeStatus_raw": value, "code_name_raw_for_query_day": source.loc[code, "code_name"] if observed else None,
            "classification": "provider_status0_suspended" if value == "0" else "provider_status1_normal_trading_missing_price" if value == "1" else "no_provider_row_unknown",
            "in_truncated_preview_first20": symbol in first20,
            "price_bar_created": False, "exchange_or_pit_eligibility_admitted": False,
        })
    facts = {
        "day": day, "missing_list_scope": "exact_external_uncapped_coverage" if exact is not None else "truncated_preview_only_unknown_remainder",
        "missing_codes_compared": len(rows), "preview_first20_count": len(first20),
        "preview_missing_count": int(preview["missing_count"]) if preview else None,
        "preview_truncated": bool(preview and int(preview["missing_count"]) > len(first20)),
        "preview_day_available": preview is not None, "exact_coverage_day_available": exact is not None,
        "provider_status0": sum(row["tradeStatus_raw"] == "0" for row in rows),
        "provider_status1_missing_prices": sum(row["tradeStatus_raw"] == "1" for row in rows),
        "no_provider_row_unknown": sum(not row["provider_row_observed"] for row in rows),
        "reconciles_price_coverage": False,
    }
    return rows, facts


def collect(days, sdk_wheel: Path, destination: Path, *, blocker_csv: Path | None = None,
            exact_coverage: Path | None = None, existing_evidence=(), documentation_root: Path | None = None) -> dict:
    days = [date.fromisoformat(day).isoformat() for day in days]
    if not days or len(days) > 10 or len(set(days)) != len(days):
        raise ValueError("Explicit unique status days must contain1..10 dates")
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    previews = {}
    if blocker_csv is not None:
        preview = pd.read_csv(blocker_csv, dtype=str, keep_default_na=False)
        if preview["day"].duplicated().any():
            raise ValueError("Duplicate blocker preview date")
        previews = {row["day"]: row for row in preview.to_dict("records")}
    exact = {}
    if exact_coverage is not None:
        observations = json.loads(Path(exact_coverage).read_text(encoding="utf-8"))
        if len({row["day"] for row in observations}) != len(observations):
            raise ValueError("Duplicate exact coverage date")
        exact = {row["day"]: row for row in observations}
    result = {
        "schema": "baostock_bounded_historical_status_batch_v1", "status": "running",
        "requested_days": days, "max_days": 10, "sequential_protected_sessions": True, "socket_timeout_seconds": 30,
        "retries": 0, "entries": [], "existing_evidence": [], "reconciliation": [],
        "blocker_preview": str(blocker_csv) if blocker_csv else None, "exact_coverage": str(exact_coverage) if exact_coverage else None,
        "source_verified": False, "pit_verified": False, "stockdb_modified": False, "ledger_mutated": False,
        "price_bars_created": False, "signal_admitted": False, "execution_admitted": False,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    rows = []
    stopped = False
    try:
        result["provider_documentation"] = _documentation_pin(documentation_root)
        for folder in existing_evidence:
            original = json.loads((Path(folder) / "receipt.json").read_text(encoding="utf-8"))
            day = original["day"]
            if day in days:
                raise ValueError("Legacy evidence inclusion is separate from requested new/resume days")
            _, frame, verification = load_cached_day(folder, day, allow_legacy_existing_evidence=True)
            result["existing_evidence"].append(verification)
            compared, facts = reconcile_missing(day, frame, exact.get(day), previews.get(day))
            facts["examples_first20"] = compared[:20]
            rows.extend(compared)
            result["reconciliation"].append(facts)
        sdk = None
        for day in days:
            folder = destination / day
            try:
                if folder.exists():
                    receipt, frame, verification = load_cached_day(folder, day)
                    entry = {"day": day, "status": "resumed_exact_schema_day_sdk_hash_raw_hash", "verification": verification,
                             "logical_queries_this_run": 0, "receipt": str(folder / "receipt.json"), "rows": len(frame)}
                else:
                    if sdk is None:
                        sdk = select_sdk(sdk_wheel)
                    receipt = probe(day, sdk_wheel, folder, _sdk_info=sdk, _provider_documentation=result["provider_documentation"])
                    entry = {"day": day, "status": receipt["status"], "logical_queries_this_run": receipt["logical_query_attempts"],
                             "receipt": str(folder / "receipt.json"), "rows": receipt["rows_preserved"], "elapsed_seconds": receipt["elapsed_seconds"]}
                    if not receipt["historical_bounded_collection_feasible"]:
                        result["entries"].append(entry)
                        result["status"] = "stopped_on_first_provider_failure_or_unusable_day_no_retry"
                        stopped = True
                        break
                    receipt, frame, verification = load_cached_day(folder, day)
                    entry["verification"] = verification
                compared, facts = reconcile_missing(day, frame, exact.get(day), previews.get(day))
                facts["examples_first20"] = compared[:20]
                rows.extend(compared)
                result["reconciliation"].append(facts)
                result["entries"].append(entry)
                print(json.dumps({"day": day, "status": entry["status"], "rows": entry["rows"], "missing_status_counts": {key: value for key, value in facts.items() if key != "examples_first20"}}, ensure_ascii=False), flush=True)
            except (OSError, ValueError, KeyError) as error:
                result["entries"].append({"day": day, "status": "existing_or_source_evidence_invalid_no_reacquisition",
                                         "error_type": type(error).__name__, "error": str(error)})
                result["status"] = "stopped_on_first_evidence_failure_no_retry"
                stopped = True
                break
        if not stopped:
            result["status"] = "completed_bounded_explicit_status_days"
    except (OSError, ValueError, KeyError) as error:
        result.update(status="stopped_on_evidence_or_sdk_setup_failure_no_retry", error_type=type(error).__name__, error=str(error))
    frame = pd.DataFrame(rows, columns=["day", "symbol", "provider_code", "provider_row_observed", "tradeStatus_raw", "code_name_raw_for_query_day", "classification", "in_truncated_preview_first20", "price_bar_created", "exchange_or_pit_eligibility_admitted"])
    examples = frame.groupby("day", sort=False, group_keys=False).head(20)
    examples.to_csv(destination / "missing_price_status_reconciliation_first20.csv", index=False, encoding="utf-8-sig")
    frequent = []
    for symbol, subset in frame.groupby("symbol", sort=False):
        frequent.append({"symbol": symbol, "pilot_missing_days": len(subset),
                         "status0_days": int(subset["tradeStatus_raw"].eq("0").sum()),
                         "status1_missing_price_days": int(subset["tradeStatus_raw"].eq("1").sum()),
                         "no_provider_row_days": int((~subset["provider_row_observed"].astype(bool)).sum()),
                         "dated_name_and_status_observations": subset[["day", "code_name_raw_for_query_day", "tradeStatus_raw"]].to_dict("records")})
    frequent.sort(key=lambda row: (-row["pilot_missing_days"], row["symbol"]))
    result.update(
        completed_at=datetime.now().astimezone().isoformat(),
        completed_requested_days=sum(entry["status"] in {"completed_provider_status_list", "resumed_exact_schema_day_sdk_hash_raw_hash"} for entry in result["entries"]),
        logical_queries_this_run=sum(entry.get("logical_queries_this_run", 0) for entry in result["entries"]),
        not_attempted_days=[day for day in days if day not in {entry["day"] for entry in result["entries"]}],
        missing_comparison_rows=len(frame), comparison_examples_persisted=len(examples),
        missing_counts_use_exact_full_list_when_available=True, top_frequent_missing_codes=frequent[:20],
        scope="Only explicit pilot dates plus separately referenced January5 evidence; not all183 decisions",
        conclusion="Status0 may justify later explicit nontrading coverage review. Status1 missing prices and absent provider rows remain unresolved. No status creates OHLC or PIT eligibility.",
    )
    (destination / "batch_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (destination / "existing_evidence_hash_pins.json").write_text(json.dumps(result["existing_evidence"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["batch"]:  # Retain the first internal form without a second parser.
        argv = argv[1:]
    parser = argparse.ArgumentParser(description=__doc__)
    dates = parser.add_mutually_exclusive_group(required=True)
    dates.add_argument("--day")
    dates.add_argument("--days", nargs="+")
    parser.add_argument("--sdk-wheel", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--blocker-csv", type=Path)
    parser.add_argument("--exact-coverage", type=Path)
    parser.add_argument("--existing-evidence", nargs="*", type=Path, default=[])
    parser.add_argument("--documentation-root", type=Path)
    args = parser.parse_args(argv)
    if args.days is not None:
        out = args.out or DATA_ROOT / "research/status_source_probe" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_bounded_status_dates")
        result = collect(args.days, args.sdk_wheel, out, blocker_csv=args.blocker_csv,
                         exact_coverage=args.exact_coverage, existing_evidence=args.existing_evidence,
                         documentation_root=args.documentation_root)
        print(json.dumps({"status": result["status"], "completed_requested_days": result["completed_requested_days"],
                          "logical_queries_this_run": result["logical_queries_this_run"], "result": str(out / "batch_result.json")}, ensure_ascii=False))
        return 0 if result["status"] == "completed_bounded_explicit_status_days" else 2
    out = args.out or DATA_ROOT / "research/status_source_probe" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_all_stock_" + args.day.replace("-", ""))
    result = probe(args.day, args.sdk_wheel, out, _provider_documentation=_documentation_pin(args.documentation_root))
    print(json.dumps({"status": result["status"], "day": result["day"], "logical_query_attempts": result["logical_query_attempts"],
                      "rows_preserved": result["rows_preserved"], "elapsed_seconds": result["elapsed_seconds"],
                      "receipt": str(out / "receipt.json"), "bulk_feasible": result["historical_bounded_collection_feasible"]}, ensure_ascii=False))
    return 0 if result["historical_bounded_collection_feasible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
