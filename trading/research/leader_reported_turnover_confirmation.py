"""Fixed reported-turnover confirmation ingredient on saved common20 batches.

This exploratory arm filters existing ranked candidates only. It does not
rebuild signals, change prices/costs, refit, replace an unknown with cash, or
claim historical roster/PIT or a confirmed turnover-leader identity.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_daily_replay import CommonCohortRawFeed, END, EXIT_POLICIES, FAMILIES, POOLS, START, _case
from .leader_expiry_hook_diagnostic import (SCHEMA as BASELINE_SCHEMA, METHOD_ID as BASELINE_METHOD,
    load_inputs, _read)
from .leader_historical_turnover_inputs import SOURCE_CONTRACT, load_historical_turnover_inputs
from .shortline_decision_v3 import FiveSessionExpiryPolicy, POLICY_ID

SCHEMA = "leader_saved_common20_reported_turnover_confirmation_v1"
REGISTRATION_SCHEMA = "leader_reported_turnover_confirmation_registration_v1"
METHOD_ID = "saved_common20_independent_expiry_add_exact_daily_turnover_and_range_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "provider_contacted": False,
    "broker_interacted": False, "persistent_ledger_mutated": False,
    "model_refit": False, "future_labels_used_for_score": False, "parameters_tuned": False,
    "signals_recomputed": False, "original_artifacts_mutated": False,
    "pit_verified": False, "corporate_actions_verified": False,
    "historical_dated_roster_verified": False, "strict_eight_type_source_admitted": False,
    "true_turnover_leader_identified": False, "independent_validation": False}
PROTOCOL = {"id": METHOD_ID, "start": START, "end": END, "decision_sessions": 183,
    "execution_tail_sessions": ["2026-10-12", "2026-10-13"], "families": list(FAMILIES), "pool": POOLS[0],
    "baseline_method_id": BASELINE_METHOD, "risk_policy_id": POLICY_ID,
    "changed_variable": "original_selected_candidates_require_reported_turn_D_gt_exact_previous_and_original_native_high_gt_low",
    "turn_unit": "percent", "turn_scaled": False, "first_previous_session": "unknown_no_2025_context",
    "original_incomplete_batch": "preserved_unmodified_no_signal_upgrade",
    "selected_candidate_unknown": "whole_complete_batch_NO_CHANGE_even_if_another_condition_false",
    "all_selected_known_false": "complete_empty_target", "no_candidate_refill": True,
    "complete_candidate_scope": "original_unique_common20_candidates_at_most_five",
    "incomplete_original_unscoped_candidates": "retained_as_inactive_source_observations_not_common20_choices",
    "native_price_basis_unchanged": True, "official_price_not_substituted": True,
    "turnover_source_contract": SOURCE_CONTRACT, "tails_no_new_entry": True,
    "exploratory_ingredient_not_full_turnover_leader_or_independent_sample": True}


def _now():
    return datetime.now(MARKET_TZ)


def _stamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.utcoffset() is None: raise ValueError("Actual research clocks must be timezone-aware")
    return result.astimezone(MARKET_TZ)


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError): return None


def confirm_batches(batches, raw, turnover, *, sessions, symbols, family):
    """Return new batches and every original candidate's unchanged-order audit."""
    if list(sorted(batches)) != sessions or raw.duplicated(["date", "symbol"]).any():
        raise ValueError("Original exact full session batches and unique dated native prices required")
    previous = {day: sessions[i-1] if i else None for i, day in enumerate(sessions)}
    native = raw.set_index(["date", "symbol"])
    turns = turnover["rows"].set_index(["date", "symbol"])
    if not turns.index.is_unique: raise ValueError("Exact reported-turnover identities must be unique")
    modified, audit = deepcopy(batches), []
    for day in sessions:
        batch = batches[day]
        candidates = batch.get("candidates", [])
        if not isinstance(candidates, list): raise ValueError("Original candidate list required")
        decision = START <= day <= END
        enabled = decision and batch.get("status") == "complete"
        if enabled and (len(candidates) > 5 or len({r["symbol"] for r in candidates}) != len(candidates)
                or any(r["symbol"] not in symbols for r in candidates)):
            raise ValueError("Only the saved unique at-most-five common20 choices may be filtered")
        if not decision and batch.get("status") == "complete":
            raise ValueError("Original execution tail must not introduce a new complete signal")
        kept, unknown = [], False
        audit_start = len(audit)
        for rank, candidate in enumerate(candidates, 1):
            symbol, prior = candidate["symbol"], previous[day]
            observations, reasons = [], []
            for target in (prior, day):
                row = turns.loc[(target, symbol)] if target is not None and (target, symbol) in turns.index else None
                value = _number(row.get("turn_percent")) if row is not None else None
                observations.append({"day": target, "reported_turn_percent": value,
                    "tradestatus": str(row["tradestatus"]) if row is not None else None,
                    "isST": str(row["isST"]) if row is not None else None})
                if target is None: reasons.append("exact_previous_session_outside_fixed_2026_input")
                elif row is None: reasons.append("exact_day_symbol_reported_turn_missing")
                elif value is None or value < 0: reasons.append("reported_turn_blank_invalid_or_negative")
            price = native.loc[(day, symbol)] if (day, symbol) in native.index else None
            high = _number(price.get("high")) if price is not None else None
            low = _number(price.get("low")) if price is not None else None
            if high is None or low is None or high <= 0 or low <= 0 or high < low:
                reasons.append("original_native_current_price_range_unknown")
            if reasons:
                condition, reason = "unknown", ";".join(dict.fromkeys(reasons))
            elif observations[1]["reported_turn_percent"] <= observations[0]["reported_turn_percent"]:
                condition, reason = "known_false", "reported_turn_not_strictly_expanding"
            elif high <= low:
                condition, reason = "known_false", "original_native_one_price_daily_range"
            else:
                condition, reason = "known_true", "reported_turn_expands_and_original_native_range_nonzero"
            if enabled and condition == "unknown": unknown = True
            if enabled and condition == "known_true": kept.append(deepcopy(candidate))
            audit.append({"family": family, "day": day, "symbol": symbol, "original_list_position": rank,
                "original_batch_status": batch.get("status"), "original_batch_candidate_count": len(candidates),
                "selected_for_confirmation": enabled, "candidate_in_common20": symbol in symbols,
                "original_candidate": deepcopy(candidate), "previous_exact_session": prior,
                "previous_turn_observation": observations[0], "current_turn_observation": observations[1],
                "original_native_high": high, "original_native_low": low,
                "condition_status": condition, "condition_reason": reason,
                "application": "eligible_original_choice" if enabled else "not_evaluated_original_incomplete_or_tail",
                "future_raw_or_outcome_used": False})
        if enabled:
            modified[day].update(candidates=[] if unknown else kept,
                status="incomplete" if unknown else "complete",
                turnover_confirmation={"method_id": METHOD_ID, "original_candidate_count": len(candidates),
                    "known_true_count": len(kept), "whole_batch_unknown": unknown,
                    "no_refill": True, "source_contract": SOURCE_CONTRACT})
            if unknown: modified[day]["incomplete_reason"] = "selected_original_candidate_turnover_confirmation_unknown"
            else: modified[day].pop("incomplete_reason", None)
        for row in audit[audit_start:]:
            row["whole_batch_application"] = ("NO_CHANGE_selected_unknown" if enabled and unknown else
                "complete_filtered_target" if enabled else "original_incomplete_or_tail_preserved")
            row["retained_in_effective_complete_batch"] = enabled and not unknown and row["condition_status"] == "known_true"
    return modified, audit


