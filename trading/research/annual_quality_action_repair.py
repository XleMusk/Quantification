"""Explicit post-event action repair of failed annual-quality research suffixes.

Original declarations, successful cases and failed checkpoints are immutable.
This is a separately labelled accounting diagnostic, never timely publication.
"""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pandas as pd

from ..config import StrategyConfig
from ..worker import market_now
from .annual_quality_portfolios import (
    AnnualQualityFeed, confirm_session_successor, declared_definition, load_quality,
)
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .low_risk import features, portfolio
from .monthly_timing import month_ends
from .prospective_research import incoming_dates, validate_extension


METHOD = "annual_quality_explicit_posthoc_action_suffix_repair_v1"
FLAGS = dict(candidate_panel_only=True, selection_enabled=False,
             execution_admitted=False, strategy_validated=False,
             returns_reportable=False, pit_verified=False)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def apply_action_changes(checkpoint, config, changes, *, reviewed_at):
    """Admit only unconsumed terms, zero-entitlement past terms, or last-close registration."""
    from .annual_quality_entitlement_repair import repair_record_close
    result, repairs = deepcopy(checkpoint), []
    state = result["state"]
    last = state["day"]
    if not last:
        raise ValueError("A real terminal account prefix is required")
    for change in changes:
        old, new = change["old"], change["new"]
        symbol, ex = new["symbol"], new["dividOperateDate"]
        registration = new["dividRegistDate"]
        key = f"{symbol}:{ex}"
        if old is not None:
            altered = {field for field in old if old[field] != new[field]}
            if (set(old) != set(new) or altered != {"dividReserveToStockPs"}
                    or old["dividReserveToStockPs"] not in {None, ""}
                    or new["dividReserveToStockPs"] != "0"):
                raise ValueError("Only a reviewed missing reserve-zero completion is supported")
            if ex <= last and state["entitlements"].get(key) != 0:
                raise ValueError("A consumed held action cannot be rewritten")
            if registration <= last < ex and key not in state["entitlements"]:
                raise ValueError("An already registered action must retain its exact saved entitlement")
        elif registration == last and ex > last:
            result, audit = repair_record_close(result, config, new, reviewed_at=reviewed_at)
            state = result["state"]
            repairs.append(audit)
        elif registration <= last or ex <= last:
            raise ValueError("A newly discovered action predates the exact saved record-close state")
    return result, repairs


def validate_reference_changes(changes, last):
    raw = ("code", "date", "close", "preclose", "tradestatus",
           "previous_observed_close", "reference_delta")
    for item in changes:
        before, after = item["old"], item["new"]
        if (before is None or after is None or after["date"] <= last
                or any(before[key] != after[key] for key in raw)
                or after["reference_formula_status"] != "reference_match"):
            raise ValueError("Only an independently reviewed future reference can change")


