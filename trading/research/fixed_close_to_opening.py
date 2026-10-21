"""Continue one registered fixed book from one new close into saved opening.

This is a thin restartable call chain around the existing supplement, fixed
replay and opening coordinator. It never collects sources or retries an
unresolved replay/producer, and it never creates another account registration.
"""
import argparse
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
import json

from ..contracts import MARKET_TZ
from ..worker import lock
from . import stockdb_fixed_daily as fixed
from . import opening_daily as opening
from .lowvol_supplement_append import append_sources, SCHEMA as APPEND_SCHEMA
from .baostock_full_close import load_complete
from .leader_recent_source_history import source_proof
from .provider_close_batches import _acquisition_times


SCHEMA = "fixed_complete_close_to_saved_opening_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "prefill_allowed": False,
    "broker_interacted": False, "personal_account_mutated": False, "network_accessed": False,
    "account_registered": False}


def _stamp(value):
    return fixed._stamp(value).astimezone(MARKET_TZ)


def _separate(destination, original):
    original = Path(original).resolve()
    if destination == original or destination in original.parents or original in destination.parents:
        raise ValueError("Daily operation must not overlap any original source, account or opening artifacts")


def _bindings(run_dir, opening_run_dir):
    head_path, registration_path = run_dir / "head.json", run_dir / "registration.json"
    head, registration = fixed._read(head_path), fixed._read(registration_path)
    registration_ref = fixed._reference(registration_path)
    if (head.get("schema") != fixed.SCHEMA or registration.get("schema") != fixed.SCHEMA
            or head.get("method_id") != fixed.METHOD or registration.get("method_id") != fixed.METHOD
            or head["registration"] != registration_ref
            or any(row.get(k) is not v for row in (head, registration) for k, v in fixed.FLAGS.items())):
        raise ValueError("Fixed daily registration, method or research boundary changed")
    record = fixed._read(fixed._checked(head["source"]["run"]))
    protocol = fixed._read(fixed._checked(head["source"]["protocol"]))
    if (record.get("status") != "completed_research_only" or record.get("variants") != [registration["signal"]]
            or record.get("replay_end") != head["last_completed_day"]
            or any(record.get(k) != registration.get(k) or protocol.get(k) != registration.get(k)
                   for k in ("config", "replay_start", "month_end_schedule", "full_exit_lifecycle"))
            or Path(protocol["snapshot"]).resolve() != Path(registration["snapshot"]).resolve()):
        raise ValueError("Current fixed source does not retain its original configuration and calendar")
    coordinator, effective = opening._load(opening_run_dir)
    routes = [row for row in effective["routes"] if row["family"] == "fixed"
              and Path(row["source"]).resolve() == run_dir and row.get("signal") == registration["signal"]]
    if len(routes) != 1:
        raise ValueError("Opening coordinator must contain exactly this registered fixed source and signal")
    route = routes[0]
    anchor = opening._registration_for(coordinator, route["id"])
    return head, registration, record, protocol, coordinator, route, anchor


def _waiting(through, reason):
    return {"schema": SCHEMA, "status": "waiting_for_complete_source", "through": through,
        "reason": reason, "operation": None, "fixed_path_completed": False,
        "fixed_consumer_called_this_call": False, "research_engine_advanced_this_call": False,
        "execution_progress_unknown": False, "opening_factory_called_this_call": False, **FLAGS}


