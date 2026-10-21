"""Single-account, fixed cyclic routing over saved eight-type evidence.

Only the existing Engine/Ledger writes positions, targets, orders and fills.
This new basket-level mechanism deliberately differs from the old independent
32 cases: one confirmed original per-stock risk exit winds down the owner.
"""
from copy import deepcopy
from dataclasses import replace
import math

from ..contracts import IntentKind, PortfolioIntent, SignalUnavailable, TradingMode, decision_time
from ..engine import ReplayRiskExitIntent, advance
from ..published_close_router import PublishedCloseRouterEnvelope, POLICY_ID as PUBLISHED_POLICY_ID
from .leader_daily_replay import CommonCohortDecisionFeed
from .leader_eight_type_panel_replay import TYPE_IDS, GUARDS, CAPS, _QualifiedFeed
from .leader_type_risk_exit import LeaderTypeRiskExitPolicy, surviving_buy_anchor
from .recent_alpha158_panel_replay import _raw_issues, _reference
from .shortline_decision_v2 import BATCH_SCHEMA, holding_origin, MAX_HOLDING_SESSIONS
from .shortline_decision_v3 import FiveSessionExpiryPolicy


METHOD_ID = "single_active_type_cyclic_environment_switch_shared_engine_v1"
POLICY_ID = METHOD_ID + ":owner_risk"
STATE_KEY = "leader_type_router"
SPEC = {"id": METHOD_ID, "types": list(TYPE_IDS), "guards": list(GUARDS),
    "capacities": [name for name, _ in CAPS],
    "selection": "calendar_index_D_minus_original_start_mod8_then_cyclic_first_allowed_type_before_candidates",
    "guard": "original_two_close_environment_ALLOW_FIXED_CANDIDATES_only_for_new_owner",
    "empty_or_unknown_selected_type": "NO_CHANGE_no_other_type_fallback",
    "owner": "retain_while_any_actual_position_or_old_positive_pending_target",
    "postopening_flat": "empty_FULL_cancel_old_pending_release_owner_next_D_only",
    "old_pending_attempt": "preframe_empty_filled_NO_CHANGE_else_empty_FULL_cancel",
    "basket_exit": "any_original_confirmed_structural_or_five_session_exit_or_environment_EXIT_ALL_sets_sticky_empty_FULL_for_entire_owner",
    "unknown_risk": "does_not_start_basket_exit_original_risk_defer_preserved",
    "exit_stage": "empty_FULL_until_actual_flat_no_rebuy_or_type_switch",
    "entry_fifo": "unchanged_actual_Ledger_fills_no_type_switch_anchor_reset",
    "tail": "reduce_only_no_new_owner_runner_explicit_EXIT_ALL_on_original_tail5_close_for_tail6_open",
    "risk": "original_type_structure_and_five_session_with_fixed_outer_policy_id_cached_once_per_D",
    "target_expiry": "original_holding_origin_and_five_session_D_fact_triggers_basket_empty_FULL_without_parallel_target_overlay",
    "decision_execution_boundary": "D_known_structure_or_holding_expiry_starts_exit_even_if_original_typed_risk_defers_on_E_quotes_unknown",
    "risk_difference_from_independent32": "basket_level_exit_is_new_not_a_pure_type_selection_change",
    "capital": "original_100000_five_slots_gross0.8_each0.16_original_costs_and_T1",
    "historical_signal_publication_verified": False, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False}


def _held(state):
    return {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}


def _positive_pending(state):
    return {symbol for symbol, weight in (state.get("pending_targets") or {}).items()
            if isinstance(weight, (int, float)) and not isinstance(weight, bool)
            and math.isfinite(weight) and weight > 0}


