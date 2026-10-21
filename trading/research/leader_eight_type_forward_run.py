"""Append actual eight-type close publications to 32 isolated research books.

The original publication owns its dated candidates/features. The latest saved
panel supplies execution raw, never a replacement signal. A whole Engine day
requires that day's real publication; no late publication or future bar is
manufactured. All source and initial execution gates precede account creation.
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

from ..contracts import IntentKind, MARKET_TZ, PortfolioIntent, RunContext, TradingMode
from ..engine import advance, choose_targets
from ..ledger import Ledger, new_state
from ..market_dataset import write_json
from ..worker import lock
from .baostock_daily_slice import digest
from .leader_daily_replay import CommonCohortDecisionFeed
from .leader_daily_signals import _calendar_view
from .leader_eight_type_panel_replay import (CAPS, GUARDS, PROTOCOL, TYPE_IDS,
    _QualifiedFeed, _config, _rank_path, load_inputs)
from .leader_eight_type_paper import _finite, _source_bindings, _stamp
from .leader_eight_type_paper_view import read_saved_plan
from .leader_published_risk_exit import (PublishedCloseLeaderTypeRiskExitPolicy,
    POLICY_ID as RISK_POLICY_ID, RULE_SPEC as FORWARD_RISK_SPEC)
from .recent_alpha158_forward_run import _frame_hash
from .recent_alpha158_panel_replay import ExactSourceRawFeed, _raw_issues, _reference
from .shortline_decision_v2 import BATCH_SCHEMA, fixed_exit_policy

SCHEMA = "leader_eight_type_published_close_shared_ledger_checkpoint_v1"
RESULT_SCHEMA = "leader_eight_type_published_close_forward_result_v1"
TARGET_POLICY_ID = "leader_eight_type_published_close_fixed_expiry_reduction_v1"
DEFAULT_REGISTRATION = Path(__file__).resolve().parents[2] / "outputs/system_landing/20261015_eight_type_forward_v1/registration.json"
CASE_IDS = tuple(f"{t}__{g}__{c}" for t in TYPE_IDS for g in GUARDS for c, _ in CAPS)
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "current_signal_admitted": False, "model_refit": False, "future_labels_used_for_score": False,
    "provider_contacted": False, "broker_interacted": False, "ths_payload_generated": False,
    "user_account_mutated": False, "pit_verified": False, "corporate_actions_verified": False}


def _now():
    return datetime.now(MARKET_TZ)


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode()).hexdigest()


def _calendar(loaded):
    value = loaded["calendar_payload"]
    year = value["year"]
    sessions, previous, status = _calendar_view(value, f"{year}-01-01", f"{year}-12-31", [])
    days = loaded["source_days"]
    if (status != "pinned_static_exchange_calendar" or not days or days != sorted(set(days))
            or [d for d in sessions if days[0] <= d <= days[-1]] != days
            or sorted(loaded["raw"].date.unique()) != days
            or tuple(loaded["qualification"].calendar) != tuple(sessions)):
        raise ValueError("Exact complete source prefix and full pinned qualification calendar required")
    return sessions, previous


def _facts(panel):
    """Only the three named derived availability locations may be re-recorded.

    Identity registration, listing decision cutoffs, source clocks and all
    nested evidence remain part of the comparison. Unknown cannot become known.
    """
    value = deepcopy(panel)
    value.pop("available_at", None)
    for item in value["types"]:
        item.pop("available_at", None)
        for candidate in item["candidates"]:
            candidate.pop("available_at", None)
    return value


def _prefix(loaded, through):
    return {"through": through,
        "sources": [deepcopy(p) for p in loaded["provenance"]["source_proofs"] if p["day"] <= through],
        "raw": [{"day": d, "sha256": _frame_hash(loaded["raw"].loc[loaded["raw"].date.eq(d)])}
            for d in loaded["source_days"] if d <= through]}


def _same_lineage(original, latest):
    days = original["source_days"]
    a, b = original["provenance"], latest["provenance"]
    if (days != latest["source_days"][:len(days)]
            or a["source_proofs"] != b["source_proofs"][:len(days)]
            or original["calendar_payload"] != latest["calendar_payload"]
            or original["scope_members"] != latest["scope_members"]
            or original["qualification"].verification != latest["qualification"].verification
            or a["context_anchor"] != b["context_anchor"]
            or original["context_anchor"] != latest["context_anchor"]
            or a["external_fixed_protocol"] != b["external_fixed_protocol"]
            or _frame_hash(original["raw"]) != _frame_hash(latest["raw"].loc[latest["raw"].date.isin(days)])):
        raise ValueError("Published source/raw/scope/calendar/qualification/protocol prefix changed")


def _phase(panel, parameters):
    if panel is None or panel["environment"].get("source_complete") is not True:
        return "unknown"
    value = panel["environment"]
    breadth, median = value.get("breadth"), value.get("median_return_pct")
    if breadth is None or median is None:
        return "unknown"
    if breadth >= parameters["strong_breadth_at_least"] and median > parameters["strong_median_above"]:
        return "broad_strength_snapshot"
    if breadth <= parameters["weak_breadth_at_most"] and median < parameters["weak_median_below"]:
        return "broad_weakness_snapshot"
    return "mixed_breadth_snapshot"


def _environment(record, loaded, sessions, previous):
    """Check the saved rule's result, never import today's TYPE_RULES."""
    env, day = record["environment_policy"], record["source_close_day"]
    parameters = record["source"]["external_fixed_protocol"]["record"]["environment_parameters"]
    if parameters["entry_confirmation_closes"] != 2:
        raise ValueError("Original two-close confirmation protocol changed")
    panels, prior = loaded["panel_by_day"], previous[day]
    phase = _phase(panels[day], parameters)
    confirmed = phase != "unknown" and phase == _phase(panels.get(prior), parameters)
    if (env["phase"] != phase or env["two_close_confirmation"] is not confirmed
            or env["exact_previous_present"] is not (prior in panels)
            or env["previous_session_required"] != prior
            or any(_stamp(p["available_at"]) > _stamp(env["as_of"]) for p in panels.values())):
        raise ValueError("Saved environment is not its own exact dated prefix and saved rule")
    for identity in TYPE_IDS:
        item = env["per_type"][identity]
        mode = ("EXIT_ALL" if phase == "broad_weakness_snapshot" else "NO_CHANGE" if not confirmed
            else "ALLOW_FIXED_CANDIDATES" if phase in item["rule"]["entry_phases_after_two_close_confirmation"] else "EXIT_ALL")
        if item["mode"] != mode:
            raise ValueError("Saved per-type environment mode changed")
    return env


def _publication(path, ipo, master, now, cache):
    path = Path(path).resolve()
    before = digest(path)
    view = read_saved_plan(path, now=now)
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if digest(path) != before:
        raise ValueError("Publication changed during read")
    source = record["source"]
    panel = str(Path(source["input_manifest"]).resolve())
    if digest(panel) != source["input_manifest_sha256"]:
        raise ValueError("Published own input manifest bytes changed")
    if panel not in cache:
        cache[panel] = load_inputs(panel, ipo, master)
    loaded = cache[panel]
    sessions, previous = _calendar(loaded)
    if source != _source_bindings(loaded, now):
        raise ValueError("Publication own source/actual clocks no longer match")
    day = record["source_close_day"]
    execution = sessions[sessions.index(day) + 1]
    stamp = _stamp(record["recorded_at"])
    if (loaded["source_days"][-1] != day or sorted(loaded["panel_by_day"]) != loaded["source_days"]
            or record["protocol"] != PROTOCOL
            or record["risk_policy_id"] != PROTOCOL["combined_risk_policy_id"]
            or record["risk_policy_spec"] != PROTOCOL["structural_risk_rules"]
            or record["proposed_execution_day"] != execution
            or record["next_open_deadline"] != execution + "T09:30:00+08:00"):
        raise ValueError("Original fixed 32-case rules/source day/exact next session changed")
    timely = (view["status"] == "saved_paper_plan" and record["execution_day_bound"] == execution
        and stamp.date().isoformat() == day and _stamp(day + "T15:05:00+08:00") <= stamp
        < _stamp(execution + "T09:30:00+08:00"))
    if not timely:
        return {"day": day, "execution": execution, "unbindable": True, "path": str(path)}
    env = _environment(record, loaded, sessions, previous)
    paths, _ = _rank_path(loaded["panel_by_day"], set(loaded["scope_members"]), loaded["source_days"][0], day)
    by_type = {item["id"]: item for item in loaded["panel_by_day"][day]["types"]}
    config = _config(100000, 5)
    base = ExactSourceRawFeed(loaded["raw"], loaded["source_days"], loaded["scope_members"], context_anchor=loaded["context_anchor"])
    intents = {}
    for row in record["cases"]:
        identity, guard, cap = row["type"], row["guard"], row["capacity"]
        route = paths[identity][cap][day]
        expected = {"original_top5": route["original_top5"], "cap_candidates": route["candidates"],
            "capacity_members_before": route["cap_symbols_before"], "capacity_members_after": route["cap_symbols_after"],
            "cap_dropped_rows": route["cap_dropped_rows"],
            "raw_weights_before_capacity_guard_and_qualification": choose_targets(route["original_top5"], [], config),
            "cap_weights_before_guard_and_qualification": choose_targets(route["candidates"], [], config),
            "type_missing_evidence": by_type[identity].get("missing_evidence", []),
            "unresolved_member_count": len(set(by_type[identity]["unresolved_symbols"]) & set(loaded["scope_members"]))}
        if any(row.get(k) != v for k, v in expected.items()):
            raise ValueError("Published original ranks/cap/unresolved weights changed")
        mode = "ALLOW_FIXED_CANDIDATES" if guard == "unguarded" else env["per_type"][identity]["mode"]
        if row["environment_mode"] != mode:
            raise ValueError("Published case changed its saved guard")
        reasons, issues, references, ipo_proofs = [], [], [], []
        if mode == "EXIT_ALL":
            decision, weights, reason = "EXIT_ALL", {}, env["per_type"][identity]["reason"]
        else:
            item = by_type[identity]
            if mode == "NO_CHANGE": reasons.append("environment_unknown_or_unconfirmed")
            if item.get("status") not in {"matched", "not_matched"}: reasons.append("type_observation_unknown")
            if not isinstance(item.get("unresolved_symbols"), list): reasons.append("whole_type_membership_evidence_missing")
            if expected["unresolved_member_count"]: reasons.append("whole_type_membership_unknown_in_fixed_panel")
            if not route["candidates"]: reasons.append("type_empty_or_capacity_filtered_all_no_signal")
            required = {candidate["symbol"] for candidate in route["candidates"]}
            issues = _raw_issues(base, day, required)
            for symbol in sorted(required):
                raw = base.rows.get(day, {}).get(symbol)
                if raw is not None and (raw["tradestatus"] != 1 or raw["isST"] != 0 or raw["volume"] <= 0 or raw["amount"] <= 0):
                    issues.append({"symbol": symbol, "day": day, "reason": "selected_current_not_active_nonST"})
            references = [_reference(base, day, s, previous) for s in sorted(required)]
            ipo_proofs = [loaded["qualification"].qualify(day, s, decision_as_of=env["as_of"]) for s in sorted(required)]
            if issues: reasons.append("selected_current_raw_unknown_or_ineligible")
            if any(r["status"] != "consistent_proxy" for r in references): reasons.append("selected_current_reference_unknown_or_incompatible")
            if any(r["engine_block_reason"] is not None or not r["ipo_window_feature_support"] for r in ipo_proofs): reasons.append("selected_current_IPO_unknown_or_unsupported")
            decision, weights = ("NO_CHANGE", None) if reasons else ("FULL_TARGET", expected["cap_weights_before_guard_and_qualification"])
            reason = ";".join(reasons) if reasons else "fixed_original_rank_and_capacity_target"
        setup = []
        for candidate in route["candidates"]:
            symbol = candidate["symbol"]
            raw = base.rows.get(day, {}).get(symbol, {})
            feature = loaded["panel_by_day"][day].get("risk_features", {}).get(symbol) or {}
            setup.append({"symbol": symbol, "source_day": day, "reference_close_cny": _finite(raw.get("close")),
                "setup_low_cny": _finite(raw.get("low")), "sma20_observed": _finite(feature.get("sma20")),
                "sma60_observed": _finite(feature.get("sma60")), "reference_is_order_price": False,
                "reference_is_actual_held_entry": False, "missing_structural_fields_stay_unknown": True})
        checks = {"decision": decision, "target_weights": weights, "reason": reason,
            "current_raw_issues": issues, "current_reference_evidence": references, "IPO_evidence": ipo_proofs,
            "candidate_reference_and_setup": setup, "entry_unknown_checks_bypassed_for_independent_exit": mode == "EXIT_ALL"}
        if any(row.get(k) != v for k, v in checks.items()):
            raise ValueError("Published own-date decision/quality/qualification/setup evidence changed")
        intents[row["case_id"]] = PortfolioIntent.from_dict(row["portfolio_intent"])
    reference = {"day": day, "file": str(path), "sha256": before, "recorded_at": record["recorded_at"],
        "execution_day": execution, "input_manifest": panel, "input_manifest_sha256": digest(panel)}
    return {"day": day, "execution": execution, "record": record, "loaded": loaded,
        "intents": intents, "reference": reference, "paths": paths}


def _quality(state, pub, base, previous, qualification, case_id=None, *, require_successor=False):
    day, execution = pub["day"], pub["execution"]
    held = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
    pending = set(state.get("pending_targets") or {}) | set((state.get("pending_risk_exits") or {}).get("symbols", []))
    intents = pub["intents"].values() if case_id is None else [pub["intents"][case_id]]
    current = {s for intent in intents for s, weight in intent.weights if weight > 0}
    members = held | pending | current
    dates = (day, execution) if require_successor else (day,)
    issues = [issue for d in dates for issue in _raw_issues(base, d, members)]
    # Preserve the actual OHLC geometry gate as well as finite/status checks.
    for d in dates:
        for s in sorted(members):
            row = base.rows.get(d, {}).get(s)
            if row and (row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]) or row["low"] > row["high"]):
                issues.append({"day": d, "symbol": s, "reason": "inconsistent_raw_OHLC_geometry"})
    refs = [_reference(base, d, s, previous) for d in dates for s in sorted(members)]
    cutoffs = [(day, day + "T09:30:00+08:00", held | pending),
        (day, pub["record"]["recorded_at"], current)]
    if require_successor:
        cutoffs.append((execution, execution + "T09:30:00+08:00", members))
    ipo = [qualification.assess(s, d, decision_as_of=clock) for d, clock, symbols in cutoffs for s in sorted(symbols)]
    if issues or any(r["status"] != "consistent_proxy" for r in refs) or any(r["engine_block_reason"] is not None for r in ipo):
        return {"reason": "required_raw_reference_or_listing_evidence_unresolved", "day": day,
            "execution_day": execution, "raw_issues": issues, "reference_evidence": refs,
            "IPO_evidence": ipo, "session_not_committed": True}
    return None


class PublishedFixedExpiryReduction:
    policy_id = TARGET_POLICY_ID

    def __init__(self, feed, state, config):
        self.reduce = fixed_exit_policy(feed, state, config)

    def __call__(self, intent):
        return self.reduce(intent)


def _waiting(status, **facts):
    return {"schema": RESULT_SCHEMA, "status": status, "case_count": 0,
        "research_checkpoint_created": False, "research_ledger_mutated": False,
        "research_accounts_created": 0, "account_created": False, "account_reset": False,
        "orders_generated": False, "metrics": None, **facts, **FLAGS}


def _registration(path, first, config, now):
    path = Path(path).resolve()
    before = digest(path)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    stamp = _stamp(value["registered_at"])
    original = value["original_publication"]
    reference = first["reference"]
    if (value.get("schema") != "leader_eight_type_published_close_forward_registration_v1"
            or value.get("first_decision_day") != first["day"]
            or value.get("first_execution_day") != first["execution"]
            or value.get("first_execution_deadline") != first["execution"] + "T09:30:00+08:00"
            or Path(original["file"]).resolve() != Path(reference["file"]).resolve()
            or original["sha256"] != reference["sha256"] or original["recorded_at"] != reference["recorded_at"]
            or not _stamp(reference["recorded_at"]) <= stamp < _stamp(value["first_execution_deadline"])
            or stamp > now or value.get("published_target_policy_id") != TARGET_POLICY_ID
            or value.get("risk_policy") != FORWARD_RISK_SPEC or value.get("case_ids") != list(CASE_IDS)
            or value.get("config") != config.to_dict()
            or value.get("registration_is_original_publication") is not False
            or value.get("original_publication_unchanged") is not True
            or any(value.get(k) is not v for k, v in FLAGS.items()) or digest(path) != before):
        raise ValueError("Actual forward method registration must bind the original publication before exact E open")
    return {"file": str(path), "sha256": before, "registered_at": value["registered_at"],
        "registration_is_original_publication": False}


def run(publications, panel, ipo_evidence, research_master_run, run_dir, *, through=None, registration=None,
        method=None):
    case_ids = CASE_IDS if method is None else tuple(method.CASE_IDS)
    checkpoint_schema = SCHEMA if method is None else method.CHECKPOINT_SCHEMA
    result_schema = RESULT_SCHEMA if method is None else method.RESULT_SCHEMA
    account_prefix = "leader-eight-type-forward:" if method is None else method.ACCOUNT_PREFIX
    target_policy_id = TARGET_POLICY_ID if method is None else method.TARGET_POLICY_ID
    risk_policy_id = RISK_POLICY_ID if method is None else method.RISK_POLICY_ID
    publication_loader = _publication if method is None else method.load_publication
    registration_loader = _registration if method is None else method.registration
    risk_factory = PublishedCloseLeaderTypeRiskExitPolicy if method is None else method.risk_policy
    target_factory = PublishedFixedExpiryReduction if method is None else method.target_policy
    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise ValueError("An explicit unique nonempty research case set is required")
    if method is not None and registration is None:
        raise ValueError("An injected forward method requires its explicit registration")
    def waiting(status, **facts):
        value = _waiting(status, **facts)
        value["schema"] = result_schema
        return value
    now, directory, cache = _now(), Path(run_dir).resolve(), {}
    published = [publication_loader(p, ipo_evidence, research_master_run, now, cache) for p in publications]
    if not published:
        return waiting("waiting_for_real_close_publication")
    published.sort(key=lambda p: p["day"])
    days = [p["day"] for p in published]
    first = published[0]
    if any(p.get("unbindable") for p in published):
        return waiting("waiting_unbindable_original_publication_no_date_or_execution_roll", decision_days=days)
    latest_path = str(Path(panel).resolve())
    if latest_path not in cache:
        cache[latest_path] = load_inputs(latest_path, ipo_evidence, research_master_run)
    latest = cache[latest_path]
    sessions, previous = _calendar(latest)
    if days != sorted(set(days)) or [d for d in sessions if days[0] <= d <= days[-1]] != days:
        raise ValueError("Actual publication decisions must be an exact consecutive prefix")
    for pub in published:
        _same_lineage(pub["loaded"], latest)
        if _stamp(pub["record"]["source"]["input_recorded_at"]) > _stamp(pub["record"]["recorded_at"]):
            raise ValueError("Publication predates its actual input")
    # Each publication retains its own past candidate/feature facts. Only the
    # three explicit derived availability locations can be recorded anew.
    authoritative = {}
    for pub in published:
        panels = pub["authoritative_panels"] if "authoritative_panels" in pub else pub["loaded"]["panel_by_day"]
        for day, values in panels.items():
            if day in authoritative and _facts(authoritative[day]) != _facts(values):
                raise ValueError("Published candidate/risk/identity prefix facts changed")
            authoritative.setdefault(day, deepcopy(values))
    config, qualification = _config(100000, 5), latest["qualification"]
    registration_path = (registration or DEFAULT_REGISTRATION) if method is None else registration
    registered = registration_loader(registration_path, first, config, now)
    binding = {"protocol": deepcopy(first["record"]["protocol"]), "config": config.to_dict(),
        "external_fixed_protocol": first["record"]["source"]["external_fixed_protocol"],
        "risk_policy": deepcopy(FORWARD_RISK_SPEC) if method is None else {"id": risk_policy_id},
        "published_target_policy_id": target_policy_id,
        "calendar": latest["calendar_payload"], "qualification": qualification.verification,
        "scope_members": latest["scope_members"], "context_anchor": latest["provenance"]["context_anchor"],
        "run_directory": str(directory), "forward_method_registration": registered}
    path = directory / "head.json"
    head = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    is_new = head is None
    if head is None and directory.exists():
        raise FileExistsError("Existing directory without a valid head cannot be reset")
    if head is not None:
        if head.get("schema") != checkpoint_schema or head.get("binding") != binding or head.get("execution_admitted") is not False:
            raise ValueError("Saved fixed thirty-two-case research account binding changed" if method is None
                             else "Saved fixed research account binding changed")
        if head["publications"] != [p["reference"] for p in published[:len(head["publications"])]]:
            raise ValueError("Processed publication prefix disappeared or changed")
        if _prefix(latest, head["source_prefix"]["through"]) != head["source_prefix"]:
            raise ValueError("Previously consumed source/raw prefix changed")
    cutoff = through or days[-1]
    if cutoff not in sessions or cutoff < days[0]:
        raise ValueError("Through must be an exact session on/after the first actual publication")
    if head and any(case["state"]["day"] and cutoff < case["state"]["day"] for case in head["cases"].values()):
        raise ValueError("Persistent research cursor cannot move backwards")
    base = ExactSourceRawFeed(latest["raw"], sessions, latest["scope_members"], context_anchor=latest["context_anchor"])
    if head is None:
        if first["execution"] not in latest["source_days"]:
            return waiting("waiting_for_exact_successor_complete_source_close", first_decision_day=first["day"], required_successor_day=first["execution"], forward_method_registration=registered)
        issue = _quality({"positions": {}, "pending_targets": None}, first, base, previous, qualification, require_successor=True)
        if issue:
            return waiting("waiting_for_required_execution_quality_evidence", blocking_evidence=issue)
    directory.mkdir(parents=True, exist_ok=True)
    with lock(directory / "runner.lock"):
        if head is not None and json.loads(path.read_text(encoding="utf-8")) != head:
            raise ValueError("Checkpoint changed during read")
        if head is None:
            if path.exists():
                raise ValueError("Concurrent runner created this checkpoint")
            head = {"schema": checkpoint_schema, "registered_at": _now().isoformat(), "binding": binding,
                "first_decision_day": first["day"], "publications": [],
                "source_prefix": _prefix(latest, first["execution"]), "cases": {}, **FLAGS}
            for key in case_ids:
                context = RunContext(account_prefix + _hash(str(directory))[:16] + ":" + key, TradingMode.REPLAY)
                head["cases"][key] = {"context": context.to_dict(), "state": new_state(config.initial_cash), "daily": [], "journal": [], "journal_files": []}
            write_json(path, head)
        if set(head["cases"]) != set(case_ids):
            raise ValueError("Exactly all original thirty-two research books are required" if method is None
                             else "Exactly all registered research books are required")
        for key, case in head["cases"].items():
            context = RunContext(account_prefix + _hash(str(directory))[:16] + ":" + key, TradingMode.REPLAY)
            processed = [r["date"] for r in case["daily"]]
            expected_risk = {"schema": "replay_risk_exit_policy_v1", "policy_id": risk_policy_id + ":" + key.split("__")[0], "context": context.to_dict()}
            expected_target = {"schema": "replay_published_close_target_policy_v1", "policy_id": target_policy_id, "context": context.to_dict()}
            if (case["context"] != context.to_dict() or case["state"].get("broker_binding")
                    or processed != days[:len(processed)] or case["state"]["day"] != (processed[-1] if processed else None)
                    or len(case["journal"]) != len(processed)
                    or (processed and (case["state"].get("replay_risk_exit_policy") != expected_risk
                        or case["state"].get("published_close_target_policy") != expected_target
                        or round(case["daily"][-1]["equity"] * 100) != Ledger(case["state"], config, context).equity()))):
                raise ValueError("Saved case context/cursor/equity/risk or target policy changed")
            if len(case.get("journal_files", [])) != len(processed):
                raise ValueError("Each processed case session requires its independent journal seal")
            for entry, proof in zip(case["journal"], case["journal_files"]):
                journal_path = Path(proof["file"]).resolve()
                journal_path.relative_to(directory / "journal")
                if digest(journal_path) != proof["sha256"] or json.loads(journal_path.read_text(encoding="utf-8")) != entry:
                    raise ValueError("Independent published case journal bytes or head copy changed")
            if processed:
                sealed = case["journal"][-1]
                for key_state in ("pending_published_close_intent", "pending_targets", "pending_risk_exits"):
                    if case["state"].get(key_state) != sealed["post_session_" + key_state]:
                        raise ValueError("Pending research target/receipt changed from independently sealed journal")
        completed = 0
        for index, pub in enumerate(published):
            if pub["day"] > cutoff or pub["day"] not in latest["source_days"]:
                break
            witness = pub["execution"] if index == 0 else pub["day"]
            attempt = {"day": pub["day"], "publication": pub["reference"], "source_prefix": _prefix(latest, witness)}
            changed = False
            for key, case in head["cases"].items():
                if len(case["daily"]) != index or case.get("failed_attempt") == attempt:
                    continue
                issue = _quality(case["state"], pub, base, previous, qualification, key, require_successor=index == 0)
                if issue:
                    if case.get("blocking_evidence") != issue:
                        case["blocking_evidence"] = issue; changed = True
                    continue
                trial, identity = deepcopy(case["state"]), key.split("__")[0]
                original_row = next(r for r in pub["record"]["cases"] if r["case_id"] == key)
                original_intent = pub["intents"][key]
                state_entry_evidence = None
                if method is not None and hasattr(method, "prepare_intent"):
                    original_intent, state_entry_evidence = method.prepare_intent(pub, key, trial)
                batches = {d: {"schema": BATCH_SCHEMA, "day": d,
                    "source": "research:actual_eight_type_publication" if method is None else "research:" + checkpoint_schema,
                    "status": "incomplete", "candidates": []} for d in authoritative}
                batches[pub["day"]].update(status="incomplete" if original_intent.kind == IntentKind.NO_CHANGE else "complete",
                    candidates=deepcopy(original_row["cap_candidates"]))
                common = CommonCohortDecisionFeed(base, batches, trial, market_defense=False)
                feed = _QualifiedFeed(common, qualification)
                risk_arguments = dict(candidate_by_day={d: next(t for t in p["types"] if t["id"] == identity)["candidates"] for d, p in authoritative.items() if d <= pub["day"]},
                    feature_by_day={d: p.get("risk_features", {}) for d, p in authoritative.items() if d <= pub["day"]},
                    complete_feature_days=[d for d, p in authoritative.items() if d <= pub["day"] and p.get("risk_feature_scope_complete") is True])
                if method is not None:
                    risk_arguments["state"] = trial
                risk = risk_factory(identity, **risk_arguments)
                context = RunContext.from_dict(case["context"])
                intent = replace(original_intent, context=context)
                try:
                    target = target_factory(feed, trial, config)
                    if method is not None and (risk.policy_id != risk_policy_id + ":" + identity
                            or target.policy_id != target_policy_id):
                        raise ValueError("Injected risk or target policy identity differs from its registered method")
                    row = advance(trial, feed, pub["day"], config, index, context=context,
                        published_close_intent=intent, published_close_target_policy=target, replay_risk_exit_policy=risk)
                except (ValueError, KeyError) as error:
                    failure = {**attempt, "error": f"{type(error).__name__}: {error}", "trial_committed": False}
                    write_json(directory / "failed_attempts" / (pub["day"] + "_" + key + "_" + _hash(attempt)[:16] + ".json"), failure)
                    case.update(error=failure["error"], failed_attempt=attempt); changed = True
                    continue
                case.update(state=trial, error=None, blocking_evidence=None)
                case.pop("failed_attempt", None)
                case["daily"].append(row)
                entry = {"decision_day": pub["day"], "execution_day": pub["execution"], "publication": pub["reference"],
                    "unbound_original_intent": pub["intents"][key].to_dict(), "bound_original_intent": intent.to_dict(),
                    "published_close_reduction": deepcopy(row.get("published_close_reduction")),
                    "committed_at": _now().isoformat(), "retrospective_successor_quality_witness": index == 0,
                    "successor_used_for_ranking_or_refill": False, "source_features_from_own_publication": True,
                    **{"post_session_" + name: deepcopy(trial.get(name)) for name in
                        ("pending_published_close_intent", "pending_targets", "pending_risk_exits")}}
                if state_entry_evidence is not None:
                    entry["state_entry_information_policy"] = state_entry_evidence
                # A process can stop after the journal lands but before the
                # atomic head replacement. Keep such orphans; a later commit
                # may reuse identical content or write a new actual clock.
                journal_file = directory / "journal" / key / (pub["day"] + "_" + _hash(entry)[:24] + ".json")
                if journal_file.exists():
                    expected_bytes = json.dumps(entry, ensure_ascii=False, indent=2, allow_nan=False).replace("\n", os.linesep).encode("utf-8")
                    if journal_file.read_bytes() != expected_bytes or json.loads(journal_file.read_text(encoding="utf-8")) != entry:
                        raise ValueError("Existing content-addressed session journal differs; never overwrite it")
                else:
                    write_json(journal_file, entry)
                case["journal"].append(entry)
                case["journal_files"].append({"file": str(journal_file), "sha256": digest(journal_file)})
                completed += 1; changed = True
            if changed:
                if len(head["publications"]) <= index:
                    head["publications"].append(pub["reference"])
                if witness > head["source_prefix"]["through"]:
                    head["source_prefix"] = _prefix(latest, witness)
                write_json(path, head)
        cases = [{"case_id": key, "processed_decision_days": [r["date"] for r in case["daily"]],
            "last_processed_day": case["state"]["day"], "orders": len(case["state"]["orders"]), "fills": len(case["state"]["fills"]),
            "pending_execution_day": (case["state"].get("pending_published_close_intent") or {}).get("execution_not_before"),
            "pending_risk_execution_day": (case["state"].get("pending_risk_exits") or {}).get("execution_on"),
            "blocking_evidence": case.get("blocking_evidence"), "error": case.get("error"), "metrics": None} for key, case in head["cases"].items()]
        blocked = any(c["error"] or c["blocking_evidence"] for c in cases)
        status = ("advanced_research_only_with_blocked_cases" if completed and blocked else "advanced_research_only" if completed
            else "blocked_or_waiting_research_only" if blocked else "waiting_for_next_real_close_publication")
        return {"schema": result_schema, "status": status, "case_count": len(case_ids), "cases": cases,
            "newly_committed_case_sessions": completed, "checkpoint": str(path), "research_checkpoint_created": True,
            "research_ledger_mutated": bool(completed), "account_reset": False, "research_accounts_created": len(case_ids) if is_new else 0,
            "user_account_created": False, "orders_generated": any(c["orders"] for c in cases),
            "next_real_close_publication_days_required": sorted({sessions[sessions.index(c["last_processed_day"]) + 1] for c in cases if c["last_processed_day"] and sessions.index(c["last_processed_day"]) + 1 < len(sessions)}),
            "whole_session_commit_requires_own_real_close_publication": True, "metrics": None,
            "metrics_semantics": "unfinished_append_only_conditional_raw_research_not_validated_total_return", **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", action="append", type=Path, required=True)
    for name in ("panel", "ipo-evidence", "research-master-run", "run-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--through")
    parser.add_argument("--registration", type=Path, default=DEFAULT_REGISTRATION)
    args = parser.parse_args(argv)
    result = run(args.publication, args.panel, args.ipo_evidence, args.research_master_run, args.run_dir,
        through=args.through, registration=args.registration)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
