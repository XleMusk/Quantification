"""Read-only historical phase sidecars for the original twenty-case replay.

All original status and metrics are preserved. Only the five completed bounded
diagnostics are annotated; a missing original case is never a zero performance.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from datetime import datetime
from hashlib import sha256
import io
import json
from pathlib import Path

from .leader_daily_signals import FAMILIES, _calendar_view
from .leader_environment_policy import MIXED, STRENGTH, WEAKNESS


SCHEMA = "leader_existing_replay_historical_phase_annotations_v1"
CASE_SCHEMA = "leader_existing_case_historical_phase_sidecar_v1"
METHOD_ID = "original_twenty_case_exact_event_phase_annotations_v1"
COMPLETED = "completed_already_inspected_engine_diagnostic"
UNAVAILABLE = {"unavailable_verified_full_market_breadth", "unavailable_complete_cohort_decision_batches"}
POOLS = ("common_pre_window_symbol20", "common_pre_window_uncapped")
EXITS = ("complete_empty_and_five_session_exit", "verified_full_market_breadth_defense")
FLAGS = {"candidate_panel_only": True, "research_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "provider_contacted": False, "engine_advanced": False, "ledger_mutated": False,
    "account_created": False, "orders_generated": False, "targets_generated": False,
    "publication_generated": False, "returns_recomputed": False, "metrics_recomputed": False,
    "original_artifacts_mutated": False, "cash_trace_promoted_to_performance": False,
    "phase_used_as_original_strategy_input": False, "historical_publication_verified": False,
    "model_refit": False, "future_labels_used_for_score": False}


def _now():
    return datetime.now().astimezone().isoformat()


def _time(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Explicit actual recording clock required")
    return stamp


def _read(path, *, csv_rows=False):
    path = Path(path).resolve()
    raw = path.read_bytes()
    proof = {"file": str(path), "sha256": sha256(raw).hexdigest(), "bytes": len(raw)}
    value = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))) if csv_rows else json.loads(raw)
    return value, proof


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _case_key(row):
    return "__".join(row[k] for k in ("pool", "family", "exit_policy"))


def load_environment(path, *, read_at):
    value, proof = _read(path)
    if (value.get("schema") != "leader_historical_observed_environment_v1"
            or value.get("method_id") != "native_active_observed_2026_fixed_phase_context_v1"
            or value.get("status") != "completed_observed_history_environment_context_not_complete_close_or_strategy"
            or any(value.get(k) is not False for k in ("execution_admitted", "selection_enabled", "strategy_validated"))
            or _time(value["available_at"]) > _time(read_at)):
        raise ValueError("Completed unadmitted source-native historical environment required")
    calendar, calendar_proof = _read(value["calendar"]["file"])
    if calendar_proof["sha256"] != value["calendar"]["sha256"]:
        raise ValueError("Pinned environment calendar changed")
    year = str(calendar["year"])
    full, _, status = _calendar_view(calendar, year + "-01-01", year + "-12-31", [])
    expected = [day for day in full if value["start"] <= day <= value["end"]]
    rows = value["days"]
    if (status != "pinned_static_exchange_calendar" or not expected
            or expected != value["calendar"]["sessions"] or [row["day"] for row in rows] != expected
            or any(row.get("phase") not in {STRENGTH, MIXED, WEAKNESS, "unknown"} for row in rows)):
        raise ValueError("Environment must preserve one row per exact pinned session")
    return {"record": value, "proof": proof, "calendar_proof": calendar_proof,
        "full_calendar": full, "by_day": {row["day"]: row for row in rows}}


def load_replay(path):
    value, proof = _read(path)
    rows = value.get("cases", [])
    expected = {(pool, family, policy) for pool in POOLS for family in FAMILIES for policy in EXITS}
    actual = [(r["pool"], r["family"], r["exit_policy"]) for r in rows]
    if (value.get("schema") != "leader_daily_comparison_v2"
            or value.get("status") != "fixed_already_inspected_comparison_complete"
            or value.get("case_count") != 20 or len(rows) != 20 or set(actual) != expected
            or any(value.get(k) is not False for k in ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("The complete original twenty-case result contract is required")
    for row in rows:
        allowed = row["pool"] == POOLS[0] and row["exit_policy"] == EXITS[0]
        if ((allowed and row.get("status") != COMPLETED)
                or (not allowed and row.get("status") not in UNAVAILABLE)):
            raise ValueError("Only the five completed bounded original cases may be annotated")
        if not allowed and row.get("metrics") is not None:
            raise ValueError("Original unavailable metrics must remain null")
        if any(row.get(k) is not False for k in ("execution_admitted", "selection_enabled", "strategy_validated")):
            raise ValueError("Original case cannot admit execution or selection")
    return value, proof


def phase_context(day, environment, *, role, missing_reason=None):
    row = environment["by_day"].get(day)
    result = {"day": day, "role": role, "phase": None, "breadth": None,
        "median_provider_pct_chg": None, "diagnostic_two_close_phase_agreement": None,
        "actual_environment_available_at": environment["record"]["available_at"],
        "retrospectively_derived_context": True, "original_signal_input": False,
        "historical_availability_verified": False,
        "known_at_execution_open_claimed": False}
    if day is None:
        return {**result, "status": "unknown_date_link", "unknown_reason": missing_reason or "date_not_bound"}
    if row is None:
        return {**result, "status": "out_of_environment_window", "unknown_reason": "no_exact_saved_environment_day_no_carry_forward"}
    return {**result, "status": "observed_phase_unknown" if row["phase"] == "unknown" else "observed_retrospective_phase",
        "phase": row["phase"], "breadth": row.get("breadth"),
        "median_provider_pct_chg": row.get("median_return_pct"),
        "diagnostic_two_close_phase_agreement": row.get("diagnostic_two_close_phase_agreement"),
        "source_observed_rows": row.get("source_observed_rows"),
        "active_observed_non_ST_rows": row.get("active_observed_non_ST_rows"),
        "unknown_reason": "saved_environment_phase_unknown" if row["phase"] == "unknown" else None}


def _event_sequence(event):
    value = event.get("sequence")
    return value if type(value) is int and value >= 0 else None


def order_signal_link(order_id, order, events, full_calendar):
    """Use the latest exact-execution target strictly before submission only."""
    unknown = {"status": "unknown_original_event_link", "signal_date": None,
        "target_sequence": None, "submitted_sequence": None, "execution_not_before": None}
    if order.get("id") != order_id:
        return {**unknown, "unknown_reason": "order_key_and_original_id_disagree"}
    submitted = [e for e in events if e.get("kind") == "submitted" and e.get("order_id") == order_id]
    if len(submitted) != 1:
        return {**unknown, "unknown_reason": "missing_submitted_event" if not submitted else "ambiguous_submitted_events"}
    submitted = submitted[0]
    sequence = _event_sequence(submitted)
    unknown["submitted_sequence"] = sequence
    if sequence is None or submitted.get("day") != order.get("day"):
        return {**unknown, "unknown_reason": "submitted_sequence_or_order_day_unknown_or_inconsistent"}
    exact = [e for e in events if e.get("kind") == "targets_generated"
        and e.get("execution_not_before") == order["day"]]
    if any(_event_sequence(e) is None for e in exact):
        return {**unknown, "unknown_reason": "exact_execution_target_has_unknown_sequence"}
    earlier = [e for e in exact if _event_sequence(e) < sequence]
    if not earlier:
        return {**unknown, "unknown_reason": "no_prior_target_for_exact_execution_day"}
    latest = max(_event_sequence(e) for e in earlier)
    candidates = [e for e in earlier if _event_sequence(e) == latest]
    if len(candidates) != 1:
        return {**unknown, "unknown_reason": "ambiguous_latest_target_sequence"}
    target = candidates[0]
    day = target.get("signal_date")
    if day not in full_calendar:
        return {**unknown, "unknown_reason": "target_signal_date_outside_pinned_calendar"}
    index = full_calendar.index(day)
    following = full_calendar[index + 1] if index + 1 < len(full_calendar) else None
    if target.get("day") != day or following != order["day"]:
        return {**unknown, "unknown_reason": "target_signal_or_exact_next_session_disagrees"}
    return {"status": "bound_by_original_ledger_event_sequence", "signal_date": day,
        "target_sequence": latest, "submitted_sequence": sequence,
        "execution_not_before": target["execution_not_before"], "unknown_reason": None,
        "link_is_not_historical_availability_verification": True}


def annotate_completed(case, replay, environment):
    folder = Path(case["artifacts"]).resolve()
    account, account_proof = _read(folder / "account.json")
    daily, daily_proof = _read(folder / "daily.csv", csv_rows=True)
    decisions, decisions_proof = _read(folder / "decisions.csv", csv_rows=True)
    if (not isinstance(account.get("orders"), dict) or not isinstance(account.get("fills"), dict)
            or not isinstance(account.get("events"), list)):
        raise ValueError("Original Ledger order/fill dictionaries and event list required")
    if len(account["orders"]) != case["orders"] or len(account["fills"]) != case["fills"]:
        raise ValueError("Original result and Ledger order/fill counts disagree")
    full_calendar = environment["full_calendar"]
    daily_dates = [r["date"] for r in daily]
    decision_dates = [r["day"] for r in decisions]
    if (len(set(daily_dates)) != len(daily_dates) or daily_dates != sorted(daily_dates)
            or len(set(decision_dates)) != len(decision_dates)
            or set(decision_dates) != set(daily_dates) or any(d not in full_calendar for d in daily_dates)):
        raise ValueError("Original daily/decision records must share distinct exact session dates")
    in_window = [day for day in daily_dates if replay["start"] <= day <= replay["end"]]
    if in_window != environment["record"]["calendar"]["sessions"]:
        raise ValueError("Original completed decision-window sessions differ from the exact environment calendar")
    decision_by_day = {r["day"]: r for r in decisions}
    daily_annotations = []
    for record in daily:
        day = record["date"]
        index = full_calendar.index(day)
        prior = full_calendar[index - 1] if index else None
        decision = decision_by_day[day]
        daily_annotations.append({"day": day, "original_daily_record": deepcopy(record),
            "original_decision_record": deepcopy(decision),
            "in_original_decision_window": replay["start"] <= day <= replay["end"],
            "valuation_day_close_phase": phase_context(day, environment, role="post_session_valuation_context"),
            "decision_day_close_phase": phase_context(day, environment, role="same_day_close_diagnostic_not_original_strategy_input"),
            "previous_exact_session": prior,
            "previous_exact_close_phase": phase_context(prior, environment, role="previous_exact_calendar_close_context",
                missing_reason="no_previous_session_in_pinned_calendar"),
            "original_batch_status_not_upgraded": True, "daily_account_change_not_recomputed": True})
    orders, by_id = [], {}
    for order_id, order in account["orders"].items():
        link = order_signal_link(order_id, order, account["events"], full_calendar)
        row = {"order_id": order_id, "original_order": deepcopy(order), "signal_link": link,
            "signal_date_phase": phase_context(link["signal_date"], environment, role="original_target_signal_day_retrospective_context",
                missing_reason=link["unknown_reason"]),
            "execution_day_close_phase": phase_context(order.get("day"), environment, role="post_execution_close_context"),
            "original_monetary_and_quantity_values_not_converted": True}
        orders.append(row)
        by_id[order_id] = row
    fills = []
    for fill_id, fill in account["fills"].items():
        order = by_id.get(fill.get("order_id"))
        link = deepcopy(order["signal_link"]) if order else {"status": "unknown_original_event_link", "signal_date": None,
            "unknown_reason": "fill_order_id_absent_from_original_ledger_orders", "target_sequence": None,
            "submitted_sequence": None, "execution_not_before": None}
        fills.append({"fill_id": fill_id, "original_fill": deepcopy(fill), "order_id": fill.get("order_id"),
            "signal_link": link,
            "signal_date_phase": phase_context(link["signal_date"], environment, role="original_target_signal_day_retrospective_context",
                missing_reason=link["unknown_reason"]),
            "execution_day_close_phase": phase_context(fill.get("day"), environment, role="post_execution_close_context"),
            "original_monetary_and_quantity_values_not_converted": True})
    return {"daily_annotations": daily_annotations, "order_annotations": orders, "fill_annotations": fills,
        "source_files": [account_proof, daily_proof, decisions_proof],
        "counts": {"daily_rows": len(daily_annotations), "orders": len(orders), "fills": len(fills),
            "orders_with_exact_original_signal_link": sum(r["signal_link"]["signal_date"] is not None for r in orders),
            "orders_with_unknown_signal_link": sum(r["signal_link"]["signal_date"] is None for r in orders),
            "fills_with_unknown_signal_link": sum(r["signal_link"]["signal_date"] is None for r in fills),
            "daily_rows_outside_environment_window": sum(r["valuation_day_close_phase"]["status"] == "out_of_environment_window" for r in daily_annotations),
            "original_order_blocked_events": sum(e.get("kind") == "order_blocked" for e in account["events"])}}


def build_annotations(*, replay_result, environment_result, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Annotations require a new independent output directory")
    started = _now()
    replay, replay_proof = load_replay(replay_result)
    environment = load_environment(environment_result, read_at=started)
    native = environment["record"]["source"]
    snapshot = Path(replay["snapshot"]).resolve()
    if (Path(native["snapshot"]).resolve() != snapshot
            or Path(native["manifest"]).resolve() != snapshot / "manifest.json"
            or (environment["record"]["start"], environment["record"]["end"]) != (replay["start"], replay["end"])):
        raise ValueError("Environment must annotate the same original snapshot and exact decision window")
    manifest, snapshot_proof = _read(snapshot / "manifest.json")
    if (snapshot_proof["sha256"] != native["manifest_sha256"]
            or manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False):
        raise ValueError("The environment's corresponding original snapshot manifest changed")
    prepared = []
    for case in replay["cases"]:
        if out.is_relative_to(Path(case["artifacts"]).resolve()):
            raise ValueError("Do not write the new sidecar inside an original case artifact folder")
        completed = case["status"] == COMPLETED
        annotation = annotate_completed(case, replay, environment) if completed else {
            "daily_annotations": None, "order_annotations": None, "fill_annotations": None,
            "source_files": [], "counts": None}
        prepared.append({"schema": CASE_SCHEMA, "method_id": METHOD_ID, "case_key": _case_key(case),
            "annotation_status": "annotated_existing_completed_diagnostic" if completed else "unavailable_original_case_not_annotated",
            "original_case": deepcopy(case), "original_status": case["status"],
            "original_metrics": deepcopy(case.get("metrics")),
            "unavailable_artifact_traces_not_consumed": not completed,
            **annotation, **FLAGS})
    created = _now()
    source = {"replay_result": replay_proof, "environment_result": environment["proof"],
        "calendar": environment["calendar_proof"], "environment_available_at": environment["record"]["available_at"],
        "matching_original_snapshot_manifest": snapshot_proof,
        "environment_scope": "source_observed_native_active_nonST_not_complete_dated_exchange_roster",
        "historical_phase_is_late_derived_not_original_strategy_input": True,
        "original_units": "unchanged_original_Ledger_and_CSV_values_no_conversion"}
    out.mkdir(parents=True, exist_ok=False)
    entries = []
    for record in prepared:
        folder = out / record["case_key"]
        folder.mkdir()
        record.update(created_at=created, read_started_at=started, source=deepcopy(source))
        file = folder / "annotations.json"
        _write(file, record)
        entries.append({"case_key": record["case_key"], "original_status": record["original_status"],
            "original_metrics": deepcopy(record["original_metrics"]), "original_case": deepcopy(record["original_case"]),
            "annotation_status": record["annotation_status"], "counts": record["counts"],
            "sidecar_file": str(file), "sidecar_sha256": sha256(file.read_bytes()).hexdigest()})
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "status": "completed_read_only_annotations_no_new_strategy_run",
        "created_at": created, "available_at": created, "read_started_at": started, "source": source,
        "counts": {"original_cases": len(entries), "annotated_completed_cases": sum(r["original_status"] == COMPLETED for r in entries),
            "unavailable_cases_not_annotated": sum(r["original_status"] != COMPLETED for r in entries),
            "original_completed_orders": sum(r["counts"]["orders"] for r in entries if r["counts"] is not None),
            "original_completed_fills": sum(r["counts"]["fills"] for r in entries if r["counts"] is not None)},
        "cases": entries, **FLAGS}
    _write(out / "index.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("replay-result", "environment-result", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_annotations(**vars(args))
    print(json.dumps({"status": result["status"], "out": str(args.out.resolve()), **result["counts"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
