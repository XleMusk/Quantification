"""Append actual router publications to four isolated shared-Engine books.

Saved D evidence chooses the owner; D raw executes the prior queue. No E bar
is required to publish or queue D's next-session decision. Journals and a
prepared head make a completed trial recoverable without running Engine twice.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path

from ..contracts import RunContext, TradingMode
from ..ledger import Ledger, new_state
from ..market_dataset import write_json
from ..worker import lock
from . import leader_type_router as policy
from . import leader_type_router_paper as paper
from .baostock_daily_slice import digest
from .leader_eight_type_forward_run import _calendar, _facts, _prefix, _same_lineage
from .leader_eight_type_panel_replay import _config, _environment, _rank_path
from .recent_alpha158_panel_replay import ExactSourceRawFeed


CHECKPOINT_SCHEMA = "leader_type_router_daily_checkpoint_v1"
RESULT_SCHEMA = "leader_type_router_daily_forward_result_v1"
JOURNAL_SCHEMA = "leader_type_router_daily_session_v1"
PREPARED_SCHEMA = "leader_type_router_daily_prepared_commit_v1"
METHOD_ID, CASE_IDS, FLAGS = paper.METHOD_ID, paper.CASE_IDS, paper.FLAGS


def _context(run_dir, case_id):
    if case_id not in CASE_IDS:
        raise ValueError("Unknown registered router case")
    return RunContext("leader-type-router-daily:" + paper._hash(str(Path(run_dir).resolve()))[:16]
        + ":" + case_id, TradingMode.REPLAY)


def _saved_journal(directory, case):
    journal, files = case["journal"], case["journal_files"]
    if len(journal) != len(files) or len(journal) != len(case["daily"]):
        raise ValueError("Each committed router session requires one sealed journal")
    for entry, reference in zip(journal, files):
        path = paper._checked(reference)
        path.resolve().relative_to(directory / "journal")
        if paper._read(path) != entry:
            raise ValueError("Saved router journal differs from head")
    if journal:
        last = journal[-1]
        if (last["post_state_sha256"] != paper._hash(case["state"])
                or last["row_sha256"] != paper._hash(case["daily"][-1])
                or last["decision_sha256"] != paper._hash(case["decisions"][-1])):
            raise ValueError("Committed router state, row or owner decision changed")


def load_saved_head(run_dir):
    """Read saved state and journal only; never load prices or run a strategy."""
    directory = Path(run_dir).resolve()
    path = directory / "head.json"
    if not path.exists():
        return None
    value = paper._read(path)
    if (value.get("schema") != CHECKPOINT_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("execution_admitted") is not False
            or value.get("binding", {}).get("run_directory") != str(directory)
            or set(value.get("cases", {})) != set(CASE_IDS)):
        raise ValueError("Saved daily router checkpoint binding is invalid")
    config = _config(100000, 5)
    first = value["binding"]["first_decision_day"]
    for key, case in value["cases"].items():
        context = _context(directory, key)
        processed = [row["date"] for row in case["daily"]]
        if (case.get("context") != context.to_dict() or case["state"].get("broker_binding")
                or case["state"].get("day") != (processed[-1] if processed else None)
                or processed != [p["day"] for p in value["publications"][:len(processed)]]
                or (processed and processed[0] != first)
                or len(case["decisions"]) != len(processed)
                or (processed and round(case["daily"][-1]["equity"] * 100)
                    != Ledger(case["state"], config, context).equity())):
            raise ValueError("Saved router account context, cursor or cash/equity changed")
        _saved_journal(directory, case)
    return value


def _restore_prepared(directory, binding, publications, latest):
    pointer = directory / "prepared.json"
    if not pointer.exists():
        return
    reference = paper._read(pointer)
    commit_path = paper._checked(reference)
    commit_path.resolve().relative_to(directory / "commits")
    prepared = paper._read(commit_path)
    target = prepared["head"]
    if (prepared.get("schema") != PREPARED_SCHEMA or target.get("binding") != binding
            or prepared.get("after_sha256") != paper._hash(target)
            or target["publications"] != publications[:len(target["publications"])]
            or _prefix(latest, target["source_prefix"]["through"]) != target["source_prefix"]):
        raise ValueError("Prepared router commit source/publication/binding changed")
    path = directory / "head.json"
    current = paper._read(path) if path.exists() else None
    current_hash = paper._hash(current) if current is not None else None
    if current_hash == prepared["after_sha256"]:
        return
    if current_hash != prepared["before_sha256"]:
        raise ValueError("Prepared router commit cannot overwrite a different or newer head")
    # Verify all immutable session files before restoring the already-run trial.
    for case in target["cases"].values():
        _saved_journal(directory, case)
    write_json(path, target)


def _commit(directory, before, after, *, day):
    commit = {"schema": PREPARED_SCHEMA, "day": day,
        "prepared_at": paper._now().isoformat(),
        "before_sha256": paper._hash(before) if before is not None else None,
        "after_sha256": paper._hash(after), "head": after,
        "engine_rerun_on_restore": False, **FLAGS}
    filename = day + "_" + paper._hash(commit)[:24] + ".json"
    path = directory / "commits" / filename
    if path.exists():
        raise FileExistsError("A prepared commit is immutable")
    write_json(path, commit)
    write_json(directory / "prepared.json", paper._pin(path))
    write_json(directory / "head.json", after)


def _result(head, path, *, committed=0, accounts_created=0):
    cases = []
    for key, case in head["cases"].items():
        state = case["state"]
        meta = state.get(policy.STATE_KEY) or {}
        cases.append({"case_id": key, "context": case["context"],
            "processed_decision_days": [row["date"] for row in case["daily"]],
            "last_processed_day": state.get("day"), "active_type": meta.get("active_type"),
            "exit_sticky": meta.get("exit_sticky"), "exit_reason": meta.get("exit_reason"),
            "orders": len(state["orders"]), "fills": len(state["fills"]),
            "pending_execution_day": (state.get("pending_published_close_intent") or {}).get("execution_not_before"),
            "blocking_evidence": case.get("blocking_evidence"), "error": case.get("error"),
            "metrics": None, "complete_PnL_available": False})
    blocked = any(case["error"] or case["blocking_evidence"] for case in cases)
    return {"schema": RESULT_SCHEMA, "method_id": METHOD_ID,
        "status": ("advanced_research_only_with_blocked_cases" if committed and blocked
            else "advanced_research_only" if committed else "blocked_or_waiting_research_only"
            if blocked else "waiting_for_next_real_close_publication"),
        "case_count": len(CASE_IDS), "cases": cases,
        "newly_committed_case_sessions": committed, "checkpoint": str(path),
        "research_accounts_created": accounts_created,
        "research_ledger_mutated": bool(committed), "engine_advanced": bool(committed),
        "orders_generated": any(case["orders"] for case in cases),
        "account_reset": False, "metrics": None,
        "publication_is_unbound_evidence_until_actual_research_head_consumption": True, **FLAGS}


def _waiting(status, **facts):
    return {"schema": RESULT_SCHEMA, "method_id": METHOD_ID, "status": status,
        "case_count": 0, "cases": [], "research_accounts_created": 0,
        "research_ledger_mutated": False, "engine_advanced": False,
        "orders_generated": False, "checkpoint": None, "metrics": None, **facts, **FLAGS}


def run(publications, panel, run_dir, *, registration, through=None):
    registered = paper.registration(registration)
    registration_ref = paper._pin(registration)
    cache = {}
    published = [paper.load_publication(path, registered, registration_ref, cache=cache)
                 for path in publications]
    if not published:
        return _waiting("waiting_for_real_router_D_publication")
    # Callers must supply the saved original order, not silently sort a broken prefix.
    days = [pub["day"] for pub in published]
    latest_path = str(Path(panel).resolve())
    if latest_path not in cache:
        cache[latest_path] = paper._load_inputs(latest_path, registered)
    latest = cache[latest_path]
    sessions, _ = _calendar(latest)
    first = registered["first_decision_day"]
    if (not days or days[0] != first or days != sorted(set(days))
            or days != [day for day in sessions if first <= day <= days[-1]]):
        raise ValueError("All actual publications must retain the exact first-D continuous original prefix")
    cutoff = through or days[-1]
    if cutoff not in sessions or cutoff < first:
        raise ValueError("Through must be an exact session on/after inception")
    authoritative = {}
    for pub in published:
        _same_lineage(pub["loaded"], latest)
        for day, panel_row in pub["loaded"]["panel_by_day"].items():
            if day in authoritative and _facts(authoritative[day]) != _facts(panel_row):
                raise ValueError("Published original candidate/risk/identity facts changed")
            authoritative.setdefault(day, deepcopy(panel_row))
    refs = [pub["reference"] for pub in published]
    directory = Path(run_dir).resolve()
    config = _config(100000, 5)
    binding = {"method_id": METHOD_ID, "registration": registration_ref,
        "run_directory": str(directory), "config": config.to_dict(),
        "first_decision_day": first, "calendar": latest["calendar_payload"],
        "qualification": latest["qualification"].verification,
        "scope_members": latest["scope_members"],
        "context_anchor": latest["provenance"]["context_anchor"], "spec": registered["spec"]}
    if latest["scope_members"] != registered["scope_members"]:
        raise ValueError("Original registered scope cannot roll or grow")
    if not any(day <= cutoff and day in latest["source_days"] for day in days):
        return _waiting("waiting_for_exact_complete_D_source")
    if directory.exists() and not (directory / "head.json").exists() and not (directory / "prepared.json").exists():
        raise FileExistsError("Existing directory without a saved head/commit cannot be reset")
    directory.mkdir(parents=True, exist_ok=True)
    with lock(directory / "runner.lock"):
        _restore_prepared(directory, binding, refs, latest)
        head = load_saved_head(directory)
        is_new = head is None
        if head is not None:
            if head["binding"] != binding or head["publications"] != refs[:len(head["publications"])]:
                raise ValueError("Saved account binding or processed actual publication prefix changed")
            if _prefix(latest, head["source_prefix"]["through"]) != head["source_prefix"]:
                raise ValueError("Consumed raw/source prefix changed")
            if any(case["state"]["day"] and case["state"]["day"] > cutoff for case in head["cases"].values()):
                raise ValueError("Persistent router cursor cannot move backwards")
        else:
            head = {"schema": CHECKPOINT_SCHEMA, "method_id": METHOD_ID, "binding": binding,
                "registered_at": paper._now().isoformat(), "publications": [],
                "source_prefix": _prefix(latest, first), "cases": {}, **FLAGS}
            for key in CASE_IDS:
                head["cases"][key] = {"context": _context(directory, key).to_dict(),
                    "state": new_state(config.initial_cash), "daily": [], "decisions": [],
                    "journal": [], "journal_files": [], "error": None, "blocking_evidence": None}
        base = ExactSourceRawFeed(latest["raw"], sessions, latest["scope_members"],
            context_anchor=latest["context_anchor"])
        committed = 0
        for index, pub in enumerate(published):
            day = pub["day"]
            if day > cutoff or day not in latest["source_days"]:
                break
            before = deepcopy(head) if not (is_new and not head["publications"]) else None
            after = deepcopy(head)
            panels = {date: panel for date, panel in authoritative.items() if date <= day}
            paths, _ = _rank_path(panels, set(latest["scope_members"]), min(panels), day)
            environments = _environment(panels, sessions, pub["record"]["recorded_at"])
            attempt = {"day": day, "publication": pub["reference"],
                "source_prefix": _prefix(latest, day), "registration": registration_ref}
            changed = False
            for key, case in after["cases"].items():
                if len(case["daily"]) != index or case.get("failed_attempt") == attempt:
                    continue
                _, guard, capacity = key.split("__")
                context = _context(directory, key)
                instance = policy.LeaderTypeRouter(base, config, context, panels=panels,
                    paths=paths, environments=environments, sessions=sessions,
                    source_days=[d for d in latest["source_days"] if d <= day],
                    members=latest["scope_members"], qualification=latest["qualification"],
                    execution=None, start=first, end=sessions[-2], guard=guard, capacity=capacity)
                trial = deepcopy(case["state"])
                try:
                    envelope = instance.publication_envelope(trial, day, index,
                        published_at=pub["record"]["recorded_at"],
                        source="research:" + METHOD_ID + ":" + pub["reference"]["sha256"])
                    state, row, audit = instance.step_published(trial, day, index, envelope=envelope)
                except (ValueError, KeyError) as error:
                    failure = {**attempt, "error": type(error).__name__ + ": " + str(error),
                        "trial_committed": False, "recorded_at": paper._now().isoformat(), **FLAGS}
                    write_json(directory / "failed_attempts" / (day + "_" + key + "_"
                        + paper._hash(failure)[:20] + ".json"), failure)
                    case.update(error=failure["error"], blocking_evidence={"day": day,
                        "reason": failure["error"], "session_not_committed": True}, failed_attempt=attempt)
                    changed = True
                    continue
                entry = {"schema": JOURNAL_SCHEMA, "day": day, "execution_day": pub["execution"],
                    "publication": pub["reference"], "source_prefix": attempt["source_prefix"],
                    "context": context.to_dict(), "envelope": envelope.to_dict(),
                    "post_state_sha256": paper._hash(state), "row_sha256": paper._hash(row),
                    "decision_sha256": paper._hash(audit),
                    "owner_after": deepcopy(state.get(policy.STATE_KEY)),
                    "committed_at": paper._now().isoformat(),
                    **{"post_session_" + name: deepcopy(state.get(name)) for name in
                        ("pending_published_close_intent", "pending_targets", "pending_risk_exits")}, **FLAGS}
                journal_path = directory / "journal" / key / (day + "_" + paper._hash(entry)[:24] + ".json")
                write_json(journal_path, entry)
                case.update(state=state, error=None, blocking_evidence=None)
                case.pop("failed_attempt", None)
                case["daily"].append(row)
                case["decisions"].append(audit)
                case["journal"].append(entry)
                case["journal_files"].append(paper._pin(journal_path))
                committed += 1
                changed = True
            if changed:
                if len(after["publications"]) == index:
                    after["publications"].append(pub["reference"])
                after["source_prefix"] = _prefix(latest, max(day, after["source_prefix"]["through"]))
                _commit(directory, before, after, day=day)
                head = after
        return _result(head, directory / "head.json", committed=committed,
            accounts_created=len(CASE_IDS) if is_new else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    command = actions.add_parser("run")
    for name in ("registration", "panel", "run-dir"):
        command.add_argument("--" + name, required=True, type=Path)
    command.add_argument("--publication", required=True, action="append", type=Path)
    command.add_argument("--through")
    command.add_argument("--result-json", type=Path)
    args = parser.parse_args(argv)
    if args.result_json:
        target = args.result_json.resolve()
        if target.exists():
            raise FileExistsError("Do not overwrite an existing command observation")
        protected = [args.run_dir.resolve(), args.panel.resolve().parent,
            args.registration.resolve().parent] + [p.resolve().parent for p in args.publication]
        if any(target == root or root in target.parents for root in protected):
            raise ValueError("Result observation must stay outside account and input directories")
    result = run(args.publication, args.panel, args.run_dir,
        registration=args.registration, through=args.through)
    if args.result_json:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        with args.result_json.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