def _normalize_summary(summary, evidence, sessions):
    result = deepcopy(summary)
    complete = [row["date"] for row in evidence["daily"]] == sessions and summary.get("error") is None
    if not complete or not summary.get("fills"):
        result["metrics"] = result["tail_included_diagnostic_metrics"] = None
        result.pop("engine_no_change_account_metrics", None)
        result["performance_unavailable_reason"] = "blocked_or_incomplete_original_window" if not complete else "no_actual_simulated_fills"
        if complete and not summary.get("fills"):
            result["status"] = "unavailable_no_simulated_fills"
    result["exact_window_completed"] = complete
    result["pending_risk_exits"] = deepcopy(evidence["state"].get("pending_risk_exits"))
    result["pending_target_weights"] = deepcopy(evidence["state"].get("pending_targets"))
    result["account_context"] = deepcopy(evidence["state"].get("replay_risk_exit_policy", {}).get("context"))
    result.update(diagnostic_method=METHOD_ID, risk_policy_id=POLICY_ID, **FLAGS)
    return result


def _baseline_summary(saved):
    """A new comparison's display boundary; the stored baseline stays intact."""
    result = deepcopy(saved)
    result["recorded_status"] = saved["status"]
    if not saved.get("fills") or saved.get("error"):
        result["metrics"] = result["tail_included_diagnostic_metrics"] = None
        result.pop("engine_no_change_account_metrics", None)
        result["performance_unavailable_reason"] = "baseline_engine_failure" if saved.get("error") else "no_actual_simulated_fills"
        if not saved.get("error"): result["status"] = "unavailable_no_simulated_fills"
    return result


