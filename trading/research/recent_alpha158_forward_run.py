"""Append real frozen Alpha158 close publications to two research Ledgers.

Each Engine call commits a whole day.  A close without its real publication is
therefore left unadvanced; it cannot later receive a backfilled evening target.
The first exact following complete raw session is a quality witness.  Later
days consume pending weights using their own raw bar and real close publication;
the next execution date needs the pinned calendar, not an invented future bar.
This module has no provider or broker transport.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..contracts import IntentKind, MARKET_TZ, PortfolioIntent, RunContext, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import Ledger, new_state
from ..market_dataset import write_json
from ..worker import lock
from .baostock_daily_slice import digest
from .ipo_qualification import IpoQualifiedResearchFeed, load_ipo_qualification
from .leader_daily_signals import _calendar_view
from .recent_alpha158_panel_replay import (PROTOCOL, ExactSourceRawFeed, _context_anchor,
    _load, _raw_issues, _reference)
from .recent_alpha158_target_publication import (FLAGS as PUBLICATION_FLAGS, SCHEMA as PUBLICATION_SCHEMA,
    _model_contract, _prepare_case, _record)
from .recent_source_alpha158 import _aware
from .recent_source_qlib_provider import METHOD_ID as PROVIDER_METHOD, SCHEMA as PROVIDER_SCHEMA
from .stockdb_alpha158_lgbm_panel_replay import _config


SCHEMA = "recent_alpha158_published_close_shared_ledger_checkpoint_v1"
RESULT_SCHEMA = "recent_alpha158_published_close_forward_result_v1"
HOLDING_POLICY = "daily_rebalance_holdings_count_5_no_fixed_session_expiry"
FLAGS = {"candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
    "execution_admitted": False, "model_refit": False, "future_labels_used_for_score": False,
    "broker_interacted": False, "provider_contacted": False, "ths_payload_generated": False,
    "pit_verified": False, "corporate_actions_verified": False, "user_account_mutated": False}


def _now():
    return datetime.now(MARKET_TZ)


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode()).hexdigest()


def _frame_hash(frame):
    frame = frame.sort_values(["date", "code"], kind="stable").reindex(sorted(frame.columns), axis=1)
    return sha256(frame.to_json(orient="split", index=False, force_ascii=False).encode()).hexdigest()


def _sessions(calendar):
    payload = json.loads(Path(calendar).read_text(encoding="utf-8"))
    year = int(payload["year"])
    sessions, previous, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Exact pinned exchange calendar required")
    return sessions, previous


def _provider(provider_view, calendar, now):
    """Consume only the saved raw copy, not native binaries or new inference."""
    path, record = _record(provider_view)
    days, sessions = record["recent_source_days"], _sessions(calendar)[0]
    if (record.get("schema") != PROVIDER_SCHEMA or record.get("provider_method_id") != PROVIDER_METHOD
            or record.get("status") != "research_provider_completed"
            or any(record.get(k) is not False for k in ("raw_prices_imputed", "model_refit", "model_trained",
                "future_labels_used_for_score", "execution_admitted", "selection_enabled", "strategy_validated"))
            or record.get("recent_source_inventory_complete") is not True
            or record.get("recent_raw_original_fields_unchanged") is not True
            or digest(calendar) != record["calendar_sha256"]
            or _aware(record["created_at"]) > now or not days or days != sorted(set(days))
            or [d for d in sessions if days[0] <= d <= days[-1]] != days
            or [p["day"] for p in record["source_proofs"]] != days):
        raise ValueError("Complete unadmitted provider, exact calendar and consecutive raw source prefix required")
    for proof in record["source_proofs"]:
        terminal = proof.get("source_completed_at") or proof.get("source_updated_at")
        if (any(proof.get(k) is not True for k in ("read_only_verified", "source_inventory_complete",
                "full_dated_roster_inventory_complete"))
                or proof.get("receipt_status") != "complete_full_dated_close_inventory_not_admitted"
                or proof.get("calendar_sha256") != digest(calendar)
                or not terminal or not _aware(proof["day"] + "T15:00:00+08:00") <= _aware(terminal)
                    <= _aware(record["created_at"]) <= now):
            raise ValueError("Each exact full close needs its actual complete source clock")
    raw_path = Path(record["recent_raw_observations_file"]).resolve()
    raw_path.relative_to(path.parent)
    if digest(raw_path) != record["recent_raw_observations_sha256"]:
        raise ValueError("Saved execution raw changed")
    raw = pd.read_parquet(raw_path)
    required = {"date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "tradestatus", "isST", "pctChg"}
    if (not required <= set(raw) or len(raw) != record["recent_raw_observation_rows"]
            or raw.duplicated(["date", "code"]).any() or sorted(raw.date.unique()) != days
            or not raw.code.str.fullmatch(r"(?:sh\.(?:60\d{4}|688\d{3})|sz\.(?:00\d{4}|30\d{4}))").all()
            or raw.groupby("date").size().to_dict() != {p["day"]: p["ordinary_inventory_rows"] for p in record["source_proofs"]}):
        raise ValueError("Execution raw must preserve all exact own-dated ordinary rows")
    anchor, anchor_proof = _context_anchor(record)
    return {"path": str(path), "record": record, "raw": raw, "days": days,
        "anchor": anchor, "anchor_proof": anchor_proof}


def _publication(path, calendar, qualification, now, cache):
    path, record = _record(path)
    if (record.get("schema") != PUBLICATION_SCHEMA or record.get("protocol") != PROTOCOL
            or record.get("config") != _config(100000, 5).to_dict()
            or record.get("holding_policy") != HOLDING_POLICY
            or record.get("calendar_sha256") != digest(calendar)
            or Path(record["calendar"]).resolve() != Path(calendar).resolve()
            or record.get("ipo_qualification") != qualification.verification
            or any(record.get(k) != value for k, value in PUBLICATION_FLAGS.items())
            or record.get("publication_origin") != "local_recording_now"
            or record.get("publication_time_backfilled") is not False
            or record.get("execution_day_rolled_forward") is not False
            or record.get("successor_raw_used") is not False):
        raise ValueError("Unchanged frozen two-case unbound publication and qualification proof required")
    sessions, previous = _sessions(calendar)
    day = record["source_close_day"]
    execution = sessions[sessions.index(day) + 1]
    stamp = _aware(record["recorded_at"])
    if (record.get("proposed_execution_day") != execution
            or record.get("next_open_deadline") != execution + "T09:30:00+08:00"
            or stamp > now):
        raise ValueError("Actual publication clock or original exact next session changed")
    bindable = (record.get("status") == "close_research_target_publication_only"
        and record.get("execution_binding_pending") is False and stamp.astimezone(MARKET_TZ).date().isoformat() == day
        and _aware(day + "T15:05:00+08:00") <= stamp < _aware(execution + "T09:30:00+08:00"))
    if not bindable:
        return {"day": day, "execution": execution, "unbindable": True, "path": str(path)}

    def loaded(batch):
        key = str(Path(batch).resolve())
        if key not in cache:
            cache[key] = _load(batch, calendar)
        return cache[key]

    source = record["source"]
    current, raw, days, original_sessions, _, actual, anchor = loaded(source["batch_run"])
    _, score_record = _record(source["batch_run"])
    contract = _model_contract(score_record)
    if (source != actual or original_sessions != sessions or score_record["score_dates"] != [day]
            or day != days[-1] or current.date.unique().tolist() != [day]
            or contract != record["frozen_model_contract"]):
        raise ValueError("Publication must retain its exact single-day dated scores/model/source")
    prefixes, dates = [], []
    saved_prefix = record["capacity_prefix"]
    if saved_prefix.get("old_scores_recomputed") is not False or saved_prefix.get("capacity_reset") is not False:
        raise ValueError("Frozen capacity prefix cannot be recomputed or reset")
    for proof in saved_prefix["batches"]:
        selected, _, old_days, old_sessions, _, actual_prefix, old_anchor = loaded(proof["batch_run"])
        _, old_score = _record(proof["batch_run"])
        if (proof != actual_prefix or _model_contract(old_score) != contract or old_sessions != sessions
                or old_days != days[:len(old_days)] or old_anchor != anchor
                or proof["source_proofs"] != source["source_proofs"][:len(old_days)]
                or max(old_score["score_dates"]) >= day or set(dates).intersection(old_score["score_dates"])):
            raise ValueError("Saved score prefix must retain exact prior model/source/date identity")
        dates.extend(old_score["score_dates"])
        prefixes.append(selected)
    if sorted(dates) != days[:-1] or sorted(dates) != saved_prefix["score_dates"]:
        raise ValueError("Capacity prefix has an overlap, gap or future score day")
    prefix = pd.concat(prefixes, ignore_index=True).sort_values(["date", "score", "symbol"],
        ascending=[True, False, True], kind="stable").reset_index(drop=True)
    if prefix.to_dict("records") != saved_prefix["top5_events"]:
        raise ValueError("Historical top-five identities changed")
    terminals = [_aware(p.get("source_completed_at") or p["source_updated_at"]) for p in source["source_proofs"]]
    if (record["source_ready_at"] != max(terminals).isoformat()
            or record["provider_ready_at"] != source["source_available_at"]
            or record["score_computed_at"] != source["score_computed_at"]
            or not max(terminals) <= _aware(source["source_available_at"]) <= _aware(source["score_computed_at"]) <= stamp):
        raise ValueError("Source/provider/score must truly precede the actual published target")
    base = ExactSourceRawFeed(raw, days, sorted(current.symbol.unique()), context_anchor=anchor)
    if len(record["cases"]) != 2 or [c["case"] for c in record["cases"]] != [c["id"] for c in PROTOCOL["cases"]]:
        raise ValueError("Exactly both original capacity cases required")
    intents = {}
    for saved, case in zip(record["cases"], PROTOCOL["cases"]):
        expected = _prepare_case(current, prefix, base, day, previous, qualification, case)
        if any(saved.get(key) != value for key, value in expected.items()):
            raise ValueError("Published candidates, cumulative cap, qualification or raw weights changed")
        metadata = {"decided_at": record["recorded_at"], "execution_not_before": execution,
            "source": "research:recent_alpha158_dated_target_publication:" + case["id"]}
        intent = (PortfolioIntent.no_change(";".join(expected["reasons"]), **metadata) if expected["reasons"] else
            PortfolioIntent.full_target(expected["raw_rule_weights_before_qualification"], **metadata))
        if saved["portfolio_intent"] != intent.to_dict() or saved["target_weights"] != intent.to_legacy_pending():
            raise ValueError("Published typed intent changed or was already account-bound")
        intents[case["id"]] = intent
    return {"day": day, "execution": execution, "record": record, "source": source, "raw": raw,
        "anchor": anchor, "contract": contract, "intents": intents, "path": str(path), "sha256": digest(path),
        "reference": {"day": day, "file": str(path), "sha256": digest(path), "recorded_at": record["recorded_at"],
            "execution_day": execution, "batch_run": source["batch_run"], "batch_sha256": source["batch_sha256"]}}


class PublishedRawFeed(IpoQualifiedResearchFeed):
    def signal_frame(self, day, config, context):
        return SignalUnavailable("no_real_close_publication_never_manufacture_ranked_signal")


def _source_prefix(provider, through):
    return {"through": through, "sources": [deepcopy(p) for p in provider["record"]["source_proofs"] if p["day"] <= through],
        "raw": [{"day": day, "sha256": _frame_hash(provider["raw"].loc[provider["raw"].date.eq(day)])}
            for day in provider["days"] if day <= through]}


def _quality(state, publication, base, previous, qualification, case_id=None, *, require_successor=False):
    day, execution = publication["day"], publication["execution"]
    held = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
    pending = set(state.get("pending_targets") or {})
    intents = publication["intents"].values() if case_id is None else [publication["intents"][case_id]]
    current = {s for intent in intents for s, w in intent.weights if w > 0}
    members = held | pending | current
    dates = (day, execution) if require_successor else (day,)
    issues = [issue for d in dates for issue in _raw_issues(base, d, members)]
    refs = [_reference(base, d, s, previous) for d in dates for s in sorted(members)]
    cutoffs = [(day, "T09:30:00+08:00"), (day, "T15:05:00+08:00")]
    if require_successor:
        cutoffs.append((execution, "T09:30:00+08:00"))
    ipo = [qualification.assess(s, d, decision_as_of=d + clock) for d, clock in
        cutoffs for s in sorted(members)]
    # The opening D cutoff concerns previous pending/held positions, not the
    # newly published D close weights.  E has the actual entry cutoff.
    ipo = [v for v in ipo if v["decision_as_of"] != day + "T09:30:00+08:00" or v["symbol"] in held | pending]
    if issues or any(r["status"] != "consistent_proxy" for r in refs) or any(r["engine_block_reason"] is not None for r in ipo):
        return {"reason": "required_raw_reference_or_listing_evidence_unresolved", "day": day,
            "execution_day": execution, "raw_issues": issues, "reference_evidence": refs, "IPO_evidence": ipo,
            "session_not_committed": True}
    return None


def _waiting(reason, **facts):
    return {"schema": RESULT_SCHEMA, "status": reason, "case_count": 0, "account_reset": False,
        "research_checkpoint_created": False, "research_ledger_mutated": False,
        "orders_generated": False, "account_created": False, "research_accounts_created": 0, **facts, **FLAGS}


def run(publications, provider_view, calendar, ipo_evidence, research_master_run, run_dir, *, through=None):
    """Continue the same two books; publications are an append-only full list."""
    now, run_dir = _now(), Path(run_dir).resolve()
    sessions, previous = _sessions(calendar)
    qualification = load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    if list(qualification.calendar) != sessions:
        raise ValueError("Listing qualification and source calendar differ")
    cache = {}
    published = [_publication(p, calendar, qualification, now, cache) for p in publications]
    if not published:
        return _waiting("waiting_for_real_close_publication")
    published.sort(key=lambda p: p["day"])
    days = [p["day"] for p in published]
    if days != sorted(set(days)) or [d for d in sessions if days[0] <= d <= days[-1]] != days:
        raise ValueError("Publication decisions must form one exact consecutive prefix, without duplicates or gaps")
    if any(p.get("unbindable") for p in published):
        return _waiting("waiting_unbindable_original_publication_no_date_or_execution_roll", decision_days=days)
    provider = _provider(provider_view, calendar, now)
    for pub in published:
        n = len(pub["source"]["source_proofs"])
        if (pub["source"]["source_proofs"] != provider["record"]["source_proofs"][:n]
                or pub["anchor"] != provider["anchor"] or pub["contract"] != published[0]["contract"]
                or _frame_hash(pub["raw"]) != _frame_hash(provider["raw"].loc[provider["raw"].date.le(pub["day"])])):
            raise ValueError("Execution witness changed the ranked publication's past raw/source/model prefix")
    for before, after in zip(published, published[1:]):
        if before["source"] not in after["record"]["capacity_prefix"]["batches"]:
            raise ValueError("New publication did not preserve the consumed dated batch in its causal capacity prefix")
    config = _config(100000, 5)
    binding = {"protocol": PROTOCOL, "config": config.to_dict(), "holding_policy": HOLDING_POLICY,
        "calendar": str(Path(calendar).resolve()), "calendar_sha256": digest(calendar),
        "frozen_model_contract": published[0]["contract"], "ipo_qualification": qualification.verification,
        "context_anchor": provider["anchor_proof"], "run_directory": str(run_dir)}
    path = run_dir / "head.json"
    head = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    is_new = head is None
    if head is None and run_dir.exists():
        raise FileExistsError("Existing directory without a valid head is not a new/resettable research run")
    if head is not None:
        if head.get("schema") != SCHEMA or head.get("binding") != binding or head.get("execution_admitted") is not False:
            raise ValueError("Saved two-case config/model/qualification/account binding changed")
        if head["publications"] != [p["reference"] for p in published[:len(head["publications"])]]:
            raise ValueError("Consumed publication prefix disappeared or changed; no backfill or reset")
        if _source_prefix(provider, head["source_prefix"]["through"]) != head["source_prefix"]:
            raise ValueError("Previously consumed exact source/raw prefix changed")
    cutoff = through or days[-1]
    if cutoff not in sessions or cutoff < days[0]:
        raise ValueError("Through must be an exact pinned session on/after the first publication")
    symbols = sorted({s for p in published for intent in p["intents"].values() for s, _ in intent.weights})
    if head:
        symbols = sorted(set(symbols) | {s for case in head["cases"].values() for s in case["state"]["positions"]})
    base = ExactSourceRawFeed(provider["raw"], sessions, symbols, context_anchor=provider["anchor"])
    feed = PublishedRawFeed(base, qualification)
    first = published[0]
    if head is None:
        if first["execution"] not in provider["days"]:
            return _waiting("waiting_for_exact_successor_complete_source_close", first_decision_day=first["day"],
                required_successor_day=first["execution"])
        empty = {"positions": {}, "pending_targets": None}
        issue = _quality(empty, first, base, previous, qualification, require_successor=True)
        if issue:
            return _waiting("waiting_for_required_execution_quality_evidence", blocking_evidence=issue)
    elif any(case["state"]["day"] and cutoff < case["state"]["day"] for case in head["cases"].values()):
        raise ValueError("Cannot move a persistent replay behind its processed decision day")
    # All read-only source/publication/first-execution gates precede mkdir,
    # lock, new_state and the first persistent account checkpoint.
    run_dir.mkdir(parents=True, exist_ok=True)
    with lock(run_dir / "runner.lock"):
        if head is not None:
            if json.loads(path.read_text(encoding="utf-8")) != head:
                raise ValueError("Checkpoint changed during read; rerun against its current prefix")
        elif path.exists():
            raise ValueError("A concurrent runner already created the checkpoint")
        if head is None:
            head = {"schema": SCHEMA, "registered_at": _now().isoformat(), "binding": binding,
                "first_decision_day": first["day"], "publications": [],
                "source_prefix": _source_prefix(provider, first["execution"]),
                "cases": {case["id"]: {"context": RunContext("recent-alpha158-forward:" + _hash(str(run_dir))[:16]
                    + ":" + case["id"], TradingMode.REPLAY).to_dict(), "state": new_state(config.initial_cash),
                    "daily": [], "journal": []} for case in PROTOCOL["cases"]}, **FLAGS}
            write_json(path, head)
        if set(head["cases"]) != {c["id"] for c in PROTOCOL["cases"]}:
            raise ValueError("Exactly the original two persistent research Ledgers required")
        for key, case in head["cases"].items():
            context = RunContext("recent-alpha158-forward:" + _hash(str(run_dir))[:16] + ":" + key, TradingMode.REPLAY)
            processed = [r["date"] for r in case["daily"]]
            if (case["context"] != context.to_dict() or case["state"].get("broker_binding")
                    or processed != days[:len(processed)] or case["state"]["day"] != (processed[-1] if processed else None)
                    or (processed and round(case["daily"][-1]["equity"] * 100) != Ledger(case["state"], config, context).equity())
                    or case["state"].get("replay_risk_exit_policy") is not None):
                raise ValueError("Saved account/cursor/equity/holding policy changed; no reset or skipped day")
        completed = 0
        for index, pub in enumerate(published):
            if pub["day"] > cutoff or pub["day"] not in provider["days"]:
                break
            witness_through = pub["execution"] if index == 0 else pub["day"]
            attempt = {"day": pub["day"], "publication": pub["reference"],
                "source_prefix": _source_prefix(provider, witness_through)}
            changed = False
            for key, case in head["cases"].items():
                if len(case["daily"]) != index or case.get("failed_attempt") == attempt:
                    continue
                issue = _quality(case["state"], pub, base, previous, qualification, key, require_successor=index == 0)
                if issue:
                    case["blocking_evidence"] = issue
                    changed = True
                    continue
                trial = deepcopy(case["state"])
                context = RunContext.from_dict(case["context"])
                intent = replace(pub["intents"][key], context=context)
                try:
                    row = advance(trial, feed, pub["day"], config, index, context=context,
                        published_close_intent=intent)
                except (ValueError, KeyError) as error:
                    failure = {**attempt, "error": f"{type(error).__name__}: {error}", "trial_committed": False}
                    write_json(run_dir / "failed_attempts" / (pub["day"] + "_" + key + "_" + _hash(attempt)[:16] + ".json"), failure)
                    case.update(error=failure["error"], failed_attempt=attempt)
                    changed = True
                    continue
                case.update(state=trial, error=None, blocking_evidence=None)
                case.pop("failed_attempt", None)
                case["daily"].append(row)
                case["journal"].append({"decision_day": pub["day"], "execution_day": pub["execution"],
                    "publication": pub["reference"], "unbound_original_intent": pub["intents"][key].to_dict(),
                    "bound_intent": intent.to_dict(), "committed_at": _now().isoformat(),
                    "retrospective_successor_quality_witness": index == 0,
                    "next_execution_raw_required_for_this_close": index == 0,
                    "source_used_for_ranking_or_refill": False})
                completed += 1
                changed = True
            if changed:
                if len(head["publications"]) <= index:
                    head["publications"].append(pub["reference"])
                if witness_through > head["source_prefix"]["through"]:
                    head["source_prefix"] = _source_prefix(provider, witness_through)
                write_json(path, head)
        cases = [{"case": key, "processed_decision_days": [r["date"] for r in case["daily"]],
            "last_processed_day": case["state"]["day"], "orders": len(case["state"]["orders"]),
            "fills": len(case["state"]["fills"]), "pending_execution_day":
                (case["state"].get("pending_published_close_intent") or {}).get("execution_not_before"),
            "blocking_evidence": case.get("blocking_evidence"), "error": case.get("error"),
            "metrics": None} for key, case in head["cases"].items()]
        blocked = any(c["error"] or c["blocking_evidence"] for c in cases)
        status = ("advanced_research_only_with_blocked_cases" if completed and blocked else
            "advanced_research_only" if completed else "blocked_or_waiting_research_only" if blocked else
            "waiting_for_next_real_close_publication")
        next_publications = sorted({sessions[sessions.index(c["last_processed_day"]) + 1]
            for c in cases if c["last_processed_day"] and sessions.index(c["last_processed_day"]) + 1 < len(sessions)})
        return {"schema": RESULT_SCHEMA, "status": status,
            "case_count": 2, "cases": cases, "newly_committed_case_sessions": completed,
            "checkpoint": str(path), "research_checkpoint_created": True,
            "research_ledger_mutated": bool(completed), "account_reset": False,
            "research_accounts_created": 2 if is_new else 0, "user_account_created": False,
            "next_real_close_publication_days_required": next_publications,
            "holding_policy": HOLDING_POLICY, "orders_generated": any(c["orders"] for c in cases),
            "whole_session_commit_requires_own_real_close_publication": True,
            "metrics": None, "metrics_semantics": "unfinished_append_only_conditional_raw_research_trace_not_validated_total_return", **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, action="append", required=True)
    parser.add_argument("--provider-view", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--ipo-evidence", type=Path, required=True)
    parser.add_argument("--research-master-run", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--through")
    args = parser.parse_args(argv)
    result = run(args.publication, args.provider_view, args.calendar, args.ipo_evidence,
        args.research_master_run, args.run_dir, through=args.through)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
