"""Four single-account leader-type routers on the existing saved native inputs.

Every period retains its original cohort, source calendar and action pair. The
new registered routing policy owns selection; this adapter owns only source
loading, four independent shared-Engine runs, and durable research outcomes.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re

import pandas as pd

from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..ledger import new_state
from ..report import metrics
from . import leader_type_router as policy
from .baostock_daily_slice import digest
from . import leader_eight_type_case_replay as durable
from .leader_eight_type_historical_native import INPUT_SCHEMA, METHOD_ID as INPUT_METHOD, load_inputs
from .leader_eight_type_panel_replay import GUARDS, CAPS, PROTOCOL, _environment, _rank_path, _validate_panel_data
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .shortline_panel_replay import _config
from .stockdb_action_accounting import load_action_accounting


METHOD_ID = "single_active_type_cyclic_environment_switch_shared_engine_v1"
SCHEMA = METHOD_ID + "_result"
CASE_IDS = tuple("single_active_type__" + guard + "__" + capacity for guard in GUARDS for capacity, _ in CAPS)
FLAGS = dict(research_only=True, candidate_panel_only=True, execution_admitted=False,
    selection_enabled=False, strategy_validated=False, returns_reportable=False,
    model_refit=False, future_labels_used_for_score=False, network_accessed=False,
    user_account_mutated=False, historical_signal_publication_verified=False,
    pit_verified=False, corporate_actions_verified=False)


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _pin(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": digest(path)}


def _checked(reference):
    path = Path(reference["path"]).resolve()
    if _pin(path) != reference:
        raise ValueError("Registered router source or implementation changed: " + str(path))
    return path


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(member) for key, member in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(member) for member in value]
    if value is pd.NA:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return _clean(value.item())
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        return None
    return value


def _save(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(_clean(value), handle, ensure_ascii=False, indent=2, allow_nan=False)


def _separate(out, references):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new exclusive router directory; no implicit retry/reset")
    for reference in references:
        parent = Path(reference["path"]).resolve().parent
        if out == parent or out in parent.parents or parent in out.parents:
            raise ValueError("Router outputs must remain outside original sources and accounts")
    return out


def _reference(value, *, manifest=False):
    if isinstance(value, dict):
        _checked(value)
        return deepcopy(value)
    path = Path(value).resolve()
    if path.is_dir():
        path = path / ("manifest.json" if manifest else "result.json")
    return _pin(path)


def _spec():
    if policy.METHOD_ID != METHOD_ID:
        raise ValueError("The shared routing policy must match this registered method")
    return {"method_id": METHOD_ID, "policy": deepcopy(policy.SPEC),
        "case_ids": list(CASE_IDS), "config": _config(100000, 5).to_dict(),
        "original_protocol": deepcopy(PROTOCOL),
        "periods": "exact_saved_2023_partial_2024_2025_separate_sources_not_one_continuous_return",
        "source_loading": "one_native_load_one_context_load_one_base_matrix_per_period_four_independent_cases",
        "ranking": "reuse_original_eight_type_rank_paths_and_original_two_capacity_methods_no_refit_or_PnL_selection",
        "tail": "original_six_source_sessions_no_new_entry_tail5_close_empty_target_tail6_open_liquidation",
        "metrics": "full_period_only_if_all_sessions_committed_has_fill_no_error_and_actual_flat",
        "unknown_action": "stop_only_affected_case_keep_failure_and_residual_no_source_refresh",
        "historical_publication": "late_derived_mechanical_research_not_original_actual_publication",
        **FLAGS}


def _runtime():
    return durable._runtime_pins() + [_pin(__file__), _pin(policy.__file__)]


def register(inputs, out):
    """Pin existing manifests/pairs and new rules without loading raw or Engine."""
    mapping_file = None
    if isinstance(inputs, (str, Path)):
        mapping_file = _pin(inputs)
        mapping = _read(inputs)
        studies = mapping["studies"] if isinstance(mapping, dict) else mapping
    else:
        studies = inputs
    if not isinstance(studies, list) or len(studies) != 3:
        raise ValueError("The three explicit saved period inputs and original action pairs are required")
    references = [mapping_file] if mapping_file else []
    registered = []
    for item in studies:
        label = item["label"]
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", label):
            raise ValueError("Use one stable safe period label")
        source = _reference(item["inputs"], manifest=True)
        header = _read(source["path"])
        if (header.get("schema") != INPUT_SCHEMA or header.get("method_id") != INPUT_METHOD
                or header.get("fixed_protocol") != PROTOCOL
                or any(header.get(key) is not value for key, value in durable.FLAGS.items())):
            raise ValueError("Original unchanged native eight-type input and flags required")
        event = _reference(item["action_event_view"])
        reference = _reference(item["action_reference_view"])
        event_ref, reference_ref = durable._pair(Path(event["path"]).parent, Path(reference["path"]).parent)
        if event != event_ref or reference != reference_ref:
            raise ValueError("Use the exact explicit original action/reference pair")
        days = header["source_days"]
        tails = [day for day in days if day > header["end"]]
        if (days != sorted(set(days)) or days[0] != header["start"] or days[-1] != header["through"]
                or len(tails) != 6 or not header["start"] <= header["end"] < header["through"]):
            raise ValueError("Preserve the original decision window and exact six tail sessions")
        payloads = {key: header[key] for key in ("raw_file", "raw_sha256", "context_raw_file",
            "context_raw_sha256", "panels_file", "panels_sha256", "calendar", "calendar_sha256")}
        registered.append({"label": label, "inputs": source, "action_event_view": event,
            "action_reference_view": reference, "start": header["start"], "end": header["end"],
            "through": header["through"], "source_days": days, "tail_days": tails,
            "scope_count": len(header["scope_members"]), "source_payloads": payloads})
        references.extend((source, event, reference))
    if len({study["label"] for study in registered}) != 3 or [study["start"][:4] for study in registered] != ["2023", "2024", "2025"]:
        raise ValueError("Retain ordered distinct2023-partial/2024/2025 studies without selecting a period")
    folder = _separate(out, references)
    code = _runtime()
    folder.mkdir(parents=True, exist_ok=False)
    snapshot = folder / "code_snapshot"
    snapshot.mkdir()
    copies = []
    for i, reference in enumerate(code):
        path = snapshot / (f"{i:02}_" + Path(reference["path"]).name)
        with path.open("xb") as handle:
            handle.write(_checked(reference).read_bytes())
        copied = _pin(path)
        if copied["sha256"] != reference["sha256"]:
            raise ValueError("Router code changed during registration snapshot")
        copies.append({"source": reference, "copy": copied})
    record = {"schema": METHOD_ID + "_registration", "method_id": METHOD_ID,
        "registered_at": _now(), "spec": _spec(), "studies": registered,
        "input_mapping": mapping_file, "dependencies": references, "code": code,
        "code_snapshots": copies, "new_accounts_created": False, "engine_advanced": False,
        "raw_or_panels_loaded": False, **FLAGS}
    _save(folder / "registration.json", record)
    return record


def _registration(path):
    record = _read(path)
    if (record.get("schema") != METHOD_ID + "_registration" or record.get("method_id") != METHOD_ID
            or record.get("spec") != _spec() or record.get("code") != _runtime()
            or any(record.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Exact registered router rules, implementation and safety boundaries required")
    for reference in record["dependencies"] + [row["copy"] for row in record["code_snapshots"]]:
        _checked(reference)
    return record


def _case(base, factory, loaded, manifest, paths, environments, members, sessions, guard, capacity, account_id):
    config = _config(100000, 5)
    execution = factory(base, loaded["source_days"])
    context = RunContext(account_id, TradingMode.REPLAY)
    state = new_state(config.initial_cash)
    daily, decisions, failure = [], [], None
    main_snapshot, failed_trial = None, None
    days = loaded["source_days"]
    tails = [day for day in days if day > manifest["end"]]
    router = policy.LeaderTypeRouter(base, config, context,
        panels=loaded["panel_by_day"], paths=paths, environments=environments, sessions=sessions,
        source_days=days, members=members, qualification=loaded["qualification"], execution=execution,
        start=manifest["start"], end=manifest["end"], guard=guard, capacity=capacity)
    for index, day in enumerate(days):
        trial = deepcopy(state)
        try:
            next_state, row, decision = router.step(trial, day, index,
                tail_mode="EXIT_ALL" if day == tails[4] else "NO_CHANGE")
        except (ValueError, KeyError) as error:
            failure = {"day": day, "reason": "shared_router_session_blocked",
                "error": type(error).__name__ + ": " + str(error), "session_not_committed": True}
            failed_trial = trial
            break
        state = next_state
        daily.append(row)
        decisions.append(decision)
        if day == manifest["end"]:
            main_snapshot = deepcopy(state)
    residual = {symbol: sum(lot["qty"] for lot in lots) for symbol, lots in state["positions"].items()
        if sum(lot["qty"] for lot in lots) > 0}
    complete = failure is None and len(daily) == len(days) and not residual and bool(state["fills"])
    return {"case_id": "single_active_type__" + guard + "__" + capacity, "guard": guard,
        "capacity": capacity, "context": context.to_dict(), "config": config.to_dict(),
        "status": "completed_conditional_router_replay" if complete else "no_complete_PnL",
        "engine_sessions_committed": len(daily), "expected_sessions": len(days),
        "last_processed_day": state.get("day"), "account": state, "daily": daily,
        "decisions": decisions, "orders": list(state["orders"].values()), "fills": list(state["fills"].values()),
        "main_window_snapshot": main_snapshot, "failed_trial": failed_trial,
        "blocking_evidence": failure, "residual_positions": residual,
        "pending_targets": deepcopy(state.get("pending_targets")),
        "pending_risk_exits": deepcopy(state.get("pending_risk_exits")),
        "complete_PnL_available": complete, "metrics": metrics(daily, config.initial_cash) if complete else None,
        "terminal_liquidation_decision": tails[4], "terminal_liquidation_execution": tails[5],
        "parent_orders": len(state["orders"]), "fill_callbacks": len(state["fills"]),
        "historical_execution_policy": execution.provenance,
        "action_reference_checks": execution.accounting.checks,
        "candidate_reference_checks": execution.checks,
        "engine_advanced": bool(daily), "paper_orders_generated": bool(state["orders"]), **FLAGS}


def _period(study, registration_ref, out):
    loaded, manifest = load_inputs(study["inputs"]["path"])
    if any(manifest[key] != value for key, value in study["source_payloads"].items()):
        raise ValueError("Registered original native payload identities changed")
    members, sessions, _, as_of = _validate_panel_data(loaded["panel_by_day"], loaded["raw"], loaded["source_days"],
        loaded["calendar_payload"], loaded["scope_members"], loaded["qualification"])
    paths, _ = _rank_path(loaded["panel_by_day"], members, study["start"], study["end"])
    environments = _environment(loaded["panel_by_day"], sessions, as_of)
    context_raw = pd.read_parquet(manifest["context_raw_file"])
    if (sorted(context_raw.date.unique()) != [loaded["context_anchor"]["day"]]
            or context_raw.date.max() >= loaded["source_days"][0] or context_raw.duplicated(["date", "code"]).any()):
        raise ValueError("The original exact preceding native context must remain unchanged")
    accounting = load_action_accounting(Path(study["action_event_view"]["path"]).parent,
        Path(study["action_reference_view"]["path"]).parent, symbols=loaded["scope_members"],
        raw_days=sorted(set(context_raw.date) | set(loaded["source_days"])), replay_days=loaded["source_days"],
        price_source="independent_baostock_raw")
    factory = HistoricalExecutionFactory(accounting)
    # Build the raw quote matrices once. Each sequential case receives an
    # independent execution/accounting policy and a fresh Ledger state.
    base = ExactSourceRawFeed(pd.concat([context_raw, loaded["raw"]], ignore_index=True),
        sorted(set(context_raw.date) | set(loaded["source_days"])), sorted(members),
        context_anchor=loaded["context_anchor"])
    results = []
    for guard in GUARDS:
        for capacity, _ in CAPS:
            case_id = "single_active_type__" + guard + "__" + capacity
            folder = out / study["label"] / case_id
            folder.mkdir(parents=True, exist_ok=False)
            _save(folder / "started.json", {"started_at": _now(), "case_id": case_id,
                "source_inputs": study["inputs"], "registration": registration_ref, **FLAGS})
            try:
                case = _case(base, factory, loaded, manifest, paths, environments, members, sessions,
                    guard, capacity, METHOD_ID + ":" + registration_ref["sha256"] + ":" + study["label"] + ":" + case_id)
                for key in ("account", "daily", "decisions", "orders", "fills", "main_window_snapshot", "failed_trial"):
                    _save(folder / (key + ".json"), case.pop(key))
                case.update(period=study["label"], source_inputs=study["inputs"], measured_at=_now())
                _save(folder / "result.json", case)
            except Exception as error:
                case = {"case_id": case_id, "period": study["label"], "status": "case_failed_no_retry",
                    "error": type(error).__name__ + ": " + str(error), "execution_progress_unknown": True,
                    "complete_PnL_available": False, "metrics": None, **FLAGS}
                _save(folder / "failure.json", case)
            results.append(case)
            print(json.dumps({"event": "router_case_saved", "period": study["label"], "case_id": case_id,
                "status": case["status"], "engine_sessions_committed": case.get("engine_sessions_committed")}), flush=True)
    return results


def run(registration, out):
    registration = Path(registration).resolve()
    record = _registration(registration)
    folder = _separate(out, record["dependencies"] + [_pin(registration)])
    folder.mkdir(parents=True, exist_ok=False)
    registration_ref = _pin(registration)
    _save(folder / "started.json", {"started_at": _now(), "registration": registration_ref,
        "expected_cases": len(record["studies"]) * 4, **FLAGS})
    cases = []
    for study in record["studies"]:
        try:
            cases.extend(_period(study, registration_ref, folder))
        except Exception as error:
            failure = {"period": study["label"], "status": "period_source_failed_no_retry",
                "error": type(error).__name__ + ": " + str(error), "execution_progress_unknown": True, **FLAGS}
            _save(folder / (study["label"] + "_failure.json"), failure)
            cases.extend({**failure, "case_id": case_id, "complete_PnL_available": False, "metrics": None} for case_id in CASE_IDS)
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "status": "all_registered_router_case_outcomes_saved",
        "completed_at": _now(), "registration": registration_ref, "spec": record["spec"],
        "case_count": len(cases), "cases": cases,
        "all_cases_complete_PnL": all(case["complete_PnL_available"] for case in cases),
        "old_32_cases_rerun": False, "original_accounts_modified": False,
        "engine_advanced": any((case.get("engine_sessions_committed") or 0) > 0 for case in cases),
        "period_returns_concatenated": False, **FLAGS}
    _save(folder / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    declaration = commands.add_parser("register")
    declaration.add_argument("--inputs", type=Path, required=True)
    declaration.add_argument("--out", type=Path, required=True)
    replay = commands.add_parser("run")
    replay.add_argument("--registration", type=Path, required=True)
    replay.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    value = register(args.inputs, args.out) if args.action == "register" else run(args.registration, args.out)
    print(json.dumps({key: value[key] for key in ("schema", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
