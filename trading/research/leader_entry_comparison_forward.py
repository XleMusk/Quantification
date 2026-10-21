"""Consume the preregistered three-arm publications through the shared Ledger."""
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
from ..worker import lock
from .baostock_daily_slice import digest
from . import leader_entry_comparison_publication as publication
from . import leader_published_forward_run as shared
from .leader_daily_replay import CommonCohortDecisionFeed, EXIT_POLICIES, FAMILIES, POOLS
from .leader_entry_confirmation_policy import ARM_IDS, POLICY_IDS, PublishedEntryConfirmationReduction
from .leader_forward_run import ForwardRawFeed, _case_key, _ipo_proof, _prefix

SCHEMA = "leader_entry_comparison_shared_ledger_checkpoint_v1"
RESULT_SCHEMA = "leader_entry_comparison_forward_result_v1"
FLAGS = dict(shared.FLAGS)


def _now():
    return publication._now()


def _waiting(registered, status, **facts):
    r = registered["record"]
    return {"schema": RESULT_SCHEMA, "status": status, "registration": registered["proof"],
        "first_decision_day": r["first_decision_day"], "required_first_execution_day": r["first_execution_day"],
        "decision_window_end": r["decision_window_end"], "execution_tail_end": r["execution_tail_end"],
        "planned_case_count": 15, "case_count": 0, "research_accounts_created": 0,
        "research_checkpoint_created": False, "research_ledger_mutated": False,
        "orders_generated": False, "metrics": None, **facts, **FLAGS}


def _context(directory, key):
    return RunContext("leader-entry-comparison:" + shared._hash(str(directory))[:16] + ":" + key,
                      TradingMode.REPLAY)


def _summary(key, case, window, initial_cash):
    family, arm = key.split(":")
    base_key = _case_key(POOLS[0], family, EXIT_POLICIES[0])
    # Count actual reduced FULL_TARGET decisions, preserving original facts in daily.
    adjusted = dict(case)
    adjusted["daily"] = [{**row, "original_publication_kind": row["effective_publication_kind"]}
                          for row in case["daily"]]
    result = shared._case_summary(base_key, adjusted, window, initial_cash)
    observations = [j["confirmation_observation"] for j in case["journal"]
                    if j["confirmation_observation"] is not None]
    evaluations = [r for observation in observations for r in observation["evaluations"]]
    result.update(case_id=key, family=family, arm=arm, base_publication_case_id=base_key,
        target_policy_id=POLICY_IDS[arm],
        confirmation_unknown_decisions=sum(o["applicable_unknown"] for o in observations),
        confirmation_removed_target_observations=sum(r["confirmation_applied"] and
            r["confirmation_status"] == "known_false" and r["retained_in_final_target"] is False for r in evaluations),
        confirmation_held_target_removals=sum(r["confirmation_applied"] and r["post_opening_quantity"] > 0
            and r["confirmation_status"] == "known_false" and r["retained_in_final_target"] is False for r in evaluations),
        blocked_orders=sum(e["kind"] == "order_blocked" for e in case["state"]["events"]),
        blocked_sell_orders=sum(e["kind"] == "order_blocked" and e.get("side") == "sell"
                                for e in case["state"]["events"]))
    return result


