"""Fixed environment-exit ablation through the existing eight-type case runner.

The new control suppresses explicit environment EXIT_ALL decisions.  It is not
a promise to change only new buys: future FULL_TARGET ranking, structural exits
and the original five-session expiry still change existing holdings.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_eight_type_historical_native import FLAGS, load_inputs as load_native_inputs
from .leader_eight_type_panel_replay import (
    CAPS, GUARDS as ORIGINAL_GUARDS, PROTOCOL, TYPE_IDS, _case, _environment,
    _rank_path, _stamp, _validate_panel_data,
)
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .shortline_panel_replay import _config
from .stockdb_action_accounting import load_action_accounting


METHOD_ID = "fixed_eight_type_environment_exit_ablation_shared_engine_v1"
STUDY_PLAN_SCHEMA = "leader_environment_exit_ablation_study_plan_v1"
REGISTRATION_SCHEMA = "leader_environment_exit_ablation_registration_v1"
RESULT_SCHEMA = "leader_environment_exit_ablation_shared_replay_v1"
CONTROL_GUARD = "two_close_entry_filter_only"
GUARDS = (*ORIGINAL_GUARDS, CONTROL_GUARD)
CASE_IDS = tuple(f"{kind}__{guard}__{cap}" for kind in TYPE_IDS for guard in GUARDS for cap, _ in CAPS)
SUPPLEMENT_METHOD_ID = "fixed_48_resolved_source_block_subset_action_supplement_v1"
SUPPLEMENT_REGISTRATION_SCHEMA = "leader_environment_exit_ablation_action_supplement_registration_v1"
SUPPLEMENT_RESULT_SCHEMA = "leader_environment_exit_ablation_action_supplement_replay_v1"
SUPPLEMENT_SPEC = {"id": SUPPLEMENT_METHOD_ID,
    "case_selection": "only_original_source_blocks_resolved_by_new_independent_action_evidence",
    "original_source_blocked_cases": 7, "unresolved_original_cases": "retain_original_failure_null_and_residual",
    "replay_from_original_cash": True,
    "checkpoint_recovery_performed": False, "prefix_check": "complete_last_close_account_plus_all_committed_daily_events_decisions_orders_and_fills_exact_except_explicit_RunContext_account_mapping",
    "automatic_retry": False, "parameters_selected_by_return": False, "raw_prices_rescanned": False}
READINESS_OPTIMIZATION = {
    "id": "fixed_48_saved_panel_necessary_entry_skip_v1",
    "version": 1,
    "skip_condition": "zero_necessary_entry_days_in_exact_saved_decision_window",
    "skipped_account": None,
    "skipped_metrics": None,
    "skipped_engine_sessions": 0,
    "strategy_protocol_changed": False,
}
SPEC = {"id": METHOD_ID, "types": list(TYPE_IDS), "guards": list(GUARDS),
    "capacities": [{"id": name, "max_symbols": limit} for name, limit in CAPS], "case_count": 48,
    "control_transform": "copy_per_type_environment_EXIT_ALL_to_NO_CHANGE_only",
    "original_allow_and_no_change": "unchanged",
    "source_observation_and_reason": "preserve_original_phase_mode_reason_and_actual_availability",
    "control_holding_semantics": "NO_CHANGE_retains_shares_but_original_structural_expiry_and_later_FULL_TARGET_rank_replacements_still_apply",
    "pure_new_buy_filter_claimed": False, "parameters_selected_by_return": False,
    "independent_holdout_claimed": False, "old_32_protocol_changed": False,
    "baseline_32_replayed_before_48": False}


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Saved ablation metadata must be a JSON object")
    return value


def _manifest_path(path):
    path = Path(path).resolve()
    return path / "manifest.json" if path.is_dir() else path


def _view_pin(path):
    folder = Path(path).resolve()
    _read(folder / "result.json")
    return {"directory": str(folder), "result": str(folder / "result.json"),
        "sha256": digest(folder / "result.json")}


def controlled_environment(environments):
    """Suppress known environment exits without changing the saved evidence."""
    result = deepcopy(environments)
    for day, observation in result.items():
        for identity, decision in observation["per_type"].items():
            if decision["mode"] == "EXIT_ALL":
                decision.update(mode="NO_CHANGE", original_mode="EXIT_ALL",
                    original_reason=decision["reason"],
                    control_reason="explicit_environment_exit_disabled_known_evidence_retains_NO_CHANGE_not_feature_unknown")
    return result


def _registration_binding(manifest, studyplan):
    """One pre-specified window, fixed costs and all 48 configurations."""
    if (studyplan.get("schema") != STUDY_PLAN_SCHEMA or studyplan.get("method_id") != METHOD_ID
            or studyplan.get("types") != list(TYPE_IDS) or studyplan.get("guards") != list(GUARDS)
            or studyplan.get("capacities") != [name for name, _ in CAPS]
            or studyplan.get("case_count") != 48
            or studyplan.get("config") != _config(100000, 5).to_dict()
            or studyplan.get("parameters_selected_by_return") is not False
            or not isinstance(manifest.get("identity_research"), dict)
            or any(studyplan.get(key) != manifest.get(key) for key in ("start", "end", "through"))
            or _stamp(studyplan["specified_at"]) > _stamp(manifest["created_at"])
            or _stamp(manifest["created_at"]) > _stamp(_now())):
        raise ValueError("Pre-specified unchanged window, derived identities, costs and all 48 cases required")
    return {"start": manifest["start"], "end": manifest["end"], "through": manifest["through"],
        "source_days": manifest["source_days"], "scope_members": manifest["scope_members"],
        "input_created_at": manifest["created_at"], "study_specified_at": studyplan["specified_at"],
        "identity_research": deepcopy(manifest["identity_research"]),
        "config": deepcopy(studyplan["config"]), "spec": deepcopy(SPEC),
        "parent_fixed_protocol": deepcopy(PROTOCOL)}


def register(inputs, study_plan, out, *, action_event_view, action_reference_view,
             check_entry_readiness=False):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Environment-exit study registration requires a new output directory")
    inputs, study_plan = _manifest_path(inputs), Path(study_plan).resolve()
    _, manifest = load_native_inputs(inputs)
    plan = _read(study_plan)
    binding = _registration_binding(manifest, plan)
    value = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID, "registered_at": _now(),
        "inputs": str(inputs), "inputs_sha256": digest(inputs),
        "study_plan": str(study_plan), "study_plan_sha256": digest(study_plan),
        "input_payload_pins": {key: manifest[key] for key in (
            "raw_file", "raw_sha256", "panels_file", "panels_sha256", "context_raw_file",
            "context_raw_sha256", "calendar", "calendar_sha256")},
        "action_event_view": _view_pin(action_event_view),
        "action_reference_view": _view_pin(action_reference_view),
        "binding": binding, "historical_diagnostic_not_current_signal": True,
        "old_result_or_publication_modified": False, **FLAGS}
    if check_entry_readiness:
        value["execution_optimization"] = deepcopy(READINESS_OPTIMIZATION)
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "registration.json", value)
    return value


def _registration_metadata(path):
    """Check the saved registration and small metadata without loading panels."""
    path = Path(path).resolve()
    value = _read(path)
    if (value.get("schema") != REGISTRATION_SCHEMA or value.get("method_id") != METHOD_ID
            or any(value.get(key) is not flag for key, flag in FLAGS.items())
            or _stamp(value["registered_at"]) > _stamp(_now())
            or any(digest(value[key]) != value[key + "_sha256"] for key in ("inputs", "study_plan"))):
        raise ValueError("The exact saved environment-exit study registration is required")
    if "execution_optimization" in value and value["execution_optimization"] != READINESS_OPTIMIZATION:
        raise ValueError("Unknown registered entry-readiness optimization")
    for key in ("action_event_view", "action_reference_view"):
        if _view_pin(value[key]["directory"]) != value[key]:
            raise ValueError("Registered event/reference evidence changed")
    manifest = _read(value["inputs"])
    if (_registration_binding(manifest, _read(value["study_plan"])) != value["binding"]
            or any(manifest.get(key) != pin for key, pin in value["input_payload_pins"].items())):
        raise ValueError("Registered source, fixed scope, identity or method changed")
    return value, manifest


def _load_registration(path):
    value, manifest = _registration_metadata(path)
    loaded, actual = load_native_inputs(value["inputs"])
    if actual != manifest:
        raise ValueError("Historical metadata changed while loading the exact saved inputs")
    return value, loaded, manifest


def _annotate_control(case, original_environment):
    for decision in case["decisions"]:
        observation = original_environment[decision["day"]]
        original = observation["per_type"][case["type"]]
        converted = original["mode"] == "EXIT_ALL"
        decision.update(original_environment_mode=original["mode"],
            original_environment_reason=original["reason"], original_environment_phase=observation["phase"],
            explicit_environment_exit_converted_to_no_change=converted)
        if converted:
            decision.update(control_no_change_not_feature_unknown=True,
                environment_conversion_note="Known environment EXIT_ALL was disabled, not made unknown. Existing shares may remain; original structural/expiry exits and later FULL_TARGET rank replacements still apply.",
                legacy_no_change_label_interpretation="The original _case reason environment_unknown_or_unconfirmed is retained as a batch label; here the environment evidence is known and the explicit exit is suppressed by the registered control.")
    return case


def _progress(path, value):
    temporary = path.with_suffix(".tmp")
    write_json(temporary, value)
    temporary.replace(path)


def _require_readiness_mode(registration, requested):
    if type(requested) is not bool:
        raise ValueError("Explicit entry-readiness mode must be boolean")
    registered = registration.get("execution_optimization")
    if registered is not None and registered != READINESS_OPTIMIZATION:
        raise ValueError("Unknown registered entry-readiness optimization")
    if requested != (registered == READINESS_OPTIMIZATION):
        raise ValueError("Replay entry-readiness flag must match its explicit saved registration")


def _execution_inputs(value, loaded, manifest):
    context_raw = pd.read_parquet(manifest["context_raw_file"])
    if (sorted(context_raw.date.unique()) != [manifest["context_anchor"]["day"]]
            or context_raw.date.max() >= loaded["source_days"][0]
            or context_raw.duplicated(["date", "code"]).any()):
        raise ValueError("The original exact preceding native context is required")
    accounting = load_action_accounting(value["action_event_view"]["directory"],
        value["action_reference_view"]["directory"], symbols=loaded["scope_members"],
        raw_days=sorted(set(context_raw.date) | set(loaded["source_days"])),
        replay_days=loaded["source_days"], price_source="independent_baostock_raw")
    factory = HistoricalExecutionFactory(accounting)
    return context_raw, factory


def _skipped_case(identity, guard, cap, readiness, config):
    return {"case_id": readiness["case_id"], "type": identity, "guard": guard, "capacity": cap,
        "status": "skipped_no_possible_entry_from_saved_panel", "config": deepcopy(config),
        "entry_readiness": deepcopy(readiness), "decisions": [], "daily": [], "orders": [], "fills": [],
        "account": None, "metrics": None, "engine_sessions_committed": 0,
        "blocking_evidence": None, "residual_positions": None, "complete_PnL_available": False,
        "observed_tail_sessions": 0, "required_tail_sessions": 6,
        "skip_semantics": "No account was created or simulated; unavailable entry is not zero return or strategy invalidity.",
        **FLAGS}


def run(registration, out, *, check_entry_readiness=False):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("The 48-case environment-exit replay requires a new output directory")
    value, loaded, manifest = _load_registration(registration)
    _require_readiness_mode(value, check_entry_readiness)
    members, sessions, previous, now = _validate_panel_data(loaded["panel_by_day"], loaded["raw"],
        loaded["source_days"], loaded["calendar_payload"], loaded["scope_members"], loaded["qualification"])
    paths, original_ranks = _rank_path(loaded["panel_by_day"], members, manifest["start"], manifest["end"])
    environments = _environment(loaded["panel_by_day"], sessions, now)
    control = controlled_environment(environments)
    readiness = None
    if check_entry_readiness:
        from .leader_panel_readiness import summarize_panels
        readiness = summarize_panels(panel_by_day=loaded["panel_by_day"], source_days=loaded["source_days"],
            calendar_payload=loaded["calendar_payload"], scope_members=loaded["scope_members"],
            start=manifest["start"], end=manifest["end"], as_of=now, environment_exit_ablation=True,
            _rank_paths=paths, _environments=environments)
    readiness_cases = {case["case_id"]: case for case in readiness["cases"]} if readiness else {}
    context_raw, factory = (None, None) if check_entry_readiness else _execution_inputs(value, loaded, manifest)
    folder.mkdir(parents=True, exist_ok=False)
    progress = {"schema": "leader_environment_exit_ablation_progress_v1", "method_id": METHOD_ID,
        "started_at": _now(), "status": "running", "expected_cases": 48, "completed_cases": [],
        "active_case_id": None, "original_baseline32_run_separately": False, **FLAGS}
    if check_entry_readiness:
        write_json(folder / "entry_readiness.json", readiness)
        progress.update(execution_optimization=deepcopy(READINESS_OPTIMIZATION),
            checked_case_count=len(readiness_cases), simulated_case_count=0, skipped_case_count=0)
    _progress(folder / "progress.json", progress)
    cases = []
    try:
        for identity in TYPE_IDS:
            for guard in GUARDS:
                for cap, _ in CAPS:
                    case_id = f"{identity}__{guard}__{cap}"
                    progress.update(active_case_id=case_id, updated_at=_now())
                    _progress(folder / "progress.json", progress)
                    entry = readiness_cases.get(case_id)
                    skipped = entry is not None and entry["potential_entry_days"] == 0
                    if skipped:
                        case = _skipped_case(identity, guard, cap, entry, value["binding"]["config"])
                    else:
                        if factory is None:
                            context_raw, factory = _execution_inputs(value, loaded, manifest)
                        case = _case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
                            paths[identity][cap], control if guard == CONTROL_GUARD else environments,
                            members, loaded["qualification"], loaded["context_anchor"],
                            manifest["start"], manifest["end"], identity, cap, guard,
                            f"leader-environment-exit-ablation:{folder.name}:{case_id}",
                            execution_policy_factory=factory, context_raw=context_raw)
                        if guard == CONTROL_GUARD:
                            _annotate_control(case, environments)
                        if entry is not None:
                            case["entry_readiness"] = deepcopy(entry)
                    case_folder = folder / case_id
                    case_folder.mkdir()
                    for key in ("decisions", "daily", "orders", "fills", "account"):
                        write_json(case_folder / (key + ".json"), case[key])
                    if entry is not None:
                        write_json(case_folder / "entry_readiness.json", entry)
                    cases.append(case)
                    completed = {"case_id": case_id, "ended_at": _now(), "status": case["status"],
                        "engine_sessions_committed": case["engine_sessions_committed"],
                        "order_count": len(case["orders"]), "fill_count": len(case["fills"]),
                        "metrics": case["metrics"], "blocking_evidence": case["blocking_evidence"],
                        "residual_positions": case["residual_positions"],
                        "complete_PnL_available": case["complete_PnL_available"]}
                    progress["completed_cases"].append(completed)
                    if check_entry_readiness:
                        progress["skipped_case_count" if skipped else "simulated_case_count"] += 1
                    progress.update(active_case_id=None, updated_at=_now())
                    _progress(folder / "progress.json", progress)
                    print(json.dumps({"event": "case_completed", "case_id": case_id,
                        "completed_cases": len(cases), "expected_cases": 48,
                        "engine_sessions_committed": case["engine_sessions_committed"],
                        "fill_count": len(case["fills"]), "status": case["status"]}, ensure_ascii=False), flush=True)
    except BaseException as error:
        progress.update(status="interrupted_or_failed", updated_at=_now(),
            error=f"{type(error).__name__}: {error}", active_case_finished=False,
            prior_completed_case_files_preserved=True, automatic_retry_allowed=False)
        _progress(folder / "progress.json", progress)
        raise
    result = {"schema": RESULT_SCHEMA, "method_id": METHOD_ID, "created_at": _now(),
        "status": "completed_fixed_48_case_research_comparison",
        "registration": str(Path(registration).resolve()), "registration_sha256": digest(registration),
        "study_plan": value["study_plan"], "study_plan_sha256": value["study_plan_sha256"],
        "start": manifest["start"], "end": manifest["end"], "source_days": loaded["source_days"],
        "scope_members": loaded["scope_members"], "source": loaded["provenance"],
        "spec": deepcopy(SPEC), "parent_fixed_protocol": deepcopy(PROTOCOL),
        "config": value["binding"]["config"], "case_count": len(cases), "cases": cases,
        "environment_decisions": environments, "control_environment_decisions": control,
        "raw_top5_paths": original_ranks, "capacity_paths": paths,
        "historical_execution_policy": factory.provenance if factory is not None else None,
        "shared_case_function": "leader_eight_type_panel_replay._case",
        "all_cases_complete_PnL": all(case["complete_PnL_available"] for case in cases),
        "baseline32_run_before_48": False, "old_results_or_protocol_changed": False,
        "case_selection_by_return": False, "independent_holdout_claimed": False,
        "persistent_account_used": False, "broker_interacted": False,
        "limitations": [
            "The control disables only explicit environment exits, not structural exits, five-session expiry or future FULL_TARGET ranking changes.",
            "Original candidate unknowns, actual execution/IPO/action checks, both cumulative capacities and six tail sessions remain unchanged.",
            "No fills, uncommitted sessions or residual shares retain null metrics; an eventually completed exit does not imply timely sale.",
            "Late historical identity/listing/source evidence remains posthoc; no current signal, market prediction, verified economic return or strategy validation is claimed.",
        ], **FLAGS}
    if check_entry_readiness:
        skipped = progress["skipped_case_count"]
        result.update(execution_optimization=deepcopy(READINESS_OPTIMIZATION),
            checked_case_count=progress["checked_case_count"], simulated_case_count=progress["simulated_case_count"],
            skipped_case_count=skipped, entry_readiness=str(folder / "entry_readiness.json"))
        if skipped:
            result["status"] = ("checked_fixed_48_cases_with_unavailable_entries" if skipped == len(cases)
                else "checked_fixed_48_cases_with_skipped_entries")
    write_json(folder / "result.json", result)
    progress.update(status="completed", active_case_id=None, completed_at=_now(), result=str(folder / "result.json"))
    _progress(folder / "progress.json", progress)
    return result


def _source_blocked_cases(original, saved):
    """Identify the complete original source-failure set, without using PnL."""
    binding = original["binding"]
    if (saved.get("schema") != RESULT_SCHEMA or saved.get("method_id") != METHOD_ID
            or saved.get("spec") != SPEC or saved.get("parent_fixed_protocol") != PROTOCOL
            or saved.get("case_count") != 48 or [case["case_id"] for case in saved["cases"]] != list(CASE_IDS)
            or any(saved.get(key) != binding[key] for key in ("start", "end", "source_days", "scope_members", "config"))
            or saved.get("source", {}).get("manifest") != original["inputs"]
            or saved.get("source", {}).get("manifest_sha256") != original["inputs_sha256"]
            or saved.get("study_plan") != original["study_plan"]
            or saved.get("study_plan_sha256") != original["study_plan_sha256"]
            or saved.get("execution_optimization") != READINESS_OPTIMIZATION):
        raise ValueError("The exact original registered readiness-enabled 48-case result is required")
    _require_readiness_mode(original, True)
    selected = []
    for case in saved["cases"]:
        kind, guard, cap = case["case_id"].split("__")
        if ((case.get("type"), case.get("guard"), case.get("capacity")) != (kind, guard, cap)
                or case.get("config") != binding["config"]):
            raise ValueError("An original fixed case identity or cost setting changed")
        block = case.get("blocking_evidence") or {}
        bad = [row for row in block.get("reference_evidence", []) if row.get("status") not in {
            "reference_match", "no_reported_event_reference_continuous"}]
        if (block.get("reason") != "held_or_pending_raw_or_action_reference_unknown"
                or block.get("session_not_committed") is not True or block.get("raw_issues") != []
                or not bad or any(row.get("status") != "unresolved_blank_terms" for row in bad)
                or any(row.get("engine_block_reason") is not None for row in block.get("IPO_evidence", []))):
            continue
        count, daily = case["engine_sessions_committed"], case["daily"]
        if (type(count) is not int or count <= 0 or [row["date"] for row in daily] != saved["source_days"][:count]
                or block.get("day") != saved["source_days"][count]
                or case.get("account", {}).get("day") != daily[-1]["date"]
                or case.get("complete_PnL_available") is not False or case.get("metrics") is not None
                or any(row.get("day") != block["day"] for row in bad)):
            raise ValueError("Original source failure must retain its exact committed account prefix")
        selected.append(case)
    if len(selected) != 7:
        raise ValueError("This supplementary method requires the exact seven original blank-term source blocks")
    return selected


def _checked_supplement_pair(original, event_view, reference_view, allowed_events):
    """Verify the narrow reviewed-amendment chain, never read a raw price matrix."""
    from ..market_dataset import normalize_symbol
    from .adjusted_prices import checked_file
    from .action_reviewed_amendment import patch_reference_rows

    old_pair = tuple(Path(original[key]["directory"]).resolve() for key in ("action_event_view", "action_reference_view"))
    current = (Path(event_view).resolve(), Path(reference_view).resolve())
    if current == old_pair:
        raise ValueError("A new reviewed action source is required; unchanged evidence is not retried")
    final, chain, seen, changed = current, [], set(), set()
    while current != old_pair:
        if current in seen or len(chain) >= len(allowed_events):
            raise ValueError("The action supplement must be a finite exact-parent curation chain")
        seen.add(current)
        event, ref = (_read(folder / "result.json") for folder in current)
        amendment = event.get("reviewed_cash_amendment", {})
        keys = [tuple(key) for key in amendment.get("events", [])]
        if (event.get("status") != "research_action_events_projected"
                or ref.get("status") != "research_reference_presence_diagnostic"
                or event.get("execution_admitted") is not False or ref.get("execution_admitted") is not False
                or Path(ref["event_view"]).resolve() != current[0]
                or ref.get("reviewed_cash_amendment") != amendment or not keys or len(set(keys)) != len(keys)
                or not set(keys) <= set(allowed_events) or set(keys) & changed
                or amendment.get("raw_prices_rescanned") is not False or amendment.get("other_events_changed") is not False):
            raise ValueError("Only reviewed corrections of original blocked events may change the saved pair")
        parents = tuple(Path(amendment[key]).resolve() for key in ("parent_event_view", "parent_reference_view"))
        before, before_ref = (_read(folder / "result.json") for folder in parents)
        if Path(before_ref["event_view"]).resolve() != parents[0]:
            raise ValueError("A supplementary action/reference parent pair is not aligned")
        for key in ("start", "end", "raw_view", "source_view", "expected_codes", "queried", "unqueried", "canonical_events"):
            if event.get(key) != before.get(key):
                raise ValueError("The amendment changed the original action source or coverage")
        for key in ("raw_daily_sha256", "price_view", "canonical_panel_sha256"):
            if ref.get(key) != before_ref.get(key):
                raise ValueError("The amendment changed the original price/reference source")
        for folder, old, new, mutable in ((current[0], before, event, {"canonical_actions.parquet"}),
                (current[1], before_ref, ref, {"raw_reference_changes.parquet", "input_events.json"})):
            if any(new["files"].get(name) != sha for name, sha in old["files"].items() if name not in mutable):
                raise ValueError("Unrelated frozen action/reference evidence changed")
            for name in set(new["files"]) - set(old["files"]):
                checked_file(folder, name, new["files"][name])
        input_events = checked_file(current[1], "input_events.json", ref["files"]["input_events.json"])
        if input_events.read_bytes() != (current[0] / "result.json").read_bytes():
            raise ValueError("Supplementary reference input must pin the exact current event result")
        frames = [pd.read_parquet(checked_file(folder, "canonical_actions.parquet", value["files"]["canonical_actions.parquet"])).fillna("")
            for folder, value in ((parents[0], before), (current[0], event))]
        left, right = frames
        amounts = {"dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs", "bonus_taxable_cash_ps"}
        mask = pd.Series([(s, d) in keys for s, d in zip(left.symbol, left.dividOperateDate)], index=left.index)
        shared = list(left.columns)
        if (len(left) != len(right) or not set(shared) <= set(right.columns)
                or not set(right.columns) - set(shared) <= {"bonus_taxable_cash_ps"}
                or int(mask.sum()) != len(keys) or not left.loc[~mask].equals(right.loc[~mask, shared])
                or not left[[c for c in shared if c not in amounts]].equals(right[[c for c in shared if c not in amounts]])
                or any(not right.loc[~mask, name].eq("").all() for name in set(right.columns) - set(shared))):
            raise ValueError("Only selected canonical amount terms may be supplemented")
        if all(left.loc[mask, name].equals(right.loc[mask, name]) for name in shared if name in amounts) and set(shared) == set(right.columns):
            raise ValueError("The purported action supplement contains no economic-term change")
        gaps = [pd.read_parquet(checked_file(folder, "raw_reference_changes.parquet", value["files"]["raw_reference_changes.parquet"]))
            for folder, value in ((parents[1], before_ref), (current[1], ref))]
        a, b = gaps
        gap_mask = pd.Series([(normalize_symbol(code).vt_symbol, day) in keys for code, day in zip(a.code, a.date)], index=a.index)
        # Parquet can promote an all-null formula-error object column to
        # float+NaN once one reviewed row becomes numeric. Compare exact
        # values with one null representation; never fill a price or zero.
        patched = patch_reference_rows(a, gap_mask, b.loc[gap_mask])
        nullable = lambda frame: frame.astype(object).where(frame.notna(), None)
        if list(a.columns) != list(b.columns) or len(a) != len(b) or not nullable(patched).equals(nullable(b)):
            raise ValueError("Raw reference facts or unrelated formula rows changed")
        chain.append({"event_view": _view_pin(current[0]), "reference_view": _view_pin(current[1]),
            "amendment": deepcopy(amendment)})
        changed.update(keys)
        current = parents
    event, ref = (_read(folder / "result.json") for folder in final)
    actions = pd.read_parquet(checked_file(final[0], "canonical_actions.parquet", event["files"]["canonical_actions.parquet"])).fillna("")
    def evidence(name):
        return json.loads(checked_file(final[1], name, ref["files"][name]).read_text(encoding="utf-8"))
    return {"chain": list(reversed(chain)), "changed_events": [list(key) for key in sorted(changed)]}, actions, {
        "special": evidence("reviewed_special_references.json"),
        "distribution": evidence("cash_reference_evidence.json") + evidence("distribution_reference_evidence.json")}


def _resolved_source_cases(blocked, actions, evidence):
    from .action_events import reference_concordance
    results = {}
    for case in blocked:
        checked = []
        for original in case["blocking_evidence"]["reference_evidence"]:
            if original.get("status") != "unresolved_blank_terms":
                continue
            gap = deepcopy(original.get("reference_evidence"))
            if (not isinstance(gap, dict) or gap.get("symbol") != original["symbol"] or gap.get("date") != original["day"]
                    or any(gap.get(key) in (None, "") for key in ("previous_observed_close", "preclose"))):
                raise ValueError("Original blocked own-native D/D-1 reference facts must be saved explicitly")
            selected = actions.loc[actions.symbol.eq(original["symbol"]) & actions.dividOperateDate.eq(original["day"])]
            if len(selected) != 1:
                raise ValueError("Original blocked action identity must remain unique")
            for key in ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"):
                gap[key] = selected.iloc[0][key]
            for key in ("reference_formula_status", "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"):
                gap.pop(key, None)
            refreshed = reference_concordance(pd.DataFrame([gap]), evidence["special"], evidence["distribution"]).iloc[0].to_dict()
            checked.append({"symbol": original["symbol"], "day": original["day"],
                "original_status": original["status"], "status": refreshed["reference_formula_status"],
                "own_native_reference_evidence": refreshed})
        results[case["case_id"]] = {"resolved": bool(checked) and all(row["status"] == "reference_match" for row in checked),
            "references": checked}
    return results


def _supplement_binding(original, saved, pair, resolutions):
    blocked = _source_blocked_cases(original, saved)
    if set(resolutions) != {case["case_id"] for case in blocked}:
        raise ValueError("Every original source-blocked case needs an explicit resolved/unknown observation")
    selected = [case["case_id"] for case in blocked if resolutions[case["case_id"]]["resolved"] is True]
    if not selected:
        raise ValueError("No original source block is resolved by the new independent evidence")
    return {"source_blocked_case_ids": [case["case_id"] for case in blocked], "replayed_case_ids": selected,
        "reused_case_ids": [case_id for case_id in CASE_IDS if case_id not in selected],
        "unresolved_source_case_ids": [case["case_id"] for case in blocked if case["case_id"] not in selected],
        "source_resolution": deepcopy(resolutions), "action_pair_changes": deepcopy(pair),
        "fixed_binding": deepcopy(original["binding"]), "original_readiness_optimization": deepcopy(READINESS_OPTIMIZATION),
        "replay_from_original_cash": True, "checkpoint_recovery_performed": False, "automatic_retry": False}


def _supplement_sources(original, saved, event_view, reference_view):
    blocked = _source_blocked_cases(original, saved)
    allowed = {(row["symbol"], row["day"]) for case in blocked
        for row in case["blocking_evidence"]["reference_evidence"] if row.get("status") == "unresolved_blank_terms"}
    pair, actions, evidence = _checked_supplement_pair(original, event_view, reference_view, allowed)
    return _supplement_binding(original, saved, pair, _resolved_source_cases(blocked, actions, evidence))


def _original_supplement_source(registration, result):
    original, manifest = _registration_metadata(registration)
    saved = _read(result)
    if saved.get("registration") != str(Path(registration).resolve()) or saved.get("registration_sha256") != digest(registration):
        raise ValueError("The original result must bind the exact saved original registration")
    progress = _read(Path(result).resolve().parent / "progress.json")
    if (progress.get("status") != "completed" or progress.get("expected_cases") != 48
            or [row["case_id"] for row in progress.get("completed_cases", [])] != list(CASE_IDS)):
        raise ValueError("The complete original 48-case operation must remain saved")
    _source_blocked_cases(original, saved)
    return original, saved, manifest


def register_supplement(registration, result, out, *, action_event_view, action_reference_view):
    """Pin new evidence and only the original source failures it now resolves."""
    registration, result, folder = Path(registration).resolve(), Path(result).resolve(), Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Action supplement registration requires a new output directory")
    original, saved, _ = _original_supplement_source(registration, result)
    binding = _supplement_sources(original, saved, action_event_view, action_reference_view)
    value = {"schema": SUPPLEMENT_REGISTRATION_SCHEMA, "method_id": SUPPLEMENT_METHOD_ID,
        "registered_at": _now(), "spec": deepcopy(SUPPLEMENT_SPEC),
        "original_registration": str(registration), "original_registration_sha256": digest(registration),
        "original_result": str(result), "original_result_sha256": digest(result),
        "original_progress": str(result.parent / "progress.json"),
        "original_progress_sha256": digest(result.parent / "progress.json"),
        "action_event_view": _view_pin(action_event_view), "action_reference_view": _view_pin(action_reference_view),
        "binding": binding, "late_evidence_not_historical_PIT": True,
        "original_results_modified": False, "independent_holdout_claimed": False, **FLAGS}
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "registration.json", value)
    return value


def _load_supplement(path):
    value = _read(path)
    if (value.get("schema") != SUPPLEMENT_REGISTRATION_SCHEMA or value.get("method_id") != SUPPLEMENT_METHOD_ID
            or value.get("spec") != SUPPLEMENT_SPEC or _stamp(value["registered_at"]) > _stamp(_now())
            or any(value.get(key) is not flag for key, flag in FLAGS.items())
            or any(digest(value[key]) != value[key + "_sha256"]
                for key in ("original_registration", "original_result", "original_progress"))):
        raise ValueError("The exact immutable source-action supplementary registration is required")
    for key in ("action_event_view", "action_reference_view"):
        if _view_pin(value[key]["directory"]) != value[key]:
            raise ValueError("Registered supplementary action evidence changed")
    original, saved, manifest = _original_supplement_source(value["original_registration"], value["original_result"])
    if _supplement_sources(original, saved, value["action_event_view"]["directory"],
            value["action_reference_view"]["directory"]) != value["binding"]:
        raise ValueError("Supplementary source resolution, original cases or frozen inputs changed")
    return value, original, saved, manifest


def _committed_prefix_check(original, supplementary, source_days):
    """Compare complete saved records, including pending orders and risk events."""
    count = original["engine_sessions_committed"]
    if (type(count) is not int or count <= 0 or [row["date"] for row in original["daily"]] != source_days[:count]
            or [row["date"] for row in supplementary["daily"][:count]] != source_days[:count]):
        raise ValueError("Supplementary replay did not reproduce every original committed session")
    last = source_days[count - 1]
    contexts = [case.get("account", {}).get("replay_risk_exit_policy", {}).get("context")
        for case in (original, supplementary)]
    if (any(not isinstance(context, dict) or context.get("contract_type") != "RunContext"
            or context.get("mode") != "historical_replay" for context in contexts)
            or {key: value for key, value in contexts[0].items() if key != "account_id"}
                != {key: value for key, value in contexts[1].items() if key != "account_id"}):
        raise ValueError("Explicit original and supplementary replay context evidence is required")
    def normalize(value):
        if isinstance(value, dict):
            if value in contexts:
                return {**value, "account_id": "explicit_original_to_supplement_replay_context"}
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    def canonical(value):
        return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    evidence = {}
    for key in ("daily", "decisions", "orders", "fills"):
        if key in {"daily", "decisions"}:
            left, right = original[key], supplementary[key][:count]
            if len(left) != count:
                raise ValueError(f"Original {key} does not match its complete committed prefix")
        else:
            if any(row["day"] > last for row in original[key]):
                raise ValueError("Original orders/fills extend beyond the original committed day")
            left = original[key]
            right = [row for row in supplementary[key] if row["day"] <= last]
        before, after = canonical(left), canonical(right)
        if before != after:
            raise ValueError(f"Supplementary committed {key} prefix changed before the original source block")
        evidence[key] = {"original_sha256": sha256(before).hexdigest(), "supplement_sha256": sha256(after).hexdigest(),
            "record_count": len(left), "exact_after_explicit_context_mapping": True}
    snapshot = supplementary.get("committed_prefix_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("day") != last:
        raise ValueError("The complete supplementary account snapshot at the original last close is required")
    before, after = canonical(original["account"]), canonical(snapshot)
    if before != after:
        raise ValueError("Supplementary committed full account prefix changed before the original source block")
    evidence["full_account"] = {"original_sha256": sha256(before).hexdigest(), "supplement_sha256": sha256(after).hexdigest(),
        "exact_after_explicit_context_mapping": True, "day": last,
        "fields": sorted(original["account"])}
    return {"status": "exact_original_committed_prefix_preserved", "through": last, "sessions": count,
        "account_id_mapping": {"original": contexts[0]["account_id"], "supplement": contexts[1]["account_id"]},
        "order_id_mapping": [{"original": row["id"], "supplement": row["id"]} for row in original["orders"]],
        "compared_records": evidence, "pending_orders_included": True, "pending_targets_included": True,
        "complete_account_snapshot_compared": True, "risk_events_included": True,
        "source_block_day": original["blocking_evidence"]["day"], "replay_from_original_cash": True}


def run_supplement(registration, out):
    """One bounded replay per newly resolved source failure; retain all others."""
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Supplementary replay requires a new output directory; no automatic retry")
    value, original, saved, manifest = _load_supplement(registration)
    loaded, actual = load_native_inputs(original["inputs"])
    if actual != manifest:
        raise ValueError("Supplementary replay cannot change original historical inputs")
    members, sessions, previous, _ = _validate_panel_data(loaded["panel_by_day"], loaded["raw"],
        loaded["source_days"], loaded["calendar_payload"], loaded["scope_members"], loaded["qualification"])
    effective = {**original, "action_event_view": value["action_event_view"], "action_reference_view": value["action_reference_view"]}
    context_raw, factory = _execution_inputs(effective, loaded, manifest)
    selected = set(value["binding"]["replayed_case_ids"])
    folder.mkdir(parents=True, exist_ok=False)
    progress = {"schema": "leader_environment_exit_ablation_action_supplement_progress_v1",
        "method_id": SUPPLEMENT_METHOD_ID, "started_at": _now(), "status": "running", "expected_cases": 48,
        "expected_replayed_cases": len(selected), "replayed_case_count": 0, "reused_case_count": 0,
        "completed_cases": [], "active_case_id": None, "automatic_retry_allowed": False, **FLAGS}
    _progress(folder / "progress.json", progress)
    cases, origins = [], {}
    try:
        for old in saved["cases"]:
            case_id = old["case_id"]
            progress.update(active_case_id=case_id, updated_at=_now())
            _progress(folder / "progress.json", progress)
            if case_id in selected:
                identity, guard, cap = old["type"], old["guard"], old["capacity"]
                case = _case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
                    saved["capacity_paths"][identity][cap],
                    saved["control_environment_decisions"] if guard == CONTROL_GUARD else saved["environment_decisions"],
                    members, loaded["qualification"], loaded["context_anchor"], manifest["start"], manifest["end"],
                    identity, cap, guard, f"leader-environment-action-supplement:{folder.name}:{case_id}",
                    execution_policy_factory=factory, context_raw=context_raw, snapshot_day=old["daily"][-1]["date"])
                if guard == CONTROL_GUARD:
                    _annotate_control(case, saved["environment_decisions"])
                case["entry_readiness"] = deepcopy(old["entry_readiness"])
            else:
                case = deepcopy(old)
            case_folder = folder / case_id
            case_folder.mkdir()
            for key in ("decisions", "daily", "orders", "fills", "account"):
                write_json(case_folder / (key + ".json"), case[key])
            if case_id in selected:
                write_json(case_folder / "committed_prefix_snapshot.json", case.get("committed_prefix_snapshot"))
            origin = {"original_result": value["original_result"], "original_result_sha256": value["original_result_sha256"],
                "original_case_id": case_id, "replayed_from_original_cash": case_id in selected,
                "reused_original_result": case_id not in selected, "checkpoint_recovery_performed": False}
            if case_id in selected:
                try:
                    origin["committed_prefix_check"] = _committed_prefix_check(old, case, saved["source_days"])
                except ValueError as error:
                    write_json(case_folder / "prefix_check.json", {"status": "failed_original_prefix_changed",
                        "error": str(error), "new_case_files_preserved": True, "automatic_retry_allowed": False, **FLAGS})
                    raise
                progress["replayed_case_count"] += 1
            else:
                progress["reused_case_count"] += 1
            write_json(case_folder / "source_origin.json", origin)
            origins[case_id] = origin
            cases.append(case)
            progress["completed_cases"].append({"case_id": case_id, "status": case["status"],
                "reused": case_id not in selected, "engine_sessions_committed": case["engine_sessions_committed"],
                "metrics": case["metrics"], "blocking_evidence": case["blocking_evidence"]})
            progress.update(active_case_id=None, updated_at=_now())
            _progress(folder / "progress.json", progress)
            print(json.dumps({"event": "supplement_case_saved", "case_id": case_id, "reused": case_id not in selected,
                "replayed_cases": progress["replayed_case_count"], "retained_cases": progress["reused_case_count"]}), flush=True)
    except BaseException as error:
        progress.update(status="interrupted_or_failed", error=f"{type(error).__name__}: {error}", updated_at=_now(),
            original_results_unchanged=True, new_case_files_preserved=True)
        _progress(folder / "progress.json", progress)
        raise
    result = {**deepcopy(saved), "schema": SUPPLEMENT_RESULT_SCHEMA, "method_id": SUPPLEMENT_METHOD_ID,
        "ablation_method_id": METHOD_ID, "created_at": _now(), "status": "completed_bounded_source_supplement_with_retained_cases",
        "registration": str(Path(registration).resolve()), "registration_sha256": digest(registration),
        "supplement_spec": deepcopy(SUPPLEMENT_SPEC), "original_registration": value["original_registration"],
        "original_registration_sha256": value["original_registration_sha256"],
        "original_result": value["original_result"], "original_result_sha256": value["original_result_sha256"],
        "source_action_supplement": deepcopy(value["binding"]), "cases": cases, "case_count": 48,
        "case_origins": origins, "replayed_case_count": len(selected), "reused_case_count": 48 - len(selected),
        "historical_execution_policy": factory.provenance, "historical_execution_policy_scope": "replayed_case_ids_only",
        "all_cases_complete_PnL": all(case["complete_PnL_available"] for case in cases),
        "original_results_modified": False, "automatic_retry_allowed": False,
        "reused_prefix_sessions_are_new_independent_observations": False, **FLAGS}
    write_json(folder / "result.json", result)
    progress.update(status="completed", active_case_id=None, completed_at=_now(), result=str(folder / "result.json"))
    _progress(folder / "progress.json", progress)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    registration = commands.add_parser("register")
    for name in ("inputs", "study-plan", "action-event-view", "action-reference-view", "out"):
        registration.add_argument("--" + name, type=Path, required=True)
    registration.add_argument("--check-entry-readiness", action="store_true")
    replay = commands.add_parser("replay")
    for name in ("registration", "out"):
        replay.add_argument("--" + name, type=Path, required=True)
    replay.add_argument("--check-entry-readiness", action="store_true")
    supplement = commands.add_parser("supplement-register")
    for name in ("registration", "result", "action-event-view", "action-reference-view", "out"):
        supplement.add_argument("--" + name, type=Path, required=True)
    supplementary_replay = commands.add_parser("supplement-replay")
    for name in ("registration", "out"):
        supplementary_replay.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register(args.inputs, args.study_plan, args.out,
            action_event_view=args.action_event_view, action_reference_view=args.action_reference_view,
            check_entry_readiness=args.check_entry_readiness)
        summary = {"registration": str(args.out.resolve() / "registration.json"),
            "registered_at": result["registered_at"], "expected_cases": 48}
    elif args.command == "supplement-register":
        result = register_supplement(args.registration, args.result, args.out,
            action_event_view=args.action_event_view, action_reference_view=args.action_reference_view)
        summary = {"registration": str(args.out.resolve() / "registration.json"),
            "replayed_case_ids": result["binding"]["replayed_case_ids"],
            "retained_case_count": len(result["binding"]["reused_case_ids"]),
            "unresolved_original_source_cases": result["binding"]["unresolved_source_case_ids"]}
    elif args.command == "supplement-replay":
        result = run_supplement(args.registration, args.out)
        summary = {"result": str(args.out.resolve() / "result.json"), "case_count": 48,
            "replayed_case_count": result["replayed_case_count"], "reused_case_count": result["reused_case_count"],
            "all_cases_complete_PnL": result["all_cases_complete_PnL"]}
    else:
        result = run(args.registration, args.out, check_entry_readiness=args.check_entry_readiness)
        summary = {"result": str(args.out.resolve() / "result.json"), "case_count": result["case_count"],
            "cases_with_fills": sum(bool(case["fills"]) for case in result["cases"]),
            "all_cases_complete_PnL": result["all_cases_complete_PnL"]}
        if args.check_entry_readiness:
            summary.update(status=result["status"], checked_cases=result["checked_case_count"],
                simulated_cases=result["simulated_case_count"], skipped_cases=result["skipped_case_count"])
    print(json.dumps({**summary, **FLAGS}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
