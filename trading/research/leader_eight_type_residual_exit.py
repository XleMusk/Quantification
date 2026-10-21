"""Explicit residual full exits for a separately registered eight-type method.

Entry anchors, structural assessments and five-session expiry stay unchanged.
The sole Engine/Ledger still attempts every order. This opt-in method retains
an already requested full exit until its actual quantity is zero; it never
changes the original 32 books or the dated catch-up method.
"""
from copy import deepcopy
from dataclasses import replace

from ..contracts import IntentKind, SignalUnavailable
from ..engine import ReplayRiskExitIntent
from .leader_dated_catchup_exit import _captured_exits, _remaining
from .leader_published_risk_exit import PublishedCloseLeaderTypeRiskExitPolicy, RULE_SPEC as BASE_RULES
from .leader_type_risk_exit import TYPE_IDS
from .shortline_decision_v2 import fixed_exit_policy
from .shortline_decision_v3 import _known_raw


TARGET_POLICY_ID = "leader_eight_type_published_target_sticky_reduction_v1"
RISK_POLICY_ID = "leader_eight_type_structure_expiry_sticky_queue_v1"
RULE_SPEC = {**BASE_RULES, "id": RISK_POLICY_ID, "target_policy_id": TARGET_POLICY_ID,
    "scope": "new_eight_type_residual_method_only_original_32_and_dated4_unchanged",
    "type_ids": list(TYPE_IDS),
    "residual_full_exit": "confirmed_risk_or_actual_zero_target_retry_until_actual_flat",
    "unknown_or_recovered_structure": "does_not_cancel_existing_full_exit",
    "new_target_reincludes_residual": "remove_weight_until_actual_flat_no_renormalization",
    "partial_reweight_or_old_buy": "not_promoted_to_sticky_full_exit_or_retried_buy",
    "reentry_after_flat": "subsequent_close_may_publish_new_entry",
    "original_entry_structure_and_five_session_thresholds_changed": False,
    "execution_lifecycle_changed": True, "signal_only_comparison": False}


class EightTypeResidualTargetReduction:
    policy_id = TARGET_POLICY_ID

    def __init__(self, feed, state, config):
        self.state = state
        self.captured = _captured_exits(state)
        self.expiry = fixed_exit_policy(feed, state, config)

    def __call__(self, intent):
        reduced = self.expiry(intent)
        if reduced.kind != IntentKind.FULL_TARGET:
            return reduced
        remaining = set(_remaining(self.state, intent.decided_at[:10], self.captured))
        weights = tuple((symbol, weight) for symbol, weight in reduced.weights if symbol not in remaining)
        return reduced if weights == reduced.weights else replace(reduced, weights=weights,
            reason="eight_type_existing_full_exit_residual_no_reentry_until_flat")


class EightTypeResidualRiskPolicy(PublishedCloseLeaderTypeRiskExitPolicy):
    def __init__(self, type_id, *, state, **kwargs):
        if type_id not in TYPE_IDS:
            raise ValueError("The registered residual exit method requires an original eight-type identity")
        super().__init__(type_id, **kwargs)
        self.policy_id = RISK_POLICY_ID + ":" + type_id
        self.captured = _captured_exits(state)
        self.original_risk = deepcopy(state.get("pending_risk_exits"))

    def __call__(self, day, book, feed):
        current = super().__call__(day, book, feed)
        remaining = _remaining(book.state, day, self.captured)
        if not remaining:
            return current
        if isinstance(current, SignalUnavailable):
            raise ValueError("Residual full exit cannot be discarded with unavailable risk evidence: " + current.reason)
        indices = {session: index for index, session in enumerate(feed.calendar)}
        if day not in indices or indices[day] + 1 >= len(feed.calendar):
            raise ValueError("Residual full exit requires the exact next exchange session")
        if any(not _known_raw(feed.rows.get(day, {}).get(symbol)) for symbol in remaining):
            raise ValueError("Residual full exit needs known current raw/status; no price imputation")
        symbols = tuple(sorted(set(remaining) | set(current.symbols if current else ())))
        following = feed.calendar[indices[day] + 1]
        book.emit(day, "eight_type_full_exit_residual_requeued", type_id=self.type_id,
            symbols=list(remaining), remaining_quantities={symbol: book.quantity(symbol) for symbol in remaining},
            original_risk_receipt=self.original_risk, execution_not_before=following,
            old_buy_order_retried=False, current_unknown_does_not_cancel=True)
        return ReplayRiskExitIntent(book.context, day, following, symbols,
            "research:eight_type_residual_full_exit", "already_requested_full_exit_remaining_until_actual_flat")


def risk_policy(identity, *, state, **kwargs):
    return EightTypeResidualRiskPolicy(identity, state=state, **kwargs)


def target_policy(feed, state, config):
    return EightTypeResidualTargetReduction(feed, state, config)
