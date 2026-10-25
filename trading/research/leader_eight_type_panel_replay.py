"""Fixed eight-type research panels through the shared Engine/Ledger.

This is a new late-derived mechanical comparison, not an extension of the
formal twenty-case protocol or evidence of historical signal publication.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path

import pandas as pd

from ..contracts import RunContext, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..market_dataset import write_json
from ..report import metrics
from .baostock_daily_slice import FIELDS, digest
from .baostock_full_close import load_complete
from .ipo_qualification import IpoQualifiedResearchFeed, load_ipo_qualification
from .leader_daily_replay import CommonCohortDecisionFeed
from .leader_daily_signals import _calendar_view
from .leader_environment import ARCHETYPES
from .leader_environment_policy import evaluate_environment_path
from .leader_recent_source_history import source_proof
from .recent_alpha158_panel_replay import ExactSourceRawFeed, _context_anchor, _raw_issues, _reference
from .shortline_decision_v2 import BATCH_SCHEMA, fixed_exit_policy
from .shortline_decision_v3 import FiveSessionExpiryPolicy, POLICY_ID
from .leader_type_risk_exit import LeaderTypeRiskExitPolicy, POLICY_ID as TYPE_RISK_ID, RULE_SPEC as TYPE_RISK_RULE
from .shortline_panel_replay import _config


INPUT_SCHEMA = "leader_eight_type_replay_input_v1"
SCHEMA = "leader_eight_type_candidate_panel_replay_v1"
TYPE_IDS = tuple(identity for identity, _ in ARCHETYPES)
GUARDS = ("unguarded", "two_close_envguard")
CAPS = (("causal_first_seen_cap20", 20), ("uncapped", None))
FLAGS = {"candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
    "execution_admitted": False, "model_refit": False, "future_labels_used_for_score": False,
    "provider_contacted": False, "broker_interacted": False, "persistent_ledger_mutated": False,
    "historical_signal_publication_verified": False, "pit_verified": False, "corporate_actions_verified": False}
PROTOCOL = {"id": "fixed_eight_type_two_guard_two_capacity_research_v1", "types": list(TYPE_IDS),
    "guards": list(GUARDS), "capacities": [{"id": name, "max_symbols": cap} for name, cap in CAPS],
    "case_count": 32, "top_per_day": 5, "capacity_basis": "each_type_daily_original_top5_first_seen_before_guard_shared_by_both_guards",
    "empty_type": "NO_CHANGE_not_cash", "unresolved_members": "intersect_fixed_panel_blocks_new_entry_not_independent_exit",
    "environment_exit_priority": "trusted_EXIT_ALL_before_type_unknown_but_never_before_owned_raw_reference_validation",
    "expiry_policy_id": POLICY_ID, "max_holding_open_sessions": 5, "tail_open_sessions": 6,
    "combined_risk_policy_id": TYPE_RISK_ID, "structural_risk_rules": TYPE_RISK_RULE,
    "parameters_selected_by_PnL": False, "old_formal_twenty_case_protocol_changed": False}


def _stamp(value):
    result = datetime.fromisoformat(str(value))
    if result.utcoffset() is None:
        raise ValueError("Actual source, feature and analysis clocks require explicit timezone")
    return result


class _QualifiedFeed(IpoQualifiedResearchFeed):
    def signal_frame(self, day, config, context):
        return self.base.signal_frame(day, config, context)


def _rank_path(panels, members, start, end):
    """Register capacity from original dated ranks, never from guard outcomes."""
    routes = {identity: {name: {} for name, _ in CAPS} for identity in TYPE_IDS}
    original = {identity: {} for identity in TYPE_IDS}
    seen = {identity: [] for identity in TYPE_IDS}
    for day in sorted(panels):
        if not start <= day <= end:
            continue
        for item in panels[day]["types"]:
            identity = item["id"]
            rows = [deepcopy(row) for row in item["candidates"] if row["symbol"] in members]
            for row in rows:
                # Existing descriptive types order by amount, never by PnL.
                if "score" not in row and "amount_cny" not in row:
                    raise ValueError("Observed fixed rank/amount is missing; never substitute zero")
                row["score"] = float(row.get("score", row.get("amount_cny")))
                if not math.isfinite(row["score"]):
                    raise ValueError("Finite fixed candidate rank required")
            if len({row["symbol"] for row in rows}) != len(rows):
                raise ValueError("Duplicate type candidate")
            top = sorted(rows, key=lambda row: (-row["score"], row["symbol"]))[:5]
            original[identity][day] = top
            before = list(seen[identity])
            for row in top:
                if row["symbol"] not in seen[identity] and len(seen[identity]) < 20:
                    seen[identity].append(row["symbol"])
            for name, limit in CAPS:
                retained = top if limit is None else [row for row in top if row["symbol"] in seen[identity]]
                routes[identity][name][day] = {"candidates": retained, "original_top5": top,
                    "cap_symbols_before": before if limit else None,
                    "cap_symbols_after": list(seen[identity]) if limit else None,
                    "cap_dropped_rows": len(top) - len(retained), "guard_used_for_capacity": False}
    return routes, original


def _environment(panels, calendar, as_of):
    result, observations = {}, []
    for day, panel in sorted(panels.items()):
        observation = {"day": day, "available_at": panel["available_at"], **panel["environment"]}
        observations.append(observation)
        result[day] = evaluate_environment_path(deepcopy(observations), calendar=calendar, as_of=as_of)
    return result


def _case(raw, source_days, sessions, previous, panels, paths, env, members, qualification,
          anchor, start, end, identity, cap_name, guard, account_id,
          execution_policy_factory=None, context_raw=None, snapshot_day=None,
          entry_membership_policy=None, resume_case=None, base_feed=None,
          risk_policy_candidates=None, risk_policy_features=None,
          risk_policy_complete_days=None):
    if snapshot_day is not None and snapshot_day not in source_days:
        raise ValueError("Explicit account snapshot must use one exact source session")
    config, state = _config(100000, 5), new_state(100000)
    if base_feed is None:
        base_raw = raw if context_raw is None else pd.concat([context_raw, raw], ignore_index=True)
        base_days = source_days if context_raw is None else sorted(set(context_raw.date) | set(source_days))
        base = ExactSourceRawFeed(base_raw, base_days, sorted(members), context_anchor=anchor)
    else:
        base = base_feed
    execution = execution_policy_factory(base, source_days) if execution_policy_factory is not None else None
    raw_issues = execution.raw_issues if execution is not None else lambda day, required: _raw_issues(base, day, required)
    references = execution.references if execution is not None else lambda day, required: [
        _reference(base, day, symbol, previous) for symbol in sorted(required)]
    reference_ok = execution.reference_ok if execution is not None else lambda item: item["status"] == "consistent_proxy"
    context = RunContext(account_id, TradingMode.REPLAY)
    prefix_count = 0
    if resume_case is not None:
        expected_id = f"{identity}__{guard}__{cap_name}"
        saved_daily, saved_decisions = resume_case.get("daily"), resume_case.get("decisions")
        saved_state = resume_case.get("account")
        if (resume_case.get("case_id") != expected_id or resume_case.get("type") != identity
                or resume_case.get("guard") != guard or resume_case.get("capacity") != cap_name
                or resume_case.get("config") != config.to_dict()
                or resume_case.get("complete_PnL_available") is not False
                or not isinstance(saved_daily, list) or not isinstance(saved_decisions, list)
                or not isinstance(saved_state, dict) or not saved_daily
                or saved_state.get("broker_binding") is not None
                or len(saved_daily) >= len(source_days)
                or [row["date"] for row in saved_daily] != source_days[:len(saved_daily)]
                or [row["day"] for row in saved_decisions] != source_days[:len(saved_daily)]
                or saved_state.get("day") != saved_daily[-1]["date"]
                or resume_case.get("engine_sessions_committed") != len(saved_daily)
                or resume_case.get("orders") != list(saved_state.get("orders", {}).values())
                or resume_case.get("fills") != list(saved_state.get("fills", {}).values())
                or (saved_state.get("replay_risk_exit_policy") or {}).get("policy_id") != TYPE_RISK_ID + ":" + identity
                or (saved_state.get("replay_risk_exit_policy") or {}).get("context") != context.to_dict()):
            raise ValueError("Exact unfinished saved case, committed calendar and original context required")
        failure = resume_case.get("blocking_evidence") or {}
        prefix_count = len(saved_daily)
        if failure.get("day") != source_days[prefix_count] or failure.get("session_not_committed") is not True:
            raise ValueError("Source continuation starts at the exact original uncommitted session")
        if snapshot_day is not None and snapshot_day <= saved_state["day"]:
            raise ValueError("Continuation cannot reconstruct an already committed account snapshot")
        state = deepcopy(saved_state)
    policy_copy_inputs = risk_policy_candidates is None or risk_policy_features is None
    if risk_policy_candidates is None:
        risk_policy_candidates = {day: next(item for item in panel["types"] if item["id"] == identity)["candidates"]
            for day, panel in panels.items()}
    if risk_policy_features is None:
        risk_policy_features = {day: panel.get("risk_features", {}) for day, panel in panels.items()}
    if risk_policy_complete_days is None:
        risk_policy_complete_days = [day for day, panel in panels.items()
                                     if panel.get("risk_feature_scope_complete") is True]
    risk_policy = LeaderTypeRiskExitPolicy(identity, candidate_by_day=risk_policy_candidates,
        feature_by_day=risk_policy_features, complete_feature_days=risk_policy_complete_days,
        copy_inputs=policy_copy_inputs)
    if execution is not None:
        risk_policy = execution.wrap_risk_policy(risk_policy)
    daily = deepcopy(resume_case["daily"]) if resume_case is not None else []
    decisions = deepcopy(resume_case["decisions"]) if resume_case is not None else []
    blocked = None
    account_snapshot = None
    batches = {day: {"schema": BATCH_SCHEMA, "day": day, "source": "research:fixed_eight_type_panel",
        "status": "incomplete", "candidates": []} for day in source_days}
    for index, day in enumerate(source_days):
        if index < prefix_count:
            continue
        held = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
        pending = set(state.get("pending_targets") or {})
        owned_issues = raw_issues(day, held | pending)
        owned_refs = references(day, held | pending)
        owned_ipo = ([qualification.assess(symbol, day, decision_as_of=day + "T09:30:00+08:00")
            for symbol in sorted(held | pending)] if execution is not None else [])
        if (owned_issues or any(not reference_ok(row) for row in owned_refs)
                or any(row["engine_block_reason"] is not None for row in owned_ipo)):
            blocked = {"day": day, "reason": "held_or_pending_raw_or_action_reference_unknown",
                "raw_issues": owned_issues, "reference_evidence": owned_refs, "session_not_committed": True}
            if execution is not None:
                blocked["IPO_evidence"] = owned_ipo
            break
        panel = panels.get(day)
        item = next((row for row in panel["types"] if row["id"] == identity), None) if panel else None
        path = paths.get(day, {"candidates": [], "original_top5": [], "cap_dropped_rows": 0})
        mode = (env[day]["per_type"][identity]["mode"] if guard != "unguarded" and day in env else "ALLOW_FIXED_CANDIDATES")
        reasons, candidate_issues, refs, ipo = [], [], [], []
        membership_evidence = None
        selected = path["candidates"]
        following = sessions[sessions.index(day) + 1] if sessions.index(day) + 1 < len(sessions) else None
        exact_successor_present = following in source_days
        in_window = start <= day <= end
        if not in_window:
            reasons.append("execution_tail_no_new_entry")
        elif not exact_successor_present:
            reasons.append("withheld_missing_exact_successor_full_source")
        elif mode == "EXIT_ALL":
            # Independent known risk reduction remains possible with unknown types.
            selected = []
        elif mode == "NO_CHANGE":
            reasons.append("environment_unknown_or_unconfirmed")
        else:
            if item is None or item.get("status") not in {"matched", "not_matched"}:
                reasons.append("type_observation_unknown")
            if entry_membership_policy is None:
                if item is not None and set(item.get("unresolved_symbols", [])).intersection(members):
                    reasons.append("type_membership_unknown_in_fixed_panel")
            else:
                membership_evidence = entry_membership_policy(day=day, identity=identity, item=item,
                    selected=selected, members=members, sessions=sessions, panels=panels,
                    held_symbols=held, pending_symbols=pending)
                reasons.extend(membership_evidence["blocking_reasons"])
            if not selected:
                reasons.append("type_empty_or_capacity_filtered_all_no_signal")
            required = {row["symbol"] for row in selected}
            # The historical native method records D's target using D's facts.
            # E's actual quotes/actions are checked on E before any mutation;
            # unavailable E facts must not turn an already recorded D into NC.
            validation_days = (day,) if execution is not None else (day, following)
            candidate_issues = [issue for date in validation_days for issue in raw_issues(date, required)]
            refs = [row for date in validation_days for row in references(date, required)]
            ipo = [qualification.assess(symbol, date, decision_as_of=date +
                ("T15:05:00+08:00" if date == day else "T09:30:00+08:00"))
                for date in validation_days for symbol in sorted(required)]
            if candidate_issues:
                reasons.append("selected_current_raw_unknown" if execution is not None else "selected_current_or_successor_raw_unknown")
            if any(not reference_ok(row) for row in refs):
                reasons.append("selected_current_or_execution_reference_unknown")
            if any(row["engine_block_reason"] is not None for row in ipo):
                reasons.append("selected_IPO_qualification_unknown_or_unsupported")
        batches[day].update(status="complete" if not reasons else "incomplete",
            candidates=[{"symbol": row["symbol"], "score": row["score"]} for row in selected])
        trial = deepcopy(state)
        common = (execution.decision_feed(batches, trial) if execution is not None
            else CommonCohortDecisionFeed(base, batches, trial, market_defense=False))
        feed = _QualifiedFeed(common, qualification)
        try:
            row = advance(trial, feed, day, config, index, context=context,
                target_policy=fixed_exit_policy(feed, trial, config), replay_risk_exit_policy=risk_policy)
        except (ValueError, KeyError) as error:
            blocked = {"day": day, "reason": "shared_engine_session_blocked", "error": f"{type(error).__name__}: {error}",
                "session_not_committed": True}
            break
        state = trial
        if day == snapshot_day:
            account_snapshot = deepcopy(state)
        daily.append(row)
        no_decision = next((event["reason"] for event in row["events"] if event["kind"] == "no_decision"), None)
        decisions.append({"day": day, "execution_day": following, "in_decision_window": in_window,
            "environment_mode": mode, "status": "NO_CHANGE" if no_decision else "EXPLICIT_EXIT_ALL" if mode == "EXIT_ALL" else "FULL_TARGET",
            "original_top5": path["original_top5"], "cap_candidates": path["candidates"],
            "cap_dropped_rows": path["cap_dropped_rows"], "reasons": reasons,
            "no_change_reason": no_decision, "raw_issues": candidate_issues, "reference_evidence": refs, "IPO_evidence": ipo,
            "source_available_at": panel["available_at"] if panel else None,
            "clock_semantics": "late_derived_historical_EOD_mechanical_assumption_not_actual_publication",
            "successor_used_for_ranking_or_refill": False})
        if execution is not None:
            decisions[-1].update(candidate_clearance_scope="decision_day_only_execution_guard_on_exact_E",
                successor_quotes_used_for_candidate_clearance=False)
        if entry_membership_policy is not None:
            decisions[-1]["entry_membership_evidence"] = membership_evidence
    tail = [day for day in source_days if day > end]
    residual = sorted(symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0)
    complete = blocked is None and len(tail) >= 6 and not residual and bool(state["fills"])
    result = {"case_id": f"{identity}__{guard}__{cap_name}", "type": identity, "guard": guard, "capacity": cap_name,
        "status": "completed_conditional_mechanical_replay" if complete else "no_complete_PnL",
        "config": config.to_dict(), "decisions": decisions, "daily": daily,
        "orders": list(state["orders"].values()), "fills": list(state["fills"].values()), "account": state,
        "blocking_evidence": blocked, "observed_tail_sessions": len(tail), "required_tail_sessions": 6,
        "residual_positions": residual, "metrics": metrics(daily, config.initial_cash) if complete else None,
        "metrics_semantics": "conditional_unadjusted_raw_shared_engine_account_not_verified_total_return",
        "engine_sessions_committed": len(daily), "complete_PnL_available": complete, **FLAGS}
    if execution is not None:
        result.update(historical_execution_policy=execution.provenance,
            action_reference_checks=execution.accounting.checks, candidate_reference_checks=execution.checks,
            returns_reportable=False,
            metrics_semantics="conditional_whole_native_shared_engine_action_components_not_verified_total_return")
    if snapshot_day is not None:
        result["committed_prefix_snapshot"] = account_snapshot
    if resume_case is not None:
        prior_state = resume_case["account"]
        if (daily[:prefix_count] != resume_case["daily"]
                or decisions[:prefix_count] != resume_case["decisions"]
                or state["events"][:len(prior_state["events"])] != prior_state["events"]
                or any(state["orders"].get(key) != value for key, value in prior_state["orders"].items())
                or any(state["fills"].get(key) != value for key, value in prior_state["fills"].items())
                or any(state["entitlements"].get(key) != value for key, value in prior_state["entitlements"].items())):
            raise ValueError("Continuation changed an already committed account or decision prefix")
        result.update(reused_sessions=prefix_count, newly_completed_sessions=len(daily) - prefix_count,
            old_committed_sessions_replayed=False, cash_reset=False,
            original_context_preserved=True, committed_prefix_preserved=True,
            prior_blocking_evidence=deepcopy(resume_case["blocking_evidence"]))
    return result


def _validate_panel_data(panel_by_day, raw, source_days, calendar_payload, scope_members, qualification):
    """Shared read-only input check; no target, state or order is created."""
    members = set(scope_members)
    sessions, previous, status = _calendar_view(calendar_payload, f"{calendar_payload['year']}-01-01", f"{calendar_payload['year']}-12-31", [])
    if (status != "pinned_static_exchange_calendar" or not members or len(members) != len(scope_members)
            or source_days != sorted(set(source_days)) or not source_days
            or [day for day in sessions if source_days[0] <= day <= source_days[-1]] != source_days
            or tuple(qualification.calendar) != tuple(sessions)
            or sorted(raw.date.unique()) != source_days or raw.duplicated(["date", "code"]).any()):
        raise ValueError("Fixed distinct panel and exact continuous complete source/calendar dates required")
    now = datetime.now().astimezone().isoformat()
    for day, panel in panel_by_day.items():
        if (day != panel["day"] or day not in source_days or _stamp(panel["available_at"]) > _stamp(now)
                or _stamp(panel["available_at"]) < _stamp(day + "T15:00:00+08:00")
                or [item["id"] for item in panel["types"]] != list(TYPE_IDS)):
            raise ValueError("Every panel needs eight original types and actual same-source availability")
        for item in panel["types"]:
            if (not isinstance(item.get("unresolved_symbols"), list) or not isinstance(item.get("candidates"), list)):
                raise ValueError("Each type must declare both observed candidates and unresolved membership")
            if (item.get("available_at") is not None and _stamp(item["available_at"]) > _stamp(panel["available_at"])):
                raise ValueError("Type evidence became available after its containing panel")
            for candidate in item["candidates"]:
                if (any(candidate.get(key, day) != day for key in ("date", "source_day"))
                        or (candidate.get("available_at") is not None
                            and _stamp(candidate["available_at"]) > _stamp(panel["available_at"]))
                        or {"hit_4", "hit_5", "executable_hit_4", "executable_hit_5"}.intersection(candidate)):
                    raise ValueError("Future, differently dated or later available candidate evidence is forbidden")
    return members, sessions, previous, now


def replay_eight_types(*, panel_by_day, raw, source_days, calendar_payload, scope_members,
                       qualification, context_anchor, start, end, out, provenance=None,
                       execution_policy_factory=None, context_raw=None):
    """Pure saved-data comparison, with one isolated research account per case."""
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Eight-type comparison requires a new output directory")
    members, sessions, previous, now = _validate_panel_data(panel_by_day, raw, source_days, calendar_payload, scope_members, qualification)
    if start > end or start not in source_days or end not in source_days:
        raise ValueError("Exact start and end must be existing source sessions")
    if not all(day in panel_by_day for day in source_days if start <= day <= end):
        raise ValueError("Missing dated panel is not a known-empty type")
    if context_raw is not None and (execution_policy_factory is None or context_raw.empty
            or context_raw.date.max() >= source_days[0] or context_raw.duplicated(["date", "code"]).any()):
        raise ValueError("Historical context rows require the explicit source policy and earlier unique dates")
    paths, original = _rank_path(panel_by_day, members, start, end)
    environments = _environment(panel_by_day, sessions, now)
    cases = [_case(raw, source_days, sessions, previous, panel_by_day, paths[identity][cap_name], environments,
        members, qualification, context_anchor, start, end, identity, cap_name, guard,
        f"leader-eight-type:{out.name}:{identity}:{guard}:{cap_name}",
        execution_policy_factory=execution_policy_factory, context_raw=context_raw)
        for identity in TYPE_IDS for guard in GUARDS for cap_name, _ in CAPS]
    result = {"schema": SCHEMA, "status": "completed_research_only_mechanical_comparison",
        "created_at": datetime.now().astimezone().isoformat(), "start": start, "end": end,
        "source_days": source_days, "protocol": deepcopy(PROTOCOL), "config": _config(100000, 5).to_dict(),
        "scope_members": sorted(members), "source": provenance or {}, "environment_decisions": environments,
        "raw_top5_paths": original, "capacity_paths": paths, "case_count": len(cases), "cases": cases,
        "case_selection_performed": False, "all_cases_complete_PnL": all(case["complete_PnL_available"] for case in cases),
        "account_created": True, "persistent_account_used": False, "late_context": True,
        "actual_decision_time_used_for_execution": False,
        "limitations": ["Thirty-two new fixed cases, not a change to or rerun of the original formal twenty-case experiment.",
            "Historical dated evidence was derived later; EOD/next-open Engine timing is a mechanical assumption, not actual publication.",
            "Original type ranks and cumulative capacity are shared by guarded and unguarded cases; no refill or profitable-case selection.",
            "Empty and unresolved types preserve NO_CHANGE; known environment exits and actual-fill expiry independently reduce shares.",
            "Incomplete six-session execution tail, blocked marks/actions, residual holdings or no fills leave metrics null.",
            "Reference consistency is only a one-percent proxy; PIT, company-action coverage and economic total returns remain unverified."], **FLAGS}
    if execution_policy_factory is not None:
        result.update(historical_execution_policy=execution_policy_factory.provenance,
            returns_reportable=False)
        result["limitations"][-1] = ("Explicit whole-native/action-formula checks replace the old one-percent proxy; "
            "posthoc listing metadata, dated-roster/PIT, company-action completeness and economic total returns remain unverified.")
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "result.json", result)
    for case in cases:
        folder = out / case["case_id"]
        folder.mkdir()
        for key in ("decisions", "daily", "orders", "fills", "account"):
            write_json(folder / (key + ".json"), case[key])
    return result


def load_inputs(panel, ipo_evidence, research_master_run):
    """Verify saved inputs only; no Engine/state/target/output or successor needed."""
    path = Path(panel).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now().astimezone()
    if (value.get("schema") != INPUT_SCHEMA or value.get("candidate_panel_only") is not True
            or any(value.get(key) is not False for key in ("execution_admitted", "selection_enabled", "strategy_validated", "model_refit", "future_labels_used_for_score"))
            or _stamp(value["created_at"]) > now or _stamp(value["scope_frozen_at"]) > _stamp(value["created_at"])):
        raise ValueError("Explicit saved unadmitted eight-type panel manifest required")
    def locate(field):
        return (path.parent / value[field]).resolve()
    calendar = locate("calendar")
    raw_file = locate("raw_file")
    if digest(calendar) != value["calendar_sha256"] or digest(raw_file) != value["raw_sha256"]:
        raise ValueError("Panel calendar or raw observation bytes changed")
    external_protocol = None
    if value.get("protocol_file") is not None or value.get("protocol_sha256") is not None:
        protocol_file = locate("protocol_file")
        if digest(protocol_file) != value["protocol_sha256"]:
            raise ValueError("External fixed eight-type protocol bytes changed")
        external_protocol = {"file": str(protocol_file), "sha256": value["protocol_sha256"],
            "record": json.loads(protocol_file.read_text(encoding="utf-8"))}
    raw = pd.read_parquet(raw_file)
    if not set(FIELDS.split(",")) <= set(raw):
        raise ValueError("Actual Bao raw fields including status and nullable values are required")
    proofs = value["source_proofs"]
    if [proof["day"] for proof in proofs] != value["source_days"]:
        raise ValueError("Panel source dates and complete proofs differ")
    for proof in proofs:
        actual, metadata = load_complete(proof["source_folder"])
        if source_proof(actual, metadata, value["calendar_sha256"]) != proof:
            raise ValueError("Source completion proof changed")
        saved = raw.loc[raw.date.eq(proof["day"]), FIELDS.split(",")].reset_index(drop=True)
        if saved.to_dict("records") != actual.to_dict("records"):
            raise ValueError("Panel raw rows must equal each full dated source; no survivor filtering or zero imputation")
    panels = {}
    for item in value["panels"]:
        day = item["day"]
        if day in panels or item["source_proof"] != next((proof for proof in proofs if proof["day"] == day), None):
            raise ValueError("Duplicate or differently sourced dated type panel")
        proof = item["source_proof"]
        terminal = _stamp(proof.get("source_completed_at") or proof.get("source_updated_at"))
        if not terminal <= _stamp(item["available_at"]) <= _stamp(value["created_at"]):
            raise ValueError("Panel recording precedes its actual complete-source availability")
        panels[day] = item
    anchor, anchor_proof = _context_anchor({"history_snapshot": str(locate("history_snapshot")),
        "original_history_manifest_sha256": value["original_history_manifest_sha256"], "base_history_end": value["base_history_end"]})
    qualification = load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    calendar_payload = json.loads(calendar.read_text(encoding="utf-8"))
    _validate_panel_data(panels, raw, value["source_days"], calendar_payload, value["scope_members"], qualification)
    return {"panel_by_day": panels, "raw": raw, "source_days": value["source_days"],
        "calendar_payload": calendar_payload, "scope_members": value["scope_members"],
        "qualification": qualification, "context_anchor": anchor,
        "provenance": {"panel": str(path), "panel_sha256": digest(path), "created_at": value["created_at"],
            "raw_file": str(raw_file), "raw_sha256": value["raw_sha256"], "source_proofs": proofs,
            "scope_basis": value["scope_basis"], "scope_frozen_at": value["scope_frozen_at"],
            "context_anchor": anchor_proof, "IPO_qualification": qualification.verification,
            "external_fixed_protocol": external_protocol, "input_record": value}}


def run(panel, ipo_evidence, research_master_run, out, *, start, end):
    return replay_eight_types(**load_inputs(panel, ipo_evidence, research_master_run), start=start, end=end, out=out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--ipo-evidence", type=Path, required=True)
    parser.add_argument("--research-master-run", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.panel, args.ipo_evidence, args.research_master_run, args.out, start=args.start, end=args.end)
    print(json.dumps({key: result[key] for key in ("status", "start", "end", "case_count", "all_cases_complete_PnL", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
