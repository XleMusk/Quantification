"""Checkpointed full dated-roster raw close inventory, separate from trading feeds.

Each immutable twenty-symbol batch reuses baostock_daily_slice.  A bounded run
is a checkpoint of one full-universe job, never a newly selected sample.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, time as day_time, timedelta, timezone
import json
from pathlib import Path
import re
import socket
import time

import pandas as pd

from a_stock_committee.market_data import baostock_session
from ..market_dataset import write_frame, write_json
from ..settings import DATA_ROOT
from . import baostock_daily_slice as daily
from .baostock_all_stock_probe import load_cached_day, probe
from .daily_source_probe import WHEEL_SHA256, select_sdk
from .exchange_calendar import DEFAULT_CALENDAR_PATH, _closed_days, load_calendar

SCHEMA = "baostock_full_dated_close_inventory_v1"
ORDINARY = re.compile(r"(?:sh\.(?:60\d{4}|688\d{3})|sz\.(?:00\d{4}|30\d{4}))$")
CN = timezone(timedelta(hours=8))
HISTORICAL_MODE = "explicit_historical_research_repair"


def latest_closed_session(calendar, now=None):
    now = now or datetime.now(CN)
    if now.tzinfo is None:
        raise ValueError("Close boundary requires an aware wall clock")
    now = now.astimezone(CN)
    payload = load_calendar(Path(calendar))
    if payload is None or payload.get("year") != now.year:
        raise ValueError("Current-year pinned exchange calendar unavailable")
    closed = _closed_days(payload)
    overrides = {date.fromisoformat(value) for value in payload.get("open_sessions", [])}
    day = now.date() if now.time() >= day_time(15) else now.date() - timedelta(days=1)
    for _ in range(370):
        if day in overrides or (day.weekday() < 5 and day not in closed):
            return day.isoformat()
        day -= timedelta(days=1)
    raise ValueError("Pinned calendar cannot locate the latest closed session")


def historical_closed_scope(day, calendar, now=None):
    """Validate an explicit historical request without altering the wall clock."""
    target = date.fromisoformat(day)
    if target.isoformat() != day:
        raise ValueError("Historical repair date must be canonical ISO")
    now = now or datetime.now(CN)
    if now.tzinfo is None:
        raise ValueError("Historical close boundary requires an aware wall clock")
    now = now.astimezone(CN)
    calendar = Path(calendar).resolve()
    payload = load_calendar(calendar)
    if payload is None or payload.get("year") != target.year:
        raise ValueError("Target-year pinned exchange calendar unavailable")
    overrides = {date.fromisoformat(value) for value in payload.get("open_sessions", [])}
    if target not in overrides and (target.weekday() >= 5 or target in _closed_days(payload)):
        raise ValueError("Historical repair target is not a pinned open exchange session")
    if datetime.combine(target, day_time(15), CN) > now:
        raise ValueError("Historical repair target has not actually closed")
    return {"schema": "full_close_historical_repair_request_v1", "acquisition_mode": HISTORICAL_MODE,
            "day": day, "calendar": str(calendar), "calendar_sha256": daily.digest(calendar),
            "validated_closed_at": now.isoformat(), "target_is_pinned_open_session": True,
            "market_close_time": "15:00:00+08:00", "roster_requested_day": day,
            "roster_basis": "query_all_stock exact target day; never later-day survivors",
            "research_only": True, "candidate_panel_only": True, "source_verified": False,
            "pit_verified": False, "stockdb_modified": False, "ledger_mutated": False,
            "strategy_validated": False, "selection_enabled": False, "execution_admitted": False}


def roster_symbols(raw):
    """Match leader_daily_signals ordinary SH/SZ scope; no status/name filter."""
    if list(raw.columns) != ["code", "tradeStatus", "code_name"] or raw.code.duplicated().any():
        raise ValueError("Dated roster schema or identity changed")
    if not raw.tradeStatus.isin(["0", "1"]).all():
        raise ValueError("Unknown dated roster trading status")
    selected = raw.loc[raw.code.map(lambda code: ORDINARY.fullmatch(code) is not None)].sort_values("code").copy()
    selected["symbol"] = selected.code.map(lambda code: code[3:] + (".SSE" if code.startswith("sh.") else ".SZSE"))
    if selected.empty:
        raise ValueError("No ordinary SH/SZ A shares in exact dated provider roster")
    return selected


def _assemble(folder, plan, receipt, *, materialize=True):
    frames, inventory, attempted, failures, attempted_symbols = [], [], 0, [], set()
    _, dated_roster, pin = load_cached_day(folder / "roster", plan["day"])
    if pin["verified_current_raw_sha256"] != plan["roster_raw_sha256"]:
        raise ValueError("Preserved dated roster changed during collection")
    status_by_code = dated_roster.set_index("code").tradeStatus.to_dict()
    disagreements = []
    for batch in plan["batches"]:
        path = folder / batch["folder"] / "receipt.json"
        if not path.exists():
            continue
        saved = json.loads(path.read_text(encoding="utf-8"))
        attempted += saved["logical_queries"]
        attempted_symbols.update(saved["completed"])
        if saved.get("in_flight"):
            attempted_symbols.add(saved["in_flight"]["symbol"])
        for symbol, entry in saved["completed"].items():
            if symbol not in batch["symbols"]:
                raise ValueError("Saved price entry lies outside its immutable batch")
            code = entry["kwargs"]["code"]
            observed_status = "0" if entry["quality"]["suspended_rows"] == 1 else "1"
            if status_by_code.get(code) != observed_status:
                disagreements.append({"symbol": symbol, "roster_status": status_by_code.get(code), "daily_status": observed_status})
            if materialize:
                frames.append(daily.cached(path.parent, entry, symbol, [plan["day"]]))
            inventory.append({"symbol": symbol, "batch": batch["id"], "raw_file": str(path.parent / entry["raw_file"]),
                              "raw_sha256": entry["raw_sha256"], "quality": entry["quality"]})
        if saved.get("in_flight") or saved.get("failures") or saved["status"] == "failed_stopped":
            failures.append({"batch": batch["id"], "receipt": str(path), "status": saved["status"],
                             "in_flight": saved.get("in_flight"), "failures": saved.get("failures", [])})
    symbols = {entry["symbol"] for entry in inventory}
    if materialize:
        panel = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=daily.FIELDS.split(","))
        if panel.duplicated(["date", "code"]).any() or len(panel) != len(symbols):
            raise ValueError("Full close aggregate has duplicate or missing completed identities")
        write_frame(folder / "raw_daily.csv", panel)
        write_json(folder / "completed_inventory.json", inventory)
        receipt.update(raw_daily=str(folder / "raw_daily.csv"), raw_daily_sha256=daily.digest(folder / "raw_daily.csv"))
    receipt.update(completed_symbols=len(symbols), completed_rows=len(symbols), logical_price_queries=attempted,
                   pending_symbols=[symbol for symbol in plan["symbols"] if symbol not in symbols],
                   unattempted_symbols=[symbol for symbol in plan["symbols"] if symbol not in attempted_symbols],
                   failed_or_interrupted_attempted_symbols=[symbol for symbol in plan["symbols"] if symbol in attempted_symbols and symbol not in symbols],
                   execution_bar_rows=sum(entry["quality"]["execution_bar_rows"] for entry in inventory),
                   nullable_suspension_rows=sum(len(entry["quality"]["unpriceable_source_rows"]) for entry in inventory),
                   suspended_rows=sum(entry["quality"]["suspended_rows"] for entry in inventory),
                   preserved_failed_batches=failures, provider_status_disagreements=disagreements,
                   full_dated_roster_inventory_complete=len(symbols) == len(plan["symbols"]) and not disagreements,
                   execution_bar_coverage_complete=len(symbols) == len(plan["symbols"]) and not disagreements and all(entry["quality"]["execution_bar_coverage_complete"] for entry in inventory),
                   updated_at=datetime.now(CN).isoformat())
    if plan.get("recovery_lineage"):
        lineage = plan["recovery_lineage"]
        receipt.update(new_generation_price_queries=attempted, parent_generation_price_attempts=lineage["parent_price_attempts"],
                       reused_completed_symbols=lineage["inherited_completed_symbols"],
                       explicit_recovery_symbol=lineage["original_failed_symbol"],
                       explicit_recovery_price_attempts_for_prior_failed_code=int(attempted > 0),
                       total_price_attempts_for_prior_failed_code=1 + int(attempted > 0))
    return receipt


def load_complete_close(folder):
    """Read and revalidate a finished full job; partial/failed jobs never load.

    Returns ``(metadata, raw_daily, ordinary_roster)``. Frames retain all source
    strings and nullable suspension fields. This never contacts a provider.
    """
    folder = Path(folder).resolve()
    plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    if plan.get("schema") == "baostock_bulk_exact_dated_close_inventory_v1":
        # A real preserved bulk call has its own strict lineage. It must not
        # masquerade as thousands of individual dailyplus acquisition receipts.
        from .baostock_bulk_close import load_complete_bulk
        return load_complete_bulk(folder)
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    if (plan.get("schema") != SCHEMA or receipt.get("schema") != SCHEMA
            or receipt.get("day") != plan.get("day")
            or receipt.get("status") != "complete_full_dated_close_inventory_not_admitted"
            or receipt.get("full_dated_roster_inventory_complete") is not True
            or receipt.get("pending_symbols") or receipt.get("preserved_failed_batches")):
        raise ValueError("Full dated close is not a complete successful roster inventory")
    for payload in (plan, receipt):
        if any(payload.get(key) is not False for key in ("source_verified", "pit_verified", "strategy_validated", "selection_enabled", "execution_admitted", "stockdb_modified", "ledger_mutated")):
            raise ValueError("Source package boundary flags changed")
    if plan.get("acquisition_mode") == HISTORICAL_MODE:
        request = json.loads((folder / "historical_repair_manifest.json").read_text(encoding="utf-8"))
        if (receipt.get("acquisition_mode") != HISTORICAL_MODE or request.get("acquisition_mode") != HISTORICAL_MODE
                or request.get("day") != plan["day"] or request.get("roster_requested_day") != plan["day"]
                or request.get("calendar_sha256") != plan["calendar_sha256"]
                or request.get("roster_raw_sha256") != plan["roster_raw_sha256"]
                or request.get("execution_admitted") is not False):
            raise ValueError("Historical repair request/roster identity changed")
        if any(request.get(key) is not False for key in ("source_verified", "pit_verified", "strategy_validated", "selection_enabled", "execution_admitted", "stockdb_modified", "ledger_mutated")):
            raise ValueError("Historical repair request boundaries changed")
    if plan.get("recovery_lineage") or receipt.get("recovery_lineage"):
        from .baostock_full_close_recovery import verify_lineage
        verify_lineage(folder, plan, receipt)
    _, raw_roster, pin = load_cached_day(folder / "roster", plan["day"])
    ordinary = roster_symbols(raw_roster)
    if (pin["verified_current_raw_sha256"] != plan["roster_raw_sha256"]
            or ordinary.symbol.tolist() != plan["symbols"] or len(raw_roster) != plan["roster_total_rows"]
            or len(ordinary) != plan["ordinary_symbols"]):
        raise ValueError("Full close dated roster identity changed")
    path = folder / "raw_daily.csv"
    if daily.digest(path) != receipt.get("raw_daily_sha256"):
        raise ValueError("Full close aggregate raw hash changed")
    panel = pd.read_csv(path, dtype=str, keep_default_na=False)
    if list(panel.columns) != daily.FIELDS.split(","):
        raise ValueError("Full close aggregate source schema changed")
    expected_codes = ordinary.code.tolist()
    if (len(panel) != len(expected_codes) or panel.code.tolist() != expected_codes
            or not panel.date.eq(plan["day"]).all() or panel.code.duplicated().any()
            or not panel.tradestatus.tolist() == ordinary.tradeStatus.tolist()):
        raise ValueError("Full close aggregate does not match the exact dated full roster/status")
    frames = []
    for batch in plan["batches"]:
        batch_folder = folder / batch["folder"]
        batch_plan = json.loads((batch_folder / "plan.json").read_text(encoding="utf-8"))
        saved = json.loads((batch_folder / "receipt.json").read_text(encoding="utf-8"))
        if (batch_plan.get("symbols") != batch["symbols"] or batch_plan.get("days") != [plan["day"]]
                or batch_plan.get("sdk_sha256") != WHEEL_SHA256
                or saved.get("status") != "complete_raw_slice_not_admitted" or saved.get("in_flight")
                or saved.get("failures") or set(saved.get("completed", {})) != set(batch["symbols"])):
            raise ValueError("Full close batch is not an exact completed raw slice")
        frames.extend(daily.cached(batch_folder, saved["completed"][symbol], symbol, [plan["day"]]) for symbol in batch["symbols"])
    if not panel.equals(pd.concat(frames, ignore_index=True)):
        raise ValueError("Full close aggregate differs from preserved individual raw responses")
    return {"plan": plan, "receipt": receipt, "source_folder": str(folder), "read_only_verified": True}, panel, ordinary


def load_complete(folder):
    """Return ``(raw_dataframe, metadata)`` for read-only full-close consumers."""
    metadata, raw, ordinary = load_complete_close(folder)
    metadata["ordinary_roster"] = ordinary.to_dict("records")
    return raw, metadata


def collect(day, calendar, sdk, destination, *, max_new_queries=200, now=None,
            session_factory=baostock_session, roster_probe=probe, generation_lineage=None,
            max_queries_per_session=200, manage_sessions=True, historical_research_repair=False):
    if not isinstance(max_new_queries, int) or max_new_queries <= 0:
        raise ValueError("New-query budget must be a positive integer")
    if sdk.get("sha256") != WHEEL_SHA256:
        raise ValueError("Use the reviewed pinned BaoStock SDK")
    if not 1 <= max_queries_per_session <= 200:
        raise ValueError("A source session may contain at most 200 new price queries")
    day = date.fromisoformat(day).isoformat()
    folder, calendar = Path(destination).resolve(), Path(calendar).resolve()
    roster_folder = folder / "roster"
    plan_path, receipt_path = folder / "plan.json", folder / "receipt.json"
    if receipt_path.exists() and str(json.loads(receipt_path.read_text(encoding="utf-8")).get("status", "")).startswith("failed"):
        raise ValueError("Prior failed provider run is preserved; automatic retry is disabled")
    request_path = folder / "historical_repair_manifest.json"
    historical_request = None
    if historical_research_repair:
        historical_request = historical_closed_scope(day, calendar, now)
        if plan_path.exists() and json.loads(plan_path.read_text(encoding="utf-8")).get("acquisition_mode") != HISTORICAL_MODE:
            raise ValueError("Historical mode cannot modify or resume an ordinary existing generation")
        if request_path.exists():
            prior_request = json.loads(request_path.read_text(encoding="utf-8"))
            if any(prior_request.get(key) != historical_request[key] for key in ("schema", "acquisition_mode", "day", "calendar", "calendar_sha256", "roster_requested_day")):
                raise ValueError("Historical repair request scope changed")
            historical_request = prior_request
        else:
            write_json(request_path, historical_request)
    elif not plan_path.exists() and day != latest_closed_session(calendar, now):
        raise ValueError("New full close jobs must use the latest actually closed pinned session")
    if not (roster_folder / "receipt.json").exists():
        roster = roster_probe(day, Path(sdk["wheel"]), roster_folder, _sdk_info=sdk)
        if not roster["historical_bounded_collection_feasible"]:
            result = {"schema": SCHEMA, "status": "failed_roster_no_retry", "day": day,
                      "roster_receipt": str(roster_folder / "receipt.json"), "execution_admitted": False}
            write_json(receipt_path, result)
            return folder, result
    roster, raw_roster, pin = load_cached_day(roster_folder, day)
    selected = roster_symbols(raw_roster)
    symbols = selected.symbol.tolist()
    plan = {"schema": SCHEMA, "day": day, "calendar": str(calendar), "calendar_sha256": daily.digest(calendar),
            "sdk_sha256": WHEEL_SHA256, "roster_raw_sha256": pin["verified_current_raw_sha256"],
            "roster_total_rows": len(raw_roster), "ordinary_symbols": len(symbols),
            "ordinary_status_counts": selected.tradeStatus.value_counts().to_dict(), "symbols": symbols,
            "ordinary_filter": ORDINARY.pattern, "selection": "all_exact_dated_provider_SH_SZ_ordinary_A_both_status0_and1",
            "batch_size": 20, "batches": [{"id": i // 20, "folder": f"batches/{i // 20:04d}", "symbols": symbols[i:i + 20]}
                                            for i in range(0, len(symbols), 20)],
            "fields": daily.FIELDS, "adjustflag": "3", "quality_contract": daily.QUALITY_CONTRACT,
            "research_only": True, "candidate_panel_only": True, "strategy_validated": False,
            "selection_enabled": False, "source_verified": False, "pit_verified": False,
            "execution_admitted": False, "stockdb_modified": False, "ledger_mutated": False}
    if generation_lineage is not None:
        plan["recovery_lineage"] = generation_lineage
    if historical_research_repair:
        plan["acquisition_mode"] = HISTORICAL_MODE
        historical_request["roster_raw_sha256"] = plan["roster_raw_sha256"]
        write_json(request_path, historical_request)
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise ValueError("Full close resumed roster/calendar/SDK/scope changed")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (receipt.get("schema") != SCHEMA or receipt.get("day") != day
                or any(receipt.get(key) is not False for key in ("source_verified", "pit_verified", "strategy_validated",
                                                                "selection_enabled", "execution_admitted", "stockdb_modified", "ledger_mutated"))):
            raise ValueError("Full close saved receipt identity or boundary changed")
        if str(receipt.get("status", "")).startswith("failed"):
            raise ValueError("Prior failed provider run is preserved; automatic retry is disabled")
        aggregate = folder / "raw_daily.csv"
        if not aggregate.is_file() or daily.digest(aggregate) != receipt.get("raw_daily_sha256"):
            raise ValueError("Full close preserved aggregate hash changed; refuse to overwrite")
    else:
        write_json(plan_path, plan)
        write_frame(folder / "ordinary_roster.csv", selected)
        receipt = {"schema": SCHEMA, "day": day, "status": "running", "created_at": datetime.now(CN).isoformat(),
                   "roster_total_rows": len(raw_roster), "ordinary_symbols": len(symbols), "ordinary_status_counts": plan["ordinary_status_counts"],
                   "roster_receipt": str(roster_folder / "receipt.json"), "roster_pinned_date": day,
                   "retries": 0, "anonymous_market_data_only": True, "broker_login_attempted": False,
                   **{key: plan[key] for key in ("research_only", "candidate_panel_only", "strategy_validated", "selection_enabled",
                                                 "source_verified", "pit_verified", "execution_admitted", "stockdb_modified", "ledger_mutated")},
                   "limitations": ["Dated provider roster is not verified exchange/PIT eligibility or company-action evidence",
                                   "Nullable suspended activity stays null and cannot become an Engine bar",
                                   "Partial checkpoints are not complete full-market closes; no StockDB fields or account Ledger changed"]}
        if historical_research_repair:
            receipt.update(acquisition_mode=HISTORICAL_MODE, historical_repair_manifest=str(request_path))
    receipt = _assemble(folder, plan, receipt)
    if receipt["preserved_failed_batches"] or receipt["provider_status_disagreements"]:
        raise ValueError("Prior failed or interrupted query is preserved; automatic retry is disabled")
    before, started = receipt["logical_price_queries"], time.monotonic()
    receipt.update(status="running", max_new_queries_this_run=max_new_queries, price_queries_before_run=before,
                   sequential_min_query_interval_seconds=.5,
                   max_new_price_queries_per_session=max_queries_per_session,
                   run_started_at=datetime.now(CN).isoformat())
    write_json(receipt_path, receipt)
    print(json.dumps({"day": day, "roster_rows": len(raw_roster), "ordinary_symbols": len(symbols),
                      "ordinary_status_counts": plan["ordinary_status_counts"], "completed_before_run": receipt["completed_symbols"],
                      "max_new_queries": max_new_queries, "roster_receipt": receipt["roster_receipt"]}), flush=True)
    remaining = receipt["pending_symbols"]
    old_timeout = socket.getdefaulttimeout()
    @contextmanager
    def provider_factory():
        if manage_sessions:
            from .baostock_full_close_recovery import rotating_sessions
            with rotating_sessions(folder, session_factory, max_queries_per_session) as provider:
                yield provider
        else:
            with session_factory() as provider:
                yield provider
    try:
        if remaining:
            socket.setdefaulttimeout(30)
            with provider_factory() as provider:
                budget = max_new_queries
                for batch in plan["batches"]:
                    missing = [symbol for symbol in batch["symbols"] if symbol in remaining]
                    if not missing or budget <= 0:
                        continue
                    prior_receipt = folder / batch["folder"] / "receipt.json"
                    queries_before_batch = json.loads(prior_receipt.read_text(encoding="utf-8"))["logical_queries"] if prior_receipt.exists() else 0
                    path, saved = daily.collect(batch["symbols"], [day], calendar, sdk, folder / batch["folder"],
                        max_new_queries=min(20, budget), session_factory=lambda: nullcontext(provider),
                        source_scope={"full_close_plan": str(plan_path), "roster_raw_sha256": plan["roster_raw_sha256"], "batch": batch["id"]})
                    used = saved["logical_queries"] - queries_before_batch
                    budget -= used
                    receipt = _assemble(folder, plan, receipt, materialize=False)
                    write_json(receipt_path, receipt)
                    if receipt["preserved_failed_batches"] or receipt["provider_status_disagreements"]:
                        raise ValueError(f"Batch {batch['id']} failed; no retry or substitution")
        receipt["status"] = "complete_full_dated_close_inventory_not_admitted" if receipt["full_dated_roster_inventory_complete"] else "partial_budget_checkpoint_not_full_close"
    except Exception as error:
        receipt.update(status="failed_stopped_no_retry", error_type=type(error).__name__, error=str(error))
    finally:
        socket.setdefaulttimeout(old_timeout)
        receipt = _assemble(folder, plan, receipt)
        receipt.update(new_price_queries_this_run=receipt["logical_price_queries"] - before,
                       run_elapsed_seconds=round(time.monotonic() - started, 3))
        write_json(receipt_path, receipt)
    return folder, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR_PATH)
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--max-new-queries", type=int, default=200)
    parser.add_argument("--max-queries-per-session", type=int, default=200)
    parser.add_argument("--historical-research-repair", action="store_true",
                        help="Explicit historical research repair for an actually closed pinned session, using its exact dated full roster")
    args = parser.parse_args(argv)
    out = args.out or DATA_ROOT / "research/full_close" / ("baostock_" + args.day.replace("-", "") + "_v1")
    folder, receipt = collect(args.day, args.calendar, select_sdk(args.sdk_wheel), out, max_new_queries=args.max_new_queries,
                              max_queries_per_session=args.max_queries_per_session, historical_research_repair=args.historical_research_repair)
    print(json.dumps({key: receipt.get(key) for key in ("status", "ordinary_symbols", "completed_symbols", "new_price_queries_this_run",
                     "logical_price_queries", "nullable_suspension_rows", "full_dated_roster_inventory_complete", "execution_admitted")}
                     | {"receipt": str(folder / "receipt.json")}), flush=True)
    return 0 if receipt.get("status") in {"complete_full_dated_close_inventory_not_admitted", "partial_budget_checkpoint_not_full_close"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
