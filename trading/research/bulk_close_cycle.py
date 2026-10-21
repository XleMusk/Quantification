"""One bounded closed-day bulk/dated-roster/import cycle; no automatic retries."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from ..market_dataset import write_json
from ..settings import DATA_ROOT
from . import baostock_all_stock_probe as roster_api
from . import baostock_bulk_close as importer
from . import daily_source_probe as daily
from .baostock_daily_slice import digest
from .baostock_full_close import CN, ORDINARY, SCHEMA as FULL_CLOSE_SCHEMA, historical_closed_scope
from .close_workbench import _process_start, process_identity


SCHEMA = "bounded_bulk_close_cycle_v1"
PROVIDER_UPDATE_SOURCE_URL = "https://www.baostock.com/helpdocs/api/markdown/home.md"
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "ledger_mutated": False,
    "stockdb_modified": False, "orders_generated": False, "ths_payload_generated": False,
    "broker_login_attempted": False, "automatic_retry_enabled": False}
TERMINAL_OLD = {"complete_full_dated_close_inventory_not_admitted", "partial_budget_checkpoint_not_full_close",
                "prepared_new_recovery_generation", "health_passed_before_explicit_recovery"}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _active_collector(inspect_process):
    locator_path = DATA_ROOT / "research/full_close/active_job.json"
    if not locator_path.is_file():
        return {"status": "no_saved_full_close_collector"}
    locator = _read(locator_path)
    if locator.get("schema") != "full_close_active_job_locator_v1" or locator.get("execution_admitted") is not False:
        return {"status": "identity_unverified", "reason": "existing_active_locator_boundary_invalid"}
    receipt = _read(Path(locator["folder"]) / "receipt.json")
    # A terminal word is authoritative only on the same legacy full-close
    # receipt contract used by the read-only close workbench.
    if (receipt.get("schema") != FULL_CLOSE_SCHEMA
            or any(receipt.get(key) is not False for key in
                ("execution_admitted", "selection_enabled", "strategy_validated", "stockdb_modified", "ledger_mutated"))):
        return {"status": "identity_unverified", "reason": "existing_source_receipt_boundary_invalid"}
    saved = receipt.get("status", "")
    if saved in TERMINAL_OLD or saved.startswith("failed"):
        return {"status": "terminal_saved_source_not_active", "source_status": saved, "locator": str(locator_path)}
    if saved not in {"running", "collecting"}:
        return {"status": "identity_unverified", "reason": "unknown_saved_collector_state", "source_status": saved}
    return {**inspect_process(locator.get("collector_pid"), locator.get("collector_process_started_at")),
            "source_status": saved, "locator": str(locator_path)}


def _bulk_cache(folder, day, now):
    receipt = _read(Path(folder) / "receipt.json")
    entries = receipt.get("entries", [])
    if (receipt.get("status") != "completed_probe" or receipt.get("days") != [day]
            or receipt.get("kinds") != ["stock"] or len(entries) != 1
            or receipt.get("execution_admitted") is not False or receipt.get("full_history_verified") is not False):
        raise ValueError("Existing bulk stage is not a complete exact-day successful stock probe; no reacquisition")
    entry = entries[0]
    if (entry.get("date") != day or entry.get("kind") != "stock"
            or entry.get("method") != "query_daily_history_k_AStock" or entry.get("error_code") != "0"
            or receipt.get("implementation_sha256") != digest(daily.__file__)
            or receipt.get("sdk", {}).get("sha256") != importer.WHEEL_SHA256
            or receipt.get("sdk", {}).get("distribution_version") != "0.9.4"
            or digest(receipt["sdk"]["wheel"]) != importer.WHEEL_SHA256):
        raise ValueError("Existing bulk date/method/SDK/source implementation changed; no reacquisition")
    frame = daily.cached_daily(folder, entry, kind="stock")
    if (list(frame) != BAOSTOCK_FIELDS.split(",") or frame.empty or not frame.date.eq(day).all()
            or frame.duplicated(["date", "code"]).any() or not frame.adjustflag.eq("3").all()
            or not frame.code.str.fullmatch(r"(?:sh|sz)\.\d{6}").all()
            or not frame.tradestatus.isin(["0", "1"]).all() or not frame.isST.isin(["0", "1"]).all()
            or not importer._stamp(day + "T15:00:00+08:00") <= importer._stamp(receipt["created_at"])
                    <= importer._stamp(receipt["finished_at"]) <= now):
        raise ValueError("Existing bulk raw schema/date/clock is invalid; no reacquisition")
    quality = entry.get("quality") or {}
    if (quality.get("rows") != len(frame) or quality.get("symbols") != frame.code.nunique()
            or set(quality.get("issues", [])) - {"missing_or_nonfinite_market_fields"}):
        raise ValueError("Existing bulk count or unresolved source quality changed")
    importer._market_quality(frame.loc[frame.code.map(lambda value: ORDINARY.fullmatch(value) is not None)])
    return {"receipt": str(Path(folder).resolve() / "receipt.json"), "receipt_sha256": digest(Path(folder) / "receipt.json"),
            "raw_sha256": entry["sha256"], "rows": len(frame), "original_call_count": 1}


def _roster_cache(folder, day, now):
    receipt, frame, pin = roster_api.load_cached_day(folder, day)
    if not importer._stamp(day + "T15:00:00+08:00") <= importer._stamp(receipt["completed_at"]) <= now:
        raise ValueError("Exact-day roster completion clock is unavailable")
    return {"receipt": str(Path(folder).resolve() / "receipt.json"), "raw_sha256": pin["verified_current_raw_sha256"],
            "rows": len(frame), "original_query_attempts": receipt.get("logical_query_attempts")}


def status(out, *, inspect_process=process_identity):
    out = Path(out).resolve()
    path = out / "receipt.json"
    if not path.is_file():
        return {"schema": SCHEMA, "status": "cycle_evidence_unavailable", "out": str(out), "job_started": False, **FLAGS}
    value = _read(path)
    if value.get("schema") != SCHEMA or any(value.get(key) is not expected for key, expected in FLAGS.items()):
        raise ValueError("Saved cycle research boundary changed")
    process = (inspect_process(value.get("pid"), value.get("process_started_at"))
        if value.get("status") == "running" else {"status": "terminal_saved_cycle_not_active"})
    return {**value, "status_only": True, "read_at": datetime.now(CN).isoformat(), "process": process,
            "provider_contacted_this_call": False, "job_started_this_call": False}


def run(day, calendar, sdk_wheel, out, *, bulk_probe=None, roster=None, inspect_process=process_identity, _now=None):
    now = importer._stamp(_now or datetime.now(CN))
    calendar, sdk_wheel, out = (Path(path).resolve() for path in (calendar, sdk_wheel, out))
    base = {"schema": SCHEMA, "day": day, "out": str(out), "checked_at": now.isoformat(),
            "output_directory_created": False, "provider_contacted": False, "job_started": False, **FLAGS}
    # This precedes every mkdir, lock, SDK import and provider operation.
    try:
        scope = historical_closed_scope(day, calendar, now)
    except ValueError as error:
        if "has not actually closed" in str(error):
            return {**base, "status": "waiting_for_market_close", "reason": str(error)}
        return {**base, "status": "blocked_invalid_closed_session", "reason": str(error)}
    if out.exists():
        return {**base, "status": "blocked_existing_cycle_no_retry", "reason": "Use --status-only; a new invocation never resumes or overwrites this cycle"}
    expected_update = now.replace(hour=17, minute=30, second=0, microsecond=0)
    missing_stages = [name for name, cache in (("bulk", bulk_probe), ("roster", roster)) if cache is None]
    if day == now.date().isoformat() and now < expected_update and missing_stages:
        # The official schedule permits a first attempt; it is not evidence
        # that today's actual response is ready or passes any quality check.
        # Two explicit caches still undergo the original strict local checks.
        return {**base, "status": "waiting_for_provider_daily_update", "earliest_expected_at": expected_update.isoformat(),
                "source_url": PROVIDER_UPDATE_SOURCE_URL, "close_scope": scope,
                "provider_readiness_verified": False, "scheduled_time_is_quality_admission": False,
                "checks": {"requested_day_is_CN_today": True, "market_session_actually_closed": True,
                           "before_expected_daily_update": True, "explicit_bulk_cache_supplied": bulk_probe is not None,
                           "explicit_roster_cache_supplied": roster is not None, "new_provider_stages_required": missing_stages}}
    try:
        other = _active_collector(inspect_process)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        other = {"status": "identity_unverified", "reason": str(error)}
    if other["status"] in {"verified_alive", "identity_unverified"}:
        return {**base, "status": "blocked_existing_collector", "collector": other}
    lock_root = DATA_ROOT / "research/bulk_close_cycles"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / ".active_cycle.lock"
    identity = {"pid": os.getpid(), "process_started_at": datetime.fromtimestamp(_process_start(os.getpid()), timezone.utc).isoformat(),
                "day": day, "out": str(out)}
    try:
        handle = lock_path.open("x", encoding="utf-8")
    except FileExistsError:
        # A stale/interrupted claim is evidence, never permission to retry it.
        try:
            owner = _read(lock_path)
            process = inspect_process(owner.get("pid"), owner.get("process_started_at"))
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            process = {"status": "identity_unverified", "reason": str(error)}
        return {**base, "status": "blocked_existing_cycle_claim_no_retry", "claim_file": str(lock_path), "process": process}
    with handle:
        json.dump(identity, handle)
    record = {**base, **identity, "status": "running", "started_at": now.isoformat(), "close_scope": scope,
        "collector": other, "stage": "cache_validation", "stages": {}, "output_directory_created": False,
        "job_started": True, "bulk_collector_invocations_this_run": 0, "roster_probe_invocations_this_run": 0,
        "retries": 0, "anonymous_market_data_only": True}
    reserved = False
    try:
        out.mkdir(parents=True, exist_ok=False)
        reserved = True
        record["output_directory_created"] = True
        receipt_path = out / "receipt.json"
        write_json(receipt_path, record)
        bulk_folder = Path(bulk_probe).resolve() if bulk_probe else out / "bulk_probe"
        roster_folder = Path(roster).resolve() if roster else out / "dated_roster"
        sdk = None
        def ready_sdk():
            nonlocal sdk
            if sdk is None:
                sdk = daily.select_sdk(sdk_wheel)
            return sdk
        if bulk_probe is not None:
            record["stages"]["bulk"] = {"reused": True, **_bulk_cache(bulk_folder, day, now)}
        if roster is not None:
            record["stages"]["roster"] = {"reused": True, **_roster_cache(roster_folder, day, now)}
        if bulk_probe is None:
            record["stage"] = "bulk_sdk_selection"
            sdk_info = ready_sdk()
            record.update(stage="bulk_probe", bulk_collector_invocations_this_run=1, provider_contacted=True)
            write_json(receipt_path, record)
            _, answer = daily.collect([day], ["stock"], sdk_info, bulk_folder)
            record["bulk_source_status"] = answer.get("status")
            record["stages"]["bulk"] = {"reused": False, **_bulk_cache(bulk_folder, day, importer._stamp(datetime.now(CN)))}
            write_json(receipt_path, record)
        if roster is None:
            record["stage"] = "roster_sdk_selection"
            sdk_info = ready_sdk()
            record.update(stage="dated_roster", roster_probe_invocations_this_run=1, provider_contacted=True)
            write_json(receipt_path, record)
            answer = roster_api.probe(day, sdk_wheel, roster_folder, _sdk_info=sdk_info)
            record["roster_source_status"] = answer.get("status")
            record["stages"]["roster"] = {"reused": False, **_roster_cache(roster_folder, day, importer._stamp(datetime.now(CN)))}
            write_json(receipt_path, record)
        record["stage"] = "typed_bulk_import"
        write_json(receipt_path, record)
        package, terminal = importer.import_bulk_close(bulk_folder, roster_folder, calendar, out / "full_close")
        # Importer's own strict validation binds full roster/status/normal and
        # nullable bars. It performs no provider calls and invents no receipts.
        record.update(status="completed_bulk_close_cycle_research_only", stage="complete", full_close_package=str(package),
            source_status=terminal["status"], ordinary_symbols=terminal["ordinary_symbols"],
            full_dated_roster_inventory_complete=terminal["full_dated_roster_inventory_complete"],
            nullable_suspension_rows=terminal["nullable_suspension_rows"],
            package_receipt_sha256=digest(package / "receipt.json"))
    except Exception as error:
        record.update(status="failed_stage_no_retry", error_type=type(error).__name__, error=str(error))
    finally:
        record["finished_at"] = datetime.now(CN).isoformat()
        if reserved:
            write_json(out / "receipt.json", record)
        lock_path.unlink()
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--day")
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--sdk-wheel", type=Path)
    parser.add_argument("--bulk-probe", type=Path)
    parser.add_argument("--roster", type=Path)
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args(argv)
    if args.status_only:
        answer = status(args.out)
    else:
        if not all((args.day, args.calendar, args.sdk_wheel)):
            parser.error("--day --calendar --sdk-wheel are required for a new cycle")
        answer = run(args.day, args.calendar, args.sdk_wheel, args.out, bulk_probe=args.bulk_probe, roster=args.roster)
    print(json.dumps(answer, ensure_ascii=False))
    return 0 if answer["status"] in {"completed_bulk_close_cycle_research_only", "waiting_for_market_close",
                                    "waiting_for_provider_daily_update", "cycle_evidence_unavailable"} or args.status_only else 2


if __name__ == "__main__":
    raise SystemExit(main())
