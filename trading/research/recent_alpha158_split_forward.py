"""Explicit split settlement for two Alpha158 paper Ledgers.

This route is deliberately separate from ``recent_alpha158_forward_run``.
The original consumer requires a real close publication for every settled day;
this consumer first settles the known raw day, then attaches a genuine close
publication later on the same settled day without replaying orders or fills.
It is research-only and keeps the original Alpha158 books untouched.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

from ..contracts import PortfolioIntent, RunContext, TradingMode
from ..engine import advance
from ..ledger import Ledger, new_state
from ..market_dataset import write_json
from ..published_close_session import METHOD_ID as SESSION_METHOD, attach_close
from ..worker import lock
from . import recent_alpha158_forward_run as original
from .baostock_daily_slice import digest


SCHEMA = "recent_alpha158_split_forward_checkpoint_v1"
RESULT_SCHEMA = "recent_alpha158_split_forward_result_v1"
GENERATION_METHOD = "recent_alpha158_explicit_split_settlement_generation_v1"
ACCOUNT_PREFIX = "alpha158-split:"
FLAGS = {**original.FLAGS, "research_only": True}
CASE_IDS = [case["id"] for case in original.PROTOCOL["cases"]]


class IdentityTargetPolicy:
    policy_id = "alpha158_identity_published_target_v1"

    def __call__(self, intent):
        return intent


class NoRiskExitPolicy:
    policy_id = "alpha158_no_risk_exit_v1"

    def __call__(self, day, book, feed):
        return None


TARGET_POLICY = IdentityTargetPolicy()
RISK_POLICY = NoRiskExitPolicy()


def _now():
    return original._now()


def _hash(value):
    return original._hash(value)


def _context(run_dir, case_id):
    return RunContext(ACCOUNT_PREFIX + _hash(str(run_dir))[:16] + ":" + case_id, TradingMode.REPLAY)


def _reference(path):
    return {"file": str(Path(path).resolve()), "sha256": digest(path)}


def _registration_path(run_dir):
    return Path(run_dir).resolve() / "registration.json"


def register(first_publication, calendar, ipo_evidence, research_master_run, run_dir,
             generation_id="alpha158_prospective_v1"):
    """Create an immutable opt-in registration for a new split generation."""
    run_dir = Path(run_dir).resolve()
    calendar = Path(calendar).resolve()
    first_publication = Path(first_publication).resolve()
    qualification = original.load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    record = json.loads(first_publication.read_text(encoding="utf-8-sig"))
    if record.get("schema") != original.PUBLICATION_SCHEMA:
        raise ValueError("First publication is not a frozen Alpha158 publication")
    day = record.get("source_close_day")
    if not day or record.get("status") != "close_research_target_publication_only":
        raise ValueError("A real same-day Alpha158 publication is required")
    value = {
        "schema": "recent_alpha158_split_registration_v1",
        "generation_method_id": GENERATION_METHOD,
        "session_method_id": SESSION_METHOD,
        "generation_id": generation_id,
        "registered_at": _now().isoformat(),
        "run_directory": str(run_dir),
        "first_decision_day": day,
        "first_publication": _reference(first_publication),
        "calendar": str(calendar),
        "calendar_sha256": digest(calendar),
        "ipo_evidence": _reference(ipo_evidence),
        "research_master_run": _reference(research_master_run),
        "config": original._config(100000, 5).to_dict(),
        "holding_policy": original.HOLDING_POLICY,
        "original_accounts_unchanged": True,
        "missing_old_publications_repaired": False,
        "cash_reset": False,
        **FLAGS,
    }
    path = _registration_path(run_dir)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8-sig"))
        if old != value:
            raise ValueError("Existing Alpha split registration cannot be changed")
        return {"registration": _reference(path), "record": old, "already_registered": True}
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(path, value)
    return {"registration": _reference(path), "record": value, "already_registered": False}


def _load_registration(run_dir):
    path = _registration_path(run_dir)
    if not path.exists():
        raise FileNotFoundError("An explicit Alpha158 split registration is required")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if (value.get("schema") != "recent_alpha158_split_registration_v1"
            or value.get("generation_method_id") != GENERATION_METHOD
            or value.get("session_method_id") != SESSION_METHOD
            or value.get("run_directory") != str(Path(run_dir).resolve())
            or value.get("original_accounts_unchanged") is not True
            or value.get("missing_old_publications_repaired") is not False
            or value.get("cash_reset") is not False):
        raise ValueError("Alpha split registration binding changed")
    return path, value


def status(run_dir):
    """Read split-generation state without producing, advancing or mutating it."""
    run_dir = Path(run_dir).resolve()
    registration_path = _registration_path(run_dir)
    result = {
        "schema": "recent_alpha158_split_forward_status_v1",
        "run_directory": str(run_dir),
        "registration": None,
        "checkpoint": None,
        "cases": [],
        "research_only": True,
        "candidate_panel_only": True,
        "execution_admitted": False,
        "selection_enabled": False,
        "strategy_validated": False,
        "network_accessed": False,
    }
    if not registration_path.exists():
        result["status"] = "unregistered"
        return result
    try:
        registration_file, registration = _load_registration(run_dir)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        result["status"] = "registration_error"
        result["error"] = f"{type(error).__name__}: {error}"
        return result
    result["registration"] = _reference(registration_file)
    result["generation_id"] = registration.get("generation_id")
    checkpoint = run_dir / "head.json"
    if not checkpoint.exists():
        result["status"] = "registered_waiting_first_run"
        return result
    try:
        head = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
        if (head.get("schema") != SCHEMA
                or head.get("generation_method_id") != GENERATION_METHOD
                or head.get("registration") != _reference(registration_file)
                or head.get("binding") != registration
                or set(head.get("cases", {})) != set(CASE_IDS)):
            raise ValueError("Alpha split checkpoint binding or case set changed")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        result["status"] = "checkpoint_error"
        result["error"] = f"{type(error).__name__}: {error}"
        result["checkpoint"] = _reference(checkpoint)
        return result
    result["checkpoint"] = _reference(checkpoint)
    for case_id, case in head["cases"].items():
        state = case.get("state", {})
        result["cases"].append({
            "case": case_id,
            "last_processed_day": state.get("day"),
            "processed_days": len(case.get("daily", [])),
            "last_publication_day": case.get("last_publication_day"),
            "missing_publication_days": list(case.get("missing_publication_days", [])),
            "attachments": len(case.get("attachments", [])),
            "orders": len(state.get("orders", [])),
            "fills": len(state.get("fills", [])),
            "error": case.get("error"),
        })
    result["status"] = "ready"
    return result


def _append_journal(directory, case_id, attempt, pre_state, post_state, row, phase):
    attempt_hash = _hash(attempt)
    path = Path(directory) / "journal" / case_id / (attempt["day"] + "_" + phase + "_" + attempt_hash[:24] + ".json")
    entry = {"schema": "recent_alpha158_split_phase_v1", "phase": phase,
             "day": attempt["day"], "attempt": attempt,
             "pre_state_sha256": _hash(pre_state), "post_state_sha256": _hash(post_state),
             "post_state": deepcopy(post_state), "row": deepcopy(row),
             "committed_at": _now().isoformat()}
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8-sig"))
        if {k: v for k, v in old.items() if k != "committed_at"} != {k: v for k, v in entry.items() if k != "committed_at"}:
            raise ValueError("Saved Alpha split phase journal changed")
    else:
        write_json(path, entry)
    return {"file": str(path), "sha256": digest(path)}, {k: v for k, v in entry.items() if k != "post_state"}


def _case_head(run_dir, case_id, config):
    context = _context(run_dir, case_id)
    return {"context": context.to_dict(), "state": new_state(config.initial_cash),
            "daily": [], "attachments": [], "journal": [], "journal_files": [],
            "last_publication_day": None, "missing_publication_days": [],
            "error": None, "blocking_evidence": None}


def _validate_head(head, run_dir, config, source_days):
    if head.get("schema") != SCHEMA or head.get("generation_method_id") != GENERATION_METHOD:
        raise ValueError("Alpha split checkpoint method changed")
    if set(head.get("cases", {})) != set(CASE_IDS):
        raise ValueError("Both original Alpha158 capacity cases are required")
    for case_id, case in head["cases"].items():
        context = _context(run_dir, case_id)
        if case.get("context") != context.to_dict():
            raise ValueError("Alpha split account context changed")
        processed = [row["date"] for row in case["daily"]]
        if processed != source_days[:len(processed)] or case["state"].get("day") != (processed[-1] if processed else None):
            raise ValueError("Alpha split cursor skipped or moved backwards")
        if len(case["journal"]) != len(case["journal_files"]):
            raise ValueError("Alpha split journal count changed")
        prior = None
        for entry, proof in zip(case["journal"], case["journal_files"]):
            path = Path(proof["file"]).resolve()
            path.relative_to(Path(run_dir).resolve() / "journal")
            sealed = json.loads(path.read_text(encoding="utf-8-sig"))
            if (digest(path) != proof["sha256"] or sealed["post_state_sha256"] != _hash(sealed["post_state"])
                    or (prior is not None and sealed["pre_state_sha256"] != prior)):
                raise ValueError("Alpha split phase journal chain changed")
            prior = sealed["post_state_sha256"]
        if prior is not None and prior != _hash(case["state"]):
            raise ValueError("Alpha split checkpoint state does not match journal")


def _bind_intent(intent, context):
    return replace(intent, context=context)


def run(publications, provider_view, calendar, ipo_evidence, research_master_run, run_dir,
        *, through=None):
    run_dir = Path(run_dir).resolve()
    registration_path, registration = _load_registration(run_dir)
    now = _now()
    qualification = original.load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    provider = original._provider(provider_view, calendar, now)
    sessions, _ = original._sessions(calendar)
    published = [original._publication(path, calendar, qualification, now, {}) for path in publications]
    if any(item.get("unbindable") for item in published):
        return {"schema": RESULT_SCHEMA, "status": "waiting_unbindable_publication", **FLAGS}
    published.sort(key=lambda item: item["day"])
    pub_days = [item["day"] for item in published]
    if pub_days != sorted(set(pub_days)):
        raise ValueError("Alpha split publications must be unique and chronological")
    first = registration.get("first_publication")
    if not published or _reference(publications[0]) != first:
        raise ValueError("First publication must match the immutable registration")
    if provider["days"][0] > pub_days[0] or any(day not in provider["days"] for day in pub_days):
        raise ValueError("Each publication needs its own complete source day")
    cutoff = through or provider["days"][-1]
    if cutoff not in provider["days"] or cutoff < pub_days[0]:
        raise ValueError("Through must be an exact complete provider day")
    for pub in published:
        n = len(pub["source"]["source_proofs"])
        if (pub["source"]["source_proofs"] != provider["record"]["source_proofs"][:n]
                or pub["anchor"] != provider["anchor"]):
            raise ValueError("Publication and provider raw/source prefix differ")
    config = original._config(100000, 5)
    source_days = [day for day in provider["days"] if pub_days[0] <= day <= cutoff]
    base = original.ExactSourceRawFeed(provider["raw"], provider["days"],
        sorted({symbol for pub in published for intent in pub["intents"].values() for symbol, _ in intent.weights}),
        context_anchor=provider["anchor"])
    feed = original.PublishedRawFeed(base, qualification)
    path = run_dir / "head.json"
    head = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else None
    is_new = head is None
    if head is None:
        if any(p.exists() for p in run_dir.iterdir()) if run_dir.exists() else False:
            # registration.json is the only allowed pre-existing file.
            leftovers = [p.name for p in run_dir.iterdir() if p.name != "registration.json"]
            if leftovers:
                raise FileExistsError("Existing Alpha split directory cannot be reset")
        head = {"schema": SCHEMA, "generation_method_id": GENERATION_METHOD,
                "registration": _reference(registration_path), "binding": registration,
                "first_decision_day": pub_days[0], "publications": [],
                "source_prefix": original._source_prefix(provider, pub_days[0]),
                "cases": {case_id: _case_head(run_dir, case_id, config) for case_id in CASE_IDS}, **FLAGS}
    else:
        _validate_head(head, run_dir, config, source_days)
        if head.get("binding") != registration or head.get("registration") != _reference(registration_path):
            raise ValueError("Alpha split registration changed after account creation")
    pub_by_day = {pub["day"]: pub for pub in published}
    sessions_index = {day: i for i, day in enumerate(sessions)}
    with lock(run_dir / "runner.lock"):
        changed = 0
        for day in source_days:
            pub = pub_by_day.get(day)
            for case_id, case in head["cases"].items():
                context = _context(run_dir, case_id)
                state = case["state"]
                if state.get("day") == day:
                    marker = state.get("last_published_close_session")
                    if pub is not None and isinstance(marker, dict) and marker.get("publication") is None:
                        attempt = {"day": day, "phase": "attach", "publication": pub["reference"],
                                   "pre_state_sha256": _hash(state)}
                        try:
                            trial = deepcopy(state)
                            row = attach_close(trial, feed, day, _bind_intent(pub["intents"][case_id], context),
                                config, sessions_index[day], context=context,
                                published_close_target_policy=TARGET_POLICY,
                                replay_risk_exit_policy=RISK_POLICY)
                        except (ValueError, KeyError) as error:
                            case["error"] = f"{type(error).__name__}: {error}"
                            continue
                        proof, journal = _append_journal(run_dir, case_id, attempt, state, trial, row, "attach")
                        case.update(state=trial, error=None, blocking_evidence=None)
                        case["attachments"].append(row); case["journal"].append(journal); case["journal_files"].append(proof)
                        case["last_publication_day"] = day
                        case["missing_publication_days"] = [d for d in case["missing_publication_days"] if d != day]
                        changed += 1
                    continue
                if state.get("day") is not None and sessions_index[state["day"]] > sessions_index[day]:
                    continue
                if state.get("day") == day:
                    continue
                intent = _bind_intent(pub["intents"][case_id], context) if pub is not None else None
                attempt = {"day": day, "phase": "settlement", "publication": pub["reference"] if pub else None,
                           "pre_state_sha256": _hash(state)}
                try:
                    trial = deepcopy(state)
                    row = advance(trial, feed, day, config, sessions_index[day], context=context,
                        published_close_intent=intent, published_close_target_policy=TARGET_POLICY,
                        replay_risk_exit_policy=RISK_POLICY,
                        published_close_session_method=SESSION_METHOD)
                except (ValueError, KeyError) as error:
                    case["error"] = f"{type(error).__name__}: {error}"
                    continue
                proof, journal = _append_journal(run_dir, case_id, attempt, state, trial, row, "settlement")
                case.update(state=trial, error=None, blocking_evidence=None)
                case["daily"].append(row); case["journal"].append(journal); case["journal_files"].append(proof)
                if pub is not None:
                    case["last_publication_day"] = day
                else:
                    case["missing_publication_days"].append(day)
                changed += 1
            if pub is not None and pub["reference"] not in head["publications"]:
                head["publications"].append(pub["reference"])
        head["source_prefix"] = original._source_prefix(provider, cutoff)
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(path, head)
    cases = [{"case": key, "last_processed_day": value["state"].get("day"),
              "processed_days": [row["date"] for row in value["daily"]],
              "last_publication_day": value["last_publication_day"],
              "missing_publication_days": value["missing_publication_days"],
              "orders": len(value["state"]["orders"]), "fills": len(value["state"]["fills"]),
              "error": value.get("error"), "metrics": None} for key, value in head["cases"].items()]
    return {"schema": RESULT_SCHEMA, "status": "advanced_research_only" if changed else "no_change",
            "case_count": 2, "cases": cases, "checkpoint": str(path),
            "newly_committed_phases": changed, "research_checkpoint_created": True,
            "research_ledger_mutated": bool(changed), "research_accounts_created": 2 if is_new else 0,
            "account_reset": False, "metrics": None, **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    reg = sub.add_parser("register")
    reg.add_argument("--first-publication", type=Path, required=True)
    reg.add_argument("--calendar", type=Path, required=True)
    reg.add_argument("--ipo-evidence", type=Path, required=True)
    reg.add_argument("--research-master-run", type=Path, required=True)
    reg.add_argument("--run-dir", type=Path, required=True)
    reg.add_argument("--generation-id", default="alpha158_prospective_v1")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--run-dir", type=Path, required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--publication", type=Path, action="append", required=True)
    run_parser.add_argument("--provider-view", type=Path, required=True)
    run_parser.add_argument("--calendar", type=Path, required=True)
    run_parser.add_argument("--ipo-evidence", type=Path, required=True)
    run_parser.add_argument("--research-master-run", type=Path, required=True)
    run_parser.add_argument("--run-dir", type=Path, required=True)
    run_parser.add_argument("--through")
    args = parser.parse_args(argv)
    if args.action == "register":
        result = register(args.first_publication, args.calendar, args.ipo_evidence,
            args.research_master_run, args.run_dir, args.generation_id)
    elif args.action == "status":
        result = status(args.run_dir)
    else:
        result = run(args.publication, args.provider_view, args.calendar, args.ipo_evidence,
            args.research_master_run, args.run_dir, through=args.through)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
