"""Post-opening, reduce-only entry-confirmation arms for published research targets."""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace

from ..contracts import IntentKind, PortfolioIntent, TradingMode
from ..ledger import Ledger
from .shortline_decision_v2 import MAX_HOLDING_SESSIONS, fixed_exit_policy


ARM_IDS = ("baseline", "daily_confirmation", "entry_only_confirmation")
POLICY_IDS = {
    arm: "leader_published_entry_confirmation_" + arm + "_v1" for arm in ARM_IDS
}
RULE_SPEC = {
    "id": "leader_published_entry_confirmation_three_arm_v1",
    "arms": list(ARM_IDS),
    "policy_ids": dict(POLICY_IDS),
    "scope": "externally_frozen_common20_original_at_most_five_unique_targets_per_family",
    "application_time": "after_D_old_pending_opening_fills_marks_and_actions",
    "confirmation": "reported_turn_D_strictly_gt_exact_previous_and_D_high_gt_low",
    "confirmation_source_clock_and_units": "validated_by_frozen_sidecar_not_this_policy",
    "holding_definition": "actual_post_opening_Ledger_quantity_strictly_positive",
    "held_includes": ["partial_fill", "same_day_T1_locked", "residual_shares", "addition_to_existing_position"],
    "entry_definition": "original_remaining_target_with_actual_post_opening_quantity_zero",
    "daily_confirmation_scope": "all_original_targets_remaining_after_fixed_exit",
    "entry_only_confirmation_scope": "only_zero_quantity_targets_remaining_after_fixed_exit",
    "daily_confirmation_can_remove_held_targets": True,
    "entry_only_held_confirmation_bypass": True,
    "fixed_exit_first": True,
    "max_holding_open_sessions": MAX_HOLDING_SESSIONS,
    "independent_expiry_queue_required_in_consumer": True,
    "original_NO_CHANGE": "preserve_without_confirmation_or_cash_target",
    "complete_empty_FULL_TARGET": "preserve_explicit_exit_without_confirmation",
    "applicable_unknown": "whole_case_NO_CHANGE_preserving_context_time_source_exact_E",
    "known_false": "remove_only_original_symbol_without_refill_or_weight_increase",
    "known_true": "preserve_original_weight",
    "held_not_in_original_target": "never_added_back",
    "return_or_coverage_based_arm_selection": False,
}
FLAGS = {
    "research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "current_signal_admitted": False, "provider_contacted": False,
    "broker_interacted": False, "ledger_mutated": False, "orders_generated": False,
    "future_labels_used_for_score": False, "pit_verified": False,
    "corporate_actions_verified": False,
}


class PublishedEntryConfirmationReduction:
    """Inspect the live trial Ledger; record observations without emitting events."""

    def __init__(self, feed, state, config, *, arm_id, confirmation):
        if arm_id not in ARM_IDS:
            raise ValueError("Unknown frozen entry-confirmation arm")
        if not isinstance(confirmation, Mapping):
            raise ValueError("Confirmation must be the current decision's symbol mapping")
        self.arm_id, self.policy_id = arm_id, POLICY_IDS[arm_id]
        self.feed, self.state, self.config = feed, state, config
        self.confirmation = deepcopy(dict(confirmation))
        self.reduce = fixed_exit_policy(feed, state, config)
        self.observation = None

    def __call__(self, intent):
        if (not isinstance(intent, PortfolioIntent) or intent.context is None
                or intent.context.mode != TradingMode.REPLAY
                or intent.kind not in (IntentKind.NO_CHANGE, IntentKind.FULL_TARGET)):
            raise ValueError("Entry confirmation requires a bound replay portfolio intent")
        book = Ledger(self.state, self.config, intent.context)
        quantities = {symbol: book.quantity(symbol) for symbol, _ in intent.weights}
        reduced = intent if intent.kind == IntentKind.NO_CHANGE else self.reduce(intent)
        remaining = dict(reduced.weights)
        evaluations = []
        unknown = False
        kept = []
        for symbol, weight in intent.weights:
            quantity = quantities[symbol]
            present = symbol in remaining
            applies = (present and self.arm_id != "baseline"
                       and (self.arm_id == "daily_confirmation" or quantity == 0))
            evidence = self.confirmation.get(symbol)
            status = evidence.get("status") if isinstance(evidence, Mapping) else None
            reason = evidence.get("reason") if isinstance(evidence, Mapping) else None
            if applies and status not in ("known_true", "known_false", "unknown"):
                status, reason = "unknown", "confirmation_missing_or_invalid_status"
            if applies and status == "unknown":
                unknown = True
            retained = present and (not applies or status == "known_true")
            if retained:
                kept.append((symbol, weight))
            evaluations.append({
                "symbol": symbol, "original_weight": weight,
                "post_opening_quantity": quantity,
                "position_role": "held_or_addition_to_existing" if quantity > 0 else "zero_quantity_new_entry",
                "remaining_after_fixed_exit": present,
                "confirmation_applied": applies,
                "confirmation_status": status,
                "confirmation_reason": reason,
                "application": ("removed_by_fixed_exit" if not present else
                                "baseline_no_confirmation" if self.arm_id == "baseline" else
                                "held_exempt_from_entry_confirmation" if not applies else
                                "applicable_confirmation"),
                "retained_before_whole_case_unknown": retained,
            })
        if intent.kind == IntentKind.NO_CHANGE:
            result, application = intent, "original_NO_CHANGE_preserved"
        elif not intent.weights:
            result, application = reduced, "original_complete_empty_exit_preserved"
        elif unknown:
            result = replace(reduced, kind=IntentKind.NO_CHANGE, weights=(),
                             reason="entry_confirmation_applicable_observation_unknown:" + self.arm_id)
            application = "whole_case_NO_CHANGE_applicable_unknown"
        else:
            weights = tuple(kept)
            result = reduced if weights == reduced.weights else replace(
                reduced, weights=weights, reason="entry_confirmation_known_false_removed:" + self.arm_id)
            application = "reduced_or_preserved_FULL_TARGET"
        final_symbols = set(dict(result.weights))
        for row in evaluations:
            row["retained_in_final_target"] = (None if result.kind == IntentKind.NO_CHANGE
                                                else row["symbol"] in final_symbols)
        self.observation = {
            "schema": "leader_published_entry_confirmation_observation_v1",
            "arm_id": self.arm_id, "policy_id": self.policy_id,
            "decision_day": intent.decided_at[:10], "decided_at": intent.decided_at,
            "execution_not_before": intent.execution_not_before, "source": intent.source,
            "post_opening_quantities": quantities,
            "fixed_exit_removed_symbols": [s for s, _ in intent.weights if s not in remaining],
            "evaluations": evaluations, "application": application,
            "applicable_unknown": unknown, "no_refill": True, "weights_renormalized": False,
            "original_intent": intent.to_dict(), "after_fixed_exit_intent": reduced.to_dict(),
            "final_intent": result.to_dict(), **FLAGS,
        }
        return result