def run(publications, publication_bundles, latest_bundle, run_dir, *, registration, through=None):
    now, directory = _now(), Path(run_dir).resolve()
    registered = publication.load_registration(registration, now=now)
    record, window, config = registered["record"], registered["window"], registered["config"]
    head_path = directory / "head.json"
    if not publications:
        if directory.exists():
            raise ValueError("An existing research directory requires its complete publication prefix")
        return _waiting(registered, "waiting_for_first_real_registered_close_publication")
    if len(publications) != len(publication_bundles) or latest_bundle is None:
        raise ValueError("Each publication needs its own input plus a current execution bundle")
    latest = shared._load(latest_bundle, registered["binding"], registered["calendar"], now)
    qualification = _ipo_proof(registered["binding"])
    if qualification is None or tuple(qualification.calendar) != tuple(registered["sessions"]):
        raise ValueError("Original typed IPO qualification/calendar required")
    cohort = {**record["original_cohort"], "pools": {POOLS[0]: record["common20"]}}
    scoped = shared._scopes(latest, cohort, window[39])
    pairs = sorted(zip(publications, publication_bundles),
        key=lambda pair: json.loads(Path(pair[0]).read_text(encoding="utf-8-sig"))["source_close_day"])
    published = []
    for path, bundle_path in pairs:
        own = shared._load(bundle_path, registered["binding"], registered["calendar"], now)
        pub = publication.load_publication(path, own, registration, now=now)
        shared._lineage(own, latest, cohort, window[39])
        # Retain each publication's own small batches and evidence, not 46 large histories.
        pub.pop("own")
        published.append(pub)
        del own
    days = [p["day"] for p in published]
    if days != window[:len(days)] or len(days) > 46:
        raise ValueError("Publications must be the exact registered 40-plus-six prefix")
    bound = {"registration": registered["proof"], "method_id": publication.METHOD_ID,
        "protocol": record["protocol"], "common20": record["common20"], "window": window,
        "config": config.to_dict(), "case_ids": list(publication.CASE_IDS),
        "target_policy_ids": POLICY_IDS, "risk_policy": shared.RISK_RULE_SPEC,
        "run_directory": str(directory)}
    head = json.loads(head_path.read_text(encoding="utf-8")) if head_path.exists() else None
    is_new = head is None
    if head is None and directory.exists():
        raise FileExistsError("Research directory without a checkpoint cannot be reset")
    if head is not None:
        if (head.get("schema") != SCHEMA or head.get("binding") != bound
                or any(head.get(k) is not v for k, v in FLAGS.items())
                or list(head["cases"]) != list(publication.CASE_IDS)):
            raise ValueError("Frozen method, arm or account binding changed")
        if head["publications"] != [p["reference"] for p in published[:len(head["publications"])]]:
            raise ValueError("Consumed publication prefix disappeared or changed")
        if _prefix(latest["raw"], scoped, head["source_prefix"]["through"]) != head["source_prefix"]:
            raise ValueError("Consumed raw/candidate source prefix changed")
    cutoff = through or days[-1]
    if cutoff not in registered["sessions"] or cutoff < window[0]:
        raise ValueError("Through must be an exact session on or after the fixed start")
    if head and any(c["state"]["day"] and cutoff < c["state"]["day"] for c in head["cases"].values()):
        raise ValueError("Research cursor cannot move backwards")
    base = ForwardRawFeed(latest["raw"], registered["sessions"], record["common20"], ipo_qualification=qualification)
    first = published[0]
    if head is None:
        if first["execution"] not in latest["days"]:
            return _waiting(registered, "waiting_for_exact_first_execution_source",
                            required_source_day=first["execution"])
        issue = shared._quality({"positions": {}, "pending_targets": None}, first,
            base, latest["previous"], qualification, successor=True)
        if issue:
            return _waiting(registered, "waiting_for_first_execution_quality", blocking_evidence=issue)
    directory.mkdir(parents=True, exist_ok=True)
    with lock(directory / "runner.lock"):
        if head is not None and json.loads(head_path.read_text(encoding="utf-8")) != head:
            raise ValueError("Checkpoint changed during read")
        if head is None:
            if head_path.exists():
                raise ValueError("Concurrent runner created the checkpoint")
            head = {"schema": SCHEMA, "created_at": _now().isoformat(), "binding": bound,
                "publications": [], "source_prefix": _prefix(latest["raw"], scoped, first["execution"]),
                "cases": {}, **FLAGS}
            for key in publication.CASE_IDS:
                head["cases"][key] = {"context": _context(directory, key).to_dict(),
                    "state": new_state(config.initial_cash), "daily": [], "journal": [], "journal_files": [],
                    "verified_breadth_seen": False}
            write_json(head_path, head)
        for key, case in head["cases"].items():
            _, arm = key.split(":")
            context = _context(directory, key)
            processed = [r["date"] for r in case["daily"]]
            if (case["context"] != context.to_dict() or case["state"].get("broker_binding")
                    or processed != days[:len(processed)]
                    or case["state"]["day"] != (processed[-1] if processed else None)
                    or len(case["journal"]) != len(processed)):
                raise ValueError("Case context, cursor or journal changed")
            shared._validate_case_journal(case, directory)
            for i, (row, entry) in enumerate(zip(case["daily"], case["journal"])):
                if (entry["decision_day"] != row["date"] or entry["publication"] != published[i]["reference"]
                        or entry["method_registration"] != registered["proof"] or entry["arm"] != arm
                        or entry["post_session_daily_row_sha256"] != shared._hash(row)):
                    raise ValueError("Committed daily evidence, publication or arm changed")
            if processed:
                if (round(case["daily"][-1]["equity"] * 100) != Ledger(case["state"], config, context).equity()
                        or case["state"].get("replay_risk_exit_policy") != {"schema": "replay_risk_exit_policy_v1",
                            "policy_id": shared.RISK_POLICY_ID, "context": context.to_dict()}
                        or case["state"].get("published_close_target_policy") != {"schema": "replay_published_close_target_policy_v1",
                            "policy_id": POLICY_IDS[arm], "context": context.to_dict()}
                        or shared._hash(case["state"]) != case["journal"][-1]["post_session_state_sha256"]):
                    raise ValueError("Committed case state or arm policy changed")
            elif case["state"] != new_state(config.initial_cash):
                raise ValueError("An unadvanced initial account cannot contain injected state")
        completed = 0
        for index, pub in enumerate(published):
            if pub["day"] > cutoff or pub["day"] not in latest["days"]:
                break
            witness = pub["execution"] if index == 0 else pub["day"]
            attempt = {"day": pub["day"], "publication": pub["reference"],
                       "source_prefix": _prefix(latest["raw"], scoped, witness)}
            changed = False
            for key, case in head["cases"].items():
                if len(case["daily"]) != index or case.get("failed_attempt") == attempt:
                    continue
                family, arm = key.split(":")
                base_key = _case_key(POOLS[0], family, EXIT_POLICIES[0])
                issue = shared._quality(case["state"], pub, base, latest["previous"], qualification,
                                        base_key, successor=index == 0)
                if issue:
                    if case.get("blocking_evidence") != issue:
                        case["blocking_evidence"] = issue
                        changed = True
                    continue
                trial, context = deepcopy(case["state"]), _context(directory, key)
                feed = CommonCohortDecisionFeed(base, pub["scoped_batches"][family], trial, market_defense=False)
                original = pub["intents"][base_key]
                intent = replace(original, context=context, decided_at=pub["record"]["recorded_at"],
                    execution_not_before=pub["execution"], source=original.source or
                    "research:leader_entry_comparison_publication:" + family)
                policy = PublishedEntryConfirmationReduction(feed, trial, config,
                    arm_id=arm, confirmation=pub["confirmation"])
                try:
                    row = advance(trial, feed, pub["day"], config, index, context=context,
                        published_close_intent=intent, published_close_target_policy=policy,
                        replay_risk_exit_policy=shared.PublishedFiveSessionExpiryPolicy())
                except (ValueError, KeyError) as error:
                    failure = {**attempt, "case_id": key, "error": f"{type(error).__name__}: {error}", "trial_committed": False}
                    write_json(directory / "failed_attempts" / (pub["day"] + "_" + shared._hash(key)[:12]
                        + "_" + shared._hash(attempt)[:16] + ".json"), failure)
                    case.update(error=failure["error"], failed_attempt=attempt)
                    changed = True
                    continue
                reduced = row.get("published_close_reduction", {}).get("reduced_intent")
                row.update(batch_status=pub["scoped_batches"][family].get(pub["day"], {}).get("status", "missing"),
                    original_publication_kind=original.kind.value,
                    effective_publication_kind=reduced["kind"] if reduced else original.kind.value,
                    registered_tail_no_new_decision=pub["tail"])
                case.update(state=trial, error=None, blocking_evidence=None)
                case.pop("failed_attempt", None)
                case["daily"].append(row)
                entry = {"decision_day": pub["day"], "execution_day": pub["execution"],
                    "publication": pub["reference"], "method_registration": registered["proof"],
                    "arm": arm, "unbound_original_intent": original.to_dict(), "bound_original_intent": intent.to_dict(),
                    "published_close_reduction": deepcopy(row.get("published_close_reduction")),
                    "confirmation_observation": policy.observation,
                    "committed_at": _now().isoformat(), "retrospective_successor_quality_witness": index == 0,
                    "successor_used_for_ranking_or_refill": False, "source_features_from_own_publication": True,
                    "post_session_state_sha256": shared._hash(trial),
                    "post_session_daily_row_sha256": shared._hash(row),
                    **{"post_session_" + field: deepcopy(trial.get(field)) for field in
                       ("pending_targets", "pending_published_close_intent", "pending_risk_exits")}}
                shared._append_case_journal(directory, key, case, entry)
                completed += 1
                changed = True
            if changed:
                if len(head["publications"]) <= index:
                    head["publications"].append(pub["reference"])
                if witness > head["source_prefix"]["through"]:
                    head["source_prefix"] = _prefix(latest["raw"], scoped, witness)
                write_json(head_path, head)
        cases = [_summary(key, case, window, config.initial_cash) for key, case in head["cases"].items()]
        blocked = any(c["error"] or c["blocking_evidence"] for c in cases)
        return {"schema": RESULT_SCHEMA, "status": "advanced_research_only_with_blocked_cases" if completed and blocked else
                "advanced_research_only" if completed else "blocked_or_waiting_research_only" if blocked else
                "waiting_for_next_real_close_publication", "registration": registered["proof"],
            "case_count": 15, "cases": cases, "newly_committed_case_sessions": completed,
            "first_decision_day": window[0], "decision_window_end": window[39], "execution_tail_end": window[45],
            "checkpoint": str(head_path), "research_checkpoint_created": True,
            "research_ledger_mutated": bool(completed), "research_accounts_created": 15 if is_new else 0,
            "orders_generated": any(c["orders"] for c in cases), "metrics": None,
            "next_real_close_publication_days_required": sorted({window[len(c["processed_decision_days"])]
                for c in cases if len(c["processed_decision_days"]) < 46}), **FLAGS}


