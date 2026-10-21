"""Source-only supplement of original native fixed32 cases, with durable attempts.

Only previously blocked blank terms resolved by the registered evidence are
replayed from original cash. All other case accounts retain their original pair.
This is late evidence, not historical PIT or a checkpoint continuation.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json

import pandas as pd

from ..market_dataset import normalize_symbol, write_json
from .adjusted_prices import checked_file
from .leader_eight_type_case_replay import (
    CASE_IDS, PAYLOADS, _checked, _now, _pin, _process_lock, _read,
    _runtime_pins as _base_runtime_pins, _save_case, _saved_case, _writer_lock_path,
)
from .leader_eight_type_historical_native import FLAGS, INPUT_SCHEMA, METHOD_ID as INPUT_METHOD, load_inputs
from .leader_eight_type_panel_replay import PROTOCOL, SCHEMA as ORIGINAL_SCHEMA, _case, _validate_panel_data
from .leader_environment import _clean
from .leader_environment_exit_ablation import (
    _checked_supplement_pair, _committed_prefix_check, _execution_inputs,
    _resolved_source_cases, _view_pin,
)
from .leader_historical_execution_policy import METHOD_ID as EXECUTION_METHOD
from .leader_momentum_capacity_continue import check_reference_append
from .shortline_panel_replay import _config


METHOD = "native_fixed32_resolved_blank_action_supplement_v1"
SPEC = {"id": METHOD, "protocol": PROTOCOL,
    "case_selection": "original_blank_term_source_blocks_resolved_by_new_evidence_only",
    "source_chain": "one_explicit_terms_batch_then_at_most_one_formula_append",
    "known_amounts_dates_raw_signals_environment_capacity_costs": "unchanged",
    "replay_from_original_cash": True, "checkpoint_recovery_performed": False,
    "complete_committed_prefix_required": True, "parameters_selected_by_return": False,
    "late_evidence_not_historical_PIT": True, "single_case_checkpoint_supported": False}
AMOUNTS = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs", "bonus_taxable_cash_ps")


def _runtime_pins():
    folder = Path(__file__).resolve().parent
    return [*_base_runtime_pins(), _pin(__file__), *[_pin(folder / name) for name in
        ("leader_momentum_capacity_continue.py", "action_reviewed_amendment.py", "action_events.py", "adjusted_prices.py")]]


def _original(inputs, result):
    inputs, result = Path(inputs).resolve(), Path(result).resolve()
    inputs = inputs / "manifest.json" if inputs.is_dir() else inputs
    manifest, saved = _read(inputs), _read(result)
    method_path = result.parent / "historical_method.json"
    method = _read(method_path)
    if (manifest.get("schema") != INPUT_SCHEMA or manifest.get("method_id") != INPUT_METHOD
            or manifest.get("fixed_protocol") != PROTOCOL
            or any(manifest.get(key) is not value for key, value in FLAGS.items())
            or saved.get("schema") != ORIGINAL_SCHEMA or saved.get("protocol") != PROTOCOL
            or saved.get("case_count") != len(CASE_IDS)
            or [case["case_id"] for case in saved.get("cases", [])] != list(CASE_IDS)
            or saved.get("config") != _config(100000, 5).to_dict()
            or any(saved.get(key) != manifest[key] for key in ("start", "end", "source_days"))
            or saved.get("scope_members") != sorted(manifest["scope_members"])
            or saved.get("source", {}).get("manifest") != str(inputs)
            or saved.get("source", {}).get("manifest_sha256") != _pin(inputs)["sha256"]
            or saved.get("source", {}).get("input_record") != manifest
            or method.get("schema") != "leader_eight_type_historical_native_result_v1"
            or method.get("method_id") != INPUT_METHOD or method.get("original_32_protocol_unchanged") is not True
            or method.get("cases") != len(CASE_IDS) or Path(method["shared_engine_result"]).resolve() != result
            or method.get("IPO_qualification") != manifest["IPO_qualification"]):
        raise ValueError("Exact original native fixed32 result, historical method and immutable inputs required")
    accounting = method["action_accounting"]
    pair = {key: _view_pin(accounting[key]) for key in ("event_view", "reference_view")}
    factory = saved.get("historical_execution_policy", {})
    if factory.get("method_id") != EXECUTION_METHOD or factory.get("accounting") != accounting:
        raise ValueError("Original whole-session native execution policy and pair must remain exact")
    for case in saved["cases"]:
        kind, guard, cap = case["case_id"].split("__")
        policy, account = case.get("historical_execution_policy", {}), case.get("account") or {}
        if ((case.get("type"), case.get("guard"), case.get("capacity")) != (kind, guard, cap)
                or case.get("config") != saved["config"] or policy.get("method_id") != EXECUTION_METHOD
                or policy.get("accounting") != accounting or policy.get("prior_volume_execution_policy") is not None
                or account.get("prior_volume_execution_policy") is not None
                or account.get("full_exit_lifecycle") is not None):
            raise ValueError("Original case identity, fees or whole-session execution policy changed")
    return manifest, saved, method, {
        "action_event_view": pair["event_view"], "action_reference_view": pair["reference_view"]}


def _source_blocked_cases(saved):
    """Classify necessary original source failures; never inspect their returns."""
    blocked = []
    for case in saved["cases"]:
        block = case.get("blocking_evidence") or {}
        bad = [row for row in block.get("reference_evidence", [])
               if row.get("status") not in {"reference_match", "no_reported_event_reference_continuous"}]
        if (block.get("reason") != "held_or_pending_raw_or_action_reference_unknown"
                or block.get("session_not_committed") is not True or block.get("raw_issues") != []
                or not bad or any(row.get("status") != "unresolved_blank_terms" for row in bad)
                or any(row.get("engine_block_reason") is not None for row in block.get("IPO_evidence", []))):
            continue
        count = case["engine_sessions_committed"]
        if (type(count) is not int or not 0 < count < len(saved["source_days"])
                or [row["date"] for row in case["daily"]] != saved["source_days"][:count]
                or block.get("day") != saved["source_days"][count]
                or case.get("account", {}).get("day") != case["daily"][-1]["date"]
                or case.get("complete_PnL_available") is not False or case.get("metrics") is not None
                or any(row.get("day") != block["day"] for row in bad)):
            raise ValueError("Original source block must retain its complete committed account prefix")
        blocked.append(case)
    return blocked


def _blank_only(old_event_view, actions, changed):
    before = _read(Path(old_event_view) / "result.json")
    old = pd.read_parquet(checked_file(old_event_view, "canonical_actions.parquet",
        before["files"]["canonical_actions.parquet"])).fillna("").set_index(["symbol", "dividOperateDate"])
    new = actions.set_index(["symbol", "dividOperateDate"])
    if not old.index.is_unique or not new.index.is_unique:
        raise ValueError("Canonical event identities must remain unique")
    for key in changed:
        for field in AMOUNTS:
            left = old.loc[key, field] if field in old else ""
            right = new.loc[key, field] if field in new else ""
            if left == "":
                if right != "":
                    try:
                        value = Decimal(str(right))
                        if not value.is_finite() or value < 0:
                            raise InvalidOperation
                    except (ValueError, InvalidOperation) as error:
                        raise ValueError("New action amount must be explicit, finite and nonnegative") from error
            else:
                try:
                    unchanged = Decimal(str(left)).is_finite() and Decimal(str(left)) == Decimal(str(right))
                except (ValueError, InvalidOperation):
                    unchanged = False
                if not unchanged:
                    raise ValueError("Known economic action amount cannot change in a blank-term supplement")


def _source_binding(original, saved, manifest, event_view, reference_view):
    blocked = _source_blocked_cases(saved)
    allowed = {(row["symbol"], row["day"]) for case in blocked
        for row in case["blocking_evidence"]["reference_evidence"] if row.get("status") == "unresolved_blank_terms"}
    if not allowed:
        raise ValueError("No original blank-term source block is eligible for this supplement")
    event_view, reference_view = Path(event_view).resolve(), Path(reference_view).resolve()
    event, final = _read(event_view / "result.json"), _read(reference_view / "result.json")
    amendment = event.get("reviewed_cash_amendment", {})
    old_event, old_ref = (Path(original[key]["directory"]).resolve()
                         for key in ("action_event_view", "action_reference_view"))
    if (Path(amendment.get("parent_event_view", "")).resolve() != old_event
            or Path(amendment.get("parent_reference_view", "")).resolve() != old_ref):
        raise ValueError("Only one explicit terms batch directly on the original pair is supported")
    inherited_formula = _read(old_ref / "result.json").get("distribution_amendment")
    formula = final.get("distribution_amendment")
    if formula != inherited_formula and (not isinstance(formula, dict) or "parent_reference_view" not in formula):
        raise ValueError("Explicit single formula-append parent is required")
    terms_ref = Path(formula["parent_reference_view"]).resolve() if formula != inherited_formula else reference_view
    if _read(terms_ref / "result.json").get("distribution_amendment") != inherited_formula:
        raise ValueError("Only one formula append after the single terms batch is supported")
    proof, actions, evidence = _checked_supplement_pair(original, event_view, terms_ref, allowed)
    if len(proof["chain"]) != 1:
        raise ValueError("Only one explicit reviewed terms batch is supported")
    changed = {tuple(key) for key in proof["changed_events"]}
    _blank_only(old_event, actions, changed)
    formula_proof = None
    if terms_ref != reference_view:
        formula_proof = check_reference_append(terms_ref, reference_view, event_view,
            [manifest["context_anchor"]["day"]])
        added = {(normalize_symbol(code).vt_symbol, day) for code, day in formula_proof["added_events"]}
        if not added <= allowed:
            raise ValueError("Formula append may only resolve the original blocked event set")
        evidence = {"special": evidence["special"], "distribution": []}
        for name in ("cash_reference_evidence.json", "distribution_reference_evidence.json"):
            evidence["distribution"].extend(_read(checked_file(reference_view, name, final["files"][name])))
    resolved = _resolved_source_cases(blocked, actions, evidence)
    selected = [case["case_id"] for case in blocked if resolved[case["case_id"]]["resolved"] is True]
    if not selected:
        raise ValueError("No original source-blocked case is resolved by the registered evidence")
    return _clean({"source_blocked_case_ids": [case["case_id"] for case in blocked],
        "allowed_blocked_events": sorted(allowed), "replayed_case_ids": selected,
        "reused_case_ids": [case_id for case_id in CASE_IDS if case_id not in selected],
        "unresolved_source_case_ids": [case["case_id"] for case in blocked if case["case_id"] not in selected],
        "source_resolution": resolved, "terms_chain": proof, "formula_append": formula_proof,
        "original_pair": original, "new_pair_scope": "replayed_case_ids_only"})


def register(inputs, result, action_event_view, action_reference_view, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Action supplement registration requires a new directory")
    inputs = Path(inputs).resolve()
    inputs = inputs / "manifest.json" if inputs.is_dir() else inputs
    result = Path(result).resolve()
    manifest, saved, method, original = _original(inputs, result)
    binding = _source_binding(original, saved, manifest, action_event_view, action_reference_view)
    record = {"schema": METHOD + "_registration", "method_id": METHOD, "registered_at": _now(),
        "inputs": _pin(inputs), "original_result": _pin(result),
        "historical_method": _pin(result.parent / "historical_method.json"),
        "action_event_view": _view_pin(action_event_view), "action_reference_view": _view_pin(action_reference_view),
        "code": _runtime_pins(), "spec": deepcopy(SPEC), "protocol": deepcopy(PROTOCOL),
        "config": saved["config"], "case_ids": list(CASE_IDS), "binding": binding,
        "original_input_created_at": manifest["created_at"],
        "original_result_created_at": saved["created_at"], "original_method": method["method_id"],
        "late_evidence_not_historical_PIT": True, "original_results_modified": False, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "registration.json", record)
    return record


def _registration(path):
    record = _read(path)
    if (record.get("schema") != METHOD + "_registration" or record.get("method_id") != METHOD
            or record.get("spec") != SPEC or record.get("protocol") != PROTOCOL
            or record.get("case_ids") != list(CASE_IDS) or record.get("code") != _runtime_pins()
            or record.get("config") != _config(100000, 5).to_dict()
            or any(record.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Exact registered source supplement, original protocol, code and fees required")
    for key in ("inputs", "original_result", "historical_method"):
        _checked(record[key])
    for key in ("action_event_view", "action_reference_view"):
        if _view_pin(record[key]["directory"]) != record[key]:
            raise ValueError("Registered supplementary action pair changed")
    manifest, saved, _, original = _original(record["inputs"]["path"], record["original_result"]["path"])
    binding = _source_binding(original, saved, manifest, record["action_event_view"]["directory"],
                              record["action_reference_view"]["directory"])
    if binding != record["binding"]:
        raise ValueError("Registered source resolution, original cases or pair changed")
    return record, manifest, saved, _pin(path)


def _origin(record, old, replayed):
    accounting = (old.get("historical_execution_policy") or {}).get("accounting", {})
    pair = ({key: record[key]["directory"] for key in ("action_event_view", "action_reference_view")}
            if replayed else {"action_event_view": accounting.get("event_view"),
                             "action_reference_view": accounting.get("reference_view")})
    return {"mode": "replayed" if replayed else "reused", "original_result": record["original_result"],
        "original_case_id": old["case_id"], "action_pair": pair,
        "replayed_from_original_cash": replayed, "checkpoint_recovery_performed": False,
        "late_evidence_not_historical_PIT": replayed}


def run(registration, out, *, resume=False, retry_active=False):
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as owner:
        try:
            owner.enter_context(_process_lock(_writer_lock_path(out)))
        except OSError as error:
            raise RuntimeError("Fixed32 supplement writer busy; run/resume not started") from error
        return _run_locked(registration, out, resume=resume, retry_active=retry_active)


def _run_locked(registration, out, *, resume=False, retry_active=False):
    record, manifest, saved, registration_pin = _registration(registration)
    if retry_active and not resume:
        raise ValueError("Retrying an unfinished case requires explicit resume")
    if resume:
        progress = _read(out / "progress.json")
        if (progress.get("method_id") != METHOD or progress.get("registration") != registration_pin
                or progress.get("run_directory") != str(out) or progress.get("case_ids") != list(CASE_IDS)
                or [entry["case_id"] for entry in progress["completed_cases"]]
                    != list(CASE_IDS[:len(progress["completed_cases"])])):
            raise ValueError("Resume requires the original supplement registration and contiguous saved cases")
        cases = [_saved_case(entry, out, record) for entry in progress["completed_cases"]]
        active = progress.get("active_case")
        if active:
            attempt = Path(active["attempt_directory"]).resolve()
            if (len(cases) >= len(CASE_IDS) or active["case_id"] != CASE_IDS[len(cases)]
                    or attempt.parent != out / active["case_id"] or not attempt.name.startswith("attempt-")
                    or not attempt.name[8:].isdigit()):
                raise ValueError("Unfinished case must be the next registered original case")
            status = _read(attempt / "attempt.json")
            if status["status"] == "completed":
                entry = status["saved_case"]
                if entry["case_id"] != active["case_id"] or Path(entry["attempt_directory"]).resolve() != attempt:
                    raise ValueError("Completed active attempt changed its case identity")
                cases.append(_saved_case(entry, out, record))
                progress["completed_cases"].append(entry)
                progress["active_case"] = None
            elif not retry_active:
                raise ValueError("Unfinished case requires explicit --retry-active in a new attempt")
        if progress.get("status") == "completed":
            return _read(_checked(progress["result"]))
    else:
        if out.exists():
            raise FileExistsError("Supplement replay requires a new directory; use explicit resume")
        out.mkdir(parents=True)
        cases = []
        progress = {"method_id": METHOD, "registration": registration_pin, "case_ids": list(CASE_IDS),
            "run_directory": str(out), "started_at": _now(), "status": "running",
            "completed_cases": [], "active_case": None, "errors": []}
        write_json(out / "progress.json", progress)
    selected = set(record["binding"]["replayed_case_ids"])
    attempt = None
    try:
        loaded, actual = load_inputs(record["inputs"]["path"])
        if actual != manifest:
            raise ValueError("Supplement replay cannot change original historical inputs")
        members, sessions, previous, _ = _validate_panel_data(loaded["panel_by_day"], loaded["raw"],
            loaded["source_days"], loaded["calendar_payload"], loaded["scope_members"], loaded["qualification"])
        context_raw, factory = None, None
        for old in saved["cases"][len(cases):]:
            case_id = old["case_id"]
            case_folder = out / case_id
            case_folder.mkdir(exist_ok=True)
            numbers = [int(path.name[8:]) for path in case_folder.glob("attempt-*")]
            attempt = case_folder / f"attempt-{max(numbers, default=0) + 1:04d}"
            attempt.mkdir()
            write_json(attempt / "attempt.json", {"status": "running", "case_id": case_id, "started_at": _now()})
            progress.update(active_case={"case_id": case_id, "attempt_directory": str(attempt)}, status="running")
            write_json(out / "progress.json", progress)
            origin = _origin(record, old, case_id in selected)
            if case_id in selected:
                if factory is None:
                    context_raw, factory = _execution_inputs(record, loaded, manifest)
                kind, guard, cap = old["type"], old["guard"], old["capacity"]
                case = _case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
                    saved["capacity_paths"][kind][cap], saved["environment_decisions"], members,
                    loaded["qualification"], loaded["context_anchor"], manifest["start"], manifest["end"],
                    kind, cap, guard, f"native-action-supplement:{registration_pin['sha256']}:{case_id}",
                    execution_policy_factory=factory, context_raw=context_raw, snapshot_day=old["daily"][-1]["date"])
                try:
                    origin["committed_prefix_check"] = _committed_prefix_check(old, case, saved["source_days"])
                except ValueError as error:
                    for key in PAYLOADS:
                        write_json(attempt / (key + ".json"), case[key])
                    write_json(attempt / "committed_prefix_snapshot.json", case.get("committed_prefix_snapshot"))
                    write_json(attempt / "prefix_check.json", {"status": "failed_original_prefix_changed", "error": str(error)})
                    raise
            else:
                case = deepcopy(old)
            case["source_origin"] = origin
            entry = _save_case(case, attempt)
            next_progress = deepcopy(progress)
            next_progress["completed_cases"].append(entry)
            next_progress.update(active_case=None, updated_at=_now())
            write_json(out / "progress.json", next_progress)
            progress = next_progress
            cases.append(case)
            attempt = None
            print(json.dumps({"event": "supplement_case_saved", "case_id": case_id,
                              "mode": origin["mode"], "saved_cases": len(cases)}), flush=True)
        result = {**deepcopy(saved), "schema": METHOD + "_result", "method_id": METHOD, "created_at": _now(),
            "registration": registration_pin, "spec": deepcopy(SPEC), "cases": cases,
            "original_result": record["original_result"], "source_action_supplement": record["binding"],
            "replayed_case_count": len(selected), "reused_case_count": len(CASE_IDS) - len(selected),
            "case_origins": {case["case_id"]: case["source_origin"] for case in cases},
            "new_action_pair": {key:record[key] for key in ("action_event_view", "action_reference_view")},
            "new_action_pair_scope": "replayed_case_ids_only",
            "historical_execution_policy": factory.provenance if factory else None,
            "historical_execution_policy_scope": "replayed_case_ids_only",
            "all_cases_complete_PnL": all(case["complete_PnL_available"] for case in cases),
            "late_evidence_not_historical_PIT": True, "original_results_modified": False, **FLAGS}
        # On a resume with only retained cases left, recover the registered new
        # method provenance from the already replayed case, not the old top pair.
        if factory is None:
            result["historical_execution_policy"] = next(
                case["historical_execution_policy"] for case in cases if case["case_id"] in selected)
        write_json(out / "result.json", result)
        progress.update(status="completed", active_case=None, ended_at=_now(), result=_pin(out / "result.json"))
        write_json(out / "progress.json", progress)
        return result
    except BaseException as error:
        failure = {"failed_at": _now(), "error": f"{type(error).__name__}: {error}",
                   "active_case": deepcopy(progress.get("active_case")), "session_checkpoint_available": False}
        if attempt is not None and (not (attempt / "attempt.json").exists()
                or _read(attempt / "attempt.json").get("status") != "completed"):
            write_json(attempt / "attempt.json", {"status": "failed", **failure})
        progress["errors"].append(failure)
        progress.update(status="interrupted_or_failed", updated_at=_now())
        write_json(out / "progress.json", progress)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("register")
    for name in ("inputs", "result", "action-event-view", "action-reference-view", "out"):
        create.add_argument("--" + name, type=Path, required=True)
    for name in ("run", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--registration", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "resume":
            command.add_argument("--retry-active", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register(args.inputs, args.result, args.action_event_view, args.action_reference_view, args.out)
        summary = {"registration": str(args.out.resolve() / "registration.json"),
                   "replayed_case_ids": result["binding"]["replayed_case_ids"]}
    else:
        result = run(args.registration, args.out, resume=args.command == "resume",
                     retry_active=getattr(args, "retry_active", False))
        summary = {"result": str(args.out.resolve() / "result.json"),
                   "replayed_cases": result["replayed_case_count"], "reused_cases": result["reused_case_count"]}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
