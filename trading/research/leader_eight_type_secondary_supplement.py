"""Explicit secondary-terms branch of a completed native32 source supplement.

Only current source blocks resolved by the one pinned secondary amendment are
replayed from original cash. Other accounts and all earlier origin layers stay
saved facts. This is posthoc research, never issuer/PIT or execution admission.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd

from ..market_dataset import normalize_symbol, write_json
from . import leader_eight_type_action_supplement as parent_method
from .action_events import reference_concordance
from .action_terms import apply_reported_terms
from .adjusted_prices import checked_file
from .leader_eight_type_case_replay import (
    CASE_IDS, PAYLOADS, _checked, _now, _pin, _read, _save_case, _saved_case,
    _process_lock, _writer_lock_path,
)
from .leader_eight_type_historical_native import FLAGS, load_inputs
from .leader_eight_type_panel_replay import PROTOCOL, _case, _validate_panel_data
from .leader_environment import _clean
from .leader_environment_exit_ablation import (
    _committed_prefix_check, _execution_inputs, _resolved_source_cases, _view_pin,
)


METHOD = "native_fixed32_secondary_terms_source_supplement_v1"
EVENTS = (("300308.SZSE", "2025-06-19"), ("300468.SZSE", "2025-06-04"))
SPEC = {"id": METHOD, "protocol": PROTOCOL,
    "parent_method": parent_method.METHOD,
    "source_chain": "single_exact_parent_secondary_amendment",
    "case_selection": "all_current_blank_source_blocks_with_parent_pair_and_reference_match",
    "replay_from_original_cash": True, "checkpoint_recovery_performed": False,
    "complete_committed_prefix_required": True, "parameters_selected_by_return": False,
    "secondary_not_issuer_confirmed": True, "late_evidence_not_historical_PIT": True,
    "single_case_checkpoint_supported": False, "saved_ranks_environment_and_costs_unchanged": True}
FORMULA_FIELDS = ("reference_formula_status", "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb")


def _runtime_pins():
    folder = Path(__file__).resolve().parent
    pins = [*parent_method._runtime_pins(), _pin(__file__), _pin(folder / "action_terms.py")]
    return list({row["path"]: row for row in pins}.values())


def _own_pair(case):
    accounting = (case.get("historical_execution_policy") or {}).get("accounting") or {}
    return {"action_event_view": accounting.get("event_view"),
            "action_reference_view": accounting.get("reference_view")}


def _same_pair(actual, expected):
    return all(actual.get(key) is not None and Path(actual[key]).resolve() == Path(value).resolve()
               for key, value in expected.items())


def _parent(result_path):
    result_path = Path(result_path).resolve()
    saved = _read(result_path)
    registration_pin = saved["registration"]
    record, manifest, original, actual_pin = parent_method._registration(_checked(registration_pin))
    progress_path = result_path.parent / "progress.json"
    progress, result_pin = _read(progress_path), _pin(result_path)
    if (actual_pin != registration_pin or saved.get("schema") != parent_method.METHOD + "_result"
            or saved.get("method_id") != parent_method.METHOD or saved.get("spec") != parent_method.SPEC
            or saved.get("original_result") != record["original_result"]
            or saved.get("source_action_supplement") != record["binding"]
            or saved.get("protocol") != PROTOCOL or saved.get("config") != record["config"]
            or saved.get("case_count") != 32 or [case["case_id"] for case in saved["cases"]] != list(CASE_IDS)
            or progress.get("status") != "completed" or progress.get("method_id") != parent_method.METHOD
            or progress.get("registration") != registration_pin or progress.get("result") != result_pin
            or progress.get("active_case") is not None
            or [row["case_id"] for row in progress["completed_cases"]] != list(CASE_IDS)
            or any(saved.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("A completed exact first native32 supplement, registration and durable result pin are required")
    for key in ("start", "end", "source_days", "scope_members", "source", "capacity_paths", "raw_top5_paths", "environment_decisions"):
        if saved[key] != original[key]:
            raise ValueError("Parent changed original manifest/sample/ranks/environment: " + key)
    for case in saved["cases"]:
        kind, guard, cap = case["case_id"].split("__")
        if ((case.get("type"), case.get("guard"), case.get("capacity")) != (kind, guard, cap)
                or case.get("config") != record["config"] or not isinstance(case.get("source_origin"), dict)
                or not _same_pair(_own_pair(case), case["source_origin"]["action_pair"])):
            raise ValueError("Parent case identity, original cost or own source origin changed")
    return record, manifest, saved, {"result": result_pin, "progress": _pin(progress_path), "registration": registration_pin}


def _checked_secondary_pair(original, event_view, reference_view):
    """Reproduce the exact existing amendment from its copied literal histories."""
    old_event, old_ref = (Path(original[key]["directory"]).resolve()
                          for key in ("action_event_view", "action_reference_view"))
    event_view, reference_view = Path(event_view).resolve(), Path(reference_view).resolve()
    before, before_ref = _read(old_event / "result.json"), _read(old_ref / "result.json")
    event, reference = _read(event_view / "result.json"), _read(reference_view / "result.json")
    amendment = event.get("secondary_amendment", {})
    keys = [tuple(key) for key in amendment.get("events", [])]
    if (event.get("status") != "research_action_events_projected"
            or reference.get("status") != "research_reference_presence_diagnostic"
            or event.get("execution_admitted") is not False or reference.get("execution_admitted") is not False
            or Path(reference["event_view"]).resolve() != event_view
            or Path(before_ref["event_view"]).resolve() != old_event
            or reference.get("secondary_amendment") != amendment or len(keys) != len(EVENTS)
            or set(keys) != set(EVENTS) or len(set(keys)) != len(keys)
            or Path(amendment.get("parent_event_view", "")).resolve() != old_event
            or Path(amendment.get("parent_reference_view", "")).resolve() != old_ref
            or amendment.get("raw_prices_rescanned") is not False
            or amendment.get("other_events_changed") is not False or amendment.get("issuer_confirmed") is not False):
        raise ValueError("One exact-parent, explicit two-event secondary amendment is required")
    event_mutable = {"secondary_amendment", "secondary_reported_reserve_completions", "secondary_terms_sources", "files", "limitations"}
    ref_mutable = {"secondary_amendment", "event_view", "reference_formula_status_counts", "reference_formula_basis_counts", "files"}
    for old, new, mutable in ((before, event, event_mutable), (before_ref, reference, ref_mutable)):
        if any(old.get(key) != new.get(key) for key in (set(old) | set(new)) - mutable):
            raise ValueError("Unrelated source metadata or original raw/reference lineage changed")
    for folder, old, new, mutable in (
            (event_view, before, event, {"canonical_actions.parquet", "secondary_term_completions.json"}),
            (reference_view, before_ref, reference, {"raw_reference_changes.parquet", "input_events.json"})):
        if any(new["files"].get(name) != value for name, value in old["files"].items() if name not in mutable):
            raise ValueError("Original source/evidence file pins changed")
        for name, value in new["files"].items():
            checked_file(folder, name, value)
    if checked_file(reference_view, "input_events.json", reference["files"]["input_events.json"]).read_bytes() != (event_view / "result.json").read_bytes():
        raise ValueError("New reference does not bind its exact event result")
    prior_sources = before.get("secondary_terms_sources", [])
    if (event.get("secondary_terms_sources", [])[:len(prior_sources)] != prior_sources
            or len(event.get("secondary_terms_sources", [])) != len(prior_sources) + 1):
        raise ValueError("Only one copied secondary source addition is supported")
    scoped = Path(event["secondary_terms_sources"][-1]).resolve()
    scoped.relative_to(event_view)
    source = _read(scoped / "result.json")
    if (source.get("status") != "completed_secondary_action_terms" or source.get("execution_admitted") is not False
            or source.get("source_selection", {}).get("events") != [list(key) for key in keys]
            or Path(source.get("event_view", "")).resolve() != old_event
            or Path(source["source_selection"]["source_root"]).resolve() != Path(amendment["source_root"]).resolve()
            or any(candidate.get("issuer_confirmed") is not False or candidate.get("reported_reserve_per_share") != "0"
                   for row in source["records"] for candidate in row["candidates"])):
        raise ValueError("Copied literal secondary candidates must retain their exact parent and nonissuer boundary")
    left = pd.read_parquet(checked_file(old_event, "canonical_actions.parquet", before["files"]["canonical_actions.parquet"])).fillna("")
    right = pd.read_parquet(checked_file(event_view, "canonical_actions.parquet", event["files"]["canonical_actions.parquet"])).fillna("")
    if list(left.columns) != list(right.columns) or len(left) != len(right):
        raise ValueError("Canonical action rows/schema changed")
    for symbol, ex in EVENTS:
        selected = left.loc[left.symbol.eq(symbol) & left.dividOperateDate.eq(ex)]
        if len(selected) != 1 or selected.iloc[0]["dividReserveToStockPs"] != "":
            raise ValueError("Only a unique existing blank reserve can become literal secondary zero")
    expected, receipts = apply_reported_terms(left, scoped)
    expected = expected.loc[:, left.columns]
    if not expected.equals(right) or {(row["symbol"], row["date"]) for row in receipts} != set(EVENTS) or len(receipts) != len(EVENTS):
        raise ValueError("Canonical change is not exactly the two matched blank-to-zero reserve completions")
    old_receipts = (_read(checked_file(old_event, "secondary_term_completions.json", before["files"]["secondary_term_completions.json"]))
                    if "secondary_term_completions.json" in before["files"] else [])
    if (_read(checked_file(event_view, "secondary_term_completions.json", event["files"]["secondary_term_completions.json"])) != old_receipts + receipts
            or event.get("secondary_reported_reserve_completions") != before.get("secondary_reported_reserve_completions", 0) + len(EVENTS)):
        raise ValueError("Secondary completion receipts or original prefix changed")
    a = pd.read_parquet(checked_file(old_ref, "raw_reference_changes.parquet", before_ref["files"]["raw_reference_changes.parquet"]))
    b = pd.read_parquet(checked_file(reference_view, "raw_reference_changes.parquet", reference["files"]["raw_reference_changes.parquet"]))
    if list(a.columns) != list(b.columns) or len(a) != len(b):
        raise ValueError("Raw reference schema/count changed")
    mask = pd.Series([(normalize_symbol(code).vt_symbol, day) in set(EVENTS) for code, day in zip(a.code, a.date)], index=a.index)
    def evidence(name):
        return _read(checked_file(reference_view, name, reference["files"][name]))
    evidence_rows = {"special": evidence("reviewed_special_references.json"),
                     "distribution": evidence("cash_reference_evidence.json") + evidence("distribution_reference_evidence.json")}
    patched = a.copy(deep=True)
    if mask.any():
        selected = a.loc[mask].drop(columns=list(FORMULA_FIELDS)).copy()
        for index, row in selected.iterrows():
            action = right.loc[right.code.eq(row.code) & right.dividOperateDate.eq(row.date)]
            if len(action) != 1:
                raise ValueError("Selected frozen reference has no unique action")
            for field in right.columns.intersection(selected.columns):
                selected.loc[index, field] = action.iloc[0][field]
        refreshed = reference_concordance(selected, evidence_rows["special"], evidence_rows["distribution"])
        patched.loc[mask, patched.columns] = refreshed.loc[:, patched.columns].to_numpy()
    nullable = lambda frame: frame.astype(object).where(frame.notna(), None)
    if (not nullable(patched).equals(nullable(b)) or amendment.get("reference_rows_refreshed") != int(mask.sum())):
        raise ValueError("Raw price facts, unrelated references or deterministic formula refresh changed")
    return _clean({"event_view": _view_pin(event_view), "reference_view": _view_pin(reference_view),
        "amendment": amendment, "copied_source": _pin(scoped / "result.json"), "events": keys,
        "issuer_confirmed": False, "PIT_verified": False}), right, evidence_rows


def _binding(parent_record, parent, event_view, reference_view):
    original = {key: parent_record[key] for key in ("action_event_view", "action_reference_view")}
    proof, actions, evidence = _checked_secondary_pair(original, event_view, reference_view)
    own = {key: original[key]["directory"] for key in original}
    eligible = [case for case in parent_method._source_blocked_cases(parent) if _same_pair(_own_pair(case), own)]
    resolutions = _resolved_source_cases(eligible, actions, evidence)
    selected = [case["case_id"] for case in eligible if resolutions[case["case_id"]]["resolved"] is True]
    if not selected:
        raise ValueError("No current own-pair source block is genuinely resolved; do not replay")
    return _clean({"pair": proof, "eligible_source_case_ids": [case["case_id"] for case in eligible],
        "replayed_case_ids": selected, "reused_case_ids": [case_id for case_id in CASE_IDS if case_id not in selected],
        "unresolved_source_case_ids": [case["case_id"] for case in eligible if case["case_id"] not in selected],
        "source_resolution": resolutions, "new_pair_scope": "replayed_case_ids_only"})


def register(parent_result, action_event_view, action_reference_view, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Secondary supplement registration requires a new directory")
    parent_record, manifest, saved, pins = _parent(parent_result)
    binding = _binding(parent_record, saved, action_event_view, action_reference_view)
    record = {"schema": METHOD + "_registration", "method_id": METHOD, "registered_at": _now(),
        "spec": deepcopy(SPEC), "protocol": deepcopy(PROTOCOL), "parent": pins,
        "inputs": parent_record["inputs"], "config": saved["config"], "case_ids": list(CASE_IDS),
        "action_event_view": _view_pin(action_event_view), "action_reference_view": _view_pin(action_reference_view),
        "binding": binding, "code": _runtime_pins(), "late_evidence_not_historical_PIT": True,
        "issuer_confirmed": False, "PIT_verified": False, "original_results_modified": False, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "registration.json", record)
    return record


def _registration(path):
    path = Path(path).resolve()
    record = _read(path)
    if (record.get("schema") != METHOD + "_registration" or record.get("method_id") != METHOD
            or record.get("spec") != SPEC or record.get("protocol") != PROTOCOL
            or record.get("case_ids") != list(CASE_IDS) or record.get("code") != _runtime_pins()
            or any(record.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Exact secondary method, original protocol and frozen runtime are required")
    for pin in record["parent"].values():
        _checked(pin)
    _checked(record["inputs"])
    parent_record, manifest, saved, pins = _parent(record["parent"]["result"]["path"])
    if (pins != record["parent"] or parent_record["inputs"] != record["inputs"] or record["config"] != saved["config"]
            or any(_view_pin(record[key]["directory"]) != record[key] for key in ("action_event_view", "action_reference_view"))
            or _binding(parent_record, saved, record["action_event_view"]["directory"],
                        record["action_reference_view"]["directory"]) != record["binding"]):
        raise ValueError("Registered parent/source resolution or original costs changed")
    return record, manifest, saved, _pin(path)


def run(registration, out, *, resume=False, retry_active=False):
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as owner:
        try:
            owner.enter_context(_process_lock(_writer_lock_path(out)))
        except OSError as error:
            raise RuntimeError("Secondary supplement writer busy; replay not started") from error
        return _run_locked(registration, out, resume=resume, retry_active=retry_active)


def _run_locked(registration, out, *, resume=False, retry_active=False):
    record, manifest, parent, registration_pin = _registration(registration)
    selected = record["binding"]["replayed_case_ids"]
    if retry_active and not resume:
        raise ValueError("Retry requires explicit resume")
    if resume:
        progress = _read(out / "progress.json")
        if (progress.get("method_id") != METHOD or progress.get("registration") != registration_pin
                or progress.get("run_directory") != str(out) or progress.get("case_ids") != selected
                or [entry["case_id"] for entry in progress["completed_cases"]] != selected[:len(progress["completed_cases"])]):
            raise ValueError("Resume requires the exact registered selected-case prefix")
        cases = [_saved_case(entry, out, record) for entry in progress["completed_cases"]]
        active = progress.get("active_case")
        if active:
            attempt = Path(active["attempt_directory"]).resolve()
            if (len(cases) >= len(selected) or active["case_id"] != selected[len(cases)]
                    or attempt.parent != out / active["case_id"] or not attempt.name.startswith("attempt-")
                    or not attempt.name[8:].isdigit()):
                raise ValueError("Active attempt is not the next selected case")
            saved = _read(attempt / "attempt.json")
            if saved["status"] == "completed":
                entry = saved["saved_case"]
                if entry["case_id"] != active["case_id"] or Path(entry["attempt_directory"]).resolve() != attempt:
                    raise ValueError("Completed attempt changed selected identity")
                cases.append(_saved_case(entry, out, record))
                progress["completed_cases"].append(entry)
                progress["active_case"] = None
            elif not retry_active:
                raise ValueError("Unfinished selected case requires explicit --retry-active in a new attempt")
        if progress.get("status") == "completed":
            return _read(_checked(progress["result"]))
    else:
        if out.exists():
            raise FileExistsError("Fresh secondary replay requires a new output; use explicit resume")
        out.mkdir(parents=True)
        cases = []
        progress = {"method_id": METHOD, "registration": registration_pin, "run_directory": str(out),
            "case_ids": selected, "started_at": _now(), "status": "running", "completed_cases": [], "active_case": None, "errors": []}
        write_json(out / "progress.json", progress)
    attempt = None
    by_id = {case["case_id"]: case for case in parent["cases"]}
    try:
        if len(cases) < len(selected):
            loaded, actual = load_inputs(record["inputs"]["path"])
            if actual != manifest:
                raise ValueError("Original saved annual inputs changed")
            members, sessions, previous, _ = _validate_panel_data(loaded["panel_by_day"], loaded["raw"],
                loaded["source_days"], loaded["calendar_payload"], loaded["scope_members"], loaded["qualification"])
            context, factory = _execution_inputs(record, loaded, manifest)
        for case_id in selected[len(cases):]:
            old = by_id[case_id]
            folder = out / case_id
            folder.mkdir(exist_ok=True)
            numbers = [int(path.name[8:]) for path in folder.glob("attempt-*")]
            attempt = folder / f"attempt-{max(numbers, default=0) + 1:04d}"
            attempt.mkdir()
            write_json(attempt / "attempt.json", {"status": "running", "case_id": case_id, "started_at": _now()})
            progress.update(status="running", active_case={"case_id": case_id, "attempt_directory": str(attempt)})
            write_json(out / "progress.json", progress)
            kind, guard, cap = old["type"], old["guard"], old["capacity"]
            case = _case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
                parent["capacity_paths"][kind][cap], parent["environment_decisions"], members,
                loaded["qualification"], loaded["context_anchor"], manifest["start"], manifest["end"], kind, cap, guard,
                f"native-secondary-supplement:{registration_pin['sha256']}:{case_id}",
                execution_policy_factory=factory, context_raw=context, snapshot_day=old["daily"][-1]["date"])
            try:
                prefix = _committed_prefix_check(old, case, parent["source_days"])
            except ValueError as error:
                for key in PAYLOADS:
                    write_json(attempt / (key + ".json"), case[key])
                write_json(attempt / "committed_prefix_snapshot.json", case.get("committed_prefix_snapshot"))
                write_json(attempt / "prefix_check.json", {"status": "failed_original_prefix_changed", "error": str(error)})
                raise
            case["source_origin"] = deepcopy(old["source_origin"])
            case["source_supplement_origin"] = {"mode": "replayed", "parent_result": record["parent"]["result"],
                "parent_case_id": case_id, "action_pair": {key: record[key]["directory"] for key in ("action_event_view", "action_reference_view")},
                "committed_prefix_check": prefix, "replayed_from_original_cash": True,
                "checkpoint_recovery_performed": False, "issuer_confirmed": False, "PIT_verified": False}
            entry = _save_case(case, attempt)
            following = deepcopy(progress)
            following["completed_cases"].append(entry)
            following.update(active_case=None, updated_at=_now())
            write_json(out / "progress.json", following)
            progress = following
            cases.append(case)
            attempt = None
        replayed = {case["case_id"]: case for case in cases}
        combined = []
        for old in parent["cases"]:
            if old["case_id"] in replayed:
                combined.append(replayed[old["case_id"]])
            else:
                retained = deepcopy(old)
                retained["source_supplement_origin"] = {"mode": "reused", "parent_result": record["parent"]["result"],
                    "parent_case_id": old["case_id"], "action_pair": _own_pair(old),
                    "replayed_from_original_cash": False, "checkpoint_recovery_performed": False}
                combined.append(retained)
        result = {**deepcopy(parent), "schema": METHOD + "_result", "method_id": METHOD, "created_at": _now(),
            "spec": deepcopy(SPEC), "registration": registration_pin, "parent_result": record["parent"]["result"],
            "cases": combined, "source_secondary_supplement": record["binding"],
            "replayed_case_count": len(selected), "reused_case_count": 32 - len(selected),
            "new_action_pair": {key: record[key] for key in ("action_event_view", "action_reference_view")},
            "new_action_pair_scope": "replayed_case_ids_only", "historical_execution_policy_scope": "replayed_case_ids_only",
            "historical_execution_policy": cases[0]["historical_execution_policy"],
            "case_origins": {case["case_id"]: case["source_supplement_origin"] for case in combined},
            "all_cases_complete_PnL": all(case["complete_PnL_available"] for case in combined),
            "late_evidence_not_historical_PIT": True, "issuer_confirmed": False, "PIT_verified": False,
            "original_results_modified": False, **FLAGS}
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
    for name in ("parent-result", "action-event-view", "action-reference-view", "out"):
        create.add_argument("--" + name, type=Path, required=True)
    for name in ("run", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--registration", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "resume":
            command.add_argument("--retry-active", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register(args.parent_result, args.action_event_view, args.action_reference_view, args.out)
    else:
        result = run(args.registration, args.out, resume=args.command == "resume",
                     retry_active=getattr(args, "retry_active", False))
    print(json.dumps({"method_id": METHOD, "selected": result.get("binding", {}).get("replayed_case_ids"),
           "replayed_case_count": result.get("replayed_case_count")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