class _OwnerRisk:
    policy_id = POLICY_ID

    def __init__(self, router, state):
        self.router, self.state = router, state
        self.ready, self.result = False, None

    def validate_session(self, state, feed, day, context):
        FiveSessionExpiryPolicy().validate_session(state, feed, day, context)

    def prepare(self, day, book, feed):
        if self.ready:
            return self.result
        owner = self.state[STATE_KEY]["active_type"]
        held = _held(self.state)
        if held and owner not in TYPE_IDS:
            raise ValueError("Actual positions require their saved owner; never infer a new type")
        if held:
            # Only surviving actual FIFO entry dates and exact own-D features.
            # The old policy constructor copies these small candidate maps;
            # its read-only feature view is not eight copies of the full year.
            anchors = [surviving_buy_anchor(self.state, symbol, feed.calendar, day)[0]
                       for symbol in sorted(held)]
            candidate_days = {anchor["decision_day"] for anchor in anchors if anchor is not None}
            candidates = {date: self.router._item(date, owner).get("candidates", [])
                          for date in candidate_days if date <= day}
            delegate = LeaderTypeRiskExitPolicy(owner, candidate_by_day=candidates, feature_by_day={})
            panel = self.router.panels.get(day, {})
            delegate.features = {day: panel.get("risk_features", {})}
            delegate.complete_feature_days = frozenset([day] if panel.get("risk_feature_scope_complete") is True else [])
            before_events = len(self.state["events"])
            self.result = delegate(day, book, feed)
            invalid = any(event.get("kind") == "leader_type_risk_assessed"
                          and event.get("policy_id") == delegate.policy_id and event.get("status") == "invalid"
                          for event in self.state["events"][before_events:])
            indices = {date: index for index, date in enumerate(feed.calendar)}
            origins = [holding_origin(self.state, symbol) for symbol in sorted(held)]
            if any(origin not in indices or indices[origin] > indices[day] for origin in origins):
                raise ValueError("Router expiry requires the original actual continuous holding origin")
            expired = any(indices[day] - indices[origin] + 1 >= MAX_HOLDING_SESSIONS for origin in origins)
            if invalid or expired:
                self.state[STATE_KEY]["exit_sticky"] = True
                self.state[STATE_KEY]["exit_reason"] = ("original_D_structure_basket_exit" if invalid
                                                        else "original_D_five_session_basket_exit")
        self.ready = True
        return self.result

    def __call__(self, day, book, feed):
        return self.prepare(day, book, feed)


class _CloseFeed:
    """Declare the existing Engine close hook, with an immutable account binding."""
    def __init__(self, base, router, state, pre, audit, risk):
        self.base, self.router, self.state = base, router, state
        self.pre, self.audit, self.risk = pre, audit, risk
        self.close_candidate_policy = {"id": METHOD_ID + ":close", "capacity": router.capacity,
            "capital_policy": "original_fixed_five_slots_0.8_gross_no_cash_transfer"}

    def __getattr__(self, key):
        return getattr(self.base, key)

    def signal_frame(self, day, config, context):
        return self.base.signal_frame(day, config, context)

    def close_signal_frame(self, day, config, context, original, book):
        action, reason = post_open_decision(self.state, self.pre, self.audit, self.risk, day, book, self)
        return (replace(original, signals=()) if action == "EMPTY_FULL" else
                SignalUnavailable(reason) if action == "NO_CHANGE" else original)


def post_open_decision(state, pre, audit, risk, day, book, feed):
    """One owner/basket decision for both legacy and actual saved publication.

    Called only after the same Engine's opening fills, marks and actions. No
    order, position or target is written here; only the trial's router metadata
    and the original risk diagnostic events are updated.
    """
    post, meta = _held(state), state[STATE_KEY]
    audit["post_opening_held_symbols"] = sorted(post)
    if (pre["held"] or pre["pending"]) and not post:
        meta.update(active_type=None, exit_sticky=False, exit_reason=None)
        audit["close_rule"] = "actual_flat_cancel_old_pending_then_release_owner"
        risk.prepare(day, book, feed)
        return "EMPTY_FULL", audit["close_rule"]
    if not pre["held"] and not pre["pending"]:
        meta["active_type"] = audit["selected_type"]
    risk.prepare(day, book, feed)
    if audit["explicit_exit"]:
        meta.update(exit_sticky=bool(post), exit_reason=audit["explicit_exit"] if post else None)
    if meta["exit_sticky"] or audit["explicit_exit"]:
        audit["close_rule"] = "sticky_owner_basket_empty_FULL"
        if not post:
            meta.update(active_type=None, exit_sticky=False, exit_reason=None)
        return "EMPTY_FULL", audit["close_rule"]
    if pre["pending"] and not pre["held"]:
        audit["close_rule"] = "old_pending_actual_fill_preserve_exact_shares"
        return "NO_CHANGE", "router_old_pending_open_attempt_filled_preserve_owner_and_shares"
    if audit["reasons"]:
        if not post:
            meta["active_type"] = None
        audit["close_rule"] = "selected_type_unknown_or_empty_NO_CHANGE"
        return "NO_CHANGE", "router:" + ";".join(audit["reasons"])
    audit["close_rule"] = "original_selected_type_candidates"
    return "SOURCE", audit["close_rule"]


