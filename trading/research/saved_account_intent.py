"""Read an exact saved fixed-strategy intent without promoting it to a signal.

These are research account instructions. Their weights and unfinished exits may
be compared with a separately observed account, but never acquire permission to
prefill or trade merely by being serialized here.
"""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..target_exit_lifecycle import FULL_EXIT_LIFECYCLE, validate_binding
from .stockdb_rolling_low_volatility import CALENDAR_MONTH_END_METHOD, SCHEMA as RUN_SCHEMA

SCHEMA = "saved_fixed_account_intent_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
         "execution_admitted": False, "selection_enabled": False,
         "strategy_validated": False, "prefill_allowed": False}


def _now():
    return datetime.now(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _stamp(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("Saved intent requires explicit knowledge timestamps")
    return stamp


def _ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _facts(source_run, signal, execution_day):
    path = Path(source_run).resolve()
    if path.is_dir():
        path /= "run.json"
    source = _read(path)
    artifacts = Path(source["artifacts"]).resolve()
    protocol_path = artifacts / "protocol.json"
    account_path = artifacts / signal / "account.json"
    replay_path = artifacts / signal / "replay.json"
    protocol, account, receipt = map(_read, (protocol_path, account_path, replay_path))
    if (source.get("schema") != RUN_SCHEMA or source.get("status") != "completed_research_only"
            or source.get("research_only") is not True or source.get("execution_admitted") is not False
            or source.get("strategy_validated") is not False or source.get("variants") != [signal]
            or source.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE
            or protocol.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE
            or receipt.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE
            or receipt.get("status") != "completed" or account.get("broker_binding")):
        raise ValueError("Saved intent requires its exact unadmitted fixed-strategy method")
    config = StrategyConfig(**source["config"]).validate()
    day = source["replay_end"]
    if (account.get("day") != day or receipt.get("last_completed_session") != day
            or receipt.get("account_basis") != "last_successfully_completed_session"
            or receipt.get("config") != config.to_dict() or protocol.get("config") != config.to_dict()
            or protocol.get("replay_end") != day or receipt.get("completed_sessions") != source["sessions"]):
        raise ValueError("Saved intent source, account and completion receipt disagree")
    cases = source.get("results") or []
    if (len(cases) != 1 or cases[0].get("signal") != signal or cases[0].get("status") != "completed"
            or cases[0].get("completed_sessions") != source["sessions"]):
        raise ValueError("Saved intent needs one completed strategy account")
    schedule = source.get("month_end_schedule") or {}
    if (schedule.get("method_id") != CALENDAR_MONTH_END_METHOD
            or protocol.get("month_end_schedule") != schedule):
        raise ValueError("Saved intent requires the same fixed exchange calendar")
    calendars = schedule.get("calendar_files") or []
    if not calendars or any(_ref(item["path"])["sha256"] != item["sha256"] for item in calendars):
        raise ValueError("Saved intent fixed calendar bytes changed")
    sessions = source.get("calendar_verification", {}).get("fixed_open_sessions", [])
    if day not in sessions or next((d for d in sessions if d > day), None) != execution_day:
        raise ValueError("Saved intent requires the exact next fixed-calendar execution day")
    source_known = _stamp(source["completed_at"])
    if (source_known < _stamp(day + "T15:00:00+08:00")
            or source_known >= _stamp(execution_day + "T09:30:00+08:00")):
        raise ValueError("Source was unavailable before its intended opening; no backdating")
    lifecycle = validate_binding(account, FULL_EXIT_LIFECYCLE,
        RunContext("saved_intent_readonly", TradingMode.REPLAY))
    if lifecycle is None:
        raise ValueError("Saved intent account lacks its execution lifecycle")
    pending = account.get("pending_targets")
    if pending is not None and (not isinstance(pending, dict) or any(
            type(weight) not in (int, float) or not 0 <= weight <= config.single_limit
            for weight in pending.values()) or sum(pending.values()) > config.gross_limit + 1e-12):
        raise ValueError("Invalid saved full-target weights or exposure")
    exits = sorted(symbol for symbol in lifecycle["remaining_full_exits"]
                   if sum(lot["qty"] for lot in account["positions"].get(symbol, [])) > 0)
    return {"schema": SCHEMA, "strategy_id": signal, "source_day": day,
        "execution_day": execution_day, "kind": "FULL_TARGET" if pending is not None else "NO_CHANGE",
        "weights": pending, "remaining_exit_symbols": exits,
        "target_version": lifecycle["target_version"], "source_known_at": source_known.isoformat(),
        "account_scope": "whole_observed_account", "execution_config": config.to_dict(),
        "refs": {"source_run": _ref(path), "account": _ref(account_path),
                 "protocol": _ref(protocol_path), "replay": _ref(replay_path),
                 "calendar_files": calendars}, **FLAGS}


def export_request(source_run, signal, execution_day, *, current=None):
    result = _facts(source_run, signal, execution_day)
    current = current or _now()
    if _stamp(result["source_known_at"]) > current:
        raise ValueError("Saved source completion is in the future")
    return {**result, "created_at": current.isoformat(),
            "signal_publication_verified": False,
            "source_basis": "saved_research_account_not_a_current_broker_signal"}


def read_request(payload, *, current=None):
    if isinstance(payload, dict) and payload.get("schema") == "saved_registered_research_account_intent_v1":
        from .registered_research_saved_opening import read_request as read_registered_request
        return read_registered_request(payload, current=current)
    if isinstance(payload, dict) and payload.get("schema") == "saved_native_preopen_account_intent_v1":
        from .native_saved_opening import read_request as read_native_request
        return read_native_request(payload, current=current)
    if isinstance(payload, dict) and payload.get("schema") in {
            "saved_leader_published_account_intent_v1", "saved_alpha158_account_intent_v1"}:
        from .saved_published_account_intent import read_request as read_published_request
        return read_published_request(payload, current=current)
    if isinstance(payload, dict) and payload.get("schema") in {
            "saved_leader_eight_type_account_intent_v1", "saved_leader_dated_catchup_account_intent_v1",
            "saved_leader_eight_type_residual_account_intent_v1", "saved_leader_known_identity_account_intent_v1"}:
        from .saved_leader_account_intent import read_request as read_leader_request
        return read_leader_request(payload, current=current)
    current = current or _now()
    if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
            or any(payload.get(key) != expected for key, expected in FLAGS.items())
            or payload.get("signal_publication_verified") is not False
            or payload.get("source_basis") != "saved_research_account_not_a_current_broker_signal"):
        raise ValueError("Saved research intent boundary changed")
    created = _stamp(payload["created_at"])
    if not _stamp(payload["source_known_at"]) <= created <= current:
        raise ValueError("Saved intent creation or source knowledge clock changed")
    expected = _facts(payload["refs"]["source_run"]["path"], payload["strategy_id"], payload["execution_day"])
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("Saved intent no longer equals the exact source weights, exits or evidence")
    return {**expected, "created_at": payload["created_at"],
            "signal_publication_verified": False, "source_basis": payload["source_basis"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-run", type=Path)
    source.add_argument("--workflow", type=Path)
    parser.add_argument("--signal", required=True)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    path = args.source_run
    if args.workflow:
        from .stockdb_fixed_daily import status
        saved = status(args.workflow)
        if saved["signal"] != args.signal:
            raise ValueError("Saved workflow strategy differs from the requested intent")
        path = saved["source_run"]
    request = export_request(path, args.signal, args.execution_day)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(request, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(str(args.out.resolve()), flush=True)
    return 0