def run(*, annotations_index, baseline_result, turnover_manifest, registration, out, data_out):
    out, data_out = Path(out).resolve(), Path(data_out).resolve()
    if out.exists() or data_out.exists(): raise FileExistsError("Use fresh report and isolated research account directories")
    now = _now()
    registration_value, registration_proof = _read(registration)
    pointers = {name: {"file": str(Path(path).resolve()), "sha256": digest(path)} for name, path in
        (("annotations_index", annotations_index), ("baseline_result", baseline_result), ("turnover_manifest", turnover_manifest))}
    if (registration_value.get("schema") != REGISTRATION_SCHEMA or registration_value.get("protocol") != PROTOCOL
            or any(registration_value.get(k) != v for k, v in pointers.items())
            or _stamp(registration_value["registered_at"]) > now
            or any(registration_value.get(k) is not v for k, v in FLAGS.items())):
        raise ValueError("Fixed exploratory turnover-confirmation registration changed")
    inputs = load_inputs(annotations_index)
    baseline, baseline_proof = _read(baseline_result, pointers["baseline_result"]["sha256"])
    baseline_protocol, baseline_protocol_proof = _read(baseline["protocol"])
    if (baseline.get("schema") != BASELINE_SCHEMA or baseline.get("method_id") != BASELINE_METHOD
            or baseline.get("status") != "completed_fixed_exit_hook_diagnostic"
            or [c["family"] for c in baseline["comparisons"]] != list(FAMILIES)
            or baseline["original_unavailable_cases_retained_without_new_replay"] != inputs["retained"]
            or baseline_protocol["source_files"] != inputs["proofs"]
            or baseline_protocol["config"] != inputs["original_protocol"]["config"]
            or baseline_protocol["frozen_symbols"] != inputs["symbols"]):
        raise ValueError("Saved independent-expiry baseline has a different original source/scope/cost binding")
    turnover = load_historical_turnover_inputs(turnover_manifest)
    if (turnover["contract"] != SOURCE_CONTRACT or turnover["record"]["sessions"] != inputs["sessions"][:183]
            or turnover["record"]["calendar"]["sha256"] != next(p["sha256"] for p in inputs["proofs"] if p["file"] == turnover["record"]["calendar"]["file"])
            or not _stamp(turnover["record"]["available_at"]) <= _stamp(registration_value["registered_at"]) <= now):
        raise ValueError("Reported turnover dates/calendar/source availability differ from the registered comparison")
    filtered = []
    for item, saved in zip(inputs["originals"], baseline["comparisons"]):
        if saved["original_batches"] != item["batch_proof"] or saved["independent_hook"]["risk_policy_id"] != POLICY_ID:
            raise ValueError("A baseline case changed its saved batches or independent expiry policy")
        batches, audit = confirm_batches(item["batches"], inputs["raw"], turnover,
            sessions=inputs["sessions"], symbols=inputs["symbols"], family=item["summary"]["family"])
        filtered.append((item, saved, batches, audit))
    # All input checks and deterministic filtering precede a new research book.
    out.mkdir(parents=True, exist_ok=False); data_out.mkdir(parents=True, exist_ok=False)
    write_json(out / "protocol.json", {"schema": SCHEMA, "registered_protocol": PROTOCOL,
        "registration": registration_proof, "source_files": inputs["proofs"], "raw_partitions": inputs["partitions"],
        "tail_source": inputs["tail_source"], "baseline_result": baseline_proof,
        "baseline_protocol": baseline_protocol_proof, "turnover_source": turnover["source_proof"], **FLAGS})
    base = CommonCohortRawFeed(inputs["raw"], inputs["sessions"], inputs["symbols"])
    comparisons = []
    for item, saved, batches, audit in filtered:
        family = item["summary"]["family"]
        context = RunContext("leader-turnover-confirmation:" + digest(registration)[:16] + ":" + family, TradingMode.REPLAY)
        summary, evidence = _case(base, batches, family, POOLS[0], EXIT_POLICIES[0], START, END,
            replay_risk_exit_policy=FiveSessionExpiryPolicy(), context=context)
        summary = _normalize_summary(summary, evidence, inputs["sessions"])
        target = data_out / family
        target.mkdir()
        write_json(target / "summary.json", summary)
        write_json(target / "account.json", evidence["state"])
        write_json(target / "decision_batches.json", batches)
        write_json(target / "candidate_confirmation.json", audit)
        pd.DataFrame([{k:v for k,v in row.items() if k != "events"} for row in evidence["daily"]]).to_csv(target / "daily.csv", index=False)
        pd.DataFrame(evidence["decisions"]).to_csv(target / "decisions.csv", index=False)
        if digest(item["batch_proof"]["file"]) != item["batch_proof"]["sha256"]:
            raise ValueError("Original saved batches changed during the new arm")
        comparisons.append({"family": family, "baseline": _baseline_summary(saved["independent_hook"]),
            "baseline_saved_summary": saved["independent_hook"],
            "baseline_artifacts": saved["new_research_artifacts"], "baseline_source": baseline_proof,
            "turnover_confirmation": summary, "new_research_artifacts": str(target),
            "original_batches": item["batch_proof"], "candidate_observation_rows": len(audit),
            "confirmation_applied_rows": sum(r["selected_for_confirmation"] for r in audit),
            "confirmation_effectively_retained_rows": sum(r["retained_in_effective_complete_batch"] for r in audit),
            "confirmation_unknown_rows": sum(r["selected_for_confirmation"] and r["condition_status"] == "unknown" for r in audit),
            "baseline_replayed": False})
        print(family + ": " + summary["status"], flush=True)
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "created_at": _now().isoformat(),
        "status": "completed_exploratory_turnover_confirmation" if all(c["turnover_confirmation"]["error"] is None for c in comparisons)
            else "preserved_engine_failure_in_exploratory_turnover_confirmation",
        "registration": registration_proof, "protocol": str(out / "protocol.json"), "new_engine_cases": 5,
        "baseline_source": baseline_proof, "baseline_replayed": False, "turnover_source": turnover["source_proof"],
        "comparisons": comparisons, "original_unavailable_cases_retained_without_new_replay": inputs["retained"],
        "artifacts": str(data_out), "existing_window_and_fixed_common20_engineering_sample_not_independent": True,
        "strict_eight_type_unknowns_not_changed": True, **FLAGS}
    write_json(out / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("annotations-index", "baseline-result", "turnover-manifest", "registration", "out", "data-out"):
        parser.add_argument("--" + name, type=Path, required=True)
    result = run(**vars(parser.parse_args(argv)))
    print(json.dumps({"status": result["status"], "created_at": result["created_at"], "new_engine_cases": 5, **FLAGS}, ensure_ascii=False))
    return 0 if result["status"] == "completed_exploratory_turnover_confirmation" else 2


if __name__ == "__main__":
    raise SystemExit(main())