def input_directories(*, publications, publication_bundles, latest_bundle, registration, run_dir,
        extra_inputs=()):
    """Declared metadata roots only; never load raw bars, models or accounts."""
    queue = list(map(Path, [*publications, *publication_bundles, registration, *extra_inputs]))
    if latest_bundle is not None:
        queue.append(Path(latest_bundle))
    roots, visited = [Path(run_dir).resolve()], set()
    relative_files = {"parent_protocol", "qualification_evidence", "research_master_run", "source_history_view"}
    while queue:
        path = queue.pop().resolve()
        if path in visited:
            continue
        visited.add(path)
        roots.append(path if path.is_dir() else path.parent)
        if path.suffix.lower() != ".json" or not path.is_file():
            continue
        value = json.loads(path.read_text(encoding="utf-8-sig"))

        def declared(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in relative_files and isinstance(child, str) and "://" not in child:
                        yield (path.parent / child).resolve()
                    else:
                        yield from declared(child)
            elif isinstance(item, list):
                for child in item:
                    yield from declared(child)
            elif isinstance(item, str) and "://" not in item and Path(item).is_absolute():
                yield Path(item)
        queue.extend(declared(value))
    return list(dict.fromkeys(str(path) for path in roots))


def _result_export_path(path, *, publications, publication_bundles, latest_bundle, registration, run_dir):
    if path is None:
        return None
    target = Path(path).resolve()
    if target.suffix.lower() != ".json" or target.exists():
        raise ValueError("Entry-comparison observations require a new external JSON file")
    roots = input_directories(publications=publications, publication_bundles=publication_bundles,
        latest_bundle=latest_bundle, registration=registration, run_dir=run_dir)
    if any(target == Path(root) or Path(root) in target.parents for root in roots):
        raise ValueError("Result observations must stay outside the account and all declared indirect inputs")
    return target


def _save_observation(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", action="append", type=Path, default=[])
    parser.add_argument("--publication-bundle", action="append", type=Path, default=[])
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--through")
    parser.add_argument("--result-json", type=Path,
        help="Save the full observation outside the account and every declared input directory")
    args = parser.parse_args(argv)
    export = _result_export_path(args.result_json, publications=args.publication,
        publication_bundles=args.publication_bundle, latest_bundle=args.bundle,
        registration=args.registration, run_dir=args.run_dir)
    try:
        result = run(args.publication, args.publication_bundle, args.bundle, args.run_dir,
                     registration=args.registration, through=args.through)
    except (ValueError, TypeError, OSError, RuntimeError, KeyError) as error:
        if export is not None:
            _save_observation(export, {"schema": "leader_entry_comparison_forward_command_failure_v1",
                "status": "failed", "recorded_at": _now().isoformat(),
                "run_directory": str(args.run_dir.resolve()), "error_type": type(error).__name__,
                "reason": str(error), "execution_progress_unknown": True, "account_reset": False,
                "newly_committed_case_sessions": None, "metrics": None, **FLAGS})
        raise
    if export is not None:
        result = {**result, "execution_progress_unknown": False}
        _save_observation(export, result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
