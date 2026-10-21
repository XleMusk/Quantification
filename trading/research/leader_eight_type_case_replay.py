"""Explicit fixed32 replay with durable completed cases, using the original case runner.

Resume reuses finished cases only. An unfinished case has no session checkpoint;
retrying it requires --retry-active and a new attempt, from its original cash.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from ..worker import lock as _process_lock
from .baostock_daily_slice import digest
from .leader_eight_type_historical_native import FLAGS, INPUT_SCHEMA, METHOD_ID as INPUT_METHOD, load_inputs
from .leader_eight_type_panel_replay import CAPS, GUARDS, PROTOCOL, TYPE_IDS, _case, _environment, _rank_path, _validate_panel_data
from .leader_environment_exit_ablation import _skipped_case
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .leader_panel_readiness import summarize_panels
from .shortline_panel_replay import _config
from .stockdb_action_accounting import load_action_accounting


METHOD = "original_fixed32_durable_case_replay_v1"
CASE_IDS = tuple(f"{kind}__{guard}__{cap}" for kind in TYPE_IDS for guard in GUARDS for cap, _ in CAPS)
PAYLOADS = ("decisions", "daily", "orders", "fills", "account")


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _pin(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": digest(path)}


def _checked(reference):
    path = Path(reference["path"]).resolve()
    if digest(path) != reference["sha256"]:
        raise ValueError("Pinned replay source or saved case changed: " + str(path))
    return path


def _runtime_pins():
    research = Path(__file__).resolve().parent
    names = ("leader_eight_type_historical_native.py", "leader_eight_type_panel_replay.py",
        "leader_historical_execution_policy.py", "stockdb_action_accounting.py", "leader_panel_readiness.py",
        "leader_environment_exit_ablation.py", "leader_environment_policy.py", "leader_type_risk_exit.py",
        "leader_daily_replay.py", "shortline_decision_v2.py", "shortline_decision_v3.py",
        "recent_alpha158_panel_replay.py", "shortline_panel_replay.py", "ipo_qualification.py")
    return [_pin(__file__), *[_pin(research / name) for name in names],
            *[_pin(research.parent / name) for name in
              ("engine.py", "ledger.py", "rules.py", "config.py", "contracts.py", "report.py", "worker.py")]]


def _pair(event_view, reference_view):
    event_view, reference_view = Path(event_view).resolve(), Path(reference_view).resolve()
    event, reference = _read(event_view / "result.json"), _read(reference_view / "result.json")
    if (event.get("status") != "research_action_events_projected"
            or reference.get("status") != "research_reference_presence_diagnostic"
            or event.get("execution_admitted") is not False or reference.get("execution_admitted") is not False
            or Path(reference["event_view"]).resolve() != event_view):
        raise ValueError("One exact unadmitted action/reference pair is required")
    return _pin(event_view / "result.json"), _pin(reference_view / "result.json")


def register(inputs, action_event_view, action_reference_view, out, *, check_entry_readiness=False):
    path, out = Path(inputs).resolve(), Path(out).resolve()
    path = path / "manifest.json" if path.is_dir() else path
    if out.exists():
        raise FileExistsError("Fixed32 registration requires a new directory")
    header = _read(path)
    if (type(check_entry_readiness) is not bool or header.get("schema") != INPUT_SCHEMA
            or header.get("method_id") != INPUT_METHOD or header.get("fixed_protocol") != PROTOCOL
            or any(header.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Unchanged historical input and original fixed32 protocol required")
    event, reference = _pair(action_event_view, action_reference_view)
    record = {"schema": METHOD + "_registration", "method_id": METHOD, "registered_at": _now(),
        "inputs": _pin(path), "action_event_view": event, "action_reference_view": reference,
        "config": _config(100000, 5).to_dict(), "protocol": deepcopy(PROTOCOL), "case_ids": list(CASE_IDS),
        "input_payload_pins": {key: header[key] for key in ("raw_file", "raw_sha256", "panels_file",
            "panels_sha256", "context_raw_file", "context_raw_sha256", "calendar", "calendar_sha256")},
        "check_entry_readiness": check_entry_readiness, "code": _runtime_pins(),
        "same_case_runner": "leader_eight_type_panel_replay._case", "session_checkpoint_supported": False,
        "retry_active_requires_explicit_new_attempt": True, "case_selection_by_return": False, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "registration.json", record)
    return record


def _registration(path):
    path = Path(path).resolve()
    record = _read(path)
    if (record.get("schema") != METHOD + "_registration" or record.get("method_id") != METHOD
            or record.get("protocol") != PROTOCOL or record.get("case_ids") != list(CASE_IDS)
            or record.get("config") != _config(100000, 5).to_dict()
            or type(record.get("check_entry_readiness")) is not bool
            or record.get("code") != _runtime_pins()
            or any(record.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Exact fixed32 registration, costs, code and protocol are required")
    for key in ("inputs", "action_event_view", "action_reference_view"):
        _checked(record[key])
    header = _read(record["inputs"]["path"])
    if any(header.get(key) != value for key, value in record["input_payload_pins"].items()):
        raise ValueError("Registered input payload identities changed")
    return record, _pin(path)


def _saved_case(entry, out, record):
    case_id = entry["case_id"]
    attempt = Path(entry["attempt_directory"]).resolve()
    if (case_id not in CASE_IDS or attempt.parent != out / case_id
            or not attempt.name.startswith("attempt-") or not attempt.name[8:].isdigit()
            or Path(entry["case_file"]["path"]).resolve() != attempt / "case.json"
            or set(entry["files"]) != set(PAYLOADS)):
        raise ValueError("Saved case must retain its original attempt and complete file set")
    case = _read(_checked(entry["case_file"]))
    if case.get("case_id") != case_id or case.get("config") != record["config"]:
        raise ValueError("Saved case identity or original cash/cost configuration changed")
    for key, reference in entry["files"].items():
        if Path(reference["path"]).resolve() != attempt / (key + ".json"):
            raise ValueError("Saved case payload path changed")
        case[key] = _read(_checked(reference))
    return case


def _save_case(case, attempt):
    for key in PAYLOADS:
        write_json(attempt / (key + ".json"), case[key])
    write_json(attempt / "case.json", {key: value for key, value in case.items() if key not in PAYLOADS})
    entry = {"case_id": case["case_id"], "attempt_directory": str(attempt),
        "case_file": _pin(attempt / "case.json"),
        "files": {key: _pin(attempt / (key + ".json")) for key in PAYLOADS}}
    write_json(attempt / "attempt.json", {"status": "completed", "ended_at": _now(), "saved_case": entry})
    return entry


def _prepare(record, registration_pin, out, progress):
    loaded, manifest = load_inputs(record["inputs"]["path"])
    members, sessions, previous, as_of = _validate_panel_data(loaded["panel_by_day"], loaded["raw"],
        loaded["source_days"], loaded["calendar_payload"], loaded["scope_members"], loaded["qualification"])
    paths, ranks = _rank_path(loaded["panel_by_day"], members, manifest["start"], manifest["end"])
    if progress.get("preparation"):
        prepared = _read(_checked(progress["preparation"]))
        if prepared.get("registration") != registration_pin:
            raise ValueError("Prepared environment belongs to a different registration")
    else:
        # This is the actual first preparation clock, not registered_at or a
        # historical publication clock. Resume reuses these saved observations.
        environments = _environment(loaded["panel_by_day"], sessions, as_of)
        readiness = summarize_panels(panel_by_day=loaded["panel_by_day"], source_days=loaded["source_days"],
            calendar_payload=loaded["calendar_payload"], scope_members=loaded["scope_members"],
            start=manifest["start"], end=manifest["end"], as_of=as_of,
            _rank_paths=paths, _environments=environments) if record["check_entry_readiness"] else None
        prepared = {"registration": registration_pin, "environment_as_of": as_of, "prepared_at": _now(),
            "historical_publication_claimed": False, "environment_decisions": environments,
            "entry_readiness": readiness}
        write_json(out / "preparation.json", prepared)
        progress["preparation"] = _pin(out / "preparation.json")
        write_json(out / "progress.json", progress)
    readiness = prepared["entry_readiness"]
    entries = {row["case_id"]: row for row in readiness["cases"]} if readiness else {}
    return loaded, manifest, members, sessions, previous, paths, ranks, prepared, entries


def _execution(record, loaded, manifest):
    context = pd.read_parquet(manifest["context_raw_file"])
    if (sorted(context.date.unique()) != [manifest["context_anchor"]["day"]]
            or context.date.max() >= loaded["source_days"][0] or context.duplicated(["date", "code"]).any()):
        raise ValueError("One exact preceding full native context is required")
    accounting = load_action_accounting(Path(record["action_event_view"]["path"]).parent,
        Path(record["action_reference_view"]["path"]).parent, symbols=loaded["scope_members"],
        raw_days=sorted(set(context.date) | set(loaded["source_days"])), replay_days=loaded["source_days"],
        price_source="independent_baostock_raw")
    return context, HistoricalExecutionFactory(accounting)


def _writer_lock_path(out):
    out = Path(out).resolve()
    return out.parent / ("." + out.name + ".fixed32-writer.lock")


def run(registration, out, *, resume=False, retry_active=False):
    """One OS-lock owner from the initial state read through every final write."""
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as owner:
        try:
            owner.enter_context(_process_lock(_writer_lock_path(out)))
        except OSError as error:
            raise RuntimeError("Fixed32 case writer busy; run/resume not started") from error
        return _run_locked(registration, out, resume=resume, retry_active=retry_active)


def _run_locked(registration, out, *, resume=False, retry_active=False):
    record, registration_pin = _registration(registration)
    out = Path(out).resolve()
    if retry_active and not resume:
        raise ValueError("Retrying an unfinished case requires explicit resume")
    if resume:
        progress = _read(out / "progress.json")
        if (progress.get("method_id") != METHOD or progress.get("registration") != registration_pin
                or progress.get("run_directory") != str(out) or progress.get("case_ids") != list(CASE_IDS)):
            raise ValueError("Resume requires the exact original registration and run directory")
        completed = progress["completed_cases"]
        if [row["case_id"] for row in completed] != list(CASE_IDS[:len(completed)]):
            raise ValueError("Completed cases must retain the original contiguous fixed32 order")
        cases = [_saved_case(entry, out, record) for entry in completed]
        active = progress.get("active_case")
        if active:
            attempt_path = Path(active["attempt_directory"]).resolve()
            if (len(cases) >= len(CASE_IDS) or active["case_id"] != CASE_IDS[len(cases)]
                    or attempt_path.parent != out / active["case_id"]
                    or not attempt_path.name.startswith("attempt-") or not attempt_path.name[8:].isdigit()):
                raise ValueError("Unfinished case is not the exact next registered case")
            attempt_record = _read(attempt_path / "attempt.json")
            # Recover the narrow atomic-write gap after all case files were
            # committed but before progress.json was replaced; never rerun it.
            if attempt_record["status"] == "completed":
                entry = attempt_record["saved_case"]
                if entry["case_id"] != active["case_id"] or Path(entry["attempt_directory"]).resolve() != attempt_path:
                    raise ValueError("Completed active attempt differs from its registered next case")
                cases.append(_saved_case(entry, out, record))
                completed.append(entry)
                progress["active_case"] = None
            elif not retry_active:
                raise ValueError("Unfinished case requires explicit --retry-active; previous attempt is retained")
        if progress.get("status") == "completed":
            return _read(_checked(progress["result"]))
    else:
        if out.exists():
            raise FileExistsError("Fresh fixed32 replay requires a new directory; use explicit resume")
        out.mkdir(parents=True, exist_ok=False)
        cases = []
        progress = {"schema": METHOD + "_progress", "method_id": METHOD, "registration": registration_pin,
            "run_directory": str(out), "started_at": _now(), "case_ids": list(CASE_IDS),
            "status": "running", "completed_cases": [], "active_case": None, "errors": []}
        write_json(out / "progress.json", progress)
    attempt = None
    try:
        loaded, manifest, members, sessions, previous, paths, ranks, prepared, readiness = _prepare(
            record, registration_pin, out, progress)
        context, factory = None, None
        for case_id in CASE_IDS[len(cases):]:
            identity, guard, cap = case_id.split("__")
            case_folder = out / case_id
            case_folder.mkdir(exist_ok=True)
            numbers = [int(path.name.removeprefix("attempt-")) for path in case_folder.glob("attempt-*")]
            attempt = case_folder / f"attempt-{max(numbers, default=0) + 1:04d}"
            attempt.mkdir()
            write_json(attempt / "attempt.json", {"status": "running", "case_id": case_id, "started_at": _now()})
            progress.update(status="running", active_case={"case_id": case_id, "attempt_directory": str(attempt)})
            write_json(out / "progress.json", progress)
            entry = readiness.get(case_id)
            if entry is not None and entry["potential_entry_days"] == 0:
                case = _skipped_case(identity, guard, cap, entry, record["config"])
            else:
                if factory is None:
                    context, factory = _execution(record, loaded, manifest)
                case = _case(loaded["raw"], loaded["source_days"], sessions, previous, loaded["panel_by_day"],
                    paths[identity][cap], prepared["environment_decisions"], members, loaded["qualification"],
                    loaded["context_anchor"], manifest["start"], manifest["end"], identity, cap, guard,
                    f"leader-eight-type-case:{registration_pin['sha256']}:{case_id}",
                    execution_policy_factory=factory, context_raw=context)
            saved = _save_case(case, attempt)
            next_progress = deepcopy(progress)
            next_progress["completed_cases"].append(saved)
            next_progress.update(active_case=None, updated_at=_now())
            write_json(out / "progress.json", next_progress)
            progress = next_progress
            cases.append(case)
            attempt = None
            print(json.dumps({"event": "case_saved", "case_id": case_id, "saved_cases": len(cases),
                              "status": case["status"]}), flush=True)
        result = {"schema": METHOD + "_result", "method_id": METHOD, "created_at": _now(),
            "registration": registration_pin, "protocol": deepcopy(PROTOCOL), "config": record["config"],
            "start": manifest["start"], "end": manifest["end"], "source_days": loaded["source_days"],
            "scope_members": sorted(members), "source": loaded.get("provenance", {}), "case_count": len(cases),
            "cases": cases, "environment_decisions": prepared["environment_decisions"],
            "preparation": progress["preparation"], "raw_top5_paths": ranks, "capacity_paths": paths,
            "all_cases_complete_PnL": all(case["complete_PnL_available"] for case in cases),
            "skipped_case_count": sum(case["status"] == "skipped_no_possible_entry_from_saved_panel" for case in cases),
            "single_case_session_checkpoint_supported": False, "old_results_modified": False, **FLAGS}
        write_json(out / "result.json", result)
        progress.update(status="completed", active_case=None, ended_at=_now(), result=_pin(out / "result.json"))
        write_json(out / "progress.json", progress)
        return result
    except BaseException as error:
        failure = {"failed_at": _now(), "error": f"{type(error).__name__}: {error}",
                   "active_case": deepcopy(progress.get("active_case")), "session_checkpoint_available": False}
        # A fully saved case survives a later progress replacement failure.
        # The prior active pointer lets explicit resume adopt it without replay.
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
    for name in ("inputs", "action-event-view", "action-reference-view", "out"):
        create.add_argument("--" + name, type=Path, required=True)
    create.add_argument("--check-entry-readiness", action="store_true")
    for name in ("run", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--registration", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "resume":
            command.add_argument("--retry-active", action="store_true",
                help="Explicitly retry an unfinished case in a new attempt after its prior process has ended")
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register(args.inputs, args.action_event_view, args.action_reference_view, args.out,
                          check_entry_readiness=args.check_entry_readiness)
        summary = {"registration": str(args.out.resolve() / "registration.json"), "case_count": len(CASE_IDS)}
    else:
        result = run(args.registration, args.out, resume=args.command == "resume",
                     retry_active=getattr(args, "retry_active", False))
        summary = {"result": str(args.out.resolve() / "result.json"), "case_count": result["case_count"],
                   "all_cases_complete_PnL": result["all_cases_complete_PnL"]}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