def run(prepared_run, inputs_file, source_audit, out, *, session_calendar, case,
        current=None):
    current = current or market_now()
    original, out = Path(prepared_run).resolve(), Path(out).resolve()
    inputs_file, source_audit = Path(inputs_file).resolve(), Path(source_audit).resolve()
    if out.exists() or out == original or original in out.parents:
        raise ValueError("Use a new independent research branch; never overwrite the original account")
    protocol, head = read(original / "protocol.json"), read(original / "head.json")
    cases, investment = declared_definition(protocol)
    if (case not in cases or protocol.get("execution_admitted") is not False
            or head.get("execution_admitted") is not False or head.get("pending_case")):
        raise ValueError("Original unbound terminal annual comparison required")
    saved = next(row for row in head["results"] if row["signal"] == case)
    if saved["status"] != "failed":
        raise ValueError("Do not repeat an already completed case")
    original_hashes = {str(path): digest(path) for path in
        (original / "protocol.json", original / "head.json",
         Path(saved["checkpoint"]) / "account.json",
         Path(saved["checkpoint"]) / "daily.csv", Path(saved["checkpoint"]) / "replay.json")}
    config = StrategyConfig(**protocol["portfolio"]).validate()
    if config != portfolio():
        raise ValueError("The original fixed allocation, costs and capital are required")
    inputs, audit = read(inputs_file), read(source_audit)
    # Builder-produced exact source deltas are checked again against stored files.
    from .annual_quality_action_inputs import validate_audit
    changes = validate_audit(inputs_file, source_audit)
    base = BulkResearchFeed(**inputs)
    try:
        calendar_binding, confirmation = confirm_session_successor(base, session_calendar)
        if calendar_binding != head.get("session_calendar_binding"):
            raise ValueError("Original official successor-calendar binding changed")
        declaration_day = pd.Timestamp(protocol["declared_at"]).tz_convert("Asia/Hong_Kong").date().isoformat()
        dates = incoming_dates(base.calendar, protocol["preview_market_day"], declaration_day, current)
        if not dates or dates[-1] > protocol["end"]:
            raise ValueError("Actual finite post-declaration sessions required")
        validate_extension(protocol["manifest"], base.manifest, protocol["preview_market_day"])
        before = read(Path(head["last_batch"]) / "run.json")["manifest"]
        for key in ("price_view", "event_view", "reference_view", "listing_master"):
            if Path(audit["inputs_before"][key]).resolve() != Path(before[key]).resolve():
                raise ValueError("The repair audit is not based on this failed batch's original source pair")
        for key in ("price_view", "raw_view", "listing_master", "scope", "canonical_codes", "source_codes"):
            if before[key] != base.manifest[key]:
                raise ValueError("An action repair cannot change raw, signal prices or membership: " + key)
        if before["historical_price_adjustments"] != base.manifest["historical_price_adjustments"]:
            raise ValueError("Special/rights actions cannot be repaired through this method")
        options = {"lot_allocation": protocol["lot_allocation"]} if protocol.get("lot_allocation") else {}
        checkpoint = load_replay_checkpoint(saved["checkpoint"], config, dates, **options)
        validate_reference_changes(changes["reference_changes"], checkpoint["state"]["day"])
        reviewed_at = changes["reviewed_at"]
        checkpoint, registrations = apply_action_changes(checkpoint, config, changes["event_changes"],
                                                          reviewed_at=reviewed_at)
        annual, quality = load_quality(protocol["quality_run"], protocol["report_day"], investment)
        if quality["id"] != protocol["feature_id"] or quality["asof"] != protocol["feature_asof"]:
            raise ValueError("Original frozen annual features changed")
        # Only future scheduled decisions need feature calculation. This suffix is
        # normally non-rebalance; it must not recompute the old first-day ranks.
        decisions = [day for day in month_ends(base.calendar, dates[-1])
                     if checkpoint["state"]["day"] < day <= dates[-1]]
        values = features(base, dates[-1], feature_days=decisions) if decisions else {}
        feed = AnnualQualityFeed(base, values, annual, case, dates[-1],
                                protocol["declared_at"], protocol["feature_asof"], investment)
        out.mkdir(parents=True, exist_ok=False)
        seed = out / "repaired_seed"
        seed.mkdir()
        for name in ("daily.csv", "replay.json"):
            shutil.copy2(Path(saved["checkpoint"]) / name, seed / name)
        write_json(seed / "account.json", checkpoint["state"])
        checkpoint["source"] = str(seed)
        write_json(seed / "repair.json", {"method_id": METHOD, "registrations": registrations,
            "original_checkpoint": saved["checkpoint"], "source_audit": str(source_audit),
            "source_audit_sha256": digest(source_audit), "reviewed_at": reviewed_at, **FLAGS})
        record = {"method_id": METHOD, "status": "running", "started_at": current.isoformat(),
            "original_prepared_run": str(original), "original_protocol_sha256": original_hashes[str(original / "protocol.json")],
            "case": case, "knowledge_mode": "posthoc_component_diagnostic",
            "not_original_timely_signal": True, "original_head_promoted": False,
            "calendar_confirmation": confirmation, "inputs": inputs,
            "inputs_sha256": digest(inputs_file), "source_audit_sha256": digest(source_audit),
            "registration_repairs": registrations, "manifest": base.manifest,
            "implementation_sha256": digest(__file__), **FLAGS}
        write_json(out / "result.json", record)
        try:
            result = baseline_ledger_result(feed, config, dates, out / case, checkpoint=checkpoint, **options)
            pd.DataFrame(feed.audit).to_csv(out / "new_pool_counts.csv", index=False)
            record.update(status=result["status"], result=result, completed_at=market_now().isoformat(),
                          original_files_unchanged=all(digest(path) == value for path, value in original_hashes.items()))
            if not record["original_files_unchanged"]:
                raise ValueError("Original annual account was modified during an isolated repair")
            write_json(out / "result.json", record)
        except BaseException as error:
            terminal_path = out / case / "replay.json"
            record.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                          error=f"{type(error).__name__}: {error}", completed_at=market_now().isoformat(),
                          terminal_replay=read(terminal_path) if terminal_path.is_file() else None)
            write_json(out / "result.json", record)
            raise
        return record
    finally:
        base.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prepared-run", "inputs", "source-audit", "out", "session-calendar"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--case", required=True)
    args = parser.parse_args(argv)
    result = run(args.prepared_run, args.inputs, args.source_audit, args.out,
                 session_calendar=args.session_calendar, case=args.case)
    print(json.dumps({"status": result["status"], "case": result["case"], "result": result["result"]}), flush=True)
    return 0 if result["status"] == "completed" else 1