def _package(package, through, registration, current):
    required = [package / name for name in ("plan.json", "receipt.json", "raw_daily.csv")]
    if not all(path.is_file() for path in required):
        return None, "exact_complete_package_files_missing"
    plan, receipt = fixed._read(required[0]), fixed._read(required[1])
    if plan.get("day") != through or receipt.get("day") != through:
        raise ValueError("Complete package must name the one exact requested next session")
    if (receipt.get("status") != "complete_full_dated_close_inventory_not_admitted"
            or receipt.get("full_dated_roster_inventory_complete") is not True
            or receipt.get("pending_symbols") or receipt.get("preserved_failed_batches")):
        return None, "exact_D_inventory_not_complete"
    calendars = registration["month_end_schedule"]["calendar_files"]
    if plan.get("calendar_sha256") not in {row["sha256"] for row in calendars}:
        raise ValueError("Complete package calendar differs from the fixed registration")
    roster_folder = (Path(plan["roster_folder"]).resolve()
                     if plan.get("roster_folder") is not None else package / "roster")
    if not (roster_folder / "receipt.json").is_file():
        return None, "exact_D_dated_roster_receipt_missing"
    roster_preview = fixed._read(roster_folder / "receipt.json")
    if not roster_preview.get("raw_csv") or not Path(roster_preview["raw_csv"]).is_file():
        return None, "exact_D_dated_roster_raw_missing"
    raw, metadata = load_complete(package)
    if metadata["plan"]["day"] != through or metadata.get("read_only_verified") is not True:
        raise ValueError("Strict source loader did not verify the exact requested session")
    clocks = _acquisition_times(metadata, package)
    if clocks["terminal_completion_observed_at"] is None or clocks["roster_receipt_completed_at"] is None:
        return None, "exact_D_source_or_roster_completion_clock_missing"
    available = max(_stamp(clocks["terminal_completion_observed_at"]), _stamp(clocks["roster_receipt_completed_at"]))
    if not _stamp(through+"T15:00:00+08:00") <= available <= current:
        raise ValueError("Complete close must actually exist after its own close and by this invocation")
    if any(_stamp(value) > available for value in [*clocks["acquisition_timestamps"].values(),
                                                  *clocks["roster_acquisition_timestamps"].values()]):
        raise ValueError("Recorded source acquisition clocks exceed its completed evidence")
    roster_receipt = Path(clocks["roster_receipt"])
    roster = fixed._read(roster_receipt)
    roster_raw = Path(roster["raw_csv"]).resolve()
    proof = source_proof(raw, metadata, plan["calendar_sha256"])
    return {"directory": str(package), "source_proof": proof, "available_at": available.isoformat(),
        "refs": {name: fixed._reference(path) for name, path in {
            "plan": required[0], "receipt": required[1], "raw": required[2],
            "roster_receipt": roster_receipt, "roster_raw": roster_raw}.items()}}, None


def _save(path, receipt):
    if path.is_file() and fixed._read(path) == receipt:
        return
    fixed._atomic(path, receipt)


def _check_package_saved(value, package):
    if value["directory"] != str(package):
        raise ValueError("Operation cannot switch its exact dated source package")
    for reference in value["refs"].values():
        fixed._checked(reference)


def _supplement(receipt, operation):
    directory = operation / "supplement"
    result_path = directory / "result.json"
    if not result_path.is_file():
        return None
    result = fixed._read(result_path)
    parent = receipt["parent_head"]["supplement"]
    proof = receipt["package"]["source_proof"]
    rows = result.get("source_proofs", [])
    if (result.get("schema") != APPEND_SCHEMA or result.get("status") != "completed_append_only_research_supplement"
            or Path(result["base_file"]).resolve() != Path(parent["path"]).resolve()
            or result["base_sha256"] != parent["sha256"]
            or Path(result["raw_daily_file"]).resolve() != directory / "raw_daily.parquet"
            or result.get("original_full_row_prefix_preserved") is not True
            or result.get("original_strings_and_nulls_preserved") is not True
            or result.get("current_source_overwrites_original_keys") is not False
            or len(rows) != 1 or any(rows[0].get(k) != v for k, v in proof.items())
            or any(result.get(k) is not False for k in
                   ("execution_admitted", "selection_enabled", "strategy_validated", "stockdb_modified", "ledger_mutated"))):
        raise ValueError("Saved append result does not retain this operation's exact parent and source")
    raw_ref = {"path": str(directory / "raw_daily.parquet"), "sha256": result["raw_daily_sha256"]}
    fixed._checked(raw_ref)
    fixed._checked(parent)
    refs = {"result": fixed._reference(result_path), "raw": raw_ref}
    previous = receipt.get("supplement")
    if previous is not None and previous != refs:
        raise ValueError("Operation's saved supplement result changed")
    return refs


def _pending_fixed_attempts(run_dir, parent):
    result = []
    for path in (run_dir / "attempts").glob("*.json"):
        attempt = fixed._read(path)
        if attempt.get("head_before") == parent and str(path) != parent.get("last_attempt"):
            result.append((path, attempt))
    return result


