"""Explicit one-generation recovery of an evidenced transport reset, never hot retry."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import shutil
import socket

import pandas as pd

from a_stock_committee.market_data import baostock_session
from ..market_dataset import write_frame, write_json
from . import baostock_daily_slice as daily
from . import baostock_full_close as full
from .baostock_all_stock_probe import load_cached_day
from .daily_source_probe import WHEEL_SHA256, select_sdk


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def inspect_parent(parent):
    """Pure read-only verification, including every inherited raw response."""
    parent = Path(parent).resolve()
    plan, receipt = _read(parent / "plan.json"), _read(parent / "receipt.json")
    if (plan.get("schema") != full.SCHEMA or receipt.get("schema") != full.SCHEMA
            or receipt.get("status") != "failed_stopped_no_retry" or plan.get("recovery_lineage")
            or receipt.get("provider_status_disagreements") or plan.get("sdk_sha256") != WHEEL_SHA256):
        raise ValueError("Parent must be the terminal original transport-failed generation")
    for value in (plan, receipt):
        if any(value.get(key) is not False for key in ("source_verified", "pit_verified", "strategy_validated", "selection_enabled", "execution_admitted", "stockdb_modified", "ledger_mutated")):
            raise ValueError("Parent research boundaries changed")
    _, roster, pin = load_cached_day(parent / "roster", plan["day"])
    if (pin["verified_current_raw_sha256"] != plan["roster_raw_sha256"]
            or full.roster_symbols(roster).symbol.tolist() != plan["symbols"]
            or daily.digest(plan["calendar"]) != plan["calendar_sha256"]):
        raise ValueError("Parent fixed dated roster/calendar changed")
    completed, frames, failures, query_count = {}, [], [], 0
    for batch in plan["batches"]:
        folder = parent / batch["folder"]
        if not (folder / "receipt.json").exists():
            continue
        saved, batch_plan = _read(folder / "receipt.json"), _read(folder / "plan.json")
        if (batch_plan.get("symbols") != batch["symbols"] or batch_plan.get("days") != [plan["day"]]
                or batch_plan.get("sdk_sha256") != WHEEL_SHA256):
            raise ValueError("Parent batch scope changed")
        query_count += saved["logical_queries"]
        for symbol in batch["symbols"]:
            if symbol not in saved["completed"]:
                continue
            entry = saved["completed"][symbol]
            if entry.get("provider_error_code") != "0" or entry["quality"]["issues"]:
                raise ValueError("A prior source-quality failure cannot be recovered")
            frame = daily.cached(folder, entry, symbol, [plan["day"]])
            completed[symbol] = {"entry": entry, "folder": str(folder), "receipt": str(folder / "receipt.json")}
            frames.append(frame)
        if saved.get("failures"):
            failures.extend(saved["failures"])
            if saved.get("in_flight") is None:
                raise ValueError("Failed source attempt identity missing")
    if (len(failures) != 1 or "ConnectionResetError:" not in failures[0]["error"] or "10054" not in failures[0]["error"]
            or failures[0].get("query") is None):
        raise ValueError("Only one documented connection-reset attempt is recoverable")
    failed = failures[0]["query"]["symbol"]
    failed_batch = next(batch for batch in plan["batches"] if failed in batch["symbols"])
    failed_code = daily.kwargs(failed, [plan["day"]])["code"]
    if any((parent / failed_batch["folder"] / (failed_code + suffix)).exists() for suffix in (".csv", ".receipt.json")):
        raise ValueError("Prior failed code already has raw/quality evidence; transport-only recovery refused")
    if (list(completed) != plan["symbols"][:len(completed)] or failed != plan["symbols"][len(completed)]
            or receipt.get("failed_or_interrupted_attempted_symbols") != [failed]
            or query_count != len(completed) + 1 or receipt.get("logical_price_queries") != query_count
            or receipt.get("completed_symbols") != len(completed)):
        raise ValueError("Parent attempts are not the original fixed queue prefix plus one reset")
    if daily.digest(parent / "raw_daily.csv") != receipt.get("raw_daily_sha256"):
        raise ValueError("Parent aggregate raw hash changed")
    aggregate = pd.read_csv(parent / "raw_daily.csv", dtype=str, keep_default_na=False)
    if not aggregate.equals(pd.concat(frames, ignore_index=True)):
        raise ValueError("Parent aggregate differs from validated completed raw responses")
    lineage = {"schema": "explicit_transport_recovery_lineage_v1", "generation": 2,
               "parent": str(parent), "parent_plan_sha256": daily.digest(parent / "plan.json"),
               "parent_terminal_receipt_sha256": daily.digest(parent / "receipt.json"),
               "parent_raw_daily_sha256": daily.digest(parent / "raw_daily.csv"),
               "parent_price_attempts": query_count, "inherited_completed_symbols": len(completed),
               "inherited_raw_sha256": {symbol: item["entry"]["raw_sha256"] for symbol, item in completed.items()},
               "original_failed_symbol": failed, "prior_failed_attempts_for_symbol": 1,
               "maximum_explicit_recovery_attempts_for_symbol": 1,
               "original_transport_failure": failures[0], "new_price_queue": plan["symbols"][len(completed):],
               "source_semantics_changed": False, "prior_failure_erased": False}
    return plan, receipt, completed, lineage


def verify_lineage(folder, plan, receipt):
    lineage = plan.get("recovery_lineage")
    if not lineage or receipt.get("recovery_lineage") != lineage:
        raise ValueError("Recovery lineage is absent or differs across plan/receipt")
    parent_plan, _, inherited, observed = inspect_parent(lineage["parent"])
    if observed != lineage or {key: value for key, value in plan.items() if key != "recovery_lineage"} != parent_plan:
        raise ValueError("Recovery changed the pinned parent roster, request scope or lineage")
    folder = Path(folder)
    for batch in plan["batches"]:
        saved = _read(folder / batch["folder"] / "receipt.json")
        for symbol in batch["symbols"]:
            if symbol in inherited and saved["completed"].get(symbol, {}).get("raw_sha256") != inherited[symbol]["entry"]["raw_sha256"]:
                raise ValueError("Inherited successful raw evidence was replaced")
    if receipt.get("health_queries_this_generation") != 1 or receipt.get("health_status") != "passed_exact_day_source_health":
        raise ValueError("Recovery has no successful independent source health check")
    health = _read(folder / "health_receipt.json")
    raw = pd.read_csv(folder / "health_raw.csv", dtype=str, keep_default_na=False)
    if (health.get("schema") != "exact_day_source_health_v1" or health.get("status") != "passed_exact_day_source_health"
            or health.get("kwargs") != {"start_date": plan["day"], "end_date": plan["day"]}
            or health.get("provider_error_code") != "0" or health.get("sdk_sha256") != WHEEL_SHA256
            or daily.digest(folder / "health_raw.csv") != health.get("raw_sha256")
            or raw.to_dict("records") != [{"calendar_date": plan["day"], "is_trading_day": "1"}]):
        raise ValueError("Recovery health raw evidence or exact-day schema changed")
    sessions = _read(folder / "price_sessions.json")
    if (not 1 <= sessions.get("limit_new_price_queries", 0) <= 200
            or any(entry.get("status") != "closed" or entry.get("error")
                   or not 1 <= entry["new_price_queries"] <= sessions["limit_new_price_queries"] for entry in sessions["sessions"])
            or sum(entry["new_price_queries"] for entry in sessions["sessions"]) != receipt["logical_price_queries"]):
        raise ValueError("Recovery price-session counts/status/200-query bound changed")
    return lineage


def prepare(parent, destination, sdk):
    plan, old_receipt, completed, lineage = inspect_parent(parent)
    folder = Path(destination).resolve()
    if folder.exists():
        raise FileExistsError("A recovery generation must have a new destination; failed generations are immutable")
    if sdk.get("sha256") != WHEEL_SHA256:
        raise ValueError("Recovery SDK changed")
    folder.mkdir(parents=True)
    parent = Path(parent).resolve()
    shutil.copytree(parent / "roster", folder / "roster")
    roster_receipt = _read(folder / "roster/receipt.json")
    roster_receipt.update(raw_csv=str(folder / "roster" / Path(roster_receipt["raw_csv"]).name),
                         reused_original_roster_receipt=str(parent / "roster/receipt.json"), logical_queries_this_generation=0)
    write_json(folder / "roster/receipt.json", roster_receipt)
    shutil.copyfile(parent / "ordinary_roster.csv", folder / "ordinary_roster.csv")
    if plan.get("acquisition_mode") == full.HISTORICAL_MODE:
        shutil.copyfile(parent / "historical_repair_manifest.json", folder / "historical_repair_manifest.json")
    plan = {**plan, "recovery_lineage": lineage}
    write_json(folder / "plan.json", plan)
    for batch in plan["batches"]:
        seeds = {symbol: completed[symbol] for symbol in batch["symbols"] if symbol in completed}
        if not seeds:
            continue
        target = folder / batch["folder"]
        target.mkdir(parents=True)
        scope = {"full_close_plan": str(folder / "plan.json"), "roster_raw_sha256": plan["roster_raw_sha256"], "batch": batch["id"]}
        batch_plan = {"schema": daily.SCHEMA, **daily.scope(batch["symbols"], [plan["day"]], plan["calendar"]),
                      "sdk_sha256": WHEEL_SHA256, "fields": daily.FIELDS, "frequency": "d", "adjustflag": "3",
                      "source_scope": scope, "quality_contract": daily.QUALITY_CONTRACT,
                      "execution_admitted": False, "stockdb_modified": False, "ledger_mutated": False}
        write_json(target / "plan.json", batch_plan)
        entries, frames = {}, []
        for symbol, item in seeds.items():
            entry = {**item["entry"], "origin": "verified_completed_prior_generation_raw",
                     "prior_generation_receipt": item["receipt"], "prior_generation_receipt_sha256": daily.digest(item["receipt"]),
                     "new_provider_queries_for_reused_row": 0}
            shutil.copyfile(Path(item["folder"]) / entry["raw_file"], target / entry["raw_file"])
            write_json(target / (entry["kwargs"]["code"] + ".receipt.json"), entry)
            entries[symbol] = entry
            frames.append(daily.cached(target, entry, symbol, [plan["day"]]))
        write_frame(target / "raw_daily.csv", pd.concat(frames, ignore_index=True))
        saved = {"schema": daily.SCHEMA, "status": "complete_raw_slice_not_admitted" if len(entries) == len(batch["symbols"]) else "partial_bounded_slice",
                 "created_at": datetime.now().astimezone().isoformat(), "sdk": sdk, "completed": entries,
                 "failures": [], "in_flight": None, "logical_queries": 0, "retries": 0,
                 "inherited_completed_symbols": len(entries), "raw_daily_sha256": daily.digest(target / "raw_daily.csv"),
                 "execution_admitted": False, "stockdb_modified": False, "ledger_mutated": False, "broker_login_attempted": False}
        write_json(target / "receipt.json", saved)
    receipt = {key: value for key, value in old_receipt.items() if key in (
        "schema", "day", "roster_total_rows", "ordinary_symbols", "ordinary_status_counts", "research_only", "candidate_panel_only",
        "strategy_validated", "selection_enabled", "source_verified", "pit_verified", "execution_admitted", "stockdb_modified", "ledger_mutated", "limitations", "acquisition_mode")}
    receipt.update(status="prepared_new_recovery_generation", created_at=datetime.now().astimezone().isoformat(),
        recovery_lineage=lineage, sdk=sdk, parent_generation_price_attempts=lineage["parent_price_attempts"],
        reused_completed_symbols=len(completed), health_queries_this_generation=0,
        explicit_recovery_attempt_limit=1, automatic_unbounded_retry=False,
        roster_receipt=str(folder / "roster/receipt.json"), roster_pinned_date=plan["day"])
    if plan.get("acquisition_mode") == full.HISTORICAL_MODE:
        receipt["historical_repair_manifest"] = str(folder / "historical_repair_manifest.json")
    receipt = full._assemble(folder, plan, receipt)
    write_json(folder / "receipt.json", receipt)
    return folder, plan, receipt


def health_check(day, folder, provider):
    arguments = {"start_date": day, "end_date": day}
    receipt = {"schema": "exact_day_source_health_v1", "method": "query_trade_dates", "kwargs": arguments,
               "status": "running", "logical_queries": 1, "execution_admitted": False, "sdk_sha256": WHEEL_SHA256}
    write_json(folder / "health_receipt.json", receipt)
    try:
        result = provider.query_trade_dates(**arguments)
        raw = result.get_data()
        if raw.empty and not len(raw.columns):
            raw = pd.DataFrame(columns=result.fields)
        write_frame(folder / "health_raw.csv", raw)
        valid = (result.error_code == "0" and list(raw.columns) == ["calendar_date", "is_trading_day"]
                 and raw.to_dict("records") == [{"calendar_date": day, "is_trading_day": "1"}])
        receipt.update(status="passed_exact_day_source_health" if valid else "failed_source_health_no_price_retry",
                       provider_error_code=result.error_code, provider_error_msg=result.error_msg,
                       raw_sha256=daily.digest(folder / "health_raw.csv"), rows=len(raw))
        if not valid:
            raise ValueError("Independent exact-day health evidence is unusable; no recovery price query")
    except Exception as error:
        receipt.update(status="failed_source_health_no_price_retry", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        write_json(folder / "health_receipt.json", receipt)
    return receipt


@contextmanager
def rotating_sessions(folder, factory, limit=200):
    if not 1 <= limit <= 200:
        raise ValueError("A source session may contain at most 200 new price queries")
    path = folder / "price_sessions.json"
    previous = _read(path) if path.exists() else None
    if previous and (previous.get("schema") != "bounded_rotating_source_sessions_v1"
            or previous.get("limit_new_price_queries") != limit
            or any(entry["status"] != "closed" for entry in previous.get("sessions", []))):
        raise ValueError("Saved session history is not clean/closed or its bound changed; no automatic continuation")
    sessions, active, provider = list(previous["sessions"]) if previous else [], None, None
    def save():
        write_json(folder / "price_sessions.json", {"schema": "bounded_rotating_source_sessions_v1", "limit_new_price_queries": limit, "sessions": sessions})
    def close(error=None):
        nonlocal active, provider
        if active is not None:
            sessions[-1].update(status="closed_after_failure" if error or sessions[-1].get("error") else "closed", closed_at=datetime.now().astimezone().isoformat())
            active.__exit__(type(error) if error else None, error, error.__traceback__ if error else None)
            active, provider = None, None
            save()
    class SequentialSource:
        def query_history_k_data_plus(self, **arguments):
            nonlocal active, provider
            if active is None or sessions[-1]["new_price_queries"] >= limit:
                close()
                active = factory()
                try:
                    provider = active.__enter__()
                except Exception:
                    active = None
                    raise
                sessions.append({"id": len(sessions), "status": "open", "opened_at": datetime.now().astimezone().isoformat(), "new_price_queries": 0})
            sessions[-1]["new_price_queries"] += 1
            save()
            try:
                return provider.query_history_k_data_plus(**arguments)
            except Exception as error:
                sessions[-1]["error"] = f"{type(error).__name__}: {error}"
                save()
                raise
    try:
        yield SequentialSource()
    except Exception as error:
        close(error)
        raise
    finally:
        close()


def recover(parent, destination, sdk, *, max_new_queries, session_limit=200, session_factory=baostock_session):
    folder, plan, receipt = prepare(parent, destination, sdk)
    if not 1 <= max_new_queries <= len(plan["recovery_lineage"]["new_price_queue"]):
        raise ValueError("Budget must be within the fixed remaining full-roster queue")
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(30)
        receipt["health_queries_this_generation"] = 1
        write_json(folder / "receipt.json", receipt)
        with session_factory() as provider:
            health = health_check(plan["day"], folder, provider)
        receipt.update(health_status=health["status"], status="health_passed_before_explicit_recovery")
        write_json(folder / "receipt.json", receipt)
    except Exception as error:
        receipt.update(status="failed_source_health_no_retry", error_type=type(error).__name__, error=str(error))
        write_json(folder / "receipt.json", receipt)
        return folder, receipt
    finally:
        socket.setdefaulttimeout(old_timeout)
    @contextmanager
    def rotated():
        with rotating_sessions(folder, session_factory, session_limit) as provider:
            yield provider
    folder, receipt = full.collect(plan["day"], plan["calendar"], sdk, folder, max_new_queries=max_new_queries,
        session_factory=rotated, generation_lineage=plan["recovery_lineage"],
        max_queries_per_session=session_limit, manage_sessions=False,
        historical_research_repair=plan.get("acquisition_mode") == full.HISTORICAL_MODE)
    failed = plan["recovery_lineage"]["original_failed_symbol"]
    attempts = receipt["logical_price_queries"]
    receipt.update(new_generation_price_queries=attempts, inherited_price_queries=plan["recovery_lineage"]["parent_price_attempts"],
                   explicit_recovery_price_attempts_for_prior_failed_code=int(attempts > 0),
                   total_price_attempts_for_prior_failed_code=1 + int(attempts > 0),
                   explicit_recovery_symbol=failed, session_new_query_limit=session_limit)
    write_json(folder / "receipt.json", receipt)
    return folder, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--max-new-queries", type=int, required=True)
    parser.add_argument("--session-query-limit", type=int, default=200)
    args = parser.parse_args(argv)
    folder, receipt = recover(args.parent, args.out, select_sdk(args.sdk_wheel), max_new_queries=args.max_new_queries, session_limit=args.session_query_limit)
    print(json.dumps({"status": receipt["status"], "completed_symbols": receipt["completed_symbols"],
                      "reused_symbols": receipt["reused_completed_symbols"], "new_price_queries": receipt["logical_price_queries"],
                      "health_queries": receipt["health_queries_this_generation"], "receipt": str(folder / "receipt.json")}), flush=True)
    return 0 if receipt["status"] in {"complete_full_dated_close_inventory_not_admitted", "partial_budget_checkpoint_not_full_close"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
