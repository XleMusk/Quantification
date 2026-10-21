"""Consume actual fixed twenty-case close publications in isolated REPLAY books.

Each dated publication owns its saved input and original unbound intent. The
current bundle supplies execution observations, never replacement candidates.
The registered 40-decision/6-tail calendar and initial cohorts cannot roll.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path

from ..contracts import IntentKind, MARKET_TZ, PortfolioIntent, RunContext, SignalUnavailable, TradingMode
from ..engine import ReplayRiskExitIntent, advance
from ..ledger import Ledger, new_state
from ..market_dataset import write_json
from ..report import metrics
from ..worker import lock
from .baostock_daily_slice import digest
from .leader_close_publication import (SCHEMA as PUBLICATION_SCHEMA, _stamp,
    load_fixed_cohort_registration, prepare_close_case_targets)
from .leader_close_publication_view import _boundaries
from .leader_daily_replay import EXIT_POLICIES, FAMILIES, POOLS, CommonCohortDecisionFeed
from .leader_daily_signals import _calendar_view
from .leader_forward_run import (TAIL_SESSIONS, WINDOW_SESSIONS, ForwardRawFeed,
    _calendar as _window_calendar, _case_key, _check_feature_binding,
    _check_source_binding, _history_available_for_decision, _ipo_proof,
    _prefix, _protocol, _scoped, _validate_bundle)
from .leader_published_inputs import input_proof, load_bundle
from .shortline_decision_v2 import MAX_HOLDING_SESSIONS, fixed_exit_policy, holding_origin, verified_breadth
from .shortline_decision_v3 import FiveSessionExpiryPolicy, POLICY_ID as ORIGINAL_RISK_ID, _known_raw

SCHEMA = "leader_published_close_shared_ledger_checkpoint_v1"
RESULT_SCHEMA = "leader_published_close_forward_result_v1"
TARGET_POLICY_ID = "leader_published_close_fixed_expiry_reduction_v1"
RISK_POLICY_ID = "leader_published_close_five_session_expiry_queue_v1"
RISK_RULE_SPEC = {"id": RISK_POLICY_ID, "original_policy_id": ORIGINAL_RISK_ID,
    "max_holding_open_sessions": MAX_HOLDING_SESSIONS,
    "age_origin": "first_actual_buy_fill_of_current_continuous_holding",
    "availability_contract": "known_D_close_exact_calendar_E_queue_then_validate_E_before_execution",
    "successor_raw_required_to_queue": False, "future_raw_fabricated": False,
    "original_historical_policy_modified": False}
CASE_IDS = tuple(_case_key(p, f, e) for p in POOLS for f in FAMILIES for e in EXIT_POLICIES)
DEFAULT_REGISTRATION = Path(__file__).resolve().parents[2] / "outputs/system_landing/20261015_leader_published_forward_v1/registration.json"
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "current_signal_admitted": False,
    "model_refit": False, "future_labels_used_for_score": False, "provider_contacted": False,
    "broker_interacted": False, "ths_payload_generated": False, "user_account_mutated": False,
    "pit_verified": False, "corporate_actions_verified": False}


def _now():
    return datetime.now(MARKET_TZ)


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _waiting(status, **facts):
    return {"schema": RESULT_SCHEMA, "status": status, "case_count": 0,
        "research_checkpoint_created": False, "research_ledger_mutated": False,
        "research_accounts_created": 0, "account_created": False, "account_reset": False,
        "orders_generated": False, "metrics": None, **facts, **FLAGS}


def _load(path, binding, calendar, now):
    path = Path(path).resolve()
    before = digest(path)
    bundle = load_bundle(path)
    proof = input_proof(path)
    raw, days = _validate_bundle(bundle)
    _check_feature_binding(bundle, binding)
    _check_source_binding(bundle, binding)
    if (proof["sha256"] != before or digest(path) != before
            or bundle.get("calendar_payload") != calendar or _stamp(proof["recorded_at"]) > now):
        raise ValueError("Own saved input/calendar/actual recording changed")
    year = calendar["year"]
    sessions, previous, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
    if (status != "pinned_static_exchange_calendar" or not set(days) <= set(sessions)
            or [d for d in sessions if days[0] <= d <= days[-1]] != days
            or str(raw.date.max()) != days[-1]):
        raise ValueError("Complete dated inputs require an exact consecutive exchange-session prefix")
    packages = bundle.get("source_packages", [])
    package_days = [p["day"] for p in packages]
    historical_days = (binding.get("history_source_view") or {}).get("view_sessions", [])
    # The provider includes frozen recent-history packages even when only D
    # was requested for decisions. They remain source evidence, never extra
    # decision days. Appending requests keeps every prior request and role.
    if (package_days != sorted(set(package_days))
            or package_days != sorted(set(historical_days) | set(days))):
        raise ValueError("Requested close days and frozen history-prefix package proofs differ")
    if historical_days and any(p.get("roles") !=
            (["recent_history_prefix"] if p["day"] in historical_days else [])
            + (["requested_current"] if p["day"] in days else []) for p in packages):
        raise ValueError("Source package history/request roles differ from their exact saved scope")
    return {"bundle": bundle, "raw": raw, "days": days, "proof": proof,
        "sessions": sessions, "previous": previous}


def _scopes(loaded, cohort, end):
    result = {pool: _scoped(loaded["bundle"], symbols, end)[0]
        for pool, symbols in cohort["pools"].items()}
    for source in result.values():
        for batches in source.values():
            for batch in batches.values():
                candidates = batch.get("candidates", [])
                if len({r["symbol"] for r in candidates}) != len(candidates):
                    raise ValueError("The fixed at-most-five candidate list must also be unique")
    return result


def _lineage(own, latest, cohort, end):
    day = own["days"][-1]
    if (own["days"] != latest["days"][:len(own["days"])]
            or own["bundle"]["source_packages"] != [p for p in latest["bundle"]["source_packages"] if p["day"] <= day]
            or _prefix(own["raw"], _scopes(own, cohort, end), day)
                != _prefix(latest["raw"], _scopes(latest, cohort, end), day)
            or own["bundle"].get("source_metadata", {}).get("history_metadata")
                != latest["bundle"].get("source_metadata", {}).get("history_metadata")):
        raise ValueError("Published own raw/candidate/coverage/source prefix changed")


def _registration(path, first_path, first_input, binding, now):
    path = Path(path).resolve()
    before = digest(path)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    cohort, proof = load_fixed_cohort_registration(path, binding, first_input["raw"], now)
    original = value["original_publication"]
    initial = value.get("initial_publication_input_bundle")
    if (Path(original["file"]).resolve() != Path(first_path).resolve()
            or value.get("window") != {"length_open_sessions": WINDOW_SESSIONS, "execution_tail_sessions": TAIL_SESSIONS}
            or value.get("published_target_policy_id", value.get("target_policy_id")) != TARGET_POLICY_ID
            or value.get("risk_policy_id") != RISK_POLICY_ID
            or (value.get("risk_rule_spec") is not None and value["risk_rule_spec"] != RISK_RULE_SPEC)
            or initial != first_input["proof"]
            or not _stamp(first_input["proof"]["recorded_at"]) <= _stamp(value["registered_at"]) <= now
            or any(value.get(k) is not v for k, v in FLAGS.items()) or digest(path) != before):
        raise ValueError("Actual twenty-case forward method/input registration changed")
    return cohort, proof


def _publication(path, own, *, cohort, registration, binding, calendar, config, qualification, window, now, first):
    path = Path(path).resolve()
    before = digest(path)
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    _boundaries(record)
    day = record["source_close_day"]
    if day not in window[:-1]:
        raise ValueError("Publication is outside the original forty plus six session window")
    execution = window[window.index(day) + 1]
    stamp, deadline = _stamp(record["recorded_at"]), _stamp(execution + "T09:30:00+08:00")
    if (record.get("schema") != PUBLICATION_SCHEMA or record.get("case_count") != 20
            or [r["case_id"] for r in record["cases"]] != list(CASE_IDS)
            or record.get("protocol_binding") != binding or record.get("ledger_checkpoint") is not None
            or record.get("ipo_qualification") != qualification.verification
            or record.get("feature_qualification") != own["bundle"].get("feature_qualification")
            or record.get("candidate_panel_only") is not True
            or record.get("publication_at") != record["recorded_at"]
            or record.get("timeliness_uses_actual_recording") is not True
            or record.get("publication_time_backfilled") is not False
            or record.get("execution_day_rolled_forward") is not False
            or record.get("successor_raw_used") is not False
            or record.get("successor_raw_required_for_target_weights") is not False
            or own["days"][-1] != day or str(own["raw"].date.max()) != day
            or record.get("proposed_execution_day") != execution
            or _stamp(record["next_open_deadline"]) != deadline
            or stamp > now or stamp.date().isoformat() != day
            or not _stamp(day + "T15:05:00+08:00") <= stamp < deadline
            or record.get("status") != "close_research_target_publication_only"
            or record.get("execution_binding_pending") is not False):
        raise ValueError("Actual unbound close publication/clock/exact E/fixed cases changed or is late")
    summary = {"anchor": cohort["anchor"], "selection": cohort["selection"],
        "counts": {pool: len(symbols) for pool, symbols in cohort["pools"].items()}}
    if record.get("cohort") != summary:
        raise ValueError("Published cohort differs from the original fixed initial membership")
    tail = window.index(day) >= WINDOW_SESSIONS
    if not first and (record.get("frozen_cohort_registration") != registration
            or record.get("publication_input_bundle") != own["proof"]
            or record.get("registered_window_tail") is not tail):
        raise ValueError("Later publications must pin this same frozen cohort registration and own saved input")
    if record.get("publication_input_bundle") is not None and (record["publication_input_bundle"] != own["proof"]
            or _stamp(own["proof"]["recorded_at"]) > stamp):
        raise ValueError("Publication input proof differs or was recorded after the publication")
    source = [p for p in own["bundle"]["source_packages"] if p["day"] == day]
    if len(source) != 1 or record.get("source_terminal_completion") != source[0]:
        raise ValueError("Publication no longer matches its own completed close source")
    ready = [_stamp(source[0]["terminal_completion_observed_at"])]
    history = binding.get("history_source_view")
    if history is not None:
        ready += [_stamp(history["available_at"]), _stamp(binding["history_source_specified_at"])]
        if (record.get("recent_source_history") != history
                or record.get("history_input_available_at") != history["available_at"]
                or record.get("history_source_specified_at") != binding["history_source_specified_at"]):
            raise ValueError("Publication historical source availability changed")
    if (ready[0] < _stamp(day + "T15:00:00+08:00") or max(ready) > stamp
            or (record.get("source_ready_at") is not None and _stamp(record["source_ready_at"]) != max(ready))):
        raise ValueError("Publication precedes actual source completion or its original input availability")
    _history_available_for_decision(binding, day)
    prepared = prepare_close_case_targets(own["bundle"], cohort=cohort, head=None, day=day,
        calendar=calendar, config=config, ipo_qualification=qualification, decision_tail=tail)
    intents = {}
    for row, expected in zip(record["cases"], prepared):
        expected = deepcopy(expected)
        weights, context = expected.pop("weights"), expected.pop("context")
        if context is not None or any(row.get(k) != v for k, v in expected.items()):
            raise ValueError("Original publication candidate/coverage/qualification/decision facts changed")
        intent = PortfolioIntent.from_dict(row["portfolio_intent"])
        wanted = (PortfolioIntent.no_change(expected["no_change_reason"], decided_at=record["recorded_at"])
            if weights is None else PortfolioIntent.full_target(weights, decided_at=record["recorded_at"],
                execution_not_before=execution, source="research:leader_close_publication:" + row["family"]))
        status = "unavailable_fixed_rule_target" if weights is None else "unbound_pure_rule_target_preview"
        if (intent != wanted or row.get("target_weights") != dict(wanted.weights)
                or row.get("target_cash_weight") != (None if weights is None else 1 - sum(weights.values()))
                or row.get("status") != status or row.get("holdings_retention_and_expiry_account_bound") is not False):
            raise ValueError("Original unbound intent/weights/NO_CHANGE/cash semantics changed")
        intents[row["case_id"]] = intent
    if digest(path) != before:
        raise ValueError("Publication changed during read")
    return {"day": day, "execution": execution, "record": record, "intents": intents, "own": own,
        "tail": tail, "reference": {"day": day, "file": str(path), "sha256": before,
            "recorded_at": record["recorded_at"], "execution_day": execution,
            "publication_input_bundle": own["proof"], "input_snapshot_was_original_publication_attachment": record.get("publication_input_bundle") is not None}}


class PublishedFiveSessionExpiryPolicy(FiveSessionExpiryPolicy):
    """Same actual-fill age rule; queue exact E without inspecting E raw."""
    policy_id = RISK_POLICY_ID

    def __call__(self, day, book, feed):
        if book.context.mode != TradingMode.REPLAY:
            raise ValueError("Published expiry queue is REPLAY-only")
        indices = {d: i for i, d in enumerate(feed.calendar)}
        held = {s for s in book.state["positions"] if book.quantity(s)}
        expired = []
        for symbol in sorted(held):
            origin = holding_origin(book.state, symbol)
            if origin not in indices or indices[origin] > indices[day]:
                raise ValueError("Expiry requires the continuous holding's actual Ledger buy-fill session")
            if indices[day] - indices[origin] + 1 >= MAX_HOLDING_SESSIONS:
                expired.append(symbol)
        if not expired:
            return None
        if indices[day] + 1 >= len(feed.calendar):
            return SignalUnavailable("published_expiry_exact_next_session_unavailable")
        required = held | set(book.state.get("pending_targets") or {})
        if any(not _known_raw(feed.rows.get(day, {}).get(s)) for s in required):
            return SignalUnavailable("published_expiry_current_raw_or_status_unavailable")
        return ReplayRiskExitIntent(book.context, day, feed.calendar[indices[day] + 1], tuple(expired),
            "research:leader_published_close_five_session_expiry", "fixed_five_session_expiry_queue_E_quality_checked_before_execution")


class PublishedFixedExpiryReduction:
    policy_id = TARGET_POLICY_ID

    def __init__(self, feed, state, config):
        self.reduce = fixed_exit_policy(feed, state, config)

    def __call__(self, intent):
        return self.reduce(intent)


def _quality(state, pub, base, previous, qualification, case_id=None, *, successor=False):
    held = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
    pending = set(state.get("pending_targets") or {}) | set((state.get("pending_risk_exits") or {}).get("symbols", []))
    intents = pub["intents"].values() if case_id is None else [pub["intents"][case_id]]
    current = {s for intent in intents for s, w in intent.weights if w > 0}
    members, issues = held | pending | current, []
    dates = [pub["day"], pub["execution"]] if successor else [pub["day"]]
    for day in dates:
        for symbol in sorted(members):
            row, prior = base.rows.get(day, {}).get(symbol), base.rows.get(previous.get(day), {}).get(symbol)
            reason = None
            if not _known_raw(row): reason = "required_current_raw_status_or_geometry_unknown"
            elif not prior or not _known_raw(prior): reason = "exact_previous_raw_reference_unknown"
            elif abs(row["preclose"] / prior["close"] - 1) > .01: reason = "raw_reference_incompatible_proxy"
            cutoffs = []
            if successor and day == pub["execution"] or symbol in held | pending:
                cutoffs.append(day + "T09:30:00+08:00")
            if day == pub["day"] and symbol in current:
                cutoffs.append(pub["record"]["recorded_at"])
            proofs = [qualification.assess(symbol, day, decision_as_of=cutoff) for cutoff in cutoffs]
            if reason or any(proof["engine_block_reason"] is not None for proof in proofs):
                issues.append({"day": day, "symbol": symbol, "reason": reason,
                    "IPO_evidence": proofs, "absence_of_company_actions_proven": False})
    return ({"reason": "required_raw_reference_or_listing_evidence_unresolved", "day": pub["day"],
        "issues": issues, "session_not_committed": True} if issues else None)


def _case_summary(key, case, window, initial_cash):
    """Report only complete eligible books; retain residual/pending state."""
    state, daily = case["state"], case["daily"]
    dates = [row["date"] for row in daily]
    finished = (dates == window[:WINDOW_SESSIONS + TAIL_SESSIONS]
        and state["day"] == window[WINDOW_SESSIONS + TAIL_SESSIONS - 1])
    effective_target = any(row.get("batch_status") == "complete"
        and row.get("original_publication_kind") == IntentKind.FULL_TARGET.value
        and row.get("registered_tail_no_new_decision") is not True for row in daily[:WINDOW_SESSIONS])
    defense_available = not key.endswith(EXIT_POLICIES[1]) or case["verified_breadth_seen"] is True
    reason = ("case_error_or_unresolved_execution_evidence" if case.get("error") or case.get("blocking_evidence") else
        "verified_full_market_breadth_unavailable" if not defense_available else
        "exact_40_plus_6_session_window_incomplete" if not finished else
        "no_effective_FULL_TARGET_in_original_40_session_window" if not effective_target else
        "no_actual_simulated_fills" if not state["fills"] else None)
    main = metrics(daily[:WINDOW_SESSIONS], initial_cash) if reason is None else None
    with_tail = metrics(daily, initial_cash) if reason is None else None
    return {"case_id": key, "processed_decision_days": dates, "last_processed_day": state["day"],
        "orders": len(state["orders"]), "fills": len(state["fills"]),
        "pending_execution_day": (state.get("pending_published_close_intent") or {}).get("execution_not_before"),
        "pending_risk_execution_day": (state.get("pending_risk_exits") or {}).get("execution_on"),
        "open_positions": {symbol: sum(lot["qty"] for lot in lots) for symbol, lots in state["positions"].items()
            if sum(lot["qty"] for lot in lots)},
        "pending_target_weights": deepcopy(state.get("pending_targets")),
        "pending_risk_exits": deepcopy(state.get("pending_risk_exits")),
        "exact_fixed_window_completed": finished, "performance_available": reason is None,
        "performance_unavailable_reason": reason,
        "status": ("blocked_source_or_engine" if case.get("error") or case.get("blocking_evidence") else
            "unavailable_verified_full_market_breadth" if not defense_available else
            "complete_fixed_window_research_only" if finished and reason is None else
            "complete_fixed_window_without_measurable_performance" if finished else "accumulating_fixed_window"),
        "blocking_evidence": case.get("blocking_evidence"), "error": case.get("error"),
        "metrics": main, "decision_window_metrics": main, "including_execution_tail_metrics": with_tail,
        "metrics_semantics": "original_raw_price_simulation_with_costs_not_verified_total_return_or_liquidated_return"}


def _append_case_journal(directory, key, case, entry):
    """Persist the same content-addressed session receipt for published consumers."""
    journal = Path(directory) / "journal" / _hash(key)[:20] / (entry["decision_day"] + "_" + _hash(entry)[:24] + ".json")
    if journal.exists():
        expected_bytes = json.dumps(entry, ensure_ascii=False, indent=2, allow_nan=False).replace("\n", os.linesep).encode("utf-8")
        if journal.read_bytes() != expected_bytes or json.loads(journal.read_text(encoding="utf-8")) != entry:
            raise ValueError("Content-addressed orphan journal cannot be overwritten")
    else:
        write_json(journal, entry)
    case["journal"].append(entry)
    case["journal_files"].append({"file": str(journal), "sha256": digest(journal)})


def _validate_case_journal(case, directory):
    if len(case["journal"]) != len(case["journal_files"]):
        raise ValueError("Independent published session journal length changed")
    for entry, proof in zip(case["journal"], case["journal_files"]):
        target = Path(proof["file"]).resolve()
        target.relative_to(Path(directory).resolve() / "journal")
        if digest(target) != proof["sha256"] or json.loads(target.read_text(encoding="utf-8")) != entry:
            raise ValueError("Independent published session journal changed")
    if case["journal"]:
        for field in ("pending_targets", "pending_published_close_intent", "pending_risk_exits"):
            if case["state"].get(field) != case["journal"][-1]["post_session_" + field]:
                raise ValueError("Pending target/receipt differs from independently sealed journal")


def run(publications, publication_bundles, latest_bundle, run_dir, *, protocol_path, registration=None, through=None):
    now, directory = _now(), Path(run_dir).resolve()
    if not publications:
        return _waiting("waiting_for_real_close_publication")
    if len(publications) != len(publication_bundles):
        raise ValueError("Each actual publication requires its own explicit saved input bundle")
    binding, calendar_payload, config = _protocol(protocol_path)
    qualification = _ipo_proof(binding)
    if qualification is None:
        raise ValueError("Explicit original typed IPO qualification is required")
    loaded = [_load(p, binding, calendar_payload, now) for p in publication_bundles]
    pairs = sorted(zip(publications, loaded), key=lambda p: p[1]["days"][-1])
    first_path, first_input = pairs[0]
    cohort, registered = _registration(registration or DEFAULT_REGISTRATION, first_path, first_input, binding, now)
    start = first_input["days"][-1]
    window = _window_calendar(calendar_payload, start)
    latest = _load(latest_bundle, binding, calendar_payload, now)
    sessions, previous = latest["sessions"], latest["previous"]
    if tuple(qualification.calendar) != tuple(sessions):
        raise ValueError("IPO qualification and source calendars differ")
    published = [_publication(path, own, cohort=cohort, registration=registered, binding=binding,
        calendar=sessions, config=config, qualification=qualification, window=window, now=now, first=i == 0)
        for i, (path, own) in enumerate(pairs)]
    days = [p["day"] for p in published]
    if days != window[:len(days)]:
        raise ValueError("Actual publications must be the exact original consecutive decision/tail prefix")
    for pub in published:
        _lineage(pub["own"], latest, cohort, window[WINDOW_SESSIONS - 1])
    scoped = _scopes(latest, cohort, window[WINDOW_SESSIONS - 1])
    bound = {"original_protocol": binding, "config": config.to_dict(), "calendar": calendar_payload,
        "cohort": cohort, "window": window, "registration": registered, "risk_policy": RISK_RULE_SPEC,
        "published_target_policy_id": TARGET_POLICY_ID, "case_ids": list(CASE_IDS), "run_directory": str(directory)}
    path = directory / "head.json"
    head = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    is_new = head is None
    if head is None and directory.exists():
        raise FileExistsError("Existing research directory without head cannot be reset")
    if head is not None:
        if head.get("schema") != SCHEMA or head.get("binding") != bound or any(head.get(k) is not v for k, v in FLAGS.items()):
            raise ValueError("Saved fixed twenty-case registration/account binding changed")
        if head["publications"] != [p["reference"] for p in published[:len(head["publications"])]]:
            raise ValueError("Previously consumed publication prefix disappeared or changed")
        if _prefix(latest["raw"], scoped, head["source_prefix"]["through"]) != head["source_prefix"]:
            raise ValueError("Previously consumed raw/candidate source prefix changed")
    cutoff = through or days[-1]
    if cutoff not in sessions or cutoff < start:
        raise ValueError("Through requires an exact session on/after the original start")
    if head and any(c["state"]["day"] and cutoff < c["state"]["day"] for c in head["cases"].values()):
        raise ValueError("Persistent research cursor cannot move backwards")
    bases = {pool: ForwardRawFeed(latest["raw"], sessions, symbols, ipo_qualification=qualification)
        for pool, symbols in cohort["pools"].items()}
    first = published[0]
    if head is None:
        if first["execution"] not in latest["days"]:
            return _waiting("waiting_for_exact_successor_complete_source_close", first_decision_day=start,
                required_successor_day=first["execution"], forward_method_registration=registered)
        for pool, base in bases.items():
            issue = _quality({"positions": {}, "pending_targets": None},
                {**first, "intents": {k: v for k, v in first["intents"].items() if k.startswith(pool + ":")}},
                base, previous, qualification, successor=True)
            if issue:
                return _waiting("waiting_for_required_execution_quality_evidence", blocking_evidence=issue)
    directory.mkdir(parents=True, exist_ok=True)
    with lock(directory / "runner.lock"):
        if head is not None and json.loads(path.read_text(encoding="utf-8")) != head:
            raise ValueError("Checkpoint changed during read")
        if head is None:
            if path.exists(): raise ValueError("Concurrent runner created the checkpoint")
            head = {"schema": SCHEMA, "registered_at": _now().isoformat(), "binding": bound,
                "publications": [], "source_prefix": _prefix(latest["raw"], scoped, first["execution"]),
                "cases": {}, **FLAGS}
            for key in CASE_IDS:
                context = RunContext("leader-published-forward:" + _hash(str(directory))[:16] + ":" + key, TradingMode.REPLAY)
                head["cases"][key] = {"context": context.to_dict(), "state": new_state(config.initial_cash),
                    "daily": [], "journal": [], "journal_files": [], "verified_breadth_seen": False}
            write_json(path, head)
        if list(head["cases"]) != list(CASE_IDS):
            raise ValueError("Exactly all original twenty cases in the original order are required")
        for key, case in head["cases"].items():
            context = RunContext("leader-published-forward:" + _hash(str(directory))[:16] + ":" + key, TradingMode.REPLAY)
            processed = [r["date"] for r in case["daily"]]
            if (case["context"] != context.to_dict() or case["state"].get("broker_binding")
                    or processed != days[:len(processed)] or case["state"]["day"] != (processed[-1] if processed else None)
                    or len(case["journal"]) != len(processed) or len(case["journal_files"]) != len(processed)
                    or (processed and (round(case["daily"][-1]["equity"] * 100) != Ledger(case["state"], config, context).equity()
                        or case["state"].get("replay_risk_exit_policy") != {"schema": "replay_risk_exit_policy_v1", "policy_id": RISK_POLICY_ID, "context": context.to_dict()}
                        or case["state"].get("published_close_target_policy") != {"schema": "replay_published_close_target_policy_v1", "policy_id": TARGET_POLICY_ID, "context": context.to_dict()}))):
                raise ValueError("Saved case context/cursor/equity/policy changed")
            _validate_case_journal(case, directory)
        completed = 0
        for index, pub in enumerate(published):
            if pub["day"] > cutoff or pub["day"] not in latest["days"]: break
            witness = pub["execution"] if index == 0 else pub["day"]
            attempt = {"day": pub["day"], "publication": pub["reference"],
                "source_prefix": _prefix(latest["raw"], scoped, witness)}
            changed = False
            for key, case in head["cases"].items():
                if len(case["daily"]) != index or case.get("failed_attempt") == attempt: continue
                pool, family, exit_policy = key.split(":")
                issue = _quality(case["state"], pub, bases[pool], previous, qualification, key, successor=index == 0)
                if issue:
                    if case.get("blocking_evidence") != issue: case["blocking_evidence"] = issue; changed = True
                    continue
                trial, context = deepcopy(case["state"]), RunContext.from_dict(case["context"])
                # Published weights already encode complete/unknown breadth.
                # Only the original independent expiry reducer runs post-opening.
                own_batches = _scopes(pub["own"], cohort, window[WINDOW_SESSIONS - 1])[pool][family]
                feed = CommonCohortDecisionFeed(bases[pool], own_batches, trial, market_defense=False)
                original = pub["intents"][key]
                intent = replace(original, context=context, decided_at=pub["record"]["recorded_at"],
                    execution_not_before=pub["execution"], source=original.source or "research:leader_close_publication:" + family)
                try:
                    row = advance(trial, feed, pub["day"], config, index, context=context,
                        published_close_intent=intent, published_close_target_policy=PublishedFixedExpiryReduction(feed, trial, config),
                        replay_risk_exit_policy=PublishedFiveSessionExpiryPolicy())
                except (ValueError, KeyError) as error:
                    failure = {**attempt, "error": f"{type(error).__name__}: {error}", "trial_committed": False}
                    write_json(directory / "failed_attempts" / (pub["day"] + "_" + _hash(key)[:12] + "_" + _hash(attempt)[:16] + ".json"), failure)
                    case.update(error=failure["error"], failed_attempt=attempt); changed = True
                    continue
                batch = own_batches[pub["day"]]
                row.update(batch_status=batch["status"], original_publication_kind=original.kind.value,
                    registered_tail_no_new_decision=pub["tail"])
                case.update(state=trial, error=None, blocking_evidence=None,
                    verified_breadth_seen=case["verified_breadth_seen"] or (batch["status"] == "complete" and verified_breadth(batch) is not None))
                case.pop("failed_attempt", None)
                case["daily"].append(row)
                entry = {"decision_day": pub["day"], "execution_day": pub["execution"], "publication": pub["reference"],
                    "unbound_original_intent": original.to_dict(), "bound_original_intent": intent.to_dict(),
                    "metadata_adapter_preserves_kind_reason_and_weights": True,
                    "published_close_reduction": deepcopy(row.get("published_close_reduction")),
                    "committed_at": _now().isoformat(), "retrospective_successor_quality_witness": index == 0,
                    "successor_used_for_ranking_or_refill": False, "source_features_from_own_publication": True,
                    **{"post_session_" + field: deepcopy(trial.get(field)) for field in
                        ("pending_targets", "pending_published_close_intent", "pending_risk_exits")}}
                _append_case_journal(directory, key, case, entry)
                completed += 1; changed = True
            if changed:
                if len(head["publications"]) <= index: head["publications"].append(pub["reference"])
                if witness > head["source_prefix"]["through"]: head["source_prefix"] = _prefix(latest["raw"], scoped, witness)
                write_json(path, head)
        cases = [_case_summary(key, case, window, config.initial_cash) for key, case in head["cases"].items()]
        blocked = any(c["error"] or c["blocking_evidence"] for c in cases)
        return {"schema": RESULT_SCHEMA, "status": ("advanced_research_only_with_blocked_cases" if completed and blocked else
                "advanced_research_only" if completed else "blocked_or_waiting_research_only" if blocked else "waiting_for_next_real_close_publication"),
            "case_count": 20, "cases": cases, "newly_committed_case_sessions": completed,
            "first_decision_day": start, "decision_window_end": window[WINDOW_SESSIONS - 1], "execution_tail_end": window[-2],
            "checkpoint": str(path), "research_checkpoint_created": True, "research_ledger_mutated": bool(completed),
            "research_accounts_created": 20 if is_new else 0, "user_account_created": False, "account_reset": False,
            "orders_generated": any(c["orders"] for c in cases), "metrics": None,
            "next_real_close_publication_days_required": sorted({window[len(c["processed_decision_days"])] for c in cases
                if len(c["processed_decision_days"]) < WINDOW_SESSIONS + TAIL_SESSIONS}),
            "whole_session_requires_own_real_publication": True,
            "retention_equivalence_scope": "unique_scoped_candidates_at_most_five_holdings_five_retain_rank_twenty_only",
            "metrics_semantics": "per_case_only_exact_40_plus_6_complete_and_filled_no_partial_or_unavailable_returns", **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", action="append", type=Path, required=True)
    parser.add_argument("--publication-bundle", action="append", type=Path, required=True)
    for key in ("bundle", "protocol", "run-dir"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--registration", type=Path, default=DEFAULT_REGISTRATION)
    parser.add_argument("--through")
    args = parser.parse_args(argv)
    result = run(args.publication, args.publication_bundle, args.bundle, args.run_dir,
        protocol_path=args.protocol, registration=args.registration, through=args.through)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