def _fixed_commit(receipt, run_dir, head, identity, inputs):
    parent = receipt["parent_head"]
    if head == parent:
        return None
    attempt_path = run_dir / "attempts" / (identity + ".json")
    if (head["registration"] != parent["registration"] or head["generation"] != parent["generation"]+1
            or head["last_completed_day"] != receipt["through"] or head.get("supplement") != receipt["supplement"]["raw"]
            or head.get("last_attempt") != str(attempt_path) or not attempt_path.is_file()):
        raise ValueError("Fixed head changed outside this exact operation or has superseded its requested day")
    attempt = fixed._read(attempt_path)
    if (attempt.get("schema") != fixed.SCHEMA or attempt.get("method_id") != fixed.METHOD
            or attempt.get("input_id") != identity or attempt.get("head_before") != parent
            or attempt.get("inputs") != inputs or attempt.get("result", {}).get("status") != "completed_research_only"
            or not attempt.get("output")
            or Path(head["source"]["run"]["path"]).resolve() != Path(attempt["output"]).resolve() / "run.json"):
        raise ValueError("Fixed head does not have its exact successful original attempt")
    for reference in head["source"].values():
        fixed._checked(reference)
    return {"head": fixed._reference(run_dir / "head.json"), "attempt": fixed._reference(attempt_path),
            "input_id": identity, "source": deepcopy(head["source"])}


def _manual_fixed(run_dir, path, attempt):
    if attempt.get("result", {}).get("status") == "completed_research_only" and attempt.get("output"):
        return f'stockdb-fixed-daily recover --run-dir "{run_dir}" --input-id {path.stem}'
    return None


def _result(receipt, path, *, fixed_called=False, opening_called=False):
    return {**deepcopy(receipt), "operation": str(path),
        "fixed_path_completed": receipt["status"] == "completed",
        "fixed_consumer_called_this_call": fixed_called,
        "research_engine_advanced_this_call": ((True if receipt["fixed"] is not None else None) if fixed_called else False),
        "execution_progress_unknown": receipt["fixed"] is None and bool(
            fixed_called or receipt["fixed_started"] or receipt.get("pending_fixed_attempts")),
        "opening_factory_called_this_call": opening_called}


def _validate_operation(receipt, run_dir, opening_run_dir, through, execution, head, route, anchor, package):
    if (receipt.get("schema") != SCHEMA or receipt.get("run_directory") != str(run_dir)
            or receipt.get("opening_run_directory") != str(opening_run_dir) or receipt.get("through") != through
            or receipt.get("execution_day") != execution or receipt.get("registration") != head["registration"]
            or receipt.get("route") != route or receipt.get("route_registration") != anchor
            or any(receipt.get(k) is not v for k, v in FLAGS.items())):
        raise ValueError("Saved daily operation cannot change date, registration, source route or execution boundary")
    _check_package_saved(receipt["package"], package)
    if receipt["fixed"] is not None and fixed._reference(run_dir / "head.json") != receipt["fixed"]["head"]:
        raise ValueError("Fixed source has changed or superseded this completed operation")


def _opening_row(receipt, opening_run_dir):
    status = opening.status(opening_run_dir)
    row = next(row for row in status["routes"] if row["id"] == receipt["route_id"])
    expected = receipt["fixed"]["head"]
    if row["source_head"] != expected:
        raise ValueError("Opening route no longer observes this operation's exact fixed head")
    product = row.get("saved_product")
    completed = (not row["source_changed"] and product is not None
                 and product["source_head"] == expected
                 and product["source_day"] == receipt["through"]
                 and product["execution_day"] == receipt["execution_day"])
    if not row["source_changed"] and not completed:
        raise ValueError("Saved fixed opening product belongs to a different day or source")
    if completed:
        for reference in product["refs"].values():
            fixed._checked(reference)
    others = [{k: value.get(k) for k in ("id", "status", "error", "source_changed")}
              for value in status["routes"] if value["id"] != receipt["route_id"] and value["source_changed"]]
    return row, completed, others


