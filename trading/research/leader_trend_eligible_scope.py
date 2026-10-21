"""Independent, first-day eligible-universe trend research through the old Engine.

The original eight-type fixed-scope results are not changed or reused as zero
return baselines.  Eligibility uses already saved, past-only observation counts;
this module neither reconstructs historical features nor claims historical PIT.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
import math
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from ..symbols import normalize_symbol
from .baostock_daily_slice import digest
from .leader_eight_type_historical_native import (
    FLAGS, _physical_native, load_inputs as load_native_inputs,
)
from .leader_eight_type_panel_replay import (
    CAPS, GUARDS, PROTOCOL, _case, _environment, _rank_path, _validate_panel_data,
)
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .leader_valid_warmup import METHOD_ID as RESTORATION_METHOD
from .shortline_panel_replay import _config
from .stockdb_action_accounting import load_action_accounting


METHOD_ID = "native_trend_first_day_61_valid_observations_eligible_scope_posthoc_v1"
SELECTION_SCHEMA = "leader_trend_anchor_eligible_scope_v1"
REGISTRATION_SCHEMA = "leader_trend_anchor_eligible_scope_registration_v1"
RESULT_SCHEMA = "leader_trend_anchor_eligible_scope_shared_replay_v1"
TYPE_ID = "trend_leader"
SUPPLEMENT_METHOD_ID = "native_trend_eligible_scope_001309_action_terms_single_case_supplement_v1"
SUPPLEMENT_REGISTRATION_SCHEMA = "leader_trend_eligible_scope_action_supplement_registration_v1"
SUPPLEMENT_RESULT_SCHEMA = "leader_trend_eligible_scope_action_supplement_replay_v1"
SUPPLEMENT_CASE_ID = "trend_anchor_eligible__unguarded__uncapped"
SUPPLEMENT_EVENT = ["001309.SZSE", "2026-04-01"]
SUPPLEMENT_SPEC = {"id": SUPPLEMENT_METHOD_ID, "event": SUPPLEMENT_EVENT,
    "original_case_id": SUPPLEMENT_CASE_ID, "only_change": "explicit_reviewed_blank_reserve_zero_terms",
    "replay": "one_original_blocked_case_from_original_cash_inception_not_checkpoint_recovery",
    "signal_scope_config_rank_environment_capacity_and_exit_changed": False,
    "original_three_complete_cases_replayed": False, "parameters_selected_by_return": False,
    "independent_holdout_claimed": False}
SPEC = {
    "id": METHOD_ID,
    "anchor": "first_decision_day_only_never_future_membership",
    "minimum_valid_observations_including_anchor": 61,
    "count_source": "saved_unchanged_physical_native_active_non_st_final_valid_count",
    "required_anchor_risk_fields": ["close", "sma20", "sma60"],
    "return_feature_availability": "61_valid_observations_establish_ret20_ret60_under_unchanged_features_formula",
    "anchor_raw": "unchanged_physical_native_active_non_st",
    "anchor_IPO": "saved_ipo_feature_support_is_true_posthoc_not_PIT",
    "anchor_trend_unresolved": "exclude_all_original_unresolved_symbols",
    "anchor_amount_threshold_added": False,
    "daily_rule": "original_trend_rule_fixed_20m_amount_filter_and_amount_ranks_unchanged",
    "daily_membership": "fixed_anchor_scope_no_replacement_or_later_admission",
    "later_unknowns": "original_case_NO_CHANGE_policy_with_independent_risk_and_expiry",
    "capacity": "original_causal_first_seen_cap20_and_uncapped_both_retained_no_daily_reset",
    "case_count": 4,
    "original_32_results_changed": False,
    "independent_holdout_claimed": False,
    "parameters_selected_by_return": False,
}


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Saved trend research metadata must be a JSON object")
    return value


def _save_new(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _finite(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def select_anchor_scope(scope_members, panel, diagnostic, raw_anchor):
    """Freeze eligibility from saved first-D facts, without inspecting later D.

    Exclusions describe evidence missing for this new research universe, not a
    judgment that the excluded security is bad or permanently untradeable.
    """
    scope = list(scope_members)
    day = panel.get("day")
    if (not scope or len(scope) != len(set(scope)) or not day
            or diagnostic.get("first_decision_day") != day
            or diagnostic.get("first_day_scope_members") != scope):
        raise ValueError("One exact first-day panel and its original frozen-scope count evidence required")
    if (not isinstance(raw_anchor, pd.DataFrame) or raw_anchor.empty
            or "date" not in raw_anchor
            or not {"code", "symbol"}.intersection(raw_anchor.columns)
            or not raw_anchor.date.eq(day).all()):
        raise ValueError("Distinct whole-native rows from the exact anchor day required")
    anchor = raw_anchor.copy()
    if "code" not in anchor:
        anchor["code"] = anchor.symbol.map(lambda symbol: normalize_symbol(symbol).baostock_code)
    if anchor.duplicated(["date", "code"]).any():
        raise ValueError("Distinct whole-native rows from the exact anchor day required")
    types = [item for item in panel.get("types", []) if item.get("id") == TYPE_ID]
    if len(types) != 1 or not isinstance(types[0].get("unresolved_symbols"), list):
        raise ValueError("The saved original trend membership observation is required")
    counts = diagnostic.get("final_valid_count")
    risk = panel.get("risk_features")
    if not isinstance(counts, dict) or not isinstance(risk, dict):
        raise ValueError("Saved valid-observation counts and anchor risk features required")
    physical = _physical_native(anchor)
    if physical.symbol.duplicated().any():
        raise ValueError("Distinct native anchor securities required")
    by_symbol = physical.set_index("symbol").to_dict("index")
    unknown = set(types[0]["unresolved_symbols"])
    selected, excluded = [], []
    for symbol in scope:
        reasons = []
        raw = by_symbol.get(symbol)
        if raw is None:
            reasons.append("anchor_native_row_missing")
        elif raw.get("active_non_st") is not True:
            reasons.append("anchor_native_not_active_non_st")
        count = counts.get(symbol)
        if isinstance(count, bool) or not isinstance(count, int) or count < 61:
            reasons.append("insufficient_or_unknown_valid_observations")
        row = risk.get(symbol)
        if not isinstance(row, dict) or any(not _finite(row.get(key)) for key in SPEC["required_anchor_risk_fields"]):
            reasons.append("anchor_risk_feature_missing_or_nonfinite")
        if not isinstance(row, dict) or row.get("ipo_feature_support") is not True:
            reasons.append("anchor_IPO_feature_not_supported")
        if symbol in unknown:
            reasons.append("anchor_trend_membership_unresolved")
        if reasons:
            excluded.append({"symbol": symbol, "reasons": reasons,
                "valid_observation_count": count,
                "restoration_outcome": diagnostic.get("outcomes", {}).get(symbol)})
        else:
            selected.append(symbol)
    return {"schema": SELECTION_SCHEMA, "method_id": METHOD_ID, "anchor_day": day,
        "original_scope_members": scope, "original_scope_count": len(scope),
        "scope_members": selected, "eligible_count": len(selected), "excluded": excluded,
        "excluded_count": len(excluded),
        "reason_counts": dict(Counter(reason for row in excluded for reason in row["reasons"])),
        "exclusions_mean_evidence_or_anchor_status_not_permanent_stock_quality": True,
        "future_rows_used_for_membership": False, "features_recomputed": False,
        "full_61_observation_rows_claimed_in_current_payloads": False, **FLAGS}


def _input_path(path):
    path = Path(path).resolve()
    return path / "manifest.json" if path.is_dir() else path


def _view_pin(path):
    folder = Path(path).resolve()
    result = folder / "result.json"
    _read(result)
    return {"directory": str(folder), "result": str(result), "sha256": digest(result)}


def _selection(loaded, manifest):
    restore = manifest.get("warmup_restoration", {})
    if (restore.get("enabled") is not True or restore.get("method_id") != RESTORATION_METHOD
            or restore.get("required_valid_observations") != 61
            or restore.get("first_decision_day") != manifest["start"]):
        raise ValueError("The pinned, original first-D 61-valid-observation restoration diagnostic is required")
    diagnostic = _read(restore["diagnostic_file"])
    if (diagnostic.get("method_id") != RESTORATION_METHOD
            or diagnostic.get("future_rows_used_for_selection_or_counting") is not False
            or diagnostic.get("halts_or_ST_counted_as_valid") is not False):
        raise ValueError("Unchanged past-only active-non-ST count semantics required")
    first = manifest["start"]
    anchor_raw = loaded["raw"].loc[loaded["raw"].date.eq(first)]
    return select_anchor_scope(loaded["scope_members"], loaded["panel_by_day"][first], diagnostic, anchor_raw)


def register(inputs, out, *, action_event_view, action_reference_view):
    """Record one new universe and unchanged four-case protocol before replay."""
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Eligible-scope registration requires a new output directory")
    path = _input_path(inputs)
    loaded, manifest = load_native_inputs(path)
    selection = _selection(loaded, manifest)
    if not selection["scope_members"]:
        raise ValueError("An empty eligible universe is unavailable research, not an all-cash baseline")
    restore = manifest["warmup_restoration"]
    value = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID, "registered_at": _now(),
        "research_kind": "new_anchor_universe_posthoc_diagnostic_not_original_32_or_validation",
        "spec": deepcopy(SPEC), "parent_fixed_protocol": deepcopy(PROTOCOL),
        "parent_manifest": str(path), "parent_manifest_sha256": digest(path),
        "parent_created_at": manifest["created_at"], "start": manifest["start"], "end": manifest["end"],
        "source_days": manifest["source_days"], "source_scope_members": manifest["scope_members"],
        "diagnostic": {"path": restore["diagnostic_file"], "sha256": restore["diagnostic_sha256"],
            "created_at": _read(restore["diagnostic_file"])["created_at"]},
        "action_event_view": _view_pin(action_event_view),
        "action_reference_view": _view_pin(action_reference_view),
        "config": _config(100000, 5).to_dict(), "selection": selection,
        "scope_members": selection["scope_members"],
        "scope_frozen_at": _now(), "selection_uses_saved_first_day_only": True,
        "old_32_results_reused_as_zero_return_baseline": False,
        "independent_holdout_claimed": False, "parameters_selected_by_return": False, **FLAGS}
    _save_new(folder / "registration.json", value)
    return value


def _load_registration(path):
    path = Path(path).resolve()
    value = _read(path)
    if (value.get("schema") != REGISTRATION_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("spec") != SPEC or value.get("parent_fixed_protocol") != PROTOCOL
            or value.get("config") != _config(100000, 5).to_dict()
            or any(value.get(key) is not flag for key, flag in FLAGS.items())
            or datetime.fromisoformat(value["registered_at"]) > datetime.now(MARKET_TZ)
            or digest(value["parent_manifest"]) != value["parent_manifest_sha256"]
            or digest(value["diagnostic"]["path"]) != value["diagnostic"]["sha256"]):
        raise ValueError("The exact registered eligible-scope method and source pins are required")
    for name in ("action_event_view", "action_reference_view"):
        if _view_pin(value[name]["directory"]) != value[name]:
            raise ValueError("Registered action event/reference view changed")
    loaded, manifest = load_native_inputs(value["parent_manifest"])
    if (manifest["created_at"] != value["parent_created_at"]
            or manifest["start"] != value["start"] or manifest["end"] != value["end"]
            or manifest["source_days"] != value["source_days"]
            or manifest["scope_members"] != value["source_scope_members"]
            or _selection(loaded, manifest) != value["selection"]
            or value["scope_members"] != value["selection"]["scope_members"]):
        raise ValueError("Registered first-day eligibility or parent observations changed")
    return value, loaded, manifest


def run(registration, out):
    """Replay only trend x original two guards x original two capacities."""
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Eligible-scope replay requires a new output directory")
    value, loaded, manifest = _load_registration(registration)
    members, sessions, previous, now = _validate_panel_data(loaded["panel_by_day"], loaded["raw"],
        loaded["source_days"], loaded["calendar_payload"], value["scope_members"], loaded["qualification"])
    if (value["start"] not in loaded["source_days"] or value["end"] not in loaded["source_days"]
            or not all(day in loaded["panel_by_day"] for day in loaded["source_days"]
                if value["start"] <= day <= value["end"])):
        raise ValueError("Exact registered decision dates and saved dated panels required")
    paths, original = _rank_path(loaded["panel_by_day"], members, value["start"], value["end"])
    environments = _environment(loaded["panel_by_day"], sessions, now)
    context_raw = pd.read_parquet(manifest["context_raw_file"])
    if (sorted(context_raw.date.unique()) != [manifest["context_anchor"]["day"]]
            or context_raw.date.max() >= loaded["source_days"][0]
            or context_raw.duplicated(["date", "code"]).any()):
        raise ValueError("The original exact preceding whole-native context is required")
    accounting = load_action_accounting(value["action_event_view"]["directory"],
        value["action_reference_view"]["directory"], symbols=value["scope_members"],
        raw_days=sorted(set(context_raw.date) | set(loaded["source_days"])),
        replay_days=loaded["source_days"], price_source="independent_baostock_raw")
    factory = HistoricalExecutionFactory(accounting)
    cases = []
    for guard in GUARDS:
        for cap, _ in CAPS:
            case = _case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
                paths[TYPE_ID][cap], environments, members, loaded["qualification"], loaded["context_anchor"],
                value["start"], value["end"], TYPE_ID, cap, guard,
                f"trend-anchor-eligible:{folder.name}:{guard}:{cap}",
                execution_policy_factory=factory, context_raw=context_raw)
            case["parent_case_id"] = case["case_id"]
            case["case_id"] = f"trend_anchor_eligible__{guard}__{cap}"
            case.update(eligible_scope_method=METHOD_ID, original_32_case_replayed=False, **FLAGS)
            cases.append(case)
    result = {"schema": RESULT_SCHEMA, "method_id": METHOD_ID, "created_at": _now(),
        "status": "completed_posthoc_eligible_universe_research_comparison",
        "registration": str(Path(registration).resolve()), "registration_sha256": digest(registration),
        "start": value["start"], "end": value["end"], "source_days": loaded["source_days"],
        "scope_members": value["scope_members"], "source_scope_count": len(value["source_scope_members"]),
        "selection": value["selection"], "source": loaded["provenance"], "spec": deepcopy(SPEC),
        "parent_fixed_protocol": deepcopy(PROTOCOL), "config": value["config"],
        "case_count": len(cases), "cases": cases,
        "environment_decisions": environments, "raw_top5_paths": {TYPE_ID: original[TYPE_ID]},
        "capacity_paths": {TYPE_ID: paths[TYPE_ID]}, "historical_execution_policy": factory.provenance,
        "shared_case_function": "leader_eight_type_panel_replay._case",
        "all_cases_complete_PnL": all(case["complete_PnL_available"] for case in cases),
        "account_created": True, "persistent_account_used": False, "broker_interacted": False,
        "old_32_results_modified": False, "old_32_results_reused_as_zero_return_baseline": False,
        "independent_holdout_claimed": False, "case_selection_by_performance": False,
        "limitations": [
            "This is a new first-day eligible universe, not a repair of the old 32-case results.",
            "Saved counts establish return-feature availability under the unchanged valid-observation formula; full warmup rows are not reconstructed here.",
            "Excluded securities retain explicit evidence/status reasons and are not labelled permanently unsuitable.",
            "Historical listing metadata and late panel availability remain posthoc; economic total returns, PIT and strategy validation are unverified.",
            "Original daily rules, amount ranks, both cumulative capacities, environment decisions, risk exits, costs and six-session tail remain in force.",
        ], **FLAGS}
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "result.json", result)
    for case in cases:
        case_dir = folder / case["case_id"]
        case_dir.mkdir()
        for key in ("decisions", "daily", "orders", "fills", "account"):
            write_json(case_dir / (key + ".json"), case[key])
    return result


def _supplement_binding(original_registration, original_result, new_event_result, new_reference_result,
                        *, event_view, reference_view):
    """Bind this one known source failure, never choose a case by its return."""
    expected = {f"trend_anchor_eligible__{guard}__{cap}" for guard in GUARDS for cap, _ in CAPS}
    cases = original_result.get("cases", [])
    if (original_result.get("schema") != RESULT_SCHEMA or original_result.get("method_id") != METHOD_ID
            or original_result.get("case_count") != 4 or len(cases) != 4
            or {case.get("case_id") for case in cases} != expected
            or original_result.get("parent_fixed_protocol") != PROTOCOL
            or any(original_result.get(key) != original_registration.get(key)
                for key in ("start", "end", "source_days", "scope_members", "spec", "config"))
            or original_result.get("source", {}).get("manifest") != original_registration["parent_manifest"]
            or original_result.get("source", {}).get("manifest_sha256") != original_registration["parent_manifest_sha256"]):
        raise ValueError("The exact original registered four-case result is required")
    selected = next(case for case in cases if case["case_id"] == SUPPLEMENT_CASE_ID)
    others = [case for case in cases if case["case_id"] != SUPPLEMENT_CASE_ID]
    block = selected.get("blocking_evidence") or {}
    failures = [row for row in block.get("reference_evidence", [])
        if row.get("status") not in {"reference_match", "no_reported_event_reference_continuous"}]
    if (selected.get("guard") != "unguarded" or selected.get("capacity") != "uncapped"
            or selected.get("type") != TYPE_ID or selected.get("complete_PnL_available") is not False
            or selected.get("metrics") is not None or selected.get("engine_sessions_committed") != 22
            or block.get("day") != SUPPLEMENT_EVENT[1]
            or block.get("reason") != "held_or_pending_raw_or_action_reference_unknown"
            or block.get("session_not_committed") is not True or block.get("raw_issues") != []
            or len(failures) != 1 or failures[0].get("symbol") != SUPPLEMENT_EVENT[0]
            or failures[0].get("status") != "unresolved_blank_terms"
            or any(case.get("complete_PnL_available") is not True or case.get("blocking_evidence") is not None
                for case in others)):
        raise ValueError("Only the original 22-session 001309-blocked case may receive this supplement")
    amendment = new_event_result.get("reviewed_cash_amendment", {})
    if (new_event_result.get("status") != "research_action_events_projected"
            or new_reference_result.get("status") != "research_reference_presence_diagnostic"
            or new_reference_result.get("reviewed_cash_amendment") != amendment
            or Path(new_reference_result["event_view"]).resolve() != Path(event_view).resolve()
            or Path(amendment.get("parent_event_view", "")).resolve()
                != Path(original_registration["action_event_view"]["directory"]).resolve()
            or Path(amendment.get("parent_reference_view", "")).resolve()
                != Path(original_registration["action_reference_view"]["directory"]).resolve()
            or amendment.get("events") != [SUPPLEMENT_EVENT]
            or amendment.get("raw_prices_rescanned") is not False
            or amendment.get("other_events_changed") is not False
            or new_event_result.get("execution_admitted") is not False
            or new_reference_result.get("execution_admitted") is not False
            or Path(event_view).resolve() == Path(amendment["parent_event_view"]).resolve()
            or Path(reference_view).resolve() == Path(amendment["parent_reference_view"]).resolve()):
        raise ValueError("A new, aligned, single-event amendment of the exact old action pair is required")
    return {"original_case_id": SUPPLEMENT_CASE_ID, "event": list(SUPPLEMENT_EVENT),
        "original_committed_sessions": selected["engine_sessions_committed"],
        "original_metrics": None, "original_blocking_day": block["day"],
        "original_residual_positions": deepcopy(selected.get("residual_positions", [])),
        "retained_complete_case_ids": [case["case_id"] for case in others],
        "amendment": deepcopy(amendment), "replay_from_original_cash": True,
        "checkpoint_recovery_performed": False, "other_three_cases_replayed": False}


def _checked_supplement_pair(original, event_view, reference_view):
    """Check the narrow frozen amendment, without reopening any price source."""
    from .adjusted_prices import checked_file
    from .action_reviewed_amendment import patch_reference_rows
    old_event = Path(original["action_event_view"]["directory"])
    old_reference = Path(original["action_reference_view"]["directory"])
    event_view, reference_view = Path(event_view).resolve(), Path(reference_view).resolve()
    before, after = _read(old_event / "result.json"), _read(event_view / "result.json")
    before_ref, after_ref = _read(old_reference / "result.json"), _read(reference_view / "result.json")
    for folder, old, new, allowed in ((event_view, before, after, {"canonical_actions.parquet"}),
            (reference_view, before_ref, after_ref, {"raw_reference_changes.parquet", "input_events.json"})):
        if any(new["files"].get(name) != sha for name, sha in old["files"].items() if name not in allowed):
            raise ValueError("The supplement changed an unrelated frozen action/reference payload")
        for name in set(new["files"]) - set(old["files"]):
            checked_file(folder, name, new["files"][name])
    frames = [pd.read_parquet(checked_file(folder, "canonical_actions.parquet", value["files"]["canonical_actions.parquet"])).fillna("")
        for folder, value in ((old_event, before), (event_view, after))]
    left, right = frames
    mask = left.symbol.eq(SUPPLEMENT_EVENT[0]) & left.dividOperateDate.eq(SUPPLEMENT_EVENT[1])
    amounts = ["dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"]
    if (list(left.columns) != list(right.columns) or len(left) != len(right) or int(mask.sum()) != 1
            or not left.loc[~mask].equals(right.loc[~mask])
            or not left.drop(columns=amounts).equals(right.drop(columns=amounts))):
        raise ValueError("Only the one original event's amount terms may change")
    old, new = left.loc[mask].iloc[0], right.loc[mask].iloc[0]
    if (old.dividReserveToStockPs != "" or Decimal(new.dividReserveToStockPs) != 0
            or any(Decimal(old[field]) != Decimal(new[field]) for field in amounts[:2])):
        raise ValueError("This supplement completes blank reserve only, preserving original cash and bonus")
    gaps = [pd.read_parquet(checked_file(folder, "raw_reference_changes.parquet",
        value["files"]["raw_reference_changes.parquet"]))
        for folder, value in ((old_reference, before_ref), (reference_view, after_ref))]
    left_gap, right_gap = gaps
    gap_mask = left_gap.code.eq("sz.001309") & left_gap.date.eq(SUPPLEMENT_EVENT[1])
    if (list(left_gap.columns) != list(right_gap.columns) or len(left_gap) != len(right_gap)
            or int(gap_mask.sum()) != 1
            or not patch_reference_rows(left_gap, gap_mask, right_gap.loc[gap_mask]).equals(right_gap)):
        raise ValueError("Supplementary reference changes must be confined to the same one frozen event")
    return after, after_ref


def register_supplement(registration, result, out, *, action_event_view, action_reference_view):
    """Pin late evidence separately, retaining the original result and registration."""
    folder, registration, result = Path(out).resolve(), Path(registration).resolve(), Path(result).resolve()
    if folder.exists():
        raise FileExistsError("Action supplement registration requires a new output directory")
    original, _, _ = _load_registration(registration)
    saved = _read(result)
    if (saved.get("registration") != str(registration)
            or saved.get("registration_sha256") != digest(registration)):
        raise ValueError("The saved four-case result must bind the exact original registration")
    new_event, new_reference = _checked_supplement_pair(original, action_event_view, action_reference_view)
    binding = _supplement_binding(original, saved, new_event, new_reference,
        event_view=action_event_view, reference_view=action_reference_view)
    value = {"schema": SUPPLEMENT_REGISTRATION_SCHEMA, "method_id": SUPPLEMENT_METHOD_ID,
        "registered_at": _now(), "spec": deepcopy(SUPPLEMENT_SPEC),
        "original_registration": str(registration), "original_registration_sha256": digest(registration),
        "original_result": str(result), "original_result_sha256": digest(result),
        "action_event_view": _view_pin(action_event_view), "action_reference_view": _view_pin(action_reference_view),
        "binding": binding, "scope_members": original["scope_members"], "config": original["config"],
        "original_three_complete_cases": [{"result": str(result), "result_sha256": digest(result),
            "case_id": case_id} for case_id in binding["retained_complete_case_ids"]],
        "late_evidence_not_historical_PIT": True, "independent_holdout_claimed": False, **FLAGS}
    _save_new(folder / "registration.json", value)
    return value


def _load_supplement(path):
    value = _read(path)
    if (value.get("schema") != SUPPLEMENT_REGISTRATION_SCHEMA
            or value.get("method_id") != SUPPLEMENT_METHOD_ID or value.get("spec") != SUPPLEMENT_SPEC
            or any(value.get(key) is not flag for key, flag in FLAGS.items())
            or datetime.fromisoformat(value["registered_at"]) > datetime.now(MARKET_TZ)
            or any(digest(value[key]) != value[key + "_sha256"]
                for key in ("original_registration", "original_result"))):
        raise ValueError("The exact immutable single-case action-supplement registration is required")
    for key in ("action_event_view", "action_reference_view"):
        if _view_pin(value[key]["directory"]) != value[key]:
            raise ValueError("The registered supplementary action pair changed")
    original, loaded, manifest = _load_registration(value["original_registration"])
    saved = _read(value["original_result"])
    if (saved.get("registration") != value["original_registration"]
            or saved.get("registration_sha256") != value["original_registration_sha256"]):
        raise ValueError("The original result no longer binds the same original registration")
    new_event, new_reference = _checked_supplement_pair(original,
        value["action_event_view"]["directory"], value["action_reference_view"]["directory"])
    binding = _supplement_binding(original, saved, new_event, new_reference,
        event_view=value["action_event_view"]["directory"], reference_view=value["action_reference_view"]["directory"])
    retained = [{"result": value["original_result"], "result_sha256": value["original_result_sha256"],
        "case_id": case_id} for case_id in binding["retained_complete_case_ids"]]
    if (binding != value["binding"] or value["scope_members"] != original["scope_members"]
            or value["config"] != original["config"] or value["original_three_complete_cases"] != retained):
        raise ValueError("The original source, scope, configuration or failed-case binding changed")
    return value, original, loaded, manifest


def run_supplement(registration, out):
    """Recreate only the failed path from original cash; do not recover a checkpoint."""
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Single-case supplementary replay requires a new output directory")
    value, original, loaded, manifest = _load_supplement(registration)
    members, sessions, previous, now = _validate_panel_data(loaded["panel_by_day"], loaded["raw"],
        loaded["source_days"], loaded["calendar_payload"], original["scope_members"], loaded["qualification"])
    paths, raw_top5 = _rank_path(loaded["panel_by_day"], members, original["start"], original["end"])
    environments = _environment(loaded["panel_by_day"], sessions, now)
    context_raw = pd.read_parquet(manifest["context_raw_file"])
    if sorted(context_raw.date.unique()) != [manifest["context_anchor"]["day"]]:
        raise ValueError("The original exact preceding whole-native context is required")
    accounting = load_action_accounting(value["action_event_view"]["directory"],
        value["action_reference_view"]["directory"], symbols=original["scope_members"],
        raw_days=sorted(set(context_raw.date) | set(loaded["source_days"])),
        replay_days=loaded["source_days"], price_source="independent_baostock_raw")
    factory = HistoricalExecutionFactory(accounting)
    case = _case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
        paths[TYPE_ID]["uncapped"], environments, members, loaded["qualification"], loaded["context_anchor"],
        original["start"], original["end"], TYPE_ID, "uncapped", "unguarded",
        f"trend-anchor-action-supplement:{folder.name}:unguarded:uncapped",
        execution_policy_factory=factory, context_raw=context_raw)
    case.update(case_id=SUPPLEMENT_CASE_ID + "__001309_action_supplement_v1",
        original_case_id=SUPPLEMENT_CASE_ID, eligible_scope_method=METHOD_ID,
        supplementary_method=SUPPLEMENT_METHOD_ID, replay_from_original_cash=True,
        checkpoint_recovery_performed=False, **FLAGS)
    result = {"schema": SUPPLEMENT_RESULT_SCHEMA, "method_id": SUPPLEMENT_METHOD_ID,
        "created_at": _now(), "registration": str(Path(registration).resolve()),
        "registration_sha256": digest(registration), "start": original["start"], "end": original["end"],
        "source_days": loaded["source_days"], "scope_members": original["scope_members"],
        "source": loaded["provenance"], "config": original["config"], "spec": deepcopy(SUPPLEMENT_SPEC),
        "parent_fixed_protocol": deepcopy(PROTOCOL), "case_count": 1, "cases": [case],
        "status": "completed_conditional_supplement_replay" if case["complete_PnL_available"] else "supplement_replay_incomplete",
        "original_result": value["original_result"], "original_result_sha256": value["original_result_sha256"],
        "original_registration": value["original_registration"],
        "original_registration_sha256": value["original_registration_sha256"],
        "original_failed_case_preserved": value["binding"],
        "retained_complete_cases": value["original_three_complete_cases"],
        "environment_decisions": environments, "raw_top5_paths": {TYPE_ID: raw_top5[TYPE_ID]},
        "capacity_paths": {TYPE_ID: {"uncapped": paths[TYPE_ID]["uncapped"]}},
        "historical_execution_policy": factory.provenance, "shared_case_function": "leader_eight_type_panel_replay._case",
        "all_cases_complete_PnL": case["complete_PnL_available"], "original_three_cases_replayed": False,
        "cash_inception_replayed": True, "checkpoint_recovery_performed": False,
        "old_22_sessions_are_new_independent_observations": False,
        "persistent_account_used": False, "broker_interacted": False, "independent_holdout_claimed": False,
        "original_result_or_registration_overwritten": False, **FLAGS}
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "result.json", result)
    case_dir = folder / case["case_id"]
    case_dir.mkdir()
    for key in ("decisions", "daily", "orders", "fills", "account"):
        write_json(case_dir / (key + ".json"), case[key])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    registration = commands.add_parser("register")
    for name in ("inputs", "out", "action-event-view", "action-reference-view"):
        registration.add_argument("--" + name, type=Path, required=True)
    replay = commands.add_parser("replay")
    for name in ("registration", "out"):
        replay.add_argument("--" + name, type=Path, required=True)
    supplement = commands.add_parser("supplement-register")
    for name in ("registration", "result", "action-event-view", "action-reference-view", "out"):
        supplement.add_argument("--" + name, type=Path, required=True)
    supplementary_replay = commands.add_parser("supplement-replay")
    for name in ("registration", "out"):
        supplementary_replay.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register(args.inputs, args.out, action_event_view=args.action_event_view,
            action_reference_view=args.action_reference_view)
        summary = {"registration": str(args.out.resolve() / "registration.json"),
            "registered_at": result["registered_at"], "eligible_count": len(result["scope_members"]),
            "excluded_count": result["selection"]["excluded_count"]}
    elif args.command == "supplement-register":
        result = register_supplement(args.registration, args.result, args.out,
            action_event_view=args.action_event_view, action_reference_view=args.action_reference_view)
        summary = {"registration": str(args.out.resolve() / "registration.json"),
            "registered_at": result["registered_at"], "case_id": SUPPLEMENT_CASE_ID,
            "replay_from_original_cash": True, "original_three_cases_replayed": False}
    else:
        result = (run_supplement if args.command == "supplement-replay" else run)(args.registration, args.out)
        summary = {"case_count": result["case_count"],
            "cases_with_fills": sum(bool(case["fills"]) for case in result["cases"]),
            "all_cases_complete_PnL": result["all_cases_complete_PnL"]}
    print(json.dumps({**summary, **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
