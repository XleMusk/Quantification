"""Persistent research-paper continuation of one explicit rolling checkpoint.

This is a saved pointer and input check around the existing replay runner, not
a second account, worker or broker path. A failed attempt may leave uncommitted
research artifacts; only the atomically replaced head is current authority.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import uuid

import pandas as pd
import pyarrow.parquet as pq

from ..config import StrategyConfig
from ..ledger import Ledger
from ..settings import DATA_ROOT, ROOT
from ..target_exit_lifecycle import FULL_EXIT_LIFECYCLE
from ..worker import lock
from . import stockdb_rolling_low_volatility as rolling
from .baostock_full_close import load_complete
from .exchange_calendar import _closed_days
from .experiment import load_replay_checkpoint
from .leader_recent_source_history import source_proof
from .lowvol_supplement_append import SCHEMA as APPEND_SCHEMA

SCHEMA = "stockdb_fixed_persistent_daily_research_v1"
METHOD = "explicit_rolling_checkpoint_calendar_full_exit_daily_v1"
CN = timezone(timedelta(hours=8))
FLAGS = {"candidate_panel_only": True, "research_only": True,
         "execution_admitted": False, "selection_enabled": False,
         "strategy_validated": False, "source_verified": False,
         "pit_verified": False, "corporate_actions_verified": False,
         "broker_connected": False, "personal_account_claimed": False}


def _now():
    return datetime.now(CN)


def _stamp(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("Source knowledge clocks require an explicit timezone")
    return result


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _digest(path):
    value = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": _digest(path)}


def _checked(reference):
    path = Path(reference["path"]).resolve()
    if _digest(path) != reference["sha256"]:
        raise ValueError(f"Previously saved input changed: {path}")
    return path


def _destination(path):
    path = Path(path).resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError("Persistent research state belongs outside the repository")
    if path == DATA_ROOT or DATA_ROOT not in path.parents:
        raise ValueError("Use a dedicated directory under QUANT_TRADING_HOME")
    return path


def _run_path(path):
    path = Path(path).resolve()
    return path / "run.json" if path.is_dir() else path


def _saved_source(path, signal):
    """Load the already saved account with the existing checkpoint validator."""
    path = _run_path(path)
    record = _read(path)
    artifacts = Path(record["artifacts"]).resolve()
    protocol_path = artifacts / "protocol.json"
    protocol = _read(protocol_path)
    schedule = record.get("month_end_schedule")
    if (record.get("schema") != rolling.SCHEMA
            or record.get("status") != "completed_research_only"
            or record.get("variants") != [signal]
            or record.get("execution_admitted") is not False
            or record.get("research_only") is not True
            or record.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE
            or not isinstance(schedule, dict)
            or schedule.get("method_id") != rolling.CALENDAR_MONTH_END_METHOD
            or protocol.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE
            or protocol.get("month_end_schedule") != schedule
            or any(protocol.get(key) != record.get(key) for key in
                   ("config", "variants", "replay_start", "replay_end"))):
        raise ValueError("Use one completed, same-method calendar/full-exit rolling study")
    results = record.get("results", [])
    if len(results) != 1 or results[0].get("signal") != signal or results[0].get("status") != "completed":
        raise ValueError("One completed signal account is required")
    folder = artifacts / signal
    dates = pd.read_csv(folder / "daily.csv", usecols=["date"], dtype=str)["date"].tolist()
    config = StrategyConfig(**record["config"]).validate()
    checkpoint = load_replay_checkpoint(folder, config, dates,
                                        full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
    receipt = _read(folder / "replay.json")
    if (not dates or checkpoint["state"]["day"] != record["replay_end"]
            or receipt.get("status") != "completed"
            or results[0].get("completed_sessions") != len(dates)
            or record.get("sessions") != len(dates)):
        raise ValueError("Saved run, account and replay completion disagree")
    references = {name: _reference(value) for name, value in {
        "run": path, "protocol": protocol_path, "account": folder / "account.json",
        "replay": folder / "replay.json", "daily": folder / "daily.csv"}.items()}
    return record, protocol, checkpoint, references


def _calendar(schedule):
    files = schedule.get("calendar_files", [])
    if not files:
        raise ValueError("An explicit fixed exchange calendar is required")
    sessions, years = set(), set()
    for item in files:
        payload = _read(_checked(item))
        year = payload.get("year")
        if (payload.get("schema") != "cn_a_share_exchange_calendar_v1"
                or type(year) is not int or year != item.get("year") or year in years
                or not isinstance(payload.get("closed_ranges"), list)
                or not isinstance(payload.get("open_sessions"), list)):
            raise ValueError("Invalid fixed exchange calendar")
        years.add(year)
        closed = _closed_days(payload)
        overrides = {date.fromisoformat(day) for day in payload["open_sessions"]}
        if any(day.year != year for day in closed | overrides):
            raise ValueError("Fixed calendar dates must remain in their declared year")
        left, right = date(year, 1, 1), date(year, 12, 31)
        for offset in range((right - left).days + 1):
            day = left + timedelta(days=offset)
            if day in overrides or day.weekday() < 5 and day not in closed:
                sessions.add(day.isoformat())
    return sorted(sessions)


def _snapshot_days(manifest, snapshot):
    """Observed dates, never infer missing sessions between chunk endpoints."""
    days = set()
    def iso(value):
        value = str(value)
        text = (value[:4] + "-" + value[4:6] + "-" + value[6:]
                if len(value) == 8 and value.isdigit() else value)
        return date.fromisoformat(text).isoformat()
    for chunk in manifest["chunks"]:
        first, last = chunk.get("first_date"), chunk.get("last_date")
        if first is not None and first == last:
            days.add(iso(first))
        else:
            # Only distinct dates are read once at registration, not OHLCV or
            # repeated historical validation on each daily call.
            values = pq.read_table(Path(snapshot) / chunk["parquet_file"], columns=["date"])
            days.update(iso(value) for value in values.column("date").unique().to_pylist())
    return sorted(days)


def _raw_path(value):
    if not value:
        return None
    path = Path(value).resolve()
    return path / "raw_daily.parquet" if path.is_dir() else path


def register(source_run, signal, run_dir):
    run_dir = _destination(run_dir)
    if run_dir.exists():
        raise FileExistsError("Use a new explicit daily research directory")
    record, protocol, checkpoint, references = _saved_source(source_run, signal)
    sessions = _calendar(record["month_end_schedule"])
    snapshot = Path(protocol["snapshot"]).resolve()
    snapshot_ref = _reference(snapshot / "manifest.json")
    manifest = _read(snapshot_ref["path"])
    if manifest != protocol["snapshot_manifest"]:
        raise ValueError("Original snapshot manifest changed")
    raw = _raw_path((protocol.get("supplemental_raw_view") or {}).get("path"))
    raw_ref = _reference(raw) if raw else None
    registered_at = _now().isoformat()
    registration = {"schema": SCHEMA, "method_id": METHOD, "registered_at": registered_at,
        "signal": signal, "mode": "replay", "seed": references,
        "seed_last_completed_day": checkpoint["state"]["day"],
        "snapshot": str(snapshot), "snapshot_manifest": snapshot_ref,
        "original_snapshot_inventory_days": _snapshot_days(manifest, snapshot),
        "initial_supplement": raw_ref, "config": record["config"],
        "replay_start": record["replay_start"], "month_end_schedule": record["month_end_schedule"],
        "full_exit_lifecycle": FULL_EXIT_LIFECYCLE,
        "account_basis": "explicit_saved_research_checkpoint_not_personal_holdings",
        "seed_history_is_prospective": False,
        "signal_publication_basis": "historical_eod_assumption_not_verified_actual_publication",
        "cash_and_positions_reset": False, **FLAGS}
    if record["replay_end"] not in sessions:
        raise ValueError("Seed account day is outside its fixed calendar")
    run_dir.mkdir(parents=True, exist_ok=False)
    with lock(run_dir / "daily.lock"):
        _atomic(run_dir / "registration.json", registration)
        head = {"schema": SCHEMA, "method_id": METHOD,
            "registration": _reference(run_dir / "registration.json"), "generation": 0,
            "last_completed_day": record["replay_end"], "source": references,
            "supplement": raw_ref, "committed_at": registered_at,
            "account_created_by_registration": False, **FLAGS}
        _atomic(run_dir / "head.json", head)
    return status(run_dir)


def _current(run_dir):
    run_dir = _destination(run_dir)
    head = _read(run_dir / "head.json")
    registration = _read(_checked(head["registration"]))
    if (head.get("schema") != SCHEMA or registration.get("schema") != SCHEMA
            or head.get("method_id") != METHOD or registration.get("method_id") != METHOD
            or Path(head["registration"]["path"]).resolve() != run_dir / "registration.json"
            or any(value.get(key) != expected for value in (head, registration)
                   for key, expected in FLAGS.items())):
        raise ValueError("Saved daily research method or boundary changed")
    for ref in head["source"].values():
        _checked(ref)
    record, protocol, checkpoint, refs = _saved_source(head["source"]["run"]["path"], registration["signal"])
    if (refs != head["source"] or record["replay_end"] != head["last_completed_day"]
            or any(record.get(key) != registration.get(key) for key in
                   ("config", "replay_start", "month_end_schedule", "full_exit_lifecycle"))):
        raise ValueError("Current checkpoint no longer matches its registered account")
    return run_dir, head, registration, record, protocol, checkpoint


def status(run_dir):
    """Pure saved-file read: no sources, strategy runner, worker or orders."""
    run_dir, head, registration, record, protocol, checkpoint = _current(run_dir)
    state = checkpoint["state"]
    ledger = Ledger(state, StrategyConfig(**registration["config"]))
    positions = {symbol: ledger.quantity(symbol) for symbol in state["positions"]
                 if ledger.quantity(symbol) > 0}
    return {"schema": SCHEMA, "method_id": METHOD, "status": "saved_research_account",
        "run_dir": str(run_dir), "signal": registration["signal"],
        "registered_at": registration["registered_at"], "mode": "replay",
        "last_completed_day": head["last_completed_day"], "generation": head["generation"],
        "source_run": head["source"]["run"]["path"],
        "terminal_account": head["source"]["account"]["path"],
        "committed_at": head["committed_at"],
        "cash": state["cash"] / 100, "equity": ledger.equity() / 100,
        "money_unit": "CNY", "positions": positions,
        "targets": state.get("targets"), "pending_targets": state.get("pending_targets"),
        "full_exit_lifecycle": state.get("full_exit_lifecycle"),
        "orders": state["orders"], "fills": state["fills"],
        "order_count": len(state["orders"]), "fill_count": len(state["fills"]),
        "metrics": record["results"][0],
        "account_basis": registration["account_basis"],
        "signal_publication_basis": registration["signal_publication_basis"], **FLAGS}


def _supplement_lineage(path, digest):
    """Verify existing append receipts and collect their original daily proofs."""
    proofs, nodes, visited = {}, [], set()
    while path is not None:
        path = Path(path).resolve()
        if path in visited:
            raise ValueError("Supplement append lineage contains a cycle")
        visited.add(path)
        result_path = path.parent / "result.json"
        if not result_path.is_file():
            break
        result = _read(result_path)
        if result.get("schema") != APPEND_SCHEMA:
            break
        if (result.get("status") != "completed_append_only_research_supplement"
                or Path(result["raw_daily_file"]).resolve() != path
                or digest(path) != result["raw_daily_sha256"]
                or result.get("original_full_row_prefix_preserved") is not True
                or result.get("original_strings_and_nulls_preserved") is not True
                or result.get("current_source_overwrites_original_keys") is not False
                or any(result.get(key) is not False for key in
                       ("execution_admitted", "selection_enabled", "strategy_validated", "stockdb_modified", "ledger_mutated"))
                or _stamp(result["created_at"]) > _now()):
            raise ValueError("Supplement append receipt or original raw changed")
        base = Path(result["base_file"]).resolve()
        if digest(base) != result["base_sha256"]:
            raise ValueError("Supplement original parent raw changed")
        nodes.append({"path": str(path), "sha256": result["raw_daily_sha256"],
                      "result": _reference(result_path), "base": str(base),
                      "base_sha256": result["base_sha256"]})
        for proof in result["source_proofs"]:
            day = proof["day"]
            if day in proofs and proofs[day] != proof:
                raise ValueError("Supplement lineage has conflicting daily proofs")
            proofs[day] = proof
        path = base
    return proofs, nodes


def _fingerprint(head, through, packages, supplement):
    # Pin input bytes rather than modification times. This identity is also
    # written before invoking replay, so a crash cannot silently retry it.
    package_refs = []
    for package in packages:
        folder = Path(package).resolve()
        package_refs.append({"folder": str(folder), "files": {
            name: _reference(folder / name) if (folder / name).is_file() else None
            for name in ("plan.json", "receipt.json", "raw_daily.csv")}})
    body = {"through": through, "source": head["source"], "packages": package_refs,
            "supplement": _reference(supplement) if supplement else None}
    identity = sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return identity, body


def _commit_result(run_dir, head, registration, checkpoint, attempt, attempt_path):
    """Publish an existing successful result, also after an interrupted publish."""
    through = attempt["inputs"]["through"]
    out = Path(attempt["output"])
    new_record, new_protocol, new_checkpoint, references = _saved_source(
        out / "run.json", registration["signal"])
    expected = [day for day in _calendar(registration["month_end_schedule"])
                if head["last_completed_day"] < day <= through]
    if (new_record != attempt["result"] or new_record["replay_end"] != through
            or new_record["config"] != registration["config"]
            or new_record["replay_start"] != registration["replay_start"]
            or new_record["month_end_schedule"] != registration["month_end_schedule"]
            or Path(new_protocol["snapshot"]).resolve() != Path(registration["snapshot"])
            or new_record.get("resume_run") != head["source"]["run"]["path"]
            or len(new_checkpoint["daily"]) - len(checkpoint["daily"]) != len(expected)):
        raise ValueError("Replay output does not complete exactly the requested continuation")
    for collection in ("orders", "fills"):
        if any(new_checkpoint["state"][collection].get(key) != value
               for key, value in checkpoint["state"][collection].items()):
            raise ValueError("Previous order or fill prefix changed")
    for ref in head["source"].values():
        _checked(ref)
    for package in attempt["inputs"]["packages"]:
        for ref in package["files"].values():
            if ref is not None:
                _checked(ref)
    _checked(registration["snapshot_manifest"])
    supplement = attempt["inputs"]["supplement"]
    if supplement:
        _checked(supplement)
        if _raw_path((new_protocol.get("supplemental_raw_view") or {}).get("path")) != Path(supplement["path"]):
            raise ValueError("Replay output changed its requested supplement")
    if _read(run_dir / "head.json") != head:
        raise ValueError("Current head changed before result publication")
    new_head = {**head, "generation": head["generation"] + 1,
        "last_completed_day": through, "source": references,
        "supplement": supplement, "committed_at": _now().isoformat(),
        "last_attempt": str(attempt_path), "last_source_inputs": attempt["source_inputs"]}
    _atomic(run_dir / "head.json", new_head)
    attempt.update(status="committed", completed_at=_now().isoformat(),
                   execution_progress_unknown=False, head_committed=True)
    _atomic(attempt_path, attempt)
    return {**status(run_dir), "status": "continued_research_only", "through": through,
            "newly_completed_sessions": len(expected), "attempt": str(attempt_path)}


def recover(run_dir, input_id):
    """Recover publication of an already completed replay; never repeat fills."""
    if len(input_id) != 64 or any(char not in "0123456789abcdef" for char in input_id):
        raise ValueError("Select the exact saved attempt input_id")
    run_dir = _destination(run_dir)
    with lock(run_dir / "daily.lock"):
        run_dir, head, registration, previous, protocol, checkpoint = _current(run_dir)
        attempt_path = run_dir / "attempts" / (input_id + ".json")
        attempt = _read(attempt_path)
        if (attempt.get("schema") != SCHEMA or attempt.get("method_id") != METHOD
                or attempt.get("input_id") != input_id):
            raise ValueError("Saved attempt identity changed")
        if head.get("last_attempt") == str(attempt_path):
            return {**status(run_dir), "status": "already_committed", "attempt": str(attempt_path)}
        if attempt.get("head_before") != head:
            raise ValueError("Saved attempt belongs to a different committed head")
        if not attempt.get("output") or attempt.get("result", {}).get("status") != "completed_research_only":
            return {**status(run_dir), "status": "unresolved_attempt_no_successful_result",
                "attempt": str(attempt_path), "execution_progress_unknown": True,
                "error": attempt.get("error")}
        result = _commit_result(run_dir, head, registration, checkpoint, attempt, attempt_path)
        return {**result, "status": "recovered_completed_result", "replay_repeated": False}


def run(run_dir, through, *, packages=(), supplemental_raw_view=None):
    through = date.fromisoformat(through).isoformat()
    run_dir = _destination(run_dir)
    if not (run_dir / "head.json").is_file():
        raise ValueError("Register an explicit completed seed first")
    with lock(run_dir / "daily.lock"):
        run_dir, head, registration, previous, protocol, checkpoint = _current(run_dir)
        if through <= head["last_completed_day"]:
            return {**status(run_dir), "status": "no_new_session", "through": through,
                    "newly_completed_sessions": 0}
        sessions = _calendar(registration["month_end_schedule"])
        if through not in sessions or through > _now().date().isoformat():
            raise ValueError("Through must be an existing, nonfuture fixed-calendar session")
        expected = [day for day in sessions if head["last_completed_day"] < day <= through]
        supplied = [Path(item).resolve() for item in packages]
        if len(supplied) != len(set(supplied)):
            raise ValueError("Complete packages must be supplied once each")
        package_days = {}
        missing = []
        for folder in supplied:
            if not (folder / "plan.json").is_file() or not (folder / "receipt.json").is_file():
                continue
            plan, receipt = _read(folder / "plan.json"), _read(folder / "receipt.json")
            day = plan.get("day")
            if day not in expected or day in package_days:
                raise ValueError("Packages must name unique exact newly processed sessions")
            package_days[day] = folder
        missing = [day for day in expected if day not in package_days]
        if missing:
            return {**status(run_dir), "status": "waiting_for_complete_sources",
                    "through": through, "missing_days": missing, "newly_completed_sessions": 0}
        supplement = (_raw_path(supplemental_raw_view) if supplemental_raw_view is not None
                      else _raw_path((protocol.get("supplemental_raw_view") or {}).get("path")))
        identity, inputs = _fingerprint(head, through, supplied, supplement)
        attempt_path = run_dir / "attempts" / (identity + ".json")
        if attempt_path.exists():
            prior = _read(attempt_path)
            raise ValueError("The same failed or interrupted input is preserved; automatic retry is disabled: "
                             + str(prior.get("status")))
        attempt = {"schema": SCHEMA, "method_id": METHOD, "input_id": identity,
            "started_at": _now().isoformat(), "status": "checking_inputs", "inputs": inputs,
            "head_before": head, "execution_progress_unknown": False, **FLAGS}
        attempt_path.parent.mkdir(exist_ok=True)
        _atomic(attempt_path, attempt)
        try:
            _checked(registration["snapshot_manifest"])
            cache = {}
            def digest(path):
                path = str(Path(path).resolve())
                if path not in cache:
                    cache[path] = _digest(path)
                return cache[path]
            proofs, lineage = _supplement_lineage(supplement, digest)
            old_raw = head.get("supplement")
            if old_raw:
                _checked(old_raw)
                if supplement != Path(old_raw["path"]).resolve():
                    if not any(node["base"] == old_raw["path"] and
                               node["base_sha256"] == old_raw["sha256"] for node in lineage):
                        raise ValueError("New supplement must append the exact saved parent raw")
            elif supplement is not None and not lineage:
                raise ValueError("A new supplement requires actual append-only source receipts")
            calendar_hashes = {item["sha256"] for item in registration["month_end_schedule"]["calendar_files"]}
            source_inputs = []
            for day in expected:
                raw, source = load_complete(package_days[day])
                plan, receipt = source["plan"], source["receipt"]
                if (plan.get("day") != day or plan.get("calendar_sha256") not in calendar_hashes
                        or receipt.get("day") != day):
                    raise ValueError("Complete source day or fixed calendar changed")
                available = _stamp(receipt.get("completed_at") or receipt["updated_at"])
                if available > _now() or available < _stamp(day + "T15:00:00+08:00"):
                    raise ValueError("Complete source clock cannot be future or before its close")
                proof = source_proof(raw, source, plan["calendar_sha256"])
                if day in proofs:
                    if any(proofs[day].get(key) != value for key, value in proof.items()):
                        raise ValueError("Package does not match the raw supplement actually used")
                    basis = "exact_complete_package_append_source_proof"
                elif day in registration["original_snapshot_inventory_days"]:
                    basis = "original_pinned_snapshot_priority_independent_complete_package"
                else:
                    raise ValueError("Complete coverage package is not attached to the actual raw source")
                source_inputs.append({"day": day, "proof": proof, "basis": basis,
                    "plan": _reference(package_days[day] / "plan.json"),
                    "receipt": _reference(package_days[day] / "receipt.json"),
                    "observed_at": _now().isoformat()})
            attempt.update(status="running_replay", source_inputs=source_inputs,
                           supplement_lineage=lineage, execution_progress_unknown=True)
            _atomic(attempt_path, attempt)
            out, result = rolling.run(registration["snapshot"], registration["replay_start"], through,
                initial_cash=registration["config"]["initial_cash"], variants=(registration["signal"],),
                supplemental_raw_view=supplement, resume_run=head["source"]["run"]["path"],
                calendars=[item["path"] for item in registration["month_end_schedule"]["calendar_files"]],
                calendar_driven_month_ends=True, persistent_full_exits=True)
            attempt.update(output=str(out), result=result)
            _atomic(attempt_path, attempt)
            return _commit_result(run_dir, head, registration, checkpoint, attempt, attempt_path)
        except Exception as error:
            committed = _read(run_dir / "head.json").get("last_attempt") == str(attempt_path)
            attempt.update(status="failed_after_head_commit" if committed else "failed_uncommitted",
                           head_committed=committed, failed_at=_now().isoformat(),
                           error=f"{type(error).__name__}: {error}",
                           orphaned_research_outputs_possible=attempt["execution_progress_unknown"])
            _atomic(attempt_path, attempt)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("register")
    create.add_argument("--source-run", type=Path, required=True)
    create.add_argument("--signal", choices=rolling.VARIANTS, required=True)
    create.add_argument("--run-dir", type=Path, required=True)
    daily = sub.add_parser("run")
    daily.add_argument("--run-dir", type=Path, required=True)
    daily.add_argument("--through", required=True)
    daily.add_argument("--package", action="append", type=Path, default=[])
    daily.add_argument("--supplemental-raw-view", type=Path)
    read = sub.add_parser("status")
    read.add_argument("--run-dir", type=Path, required=True)
    restore = sub.add_parser("recover")
    restore.add_argument("--run-dir", type=Path, required=True)
    restore.add_argument("--input-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register(args.source_run, args.signal, args.run_dir)
    elif args.command == "status":
        result = status(args.run_dir)
    elif args.command == "recover":
        result = recover(args.run_dir, args.input_id)
    else:
        result = run(args.run_dir, args.through, packages=args.package,
                     supplemental_raw_view=args.supplemental_raw_view)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