def run(run_dir, opening_run_dir, through, package, out, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    if not isinstance(through, str) or date.fromisoformat(through).isoformat() != through:
        raise ValueError("Use one exact ISO trading session")
    run_dir, opening_run_dir = fixed._destination(run_dir), opening._destination(opening_run_dir)
    operation, package = fixed._destination(out), Path(package).resolve()
    head, registration, record, protocol, coordinator, route, anchor = _bindings(run_dir, opening_run_dir)
    path = operation / "receipt.json"
    existing = fixed._read(path) if path.is_file() else None
    protected_head = existing["parent_head"] if existing is not None else head
    for protected in (run_dir, opening_run_dir, package, Path(registration["snapshot"]),
                      Path(protected_head["supplement"]["path"]).parent,
                      *(Path(ref["path"]).parent for ref in protected_head["source"].values()),
                      *(Path(path).parent for path in route.get("adopt", {}).values())):
        _separate(operation, protected)
    sessions = fixed._calendar(registration["month_end_schedule"])
    execution = next((day for day in sessions if day > through), None)
    if through not in sessions or execution is None:
        raise ValueError("Requested D and its next opening must exist in the original fixed calendar")
    if existing is None:
        if operation.exists():
            raise ValueError("Existing operation directory has no saved receipt; progress is unknown")
        if next((day for day in sessions if day > head["last_completed_day"]), None) != through:
            raise ValueError("Only the exact one next session of the existing registered account is supported")
        if current < _stamp(through+"T15:00:00+08:00"):
            if (package / "receipt.json").is_file() and fixed._read(package / "receipt.json").get("status") == "complete_full_dated_close_inventory_not_admitted":
                raise ValueError("A completed future close package cannot exist before its own market-close clock")
            return _waiting(through, "requested_exact_session_has_not_closed")
        source, reason = _package(package, through, registration, current)
        if source is None:
            return _waiting(through, reason)
        receipt = {"schema": SCHEMA, "status": "source_complete", "through": through,
            "execution_day": execution, "run_directory": str(run_dir), "opening_run_directory": str(opening_run_dir),
            "route_id": route["id"], "route": deepcopy(route), "route_registration": anchor,
            "registration": head["registration"], "parent_head": deepcopy(head),
            "parent_head_ref": fixed._reference(run_dir / "head.json"), "package": source,
            "supplement": None, "fixed": None, "opening": None,
            "append_started": False, "fixed_started": False, "opening_started": False,
            "created_at": current.isoformat(), **FLAGS}
        operation.mkdir(parents=True, exist_ok=False)
        _save(path, receipt)
    else:
        _validate_operation(existing, run_dir, opening_run_dir, through, execution, head, route, anchor, package)
        if existing["status"] == "completed":
            row, complete, _ = _opening_row(existing, opening_run_dir)
            if not complete or row["saved_product"] != existing["opening"]["product"]:
                raise ValueError("Completed operation's saved opening product changed")
            # No lock (its existing implementation writes a byte), source
            # loader, append, replay or factory on this completed-input path.
            return _result(existing, path)
    # All output directories for this one fixed account share the same driver
    # exclusion. Keep the pending-attempt check and consumer call in one scope.
    with lock(run_dir / "close_to_opening.lock"), lock(operation / "operation.lock"):
        receipt = fixed._read(path)
        _validate_operation(receipt, run_dir, opening_run_dir, through, execution, head, route, anchor, package)
        if receipt["status"] == "completed":
            row, completed, others = _opening_row(receipt, opening_run_dir)
            if not completed or row["saved_product"] != receipt["opening"]["product"]:
                raise ValueError("Completed operation's saved opening product changed")
            return _result(receipt, path)
        supplement = _supplement(receipt, operation)
        if supplement is None:
            if fixed._read(run_dir / "head.json") != receipt["parent_head"]:
                raise ValueError("Fixed parent changed before this operation had a complete supplement")
            pending = _pending_fixed_attempts(run_dir, receipt["parent_head"])
            if pending:
                receipt.update(status="fixed_pending", reason="prior_fixed_attempt_for_same_parent_requires_review_before_append",
                    pending_fixed_attempts=[{"path": str(file), "status": value.get("status"),
                        "manual_recovery_command": _manual_fixed(run_dir, file, value)} for file, value in pending])
                _save(path, receipt)
                return _result(receipt, path)
            if receipt["append_started"] or (operation / "supplement").exists():
                receipt.update(status="source_complete", reason="append_progress_unknown_no_successful_result_no_retry")
                _save(path, receipt)
                return _result(receipt, path)
            receipt["append_started"] = True
            _save(path, receipt)
            try:
                append_sources(receipt["parent_head"]["supplement"]["path"], [package], operation / "supplement")
            except Exception as error:
                receipt.update(status="source_complete", reason="append_failed_or_interrupted_no_automatic_retry",
                               error=f"{type(error).__name__}: {error}")
                _save(path, receipt)
                return _result(receipt, path)
            supplement = _supplement(receipt, operation)
            if supplement is None:
                raise ValueError("Append returned without a complete saved result")
        receipt.update(supplement=supplement, status="fixed_pending")
        _save(path, receipt)
        identity, inputs = fixed._fingerprint(receipt["parent_head"], through, [package], Path(supplement["raw"]["path"]))
        current_head = fixed._read(run_dir / "head.json")
        commit = _fixed_commit(receipt, run_dir, current_head, identity, inputs)
        fixed_called = False
        if commit is None:
            pending = _pending_fixed_attempts(run_dir, receipt["parent_head"])
            if pending or receipt["fixed_started"]:
                receipt.update(reason="fixed_attempt_requires_explicit_recovery_or_review_no_retry",
                    pending_fixed_attempts=[{"path": str(file), "status": value.get("status"),
                        "manual_recovery_command": _manual_fixed(run_dir, file, value)} for file, value in pending])
                _save(path, receipt)
                return _result(receipt, path)
            receipt.update(fixed_started=True, fixed_input_id=identity)
            _save(path, receipt)
            fixed_called = True
            try:
                fixed.run(run_dir, through, packages=[package], supplemental_raw_view=supplement["raw"]["path"])
            except Exception as error:
                receipt.update(error=f"{type(error).__name__}: {error}")
            commit = _fixed_commit(receipt, run_dir, fixed._read(run_dir / "head.json"), identity, inputs)
            if commit is None:
                pending = _pending_fixed_attempts(run_dir, receipt["parent_head"])
                receipt.update(reason="fixed_attempt_not_committed_no_automatic_retry",
                    pending_fixed_attempts=[{"path": str(file), "status": value.get("status"),
                        "manual_recovery_command": _manual_fixed(run_dir, file, value)} for file, value in pending])
                _save(path, receipt)
                return _result(receipt, path, fixed_called=fixed_called)
        receipt.update(fixed=commit, status="opening_pending", reason=None)
        _save(path, receipt)
        row, complete, others = _opening_row(receipt, opening_run_dir)
        opening_called = False
        if not complete and row["status"] == "source_changed_product_required" and not receipt["opening_started"]:
            receipt["opening_started"] = True
            _save(path, receipt)
            try:
                answer = opening.run(opening_run_dir, current=current)
                opening_called = bool(answer.get("factory_called"))
            except Exception as error:
                receipt.update(error=f"{type(error).__name__}: {error}")
            row, complete, others = _opening_row(receipt, opening_run_dir)
        if complete:
            receipt.update(status="completed", reason=None, opening={"product": row["saved_product"],
                "input_id": row["input_id"], "attempt": row["attempt"]},
                completed_at=current.isoformat(), other_routes_needing_attention=others,
                all_system_routes_refreshed=False)
        else:
            command = (f'opening-daily recover --run-dir "{opening_run_dir}" --route {route["id"]} --input-id {row["input_id"]}'
                       if row["status"] == "recoverable_saved_product" else None)
            receipt.update(reason="opening_attempt_requires_explicit_recovery_or_review_no_retry",
                opening_observation={k: row.get(k) for k in ("status", "input_id", "attempt", "attempt_status", "error")},
                manual_recovery_command=command, other_routes_needing_attention=others,
                all_system_routes_refreshed=False)
        _save(path, receipt)
        return _result(receipt, path, fixed_called=fixed_called, opening_called=opening_called)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--opening-run-dir", required=True, type=Path)
    parser.add_argument("--through", required=True)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    value = run(args.run_dir, args.opening_run_dir, args.through, args.package, args.out)
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0