class _PublishedClosePolicy:
    policy_id = PUBLISHED_POLICY_ID

    def __init__(self, prepared, risk):
        self.prepared, self.risk = prepared, risk

    def __call__(self, envelope, book, feed, config):
        value = self.prepared
        action, reason = post_open_decision(value["trial"], value["pre"], value["audit"],
            self.risk, envelope.source_day, book, feed)
        original = envelope.original_intent
        if action == "SOURCE":
            return original
        if action == "EMPTY_FULL":
            return replace(original, kind=IntentKind.FULL_TARGET, weights=(), reason=reason)
        return replace(original, kind=IntentKind.NO_CHANGE, weights=(), reason=reason)


class LeaderTypeRouter:
    def __init__(self, base_feed, config, context, *, panels, paths, environments,
                 sessions, source_days, members, qualification, execution,
                 start, end, guard, capacity):
        if (context.mode != TradingMode.REPLAY or guard not in GUARDS
                or capacity not in {name for name, _ in CAPS} or config.holdings != 5
                or config.gross_limit != .8 or config.initial_cash != 100000
                or config.rebalance_sessions != 1):
            raise ValueError("The fixed four-arm single-account research config is required")
        if (start not in sessions or end not in sessions or start > end
                or list(sessions) != sorted(set(sessions))
                or list(source_days) != sorted(set(source_days))
                or not set(source_days).issubset(sessions)):
            raise ValueError("Exact ordered source/exchange sessions and original period are required")
        self.base, self.config, self.context = base_feed, config, context
        self.panels, self.paths, self.environments = panels, paths, environments
        self.sessions, self.source_days, self.members = list(sessions), set(source_days), set(members)
        self.qualification, self.execution = qualification, execution
        self.start, self.end, self.guard, self.capacity = start, end, guard, capacity
        self.binding = {"method_id": METHOD_ID, "guard": guard, "capacity": capacity,
            "start": start, "end": end, "context": context.to_dict()}
        self.previous = {date: sessions[index - 1] if index else None for index, date in enumerate(sessions)}

    def _item(self, day, owner):
        return next((item for item in self.panels.get(day, {}).get("types", []) if item["id"] == owner), {})

    def _mode(self, day, owner):
        if self.guard == "unguarded":
            return "ALLOW_FIXED_CANDIDATES"
        return self.environments.get(day, {}).get("per_type", {}).get(owner, {}).get("mode", "NO_CHANGE")

    def _checks(self, day, required, *, opening=False, published_at=None):
        raw = (self.execution.raw_issues(day, required) if self.execution else _raw_issues(self.base, day, required))
        refs = (self.execution.references(day, required) if self.execution else
                [_reference(self.base, day, symbol, self.previous) for symbol in sorted(required)])
        ok = self.execution.reference_ok if self.execution else lambda row: row["status"] == "consistent_proxy"
        ipo = [(self.qualification.qualify(day, symbol, decision_as_of=published_at)
                if published_at is not None and not opening else
                self.qualification.assess(symbol, day, decision_as_of=day +
                    ("T09:30:00+08:00" if opening else "T15:05:00+08:00"))) for symbol in sorted(required)]
        return {"raw_issues": raw, "reference_evidence": refs, "IPO_evidence": ipo,
                "blocked": bool(raw or any(not ok(row) for row in refs)
                                or any(row["engine_block_reason"] is not None for row in ipo))}

    def _prepare(self, state, day, session_number, *, tail_mode="NO_CHANGE", published_at=None):
        if day not in self.source_days or tail_mode not in {"NO_CHANGE", "EXIT_ALL"}:
            raise ValueError("An exact saved source day and explicit reduce-only tail mode are required")
        offset = self.sessions.index(day) - self.sessions.index(self.start)
        if offset < 0 or session_number != offset:
            raise ValueError("Router clock must equal the original exact calendar offset, never successful-row count")
        if tail_mode == "EXIT_ALL" and day <= self.end:
            raise ValueError("Study-end liquidation is a tail-only explicit boundary")
        held, pending = _held(state), _positive_pending(state)
        saved = state.get(STATE_KEY)
        if saved is not None and saved.get("binding") != self.binding:
            raise ValueError("Saved router owner binding changed")
        owner = (saved or {}).get("active_type")
        if (held or pending) and owner not in TYPE_IDS:
            raise ValueError("Actual holdings or positive pending targets need the original saved owner")
        owned = self._checks(day, held | set(state.get("pending_targets") or {}), opening=True)
        if owned["blocked"]:
            raise ValueError("held_or_pending_raw_or_action_reference_unknown: " + str(owned))
        trial = deepcopy(state)
        meta = trial.setdefault(STATE_KEY, {"binding": deepcopy(self.binding), "active_type": None,
                                           "exit_sticky": False, "exit_reason": None})
        following = self.sessions[self.sessions.index(day) + 1] if self.sessions.index(day) + 1 < len(self.sessions) else None
        in_window = self.start <= day <= self.end
        selected_type = owner if held or pending else None
        cycle = [TYPE_IDS[(offset + index) % len(TYPE_IDS)] for index in range(len(TYPE_IDS))]
        if selected_type is None and in_window:
            selected_type = next((identity for identity in cycle
                                  if self._mode(day, identity) == "ALLOW_FIXED_CANDIDATES"), None)
            # Keep the actual pre-opening owner until the Engine's callback.
            # A published envelope binds this pre-D owner, not the future one.
            meta.update(active_type=None, exit_sticky=False, exit_reason=None)
        mode = self._mode(day, selected_type) if selected_type else "NO_CHANGE"
        reasons, selected = [], []
        explicit_exit = "study_end_tail_liquidation" if tail_mode == "EXIT_ALL" else None
        if in_window and owner and mode == "EXIT_ALL":
            explicit_exit = "original_environment_EXIT_ALL"
        if not in_window and not explicit_exit:
            reasons.append("execution_tail_no_new_entry")
        elif selected_type is None and not explicit_exit:
            reasons.append("no_environment_allowed_type")
        elif mode != "ALLOW_FIXED_CANDIDATES" and not explicit_exit:
            reasons.append("environment_unknown_or_unconfirmed")
        if in_window and selected_type and not explicit_exit and not meta["exit_sticky"] and not (pending and not held):
            item = self._item(day, selected_type)
            path = self.paths.get(selected_type, {}).get(self.capacity, {}).get(day)
            if item.get("status") not in {"matched", "not_matched"}:
                reasons.append("type_observation_unknown")
            if set(item.get("unresolved_symbols", [])).intersection(self.members):
                reasons.append("type_membership_unknown_in_fixed_panel")
            if path is None:
                reasons.append("exact_type_capacity_path_unknown")
            else:
                selected = path["candidates"]
            if not selected:
                reasons.append("type_empty_or_capacity_filtered_all_no_signal")
        checks = None
        if selected and not reasons:
            checks = self._checks(day, {row["symbol"] for row in selected}, published_at=published_at)
            if checks["blocked"]:
                reasons.append("selected_current_raw_reference_or_IPO_unknown")
        # An empty complete original frame is only a hook carrier. The hook
        # distinguishes explicit empty FULL from known empty/unknown NC.
        candidates = [{"symbol": row["symbol"], "score": row["score"]} for row in selected] if not reasons else []
        if (pending and not held) or meta["exit_sticky"] or explicit_exit:
            candidates = []
        audit = {"day": day, "execution_day": following, "calendar_offset": offset,
            "cycle": cycle, "pre_owner": owner, "selected_type": selected_type,
            "pre_held_symbols": sorted(held), "pre_positive_pending_symbols": sorted(pending),
            "environment_mode": mode, "explicit_exit": explicit_exit, "reasons": reasons,
            "selected_symbols": [row["symbol"] for row in candidates], "selected_checks": checks,
            "owned_checks": owned, "candidate_content_used_to_select_type": False,
            "future_candidates_used": False}
        return {"trial": trial, "audit": audit, "candidates": candidates,
                "pre": {"held": held, "pending": pending}, "following": following}

    def _feed(self, prepared, day, *, published=False):
        batch = {"schema": BATCH_SCHEMA, "day": day, "source": "research:" + METHOD_ID,
            "status": "complete" if published or prepared["following"] in self.source_days else "incomplete",
            "candidates": prepared["candidates"]}
        common = (self.execution.decision_feed({day: batch}, prepared["trial"]) if self.execution else
                  CommonCohortDecisionFeed(self.base, {day: batch}, prepared["trial"], market_defense=False))
        return _QualifiedFeed(common, self.qualification)

    def _finish(self, prepared, day, row):
        trial, audit = prepared["trial"], prepared["audit"]
        meta = trial[STATE_KEY]
        meta["last_day"] = day
        audit.update(post_owner=meta["active_type"], exit_sticky=meta["exit_sticky"],
            exit_reason=meta["exit_reason"], status="NO_CHANGE" if any(event["kind"] == "no_decision"
                for event in row["events"]) else "EMPTY_FULL" if not trial.get("targets") else "FULL_TARGET")
        return trial, row, audit

    def step(self, state, day, session_number, *, tail_mode="NO_CHANGE"):
        prepared = self._prepare(state, day, session_number, tail_mode=tail_mode)
        trial = prepared["trial"]
        qualified = self._feed(prepared, day)
        risk = _OwnerRisk(self, trial)
        feed = _CloseFeed(qualified, self, trial, prepared["pre"], prepared["audit"], risk)
        wrapped_risk = self.execution.wrap_risk_policy(risk) if self.execution else risk
        row = advance(trial, feed, day, self.config, session_number, context=self.context,
            replay_risk_exit_policy=wrapped_risk)
        return self._finish(prepared, day, row)

    def _envelope(self, prepared, day, *, published_at, source):
        audit, pre = prepared["audit"], prepared["pre"]
        metadata = dict(context=self.context, decided_at=decision_time(day),
            execution_not_before=prepared["following"], source=source)
        if (audit["explicit_exit"] or prepared["trial"][STATE_KEY]["exit_sticky"]):
            intent = PortfolioIntent.full_target({}, **metadata)
        elif audit["reasons"] or pre["pending"] and not pre["held"]:
            intent = PortfolioIntent.no_change(";".join(audit["reasons"]) or "router_old_pending_open_attempt", **metadata)
        else:
            intent = PortfolioIntent.full_target({row["symbol"]: .16 for row in prepared["candidates"]}, **metadata)
        return PublishedCloseRouterEnvelope(intent, day, prepared["following"], published_at,
            PUBLISHED_POLICY_ID, audit["pre_owner"], tuple(sorted(pre["held"] | pre["pending"])))

    def publication_envelope(self, state, day, session_number, *, published_at, source, tail_mode="NO_CHANGE"):
        """Pure source-D input bound to the actual pre-D research checkpoint.

        The publisher seals all dated evidence first; the consumer supplies its
        original saved publication clock. E quotes are never queried here.
        """
        prepared = self._prepare(state, day, session_number, tail_mode=tail_mode, published_at=published_at)
        return self._envelope(prepared, day, published_at=published_at, source=source)

    def step_published(self, state, day, session_number, *, envelope, tail_mode="NO_CHANGE"):
        if not isinstance(envelope, PublishedCloseRouterEnvelope):
            raise ValueError("Actual published-close router requires its explicit envelope")
        prepared = self._prepare(state, day, session_number, tail_mode=tail_mode, published_at=envelope.published_at)
        expected = self._envelope(prepared, day, published_at=envelope.published_at, source=envelope.original_intent.source)
        if envelope != expected:
            raise ValueError("Published router input differs from the exact saved D evidence or pre-D owner checkpoint")
        trial = prepared["trial"]
        qualified = self._feed(prepared, day, published=True)
        risk = _OwnerRisk(self, trial)
        wrapped_risk = self.execution.wrap_risk_policy(risk) if self.execution else risk
        row = advance(trial, qualified, day, self.config, session_number, context=self.context,
            replay_risk_exit_policy=wrapped_risk, published_close_router=envelope,
            published_close_router_policy=_PublishedClosePolicy(prepared, risk))
        return self._finish(prepared, day, row)
