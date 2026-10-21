"""Publish fixed close-only research target weights, without waiting for D+1 bars.

This is a read-only consumer of the existing producer, selector and Ledger.
It never sizes/submits orders, advances an account, or substitutes a later
execution day when a source close was acquired after its intended next open.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path

import pandas as pd

from ..contracts import IntentKind, MARKET_TZ, PortfolioIntent, RunContext, TradingMode
from ..engine import choose_targets
from ..ledger import Ledger
from ..market_dataset import write_json
from ..settings import ROOT
from .leader_daily_replay import EXIT_POLICIES, FAMILIES, POOLS, freeze_cohorts
from .leader_daily_signals import _calendar_view
from .leader_forward_run import _case_key, _pending_source_packages, _protocol, _validate_bundle, _check_feature_binding, _check_source_binding, _ipo_proof
from .provider_close_batches import build_provider_close_bundle, scope_provider_batches
from .shortline_decision_v2 import MAX_HOLDING_SESSIONS, holding_origin, verified_breadth
from .shortline_decision_v3 import POLICY_ID
from .shortline_panel_replay import FUTURE_LABEL_COLUMNS
from .ipo_qualification import IpoQualification, load_ipo_qualification


SCHEMA = "leader_close_target_publication_v1"


def _now():
    return datetime.now(MARKET_TZ)


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Publication and source clocks require timezone-aware ISO timestamps")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Publication and source clocks require explicit timezone")
    return parsed.astimezone(MARKET_TZ)


def _checkpoint(path, binding, day):
    if path is None:
        return None
    head = json.loads(Path(path).read_text(encoding="utf-8"))
    if (head.get("schema") != "leader_forward_shared_ledger_checkpoint_v1"
            or head.get("binding") != binding or head.get("execution_admitted") is not False):
        raise ValueError("Ledger evidence is not the same frozen unadmitted research checkpoint")
    return head


def _ledger_case(head, key, day, config):
    if head is None:
        return None, None, "no_current_ledger_evidence"
    case = head.get("cases", {}).get(key)
    if case is None:
        return None, None, "case_ledger_evidence_missing"
    if case.get("state", {}).get("day") != day:
        return None, None, "ledger_asof_not_current_source_close"
    state = case["state"]
    context_payload = state.get("replay_risk_exit_policy", {}).get("context")
    context = RunContext.from_dict(context_payload)
    if (context.mode != TradingMode.REPLAY or not context.account_id.endswith(":" + key)
            or state["replay_risk_exit_policy"].get("policy_id") != POLICY_ID
            or _case_key(case["pool"], case["family"], case["exit_policy"]) != key
            or any(fill["day"] > day for fill in state["fills"].values())):
        raise ValueError("Current Ledger evidence changed account/strategy/policy or contains later fills")
    return Ledger(state, config, context), context, "same_ledger_current_source_close"


def load_fixed_cohort_registration(path, binding, raw, now):
    """Read a method registered before first E; keep the initial full cohort."""
    path = Path(path).resolve()
    before = sha256(path.read_bytes()).hexdigest()
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    original = value["original_publication"]
    publication_path = Path(original["file"]).resolve()
    publication_bytes = publication_path.read_bytes()
    publication = json.loads(publication_bytes.decode("utf-8-sig"))
    first, execution = value["first_decision_day"], value["first_execution_day"]
    calendar_payload = json.loads(Path(binding["calendar"]).read_text(encoding="utf-8"))
    year = calendar_payload["year"]
    sessions, _, status = _calendar_view(calendar_payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar" or first not in sessions or sessions.index(first) + 1 >= len(sessions):
        raise ValueError("Fixed-cohort registration needs an exact initial exchange session")
    cohort = freeze_cohorts(raw, first)
    keys = [_case_key(pool, family, policy) for pool in POOLS for family in FAMILIES for policy in EXIT_POLICIES]
    summary = {"anchor": cohort["anchor"], "selection": cohort["selection"],
        "counts": {pool: len(symbols) for pool, symbols in cohort["pools"].items()}}
    recorded, stamp = _stamp(publication["recorded_at"]), _stamp(value["registered_at"])
    deadline = _stamp(execution + "T09:30:00+08:00")
    if (value.get("schema") != "leader_published_close_forward_registration_v1"
            or execution != sessions[sessions.index(first) + 1]
            or value.get("first_execution_deadline") != deadline.isoformat()
            or not recorded <= stamp < deadline or stamp > now
            or value.get("protocol_binding") != binding or value.get("config") != binding["config"]
            or value.get("window") != {"length_open_sessions": 40, "execution_tail_sessions": 6}
            or recorded.date().isoformat() != first
            or value.get("case_ids") != keys or value.get("cohort") != cohort
            or publication.get("schema") != SCHEMA or publication.get("source_close_day") != first
            or publication.get("proposed_execution_day") != execution
            or publication.get("next_open_deadline") != deadline.isoformat()
            or publication.get("status") != "close_research_target_publication_only"
            or publication.get("protocol_binding") != binding or publication.get("cohort") != summary
            or original.get("recorded_at") != publication["recorded_at"]
            or original.get("sha256") != sha256(publication_bytes).hexdigest()
            or value.get("registration_is_original_publication") is not False
            or any(value.get(k) is not False for k in
                ("strategy_validated", "selection_enabled", "execution_admitted", "model_refit", "future_labels_used_for_score"))
            or value.get("candidate_panel_only") is not True
            or any(publication.get(k) is not False for k in
                ("strategy_validated", "selection_enabled", "execution_admitted", "model_refit", "future_labels_used_for_score"))
            or sha256(path.read_bytes()).hexdigest() != before
            or publication_path.read_bytes() != publication_bytes):
        raise ValueError("Actual fixed-cohort registration or original publication changed")
    return deepcopy(cohort), {"file": str(path), "sha256": before, "registered_at": value["registered_at"],
        "first_decision_day": first, "first_execution_day": execution}


def prepare_close_case_targets(bundle, *, cohort, head, day, calendar, config, ipo_qualification, decision_tail=False,
                               case_ids=None):
    """Pure dated target preparation, shared with the publication consumer."""
    all_ids = tuple(_case_key(pool, family, policy) for pool in POOLS for family in FAMILIES for policy in EXIT_POLICIES)
    selected = all_ids if case_ids is None else tuple(case_ids)
    if (not selected or len(set(selected)) != len(selected)
            or selected != tuple(key for key in all_ids if key in selected)):
        raise ValueError("Close target subset must preserve declared case identities and order")
    prepared = []
    for pool in POOLS:
        if not any(key.startswith(pool + ":") for key in selected):
            continue
        symbols = cohort["pools"][pool]
        scoped, coverage = scope_provider_batches(bundle, symbols)
        for family in FAMILIES:
            for exit_policy in EXIT_POLICIES:
                key = _case_key(pool, family, exit_policy)
                if key not in selected:
                    continue
                batch = scoped[family].get(day)
                book, context, ledger_status = _ledger_case(head, key, day, config)
                held = [symbol for symbol in book.state["positions"] if book.quantity(symbol)] if book else []
                reason, weights, expired, raw_rule_weights, qualification_rows = None, None, [], None, []
                if decision_tail:
                    reason = "registered_forward_tail_no_new_decision"
                elif batch is None or batch.get("status") != "complete":
                    reason = "source_or_frozen_cohort_candidate_batch_incomplete"
                elif exit_policy == EXIT_POLICIES[1] and verified_breadth(batch) is None:
                    reason = "verified_full_market_breadth_unavailable"
                else:
                    candidates = batch.get("candidates")
                    if not isinstance(candidates, list):
                        raise ValueError("Complete source batch requires explicit candidates, including empty")
                    if any(FUTURE_LABEL_COLUMNS.intersection(row) or not math.isfinite(float(row["score"])) for row in candidates):
                        raise ValueError("Target candidates contain future labels or invalid scores")
                    raw_rule_weights = choose_targets(candidates, held, config)
                    supported = []
                    unknown = False
                    for candidate in candidates:
                        proof = (ipo_qualification.qualify(day, candidate["symbol"]) if ipo_qualification is not None else
                                 {"symbol": candidate["symbol"], "day": day, "ipo_window_feature_support": False,
                                  "knowledge_status": "unknown", "ipo_feature_exclusion_reason": "explicit_listing_qualification_missing",
                                  "pit_verified": False, "execution_admitted": False})
                        qualification_rows.append(proof)
                        if proof["ipo_window_feature_support"]:
                            supported.append(candidate)
                        elif proof["knowledge_status"] == "unknown":
                            unknown = True
                    # A known first-five no-limit session is outside these
                    # fixed limit-up features. Unknown qualification is not a
                    # known-empty eligible pool and must not become cash.
                    if unknown:
                        reason = "candidate_listing_window_qualification_unknown"
                        weights = None
                    else:
                        weights = choose_targets(supported, held, config)
                    if book is not None:
                        indices = {session: index for index, session in enumerate(calendar)}
                        for symbol in held:
                            origin = holding_origin(book.state, symbol)
                            if origin not in indices or origin > day:
                                reason = "current_ledger_holding_age_not_observed_in_pinned_calendar"
                                weights = None
                                break
                            if indices[day] - indices[origin] + 1 >= MAX_HOLDING_SESSIONS:
                                expired.append(symbol)
                        if weights is not None:
                            weights = {symbol: weight for symbol, weight in weights.items() if symbol not in expired}
                    if (weights is not None and exit_policy == EXIT_POLICIES[1]
                            and verified_breadth(batch) < 0):
                        weights = {}
                prepared.append({"case_id": key, "family": family, "pool": pool, "exit_policy": exit_policy,
                                 "ledger_status": ledger_status, "context": context, "weights": weights,
                                 "raw_rule_weights_before_qualification": raw_rule_weights,
                                 "candidate_listing_qualification": qualification_rows,
                                 "no_change_reason": reason, "expired_held_symbols": expired,
                                 "coverage": deepcopy(coverage.get(day))})
    return prepared


def publish_close_targets(bundle, out_dir, *, protocol_path, ledger_checkpoint=None,
                          publication_at=None, decision_day=None, ipo_qualification=None, cohort_registration=None):
    """Save actual publication evidence and complete target weights; never orders.

    An optional caller time is an unverified claim. The actual local recording
    time controls timeliness and the typed intent, so it cannot backdate a plan.
    Current-D Ledger evidence is read only; missing/stale evidence produces an
    explicitly unbound pure-rule preview, never a new account.
    """
    raw, complete_days = _validate_bundle(bundle)
    binding, calendar_payload, config = _protocol(protocol_path)
    _check_feature_binding(bundle, binding)
    _check_source_binding(bundle, binding)
    if ipo_qualification is None and binding.get("ipo_qualification") is not None:
        saved = binding["ipo_qualification"]
        ipo_qualification = load_ipo_qualification(saved["evidence_file"], saved["calendar"],
            master_run=(saved.get("research_master") or {}).get("run"))
    if ipo_qualification is not None and not isinstance(ipo_qualification, IpoQualification):
        raise ValueError("Close targets require explicit typed IPO qualification, not inferred first-observed dates")
    day = decision_day or complete_days[-1]
    if day not in complete_days or bundle.get("calendar_payload") != calendar_payload:
        raise ValueError("Publication needs the exact complete close and frozen exchange calendar")
    if str(raw.date.max()) > day or complete_days[-1] > day:
        raise ValueError("Close publication may consume only D and earlier observations")
    year = calendar_payload["year"]
    calendar, _, status = _calendar_view(calendar_payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar" or day not in calendar or calendar.index(day) + 1 >= len(calendar):
        raise ValueError("Actual next exchange session is unavailable")
    execution_day = calendar[calendar.index(day) + 1]
    source_rows = [source for source in bundle.get("source_packages", []) if source.get("day") == day]
    if len(source_rows) != 1 or not source_rows[0].get("terminal_completion_observed_at"):
        raise ValueError("A real recorded source terminal-completion timestamp is required")
    source = deepcopy(source_rows[0])
    source_ready = _stamp(source["terminal_completion_observed_at"])
    close_at = _stamp(day + "T15:00:00+08:00")
    if source_ready < close_at:
        raise ValueError("Completed close source was recorded before the actual session close")
    history = binding.get("history_source_view")
    if history is not None:
        source_ready = max(source_ready, _stamp(history["available_at"]), _stamp(binding["history_source_specified_at"]))
    if cohort_registration is not None and ledger_checkpoint is not None:
        raise ValueError("Choose one fixed-cohort registration or current Ledger evidence")
    head = _checkpoint(ledger_checkpoint, binding, day)
    use_frozen_account_cohort = head is not None and head.get("start", "9999") <= day
    cohort_proof = None
    if cohort_registration is not None:
        cohort, cohort_proof = load_fixed_cohort_registration(cohort_registration, binding, raw, _now())
        if day < cohort_proof["first_decision_day"]:
            raise ValueError("Publication precedes the fixed cohort registration window")
    else:
        cohort = deepcopy(head["cohort"]) if use_frozen_account_cohort else freeze_cohorts(raw, day)
    if cohort.get("anchor", day) >= day or set(cohort.get("pools", {})) != set(POOLS):
        raise ValueError("Target universe requires the original fixed pre-close cohort recipe")
    decision_tail = False
    if cohort_proof is not None:
        window_index = calendar.index(day) - calendar.index(cohort_proof["first_decision_day"])
        if not 0 <= window_index < 46:
            raise ValueError("Registered forty-session window and six-session tail cannot shift or extend")
        decision_tail = window_index >= 40
    prepared = prepare_close_case_targets(bundle, cohort=cohort, head=head, day=day,
        calendar=calendar, config=config, ipo_qualification=ipo_qualification, decision_tail=decision_tail)
    publication_input = None
    if cohort_proof is not None:
        from .leader_published_inputs import save_bundle, input_proof
        out = Path(out_dir).resolve()
        out.mkdir(parents=True, exist_ok=False)
        input_path = save_bundle(bundle, out / "input_bundle", origin="saved_before_actual_close_publication")
        publication_input = input_proof(input_path)
    # Candidate/cohort work is finished before taking the actual publish clock.
    recorded = _now().astimezone(MARKET_TZ)
    if recorded < source_ready or recorded < close_at:
        raise ValueError("Actual recording time precedes source completion or source close")
    claimed = _stamp(publication_at) if publication_at is not None else None
    if claimed is not None and (claimed > recorded or claimed < source_ready or claimed < close_at):
        raise ValueError("Claimed publication cannot be future-dated or precede completed close evidence")
    opening = _stamp(execution_day + "T09:30:00+08:00")
    late = recorded >= opening
    # Existing EOD intent contract cannot bind execution to the same local day
    # as an after-midnight publication. Keep that separate binding pending.
    intent_execution_day = execution_day if recorded.date().isoformat() < execution_day else None
    cases = []
    for item in prepared:
        context = item.pop("context")
        weights = item.pop("weights")
        if weights is None:
            intent = PortfolioIntent.no_change(item["no_change_reason"], context=context, decided_at=recorded.isoformat())
            case_status = "unavailable_fixed_rule_target"
        else:
            intent = PortfolioIntent.full_target(weights, context=context, decided_at=recorded.isoformat(),
                                                  execution_not_before=intent_execution_day,
                                                  source=f"research:leader_close_publication:{item['family']}")
            case_status = ("late_context_only" if late else "account_bound_research_target_preview" if context is not None
                           else "unbound_pure_rule_target_preview")
        item.update(status=case_status, portfolio_intent=intent.to_dict(),
                    target_weights=dict(intent.weights),
                    target_cash_weight=1 - sum(weight for _, weight in intent.weights) if intent.kind == IntentKind.FULL_TARGET else None,
                    formal_target_admitted=False,
                    current_pit_eligibility="unverified", current_company_actions="unverified",
                    eligibility_unknown_cannot_promote_target=True,
                    holdings_retention_and_expiry_account_bound=context is not None,
                    order_sizing_binding_pending=True, execution_admitted=False)
        cases.append(item)
    result = {"schema": SCHEMA, "status": "late_context_only" if late else "close_research_target_publication_only",
              "source_close_day": day, "proposed_execution_day": execution_day,
              "next_open_deadline": opening.isoformat(), "publication_at": recorded.isoformat(),
              "recorded_at": recorded.isoformat(), "publication_origin": "local_recording_now",
              "claimed_publication_at": claimed.isoformat() if claimed else None,
              "claimed_time_verified": False, "timeliness_uses_actual_recording": True,
              "publication_time_backfilled": False, "execution_day_rolled_forward": False,
              "source_terminal_completion": source, "protocol_binding": binding,
              "ipo_qualification": deepcopy(ipo_qualification.verification) if ipo_qualification is not None else None,
              "mechanical_support_filter": {"id": "leader_close_ipo_feature_support_filter_v1",
                  "basis": "Explicit source qualification only; known unsupported no-limit IPO sessions excluded; unknown means NO_CHANGE",
                  "old_producer_or_protocol_modified": False, "return_based_filter": False,
                  "raw_rule_weights_preserved_for_audit": True, "pit_admission_inferred": False},
              "cohort": {"anchor": cohort["anchor"], "selection": cohort["selection"],
                         "counts": {pool: len(symbols) for pool, symbols in cohort["pools"].items()}},
              "ledger_checkpoint": str(Path(ledger_checkpoint).resolve()) if ledger_checkpoint else None,
              "cases": cases, "case_count": len(cases), "successor_raw_required_for_target_weights": False,
              "successor_raw_used": False, "execution_binding_pending": intent_execution_day is None,
              "orders_generated": False, "ths_payload_generated": False, "account_created": False,
              "ledger_mutated": False, "broker_interacted": False, "formal_target_admitted": False,
              "source_verified": False, "pit_verified": False, "corporate_actions_verified": False,
              "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
              "model_refit": False, "future_labels_used_for_score": False, "execution_admitted": False,
              "future_independent_window_advanced": False,
              "limitations": ["Weights are fixed-rule research targets, not qualified or selected account orders.",
                              "No current same-Ledger evidence means retention/expiry is not an account-specific decision.",
                              "Unknown PIT/IPO/action qualifications remain blocked; a target publication cannot clear them.",
                              "D+1 raw prices, quantities, costs and account observations are separate order-sizing requirements.",
                              "Late source capture remains context only for the original next open; no D+2 substitution."]}
    if bundle.get("feature_qualification") is not None:
        result["feature_qualification"] = deepcopy(bundle["feature_qualification"])
        result["raw_rule_weights_semantics"] = "before_current_day_listing_filter_after_explicit_listing_feature_qualification"
        result["mechanical_support_filter"].update(new_feature_method_opt_in=True, old_default_behavior_preserved=True)
    if history is not None:
        result.update(recent_source_history=deepcopy(history), history_input_available_at=history["available_at"],
                      history_source_specified_at=binding["history_source_specified_at"], source_ready_at=source_ready.isoformat())
    if cohort_proof is not None:
        result["frozen_cohort_registration"] = cohort_proof
        result["publication_input_bundle"] = publication_input
        result["registered_window_tail"] = decision_tail
    out = Path(out_dir).resolve()
    if cohort_proof is None:
        out.mkdir(parents=True, exist_ok=False)
    write_json(out / "publication.json", result)
    rows = [{"case_id": case["case_id"], "status": case["status"], "symbol": symbol,
             "target_weight": weight, "formal_target_admitted": False}
            for case in cases for symbol, weight in case["target_weights"].items()]
    pd.DataFrame(rows, columns=["case_id", "status", "symbol", "target_weight", "formal_target_admitted"]).to_csv(
        out / "target_weights.csv", index=False, encoding="utf-8-sig")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, action="append", required=True)
    parser.add_argument("--history-snapshot", type=Path, required=True)
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--ledger-checkpoint", type=Path)
    parser.add_argument("--cohort-registration", type=Path, help="Keep the original pre-window cohort for published-forward research")
    parser.add_argument("--publication-at", help="Optional unverified earlier claim; actual recording controls timeliness")
    parser.add_argument("--decision-day")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--gate-output", type=Path, help="Save only a blocked/waiting source gate receipt")
    args = parser.parse_args(argv)
    try:
        pending = _pending_source_packages(args.package)
        if pending:
            result = {"schema": "leader_close_publication_gate_v1", "status": "waiting_for_complete_source_close",
                      "source_packages": pending, "publication_created": False, "account_created": False,
                      "orders_generated": False, "execution_admitted": False}
            if args.gate_output:
                write_json(args.gate_output, result)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        source_binding = _protocol(args.protocol)[0]
        calendar = args.calendar or Path(source_binding["calendar"])
        options = ({"ipo_qualification": _ipo_proof(source_binding)}
                   if source_binding.get("feature_qualification_method_id") else {})
        if source_binding.get("history_source_view") is not None:
            options["recent_source_view"] = source_binding["history_source_view"]["view_file"]
        bundle = build_provider_close_bundle(args.package, args.history_snapshot, calendar, **options)
        out = args.out or ROOT / "outputs/leader_close_publication" / _now().strftime("%Y%m%d_%H%M%S_%f")
        result = publish_close_targets(bundle, out, protocol_path=args.protocol,
                                       ledger_checkpoint=args.ledger_checkpoint, publication_at=args.publication_at,
                                       decision_day=args.decision_day, cohort_registration=args.cohort_registration)
    except (OSError, ValueError, KeyError) as error:
        result = {"schema": "leader_close_publication_gate_v1", "status": "failed_source_or_publication_gate",
                  "error_type": type(error).__name__, "error": str(error), "publication_created": False,
                  "account_created": False, "orders_generated": False, "execution_admitted": False}
        if args.gate_output:
            write_json(args.gate_output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps({"status": result["status"], "source_close_day": result["source_close_day"],
                      "proposed_execution_day": result["proposed_execution_day"], "case_count": result["case_count"],
                      "publication_file": str(Path(out).resolve() / "publication.json"),
                      "orders_generated": False, "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
