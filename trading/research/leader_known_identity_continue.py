"""Source-only checkpoint continuation of the four fixed crossing-scope cases.

Successful cases and committed sessions are retained verbatim. Only newly
resolved first-uncommitted source failures enter the existing shared runner.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd

from ..market_dataset import write_json
from . import leader_known_identity_scope as origin
from .leader_environment_exit_ablation import _checked_supplement_pair, _resolved_source_cases, _view_pin
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .leader_momentum_source_continue import check_source_change
from .leader_eight_type_panel_replay import _case, _validate_panel_data
from .leader_eight_type_historical_native import load_inputs, FLAGS
from .leader_historical_identity_inputs import _now, _read
from .baostock_daily_slice import digest
from .stockdb_action_accounting import load_action_accounting


METHOD_ID = "known_positive_crossing_source_only_checkpoint_continuation_v1"
REGISTRATION_SCHEMA = "leader_known_identity_source_continuation_registration_v1"
SCHEMA = "leader_known_identity_source_continuation_four_case_result_v1"
SPEC = {"id": METHOD_ID, "case_ids": list(origin.CASE_IDS),
    "signal_policy": origin.SPEC, "parent_fixed_protocol": origin.PROTOCOL,
    "selection": "all_newly_resolved_first_uncommitted_source_blocks_never_PnL",
    "source_change": "existing_blank_to_known_amendment_protocol_future_economic_effects_only",
    "state": "exact_saved_account_daily_decisions_context_global_session_indices",
    "saved_paths": "retain_original_capacity_raw_top5_and_environment_no_reranking",
    "old_committed_sessions_replayed": False, "cash_reset": False,
    "past_entitlements_reconstructed": False, "automatic_retry": False}


def _pin(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": digest(path)}


def _protected(out, paths):
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Continuation requires a new output directory")
    for value in paths:
        path = Path(value).resolve()
        path = path if path.is_dir() else path.parent
        if folder == path or folder.is_relative_to(path) or path.is_relative_to(folder):
            raise ValueError("Continuation output overlaps a saved source or parent")
    return folder


def _parent(path, seen=None):
    path = Path(path).resolve()
    seen = set() if seen is None else set(seen)
    if path in seen:
        raise ValueError("Continuation parent cycle")
    seen.add(path)
    saved = _read(path)
    if saved.get("schema") == origin.SCHEMA:
        reg = Path(saved["registration"]).resolve()
        if saved.get("registration_sha256") != digest(reg):
            raise ValueError("Original scope registration changed")
        record, _ = origin.load_registration(reg)
        event, reference = record["action_event_view"], record["action_reference_view"]
    elif saved.get("schema") == SCHEMA:
        reg = Path(saved["registration"]).resolve()
        if saved.get("registration_sha256") != digest(reg):
            raise ValueError("Continuation registration changed")
        registered, _, record, _, _ = _load_registration(reg, seen)
        if (saved.get("method_id") != METHOD_ID or saved.get("spec") != SPEC
                or saved.get("origin_registration") != registered["origin_registration"]
                or saved.get("action_pair") != registered["action_pair"]):
            raise ValueError("Exact current continuation result required")
        reg = Path(registered["origin_registration"]["path"])
        event, reference = (saved["action_pair"][key]["directory"] for key in ("event", "reference"))
    else:
        raise ValueError("A saved original scope or source-continuation result is required")
    if (saved.get("case_count") != 4 or [c.get("case_id") for c in saved.get("cases", [])] != list(origin.CASE_IDS)
            or saved.get("parent_fixed_protocol") != origin.PROTOCOL
            or saved.get("entry_information_policy") != origin.SPEC
            or saved.get("source", {}).get("manifest") != record["inputs"]
            or saved.get("source", {}).get("manifest_sha256") != record["inputs_sha256"]
            or any(saved.get(key) is not flag for key, flag in FLAGS.items())):
        raise ValueError("All four unchanged scope cases and original source binding required")
    for case in saved["cases"]:
        identity, guard, cap = case["case_id"].split("__")
        if ((case.get("type"), case.get("guard"), case.get("capacity")) != (identity, guard, cap)
                or case.get("config") != origin._config(100000, 5).to_dict()):
            raise ValueError("Original case identity or costs changed")
    if (set(saved.get("capacity_paths", {})) != {origin.TYPE_ID}
            or set(saved.get("raw_top5_paths", {})) != {origin.TYPE_ID}
            or not isinstance(saved.get("environment_decisions"), dict)):
        raise ValueError("Saved original capacity, rank and environment paths required")
    return saved, record, _pin(reg), Path(event).resolve(), Path(reference).resolve()


def _blocks(saved):
    blocked = []
    days = saved["source_days"]
    for case in saved["cases"]:
        block = case.get("blocking_evidence") or {}
        bad = [r for r in block.get("reference_evidence", []) if r.get("status") not in {
            "reference_match", "no_reported_event_reference_continuous"}]
        if (case.get("complete_PnL_available") is not False
                or block.get("reason") != "held_or_pending_raw_or_action_reference_unknown"
                or block.get("session_not_committed") is not True or block.get("raw_issues") != []
                or not bad or any(r.get("status") != "unresolved_blank_terms" for r in bad)
                or any(r.get("engine_block_reason") is not None for r in block.get("IPO_evidence", []))):
            continue
        n = case.get("engine_sessions_committed")
        state = case.get("account")
        if (type(n) is not int or not 0 < n < len(days) or len(case["daily"]) != n
                or [r["date"] for r in case["daily"]] != days[:n]
                or [r["day"] for r in case["decisions"]] != days[:n]
                or not isinstance(state, dict) or state.get("day") != days[n-1]
                or block.get("day") != days[n] or any(r.get("day") != days[n] for r in bad)
                or case.get("metrics") is not None
                or case["orders"] != list(state.get("orders", {}).values())
                or case["fills"] != list(state.get("fills", {}).values())):
            raise ValueError("Source failure must retain its exact committed account prefix")
        blocked.append(case)
    return blocked


def _binding(saved, old_event, old_reference, event_view, reference_view):
    blocked = _blocks(saved)
    allowed = {(r["symbol"], r["day"]) for c in blocked
        for r in c["blocking_evidence"]["reference_evidence"] if r.get("status") == "unresolved_blank_terms"}
    if not allowed:
        raise ValueError("No saved first-uncommitted blank-term source failure")
    original = {"action_event_view": {"directory": str(old_event)},
        "action_reference_view": {"directory": str(old_reference)}}
    pair, actions, evidence = _checked_supplement_pair(original, event_view, reference_view, allowed)
    resolutions = _resolved_source_cases(blocked, actions, evidence)
    selected = [c for c in blocked if resolutions[c["case_id"]]["resolved"]]
    if not selected:
        raise ValueError("New source evidence resolves none of the original first-uncommitted failures")
    proof = check_source_change(old_event, old_reference, event_view, reference_view,
        [c["account"] for c in selected])
    return {"continued_case_ids": [c["case_id"] for c in selected],
        "reused_case_ids": [c["case_id"] for c in saved["cases"] if c not in selected],
        "source_resolution": resolutions, "source_change": proof, "action_pair_changes": pair}


def register(parent_result, action_event_view, action_reference_view, out):
    saved, record, reg, old_event, old_reference = _parent(parent_result)
    folder = _protected(out, [parent_result, record["inputs"], reg["path"], old_event,
        old_reference, action_event_view, action_reference_view])
    binding = _binding(saved, old_event, old_reference, action_event_view, action_reference_view)
    value = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID, "registered_at": _now(),
        "spec": deepcopy(SPEC), "parent_result": _pin(parent_result), "origin_registration": reg,
        "action_pair": {"event": _view_pin(action_event_view), "reference": _view_pin(action_reference_view)},
        "binding": binding, "new_PnL_computed_at_registration": False,
        "old_accounts_modified": False, "independent_holdout_claimed": False, **FLAGS}
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "registration.json", value)
    return value


def _load_registration(path, seen=None):
    value = _read(path)
    if (value.get("schema") != REGISTRATION_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("spec") != SPEC or value.get("registered_at", "9999") > _now()
            or any(value.get(key) is not flag for key, flag in FLAGS.items())):
        raise ValueError("Exact source-only crossing continuation registration required")
    for name in ("parent_result", "origin_registration"):
        if _pin(value[name]["path"]) != value[name]:
            raise ValueError("Pinned parent result or original registration changed")
    saved, record, reg, old_event, old_reference = _parent(value["parent_result"]["path"], seen)
    if reg != value["origin_registration"]:
        raise ValueError("Original information-scope registration changed")
    pair = value["action_pair"]
    for key in ("event", "reference"):
        if _view_pin(pair[key]["directory"]) != pair[key]:
            raise ValueError("Registered amended source changed")
    binding = _binding(saved, old_event, old_reference, pair["event"]["directory"], pair["reference"]["directory"])
    if json.loads(json.dumps(binding)) != value["binding"]:
        raise ValueError("Registered source resolution or account prefix changed")
    return value, saved, record, old_event, old_reference


def run(registration, out):
    value, saved, record, old_event, old_reference = _load_registration(registration)
    pair = value["action_pair"]
    folder = _protected(out, [registration, value["parent_result"]["path"], record["inputs"],
        value["origin_registration"]["path"], old_event, old_reference,
        pair["event"]["directory"], pair["reference"]["directory"]])
    loaded, manifest = load_inputs(record["inputs"])
    if (loaded["source_days"] != saved["source_days"] or manifest["start"] != saved["start"]
            or manifest["end"] != saved["end"] or manifest["scope_members"] != saved["scope_members"]):
        raise ValueError("Continuation cannot change source dates, scope or decision window")
    members, sessions, previous, _ = _validate_panel_data(loaded["panel_by_day"], loaded["raw"],
        loaded["source_days"], loaded["calendar_payload"], loaded["scope_members"], loaded["qualification"])
    context_raw = pd.read_parquet(manifest["context_raw_file"])
    accounting = load_action_accounting(pair["event"]["directory"], pair["reference"]["directory"],
        symbols=manifest["scope_members"], raw_days=sorted(set(context_raw.date) | set(manifest["source_days"])),
        replay_days=manifest["source_days"], price_source="independent_baostock_raw")
    factory = HistoricalExecutionFactory(accounting)
    selected = set(value["binding"]["continued_case_ids"])
    folder.mkdir(parents=True, exist_ok=False)
    progress = {"schema": SCHEMA, "status": "running", "started_at": _now(),
        "continued_case_ids": list(value["binding"]["continued_case_ids"]), "completed_cases": [], **FLAGS}
    write_json(folder / "progress.json", progress)
    cases, origins = [], {}
    try:
        for old in saved["cases"]:
            case_id = old["case_id"]
            if case_id in selected:
                account_id = old["account"]["replay_risk_exit_policy"]["context"]["account_id"]
                case = _case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
                    saved["capacity_paths"][origin.TYPE_ID][old["capacity"]], saved["environment_decisions"],
                    members, loaded["qualification"], loaded["context_anchor"], manifest["start"], manifest["end"],
                    origin.TYPE_ID, old["capacity"], old["guard"], account_id,
                    execution_policy_factory=factory, context_raw=context_raw,
                    entry_membership_policy=origin.entry_membership_policy, resume_case=old)
            else:
                case = deepcopy(old)
            case_folder = folder / case_id
            case_folder.mkdir()
            for key in ("decisions", "daily", "orders", "fills", "account"):
                write_json(case_folder / (key + ".json"), case[key])
            origins[case_id] = {"parent_result": value["parent_result"], "continued_from_checkpoint": case_id in selected,
                "reused_original_case": case_id not in selected, "old_committed_sessions_replayed": False,
                "prior_blocking_evidence": deepcopy(old.get("blocking_evidence")),
                "reused_sessions": old["engine_sessions_committed"],
                "newly_completed_sessions": case["engine_sessions_committed"] - old["engine_sessions_committed"]}
            write_json(case_folder / "source_origin.json", origins[case_id])
            cases.append(case)
            progress["completed_cases"].append({"case_id": case_id, **origins[case_id]})
            write_json(folder / "progress.json", progress)
    except BaseException as error:
        progress.update(status="interrupted_or_failed", error=f"{type(error).__name__}: {error}",
            original_results_unchanged=True, automatic_retry_allowed=False, updated_at=_now())
        write_json(folder / "progress.json", progress)
        raise
    result = {**deepcopy(saved), "schema": SCHEMA, "method_id": METHOD_ID, "spec": deepcopy(SPEC),
        "created_at": _now(), "registration": str(Path(registration).resolve()), "registration_sha256": digest(registration),
        "origin_registration": value["origin_registration"], "parent_result": value["parent_result"],
        "action_pair": pair, "source_continuation": value["binding"], "cases": cases, "case_count": 4,
        "case_origins": origins, "new_cases_executed": 0, "continued_case_count": len(selected),
        "reused_case_count": 4-len(selected), "execution_policy": factory.provenance,
        "execution_policy_scope": "continued_case_ids_only_reused_cases_keep_their_original_pair",
        "old_committed_sessions_replayed": False, "cash_reset": False, "old_accounts_modified": False,
        "all_cases_complete_PnL": all(c["complete_PnL_available"] for c in cases),
        "independent_holdout_claimed": False, **FLAGS}
    write_json(folder / "result.json", result)
    progress.update(status="completed", completed_at=_now(), result=str(folder / "result.json"))
    write_json(folder / "progress.json", progress)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register_parser = commands.add_parser("register")
    for name in ("parent-result", "action-event-view", "action-reference-view", "out"):
        register_parser.add_argument("--"+name, type=Path, required=True)
    replay_parser = commands.add_parser("run")
    for name in ("registration", "out"):
        replay_parser.add_argument("--"+name, type=Path, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    result = (register if command == "register" else run)(**args)
    print(json.dumps({key: result.get(key) for key in ("schema", "registered_at", "continued_case_count",
        "reused_case_count", "all_cases_complete_PnL")}, ensure_ascii=False))
    return 0
