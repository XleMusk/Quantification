"""Settle known eight-type plans without inventing a missing close decision.

This is an explicitly registered new research generation. Settlement rows and
later same-day publication attachments are sealed separately; old generations
and the original strict close-publication consumer retain their own contracts.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

from ..contracts import IntentKind, RunContext, TradingMode
from ..engine import advance
from ..ledger import Ledger, new_state
from ..market_dataset import write_json
from ..published_close_session import METHOD_ID as SESSION_METHOD, attach_close
from ..worker import lock
from . import leader_eight_type_forward_run as original


SCHEMA = "leader_eight_type_split_forward_checkpoint_v1"
RESULT_SCHEMA = "leader_eight_type_split_forward_result_v1"
GENERATION_METHOD = "leader_eight_type_explicit_split_settlement_generation_v1"
ACCOUNT_PREFIX = "leader-eight-type-split:"
CASE_IDS = original.CASE_IDS
FLAGS = original.FLAGS


def _binding(registration, first, directory, ipo, master, latest, now):
    config = original._config(100000, 5)
    registered = original._registration(registration, first, config, now)
    value = json.loads(Path(registration).read_text(encoding="utf-8-sig"))
    generation = value.get("generation", {})
    own = {"file": first["reference"]["input_manifest"],
        "sha256": first["reference"]["input_manifest_sha256"]}
    qualification = {name: {"file": str(Path(file).resolve()), "sha256": original.digest(file)}
        for name, file in (("ipo_evidence", ipo), ("research_master_run", master))}
    if (value.get("generation_method_id") != GENERATION_METHOD
            or value.get("session_method_id") != SESSION_METHOD
            or value.get("initial_quality_scope") != "per_case"
            or value.get("registered_run_dir") != str(directory)
            or generation.get("kind") != "independent_new_research_generation"
            or generation.get("intended_run_directory") != str(directory)
            or generation.get("original_generations_unchanged") is not True
            or generation.get("original_cash_reused") is not False
            or generation.get("missing_old_publications_repaired") is not False
            or value.get("own_input_manifest") != own
            or value.get("qualification_inputs") != qualification
            or value.get("source_bindings") != first["record"]["source"]
            or value.get("source_calendar") != first["loaded"]["calendar_payload"]
            or value.get("scope_members") != first["loaded"]["scope_members"]
            or original.digest(registration) != registered["sha256"]):
        raise ValueError("Explicit split generation must bind its original method, directory and input evidence")
    descriptor = {"method_id": GENERATION_METHOD, "session_method_id": SESSION_METHOD,
        "initial_quality_scope": "per_case", "registered_run_dir": str(directory),
        "own_input_manifest": own, "qualification_inputs": qualification}
    return config, {"protocol": deepcopy(first["record"]["protocol"]), "config": config.to_dict(),
        "external_fixed_protocol": first["record"]["source"]["external_fixed_protocol"],
        "risk_policy": deepcopy(original.FORWARD_RISK_SPEC),
        "published_target_policy_id": original.TARGET_POLICY_ID,
        "calendar": latest["calendar_payload"], "qualification": latest["qualification"].verification,
        "scope_members": latest["scope_members"], "context_anchor": latest["provenance"]["context_anchor"],
        "run_directory": str(directory), "forward_method_registration": registered,
        "explicit_new_generation": descriptor}


def _quality(state, day, pub, base, previous, qualification, key):
    if pub is not None:
        return original._quality(state, pub, base, previous, qualification, key)
    held = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
    members = held | set(state.get("pending_targets") or {}) | set((state.get("pending_risk_exits") or {}).get("symbols", []))
    issues = original._raw_issues(base, day, members)
    for symbol in sorted(members):
        row = base.rows.get(day, {}).get(symbol)
        if row and (row["low"] > min(row["open"], row["close"])
                or row["high"] < max(row["open"], row["close"]) or row["low"] > row["high"]):
            issues.append({"day": day, "symbol": symbol, "reason": "inconsistent_raw_OHLC_geometry"})
    references = [original._reference(base, day, symbol, previous) for symbol in sorted(members)]
    ipo = [qualification.assess(symbol, day, decision_as_of=day + "T09:30:00+08:00") for symbol in sorted(members)]
    if issues or any(item["status"] != "consistent_proxy" for item in references) or any(item["engine_block_reason"] is not None for item in ipo):
        return {"reason": "required_raw_reference_or_listing_evidence_unresolved", "day": day,
            "raw_issues": issues, "reference_evidence": references, "IPO_evidence": ipo,
            "session_not_committed": True}
    return None


def _components(base, qualification, trial, key, day, publications, config, *, include_current):
    # A later publication's historical context never supplies an earlier
    # unpublished close feature to a session that is being settled now.
    available = [pub for pub in publications if pub["day"] < day or include_current and pub["day"] == day]
    last = available[-1]
    panels = last["loaded"]["panel_by_day"]
    identity = key.split("__")[0]
    batches = {date: {"schema": original.BATCH_SCHEMA, "day": date, "status": "incomplete",
        "source": "research:eight_type_split_published_close", "candidates": []} for date in base.calendar}
    for pub in available:
        row = next(row for row in pub["record"]["cases"] if row["case_id"] == key)
        batches[pub["day"]].update(status="incomplete" if pub["intents"][key].kind == IntentKind.NO_CHANGE else "complete",
            candidates=deepcopy(row["cap_candidates"]))
    common = original.CommonCohortDecisionFeed(base, batches, trial, market_defense=False)
    feed = original._QualifiedFeed(common, qualification)
    risk = original.PublishedCloseLeaderTypeRiskExitPolicy(identity,
        candidate_by_day={date: next(item for item in panel["types"] if item["id"] == identity)["candidates"]
            for date, panel in panels.items() if date <= last["day"]},
        feature_by_day={date: panel.get("risk_features", {}) for date, panel in panels.items() if date <= last["day"]},
        complete_feature_days=[date for date, panel in panels.items()
            if date <= last["day"] and panel.get("risk_feature_scope_complete") is True])
    return feed, risk, original.PublishedFixedExpiryReduction(feed, trial, config)


def _context(directory, key):
    return RunContext(ACCOUNT_PREFIX + original._hash(str(directory))[:16] + ":" + key, TradingMode.REPLAY)


def _validate_case(directory, key, case, source_days, config):
    context = _context(directory, key)
    processed = [row["date"] for row in case["daily"]]
    if (case["context"] != context.to_dict() or case["state"].get("broker_binding")
            or processed != source_days[:len(processed)]
            or case["state"]["day"] != (processed[-1] if processed else None)
            or len(case["journal"]) != len(case["journal_files"])):
        raise ValueError("Saved split case context or continuous settlement cursor changed")
    prior = None
    for entry, proof in zip(case["journal"], case["journal_files"]):
        path = Path(proof["file"]).resolve()
        path.relative_to(directory / "journal")
        sealed = json.loads(path.read_text(encoding="utf-8"))
        if (original.digest(path) != proof["sha256"]
                or {name: value for name, value in sealed.items() if name != "post_state"} != entry
                or entry["post_state_sha256"] != original._hash(sealed["post_state"])
                or (prior is not None and entry["pre_state_sha256"] != prior)):
            raise ValueError("Independent split phase journal changed or lost its state chain")
        prior = entry["post_state_sha256"]
    if prior is not None and prior != original._hash(case["state"]):
        raise ValueError("Saved split account changed after its independently sealed phase")
    if processed and round(case["daily"][-1]["equity"] * 100) != Ledger(case["state"], config, context).equity():
        raise ValueError("Saved split account equity differs from its settlement row")
    if [entry["row"] for entry in case["journal"] if entry["phase"] == "settlement"] != case["daily"]:
        raise ValueError("Every settlement requires its independent phase journal")
    if [entry["row"] for entry in case["journal"] if entry["phase"] == "attach"] != case["attachments"]:
        raise ValueError("Every publication attachment requires its independent phase journal")
    actual_days = {entry["day"] for entry in case["journal"] if entry["attempt"]["publication"] is not None}
    if (case["last_publication_day"] != max(actual_days, default=None)
            or case["missing_publication_days"] != [day for day in processed if day not in actual_days]):
        raise ValueError("Missing and attached publication status changed from sealed evidence")


def _commit_phase(directory, key, case, attempt, execute):
    """An orphan successful journal can be adopted without re-executing Engine."""
    attempt_sha = original._hash(attempt)
    if case.get("failed_attempt_sha256") == attempt_sha:
        return False
    path = directory / "journal" / key / (attempt["day"] + "_" + attempt["phase"] + "_" + attempt_sha[:24] + ".json")
    prior = original._hash(case["state"])
    if path.exists():
        entry = json.loads(path.read_text(encoding="utf-8"))
        if (entry.get("attempt") != attempt or entry.get("pre_state_sha256") != prior
                or original._hash(entry["post_state"]) != entry.get("post_state_sha256")):
            raise ValueError("Orphan split phase journal differs; never overwrite or repeat it")
        trial, row = entry["post_state"], entry["row"]
    else:
        trial = deepcopy(case["state"])
        try:
            row = execute(trial)
        except (ValueError, KeyError) as error:
            failure = {"attempt": attempt, "error": f"{type(error).__name__}: {error}", "trial_committed": False}
            failed = directory / "failed_attempts" / key / (attempt_sha + ".json")
            if failed.exists():
                if json.loads(failed.read_text(encoding="utf-8")) != failure:
                    raise ValueError("Saved failed split attempt differs; never overwrite it")
            else:
                write_json(failed, failure)
            case.update(error=failure["error"], failed_attempt_sha256=attempt_sha)
            return False
        entry = {"phase": attempt["phase"], "day": attempt["day"], "attempt": attempt,
            "pre_state_sha256": prior, "post_state_sha256": original._hash(trial),
            "post_state": deepcopy(trial), "row": deepcopy(row), "committed_at": original._now().isoformat()}
        write_json(path, entry)
    case.update(state=trial, error=None, blocking_evidence=None)
    case.pop("failed_attempt_sha256", None)
    case["daily" if attempt["phase"] == "settlement" else "attachments"].append(row)
    case["journal"].append({name: value for name, value in entry.items() if name != "post_state"})
    case["journal_files"].append({"file": str(path), "sha256": original.digest(path)})
    if attempt["publication"] is not None:
        case["last_publication_day"] = attempt["day"]
        case["missing_publication_days"] = [day for day in case["missing_publication_days"] if day != attempt["day"]]
    elif attempt["phase"] == "settlement":
        case["missing_publication_days"].append(attempt["day"])
    return True


def run(publications, panel, ipo_evidence, research_master_run, run_dir, *, through=None, registration=None):
    if registration is None:
        raise ValueError("A new split-method registration is required; no legacy migration")
    now, directory, cache = original._now(), Path(run_dir).resolve(), {}
    published = [original._publication(path, ipo_evidence, research_master_run, now, cache) for path in publications]
    if not published or any(pub.get("unbindable") for pub in published):
        return {**original._waiting("waiting_for_actual_timely_close_publication"), "schema": RESULT_SCHEMA}
    days = [pub["day"] for pub in published]
    if days != sorted(set(days)):
        raise ValueError("Actual publications must retain their unique chronological input order")
    panel = str(Path(panel).resolve())
    latest = cache.get(panel) or original.load_inputs(panel, ipo_evidence, research_master_run)
    sessions, previous = original._calendar(latest)
    cutoff = through or latest["source_days"][-1]
    if cutoff not in latest["source_days"] or cutoff < days[-1]:
        raise ValueError("Through must be an exact complete source day covering the actual publication prefix")
    authoritative = {}
    for pub in published:
        original._same_lineage(pub["loaded"], latest)
        if original._stamp(pub["record"]["source"]["input_recorded_at"]) > original._stamp(pub["record"]["recorded_at"]):
            raise ValueError("Actual publication predates its input")
        for day, facts in pub["loaded"]["panel_by_day"].items():
            if day in authoritative and original._facts(authoritative[day]) != original._facts(facts):
                raise ValueError("Published candidate/risk/identity prefix facts changed")
            authoritative.setdefault(day, deepcopy(facts))
    first = published[0]
    config, binding = _binding(registration, first, directory, ipo_evidence, research_master_run, latest, now)
    path = directory / "head.json"
    head = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    is_new = head is None
    source_days = [day for day in latest["source_days"] if first["day"] <= day <= cutoff]
    refs = [pub["reference"] for pub in published]
    if not source_days or source_days[0] != first["day"]:
        raise ValueError("Initial publication requires its own exact raw source")
    if head is None and directory.exists():
        raise FileExistsError("Existing directory without split head cannot be reset")
    if head is not None:
        if (head.get("schema") != SCHEMA or head.get("binding") != binding
                or any(head.get(key) is not value for key, value in FLAGS.items())):
            raise ValueError("Split generation binding changed; old or fixed-method books cannot migrate")
        if head["publications"] != refs[:len(head["publications"])]:
            raise ValueError("Actual saved publication prefix disappeared, changed or was inserted retroactively")
        if original._prefix(latest, head["source_prefix"]["through"]) != head["source_prefix"]:
            raise ValueError("Previously consumed raw/source prefix changed")
        new_pubs = published[len(head["publications"]):]
        for case in head["cases"].values():
            if case["state"]["day"] and cutoff < case["state"]["day"]:
                raise ValueError("Split account cursor cannot move backwards")
            if any(pub["day"] < (case["state"]["day"] or first["day"]) for pub in new_pubs):
                raise ValueError("Only the latest settled day can receive a new publication attachment")
    base = original.ExactSourceRawFeed(latest["raw"], sessions, latest["scope_members"], context_anchor=latest["context_anchor"])
    directory.mkdir(parents=True, exist_ok=True)
    with lock(directory / "runner.lock"):
        if head is not None and json.loads(path.read_text(encoding="utf-8")) != head:
            raise ValueError("Split checkpoint changed while acquiring the runner lock")
        if head is None:
            if path.exists():
                raise ValueError("Concurrent split runner already created the checkpoint")
            head = {"schema": SCHEMA, "binding": binding, "registered_at": original._now().isoformat(),
                "first_decision_day": first["day"], "publications": [],
                "source_prefix": original._prefix(latest, first["day"]), "cases": {}, **FLAGS}
            for key in CASE_IDS:
                head["cases"][key] = {"context": _context(directory, key).to_dict(), "state": new_state(config.initial_cash),
                    "daily": [], "attachments": [], "journal": [], "journal_files": [],
                    "last_publication_day": None, "missing_publication_days": []}
            write_json(path, head)
        if set(head["cases"]) != set(CASE_IDS):
            raise ValueError("All original thirty-two independent research cases are required")
        before_orders = sum(len(case["state"]["orders"]) for case in head["cases"].values())
        before_fills = sum(len(case["state"]["fills"]) for case in head["cases"].values())
        for key, case in head["cases"].items():
            _validate_case(directory, key, case, source_days, config)
        committed, attachments = 0, 0
        pub_by_day = {pub["day"]: pub for pub in published}
        for index, day in enumerate(source_days):
            pub = pub_by_day.get(day)
            changed = False
            for key, case in head["cases"].items():
                cursor = case["state"]["day"]
                if cursor is not None and cursor > day:
                    continue
                # Except for the initial empty D book, E settlement is always
                # independent of E's new candidates and their quality checks.
                # Unknown E decision evidence cannot stop D's known opening.
                for phase in ("settlement", "attach"):
                    if phase == "settlement":
                        if case["state"]["day"] == day or len(case["daily"]) != index:
                            continue
                        phase_pub = pub if index == 0 else None
                    else:
                        if pub is None or case["state"]["day"] != day or case["last_publication_day"] == day:
                            continue
                        phase_pub = pub
                    attempt = {"day": day, "phase": phase, "publication": phase_pub["reference"] if phase_pub else None,
                        "source_prefix": original._prefix(latest, day), "pre_state_sha256": original._hash(case["state"])}
                    if case.get("failed_attempt_sha256") == original._hash(attempt):
                        continue
                    issue = _quality(case["state"], day, phase_pub, base, previous, latest["qualification"], key)
                    if issue:
                        issue["phase"] = phase
                        gap_path = directory / "quality_gaps" / key / (original._hash(attempt) + ".json")
                        gap = {"attempt": attempt, "blocking_evidence": deepcopy(issue),
                            "engine_trial_started": False, "session_state_changed": False}
                        if gap_path.exists():
                            if json.loads(gap_path.read_text(encoding="utf-8")) != gap:
                                raise ValueError("Saved individual quality gap differs; never overwrite it")
                        else:
                            write_json(gap_path, gap)
                        issue["saved_gap_record"] = {"file": str(gap_path), "sha256": original.digest(gap_path)}
                        if case.get("blocking_evidence") != issue:
                            case["blocking_evidence"] = issue
                            changed = True
                        continue
                    def execute(trial):
                        feed, risk, target = _components(base, latest["qualification"], trial, key, day, published, config,
                            include_current=phase_pub is not None)
                        context = RunContext.from_dict(case["context"])
                        intent = replace(phase_pub["intents"][key], context=context) if phase_pub is not None else None
                        arguments = {"context": context, "published_close_target_policy": target, "replay_risk_exit_policy": risk}
                        if phase == "attach":
                            return attach_close(trial, feed, day, intent, config, len(case["daily"]) - 1, **arguments)
                        return advance(trial, feed, day, config, len(case["daily"]), published_close_intent=intent,
                            published_close_session_method=SESSION_METHOD, **arguments)
                    before = (case.get("error"), case.get("failed_attempt_sha256"), case.get("blocking_evidence"))
                    success = _commit_phase(directory, key, case, attempt, execute)
                    if success:
                        if phase == "attach":
                            attachments += 1
                        else:
                            committed += 1
                    changed = changed or success or before != (case.get("error"), case.get("failed_attempt_sha256"), case.get("blocking_evidence"))
            if changed or head["publications"] != refs:
                head["publications"] = deepcopy(refs)
                if day > head["source_prefix"]["through"]:
                    head["source_prefix"] = original._prefix(latest, day)
                write_json(path, head)
        cases = [{"case_id": key, "last_processed_day": case["state"]["day"],
            "settled_days": [row["date"] for row in case["daily"]],
            "processed_decision_days": [row["date"] for row in case["daily"]
                if row["date"] not in case["missing_publication_days"]],
            "last_publication_day": case["last_publication_day"],
            "unattached_publication_days": case["missing_publication_days"],
            "missing_publication_days": [d for d in case["missing_publication_days"] if d not in pub_by_day],
            "orders": len(case["state"]["orders"]), "fills": len(case["state"]["fills"]),
            "pending_execution_day": (case["state"].get("pending_published_close_intent") or {}).get("execution_not_before"),
            "pending_risk_execution_day": (case["state"].get("pending_risk_exits") or {}).get("execution_on"),
            "blocking_evidence": case.get("blocking_evidence"), "error": case.get("error"), "metrics": None}
            for key, case in head["cases"].items()]
        blocked = any(case["blocking_evidence"] or case["error"] for case in cases)
        changed = committed + attachments > 0
        new_order_count = sum(case["orders"] for case in cases) - before_orders
        new_fill_count = sum(case["fills"] for case in cases) - before_fills
        return {"schema": RESULT_SCHEMA,
            "status": "advanced_research_only_with_blocked_cases" if changed and blocked else "advanced_research_only" if changed
                else "blocked_or_waiting_research_only" if blocked else "saved_split_settlement_unchanged",
            "case_count": len(CASE_IDS), "cases": cases, "newly_committed_case_sessions": committed,
            "newly_attached_case_publications": attachments, "checkpoint": str(path),
            "research_checkpoint_created": True, "research_ledger_mutated": changed,
            "research_accounts_created": len(CASE_IDS) if is_new else 0,
            "account_reset": False, "user_account_created": False,
            "new_order_count": new_order_count, "new_fill_count": new_fill_count,
            "orders_generated": new_order_count > 0,
            "cumulative_orders_present": any(case["orders"] for case in cases),
            "whole_session_commit_requires_own_real_close_publication": False,
            "missing_publication_is_no_change": False, "metrics": None, **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", action="append", required=True, type=Path)
    for name in ("panel", "ipo-evidence", "research-master-run", "run-dir", "registration"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--through")
    args = parser.parse_args(argv)
    result = run(args.publication, args.panel, args.ipo_evidence, args.research_master_run, args.run_dir,
        through=args.through, registration=args.registration)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
