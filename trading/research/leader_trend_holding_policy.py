"""Opt-in trend holding-clock control; all original strategy rules stay intact.

Only the two five-session expiry paths are disabled. Original structural
evaluation, entry/FIFO anchors, rank replacement, environment decisions and
Engine execution constraints remain outside or underneath this policy.
"""
from ..contracts import SignalUnavailable, TradingMode
from ..engine import ReplayRiskExitIntent
from .leader_type_risk_exit import LeaderTypeRiskExitPolicy, RULE_SPEC as ORIGINAL_RULE_SPEC
from .shortline_decision_v3 import FiveSessionExpiryPolicy, _known_raw


METHOD_ID = "leader_trend_structure_without_five_session_holding_clock_v1"
POLICY_ID = METHOD_ID + ":risk"
SIGNAL_SOURCE = "research:" + METHOD_ID
RULE_SPEC = {**ORIGINAL_RULE_SPEC, "id": METHOD_ID,
    "type_ids": ["trend_leader"], "expiry_enabled": False,
    "target_expiry_enabled": False, "risk_expiry_enabled": False,
    "expiry": "disabled_at_target_and_independent_risk_paths",
    "unknown": "defer_structural_exit_no_calendar_expiry_in_this_arm",
    "target_policy": "None_use_original_Engine_CommonCohort_target_allocation",
    "structure": "unchanged_parent_trend_close_sma20_sma60_and_actual_FIFO_entry",
    "environment_and_rank_replacement": "unchanged_original_runner_decisions",
    "study_end": "new_runner_separately_registered_terminal_clock_not_original_expiry_tail",
    "execution": "unchanged_Engine_and_original_current_successor_raw_guards",
    "old_rules_or_registrations_modified": False,
    "parameters_selected_by_PnL": False}


class NoHoldingClockPolicy(FiveSessionExpiryPolicy):
    """Keep the parent's exact-session/owned-quote validation, emit no expiry."""

    policy_id = METHOD_ID + ":no_calendar_expiry"

    def __call__(self, day, book, feed):
        if book.context.mode != TradingMode.REPLAY:
            raise ValueError("Independent shortline expiry is replay-only")
        if day not in feed.calendar:
            raise ValueError("Independent expiry requires a declared exchange session")
        # Engine's original validation entry may be explicitly wrapped for
        # native suspensions; do not bypass that wrapper with a second guard.
        return None


class TrendStructureOnlyRiskPolicy(LeaderTypeRiskExitPolicy):
    """Original trend structure and anchors, with no independent age deadline."""

    def __init__(self, type_id, *, candidate_by_day, feature_by_day, complete_feature_days=()):
        if type_id != "trend_leader":
            raise ValueError("This independently registered holding-clock control is trend-only")
        super().__init__(type_id, candidate_by_day=candidate_by_day,
            feature_by_day=feature_by_day, complete_feature_days=complete_feature_days)
        self.policy_id = POLICY_ID
        self.expiry = NoHoldingClockPolicy()

    def __call__(self, day, book, feed):
        if book.context.mode != TradingMode.REPLAY:
            raise ValueError("Trend holding-clock control is replay-only")
        self.expiry(day, book, feed)
        held = sorted(symbol for symbol in book.state["positions"] if book.quantity(symbol))
        invalid = []
        for symbol in held:
            result = self.evaluate(day, book, feed, symbol)
            book.emit(day, "leader_type_risk_assessed", policy_id=self.policy_id,
                expiry_enabled=False, **result)
            if result["status"] == "invalid":
                invalid.append(symbol)
            elif result["status"] == "unknown":
                book.emit(day, "leader_type_risk_deferred", symbol=symbol,
                    type_id=self.type_id, reasons=result["reasons"], expiry_enabled=False,
                    structural_exit_deferred=True)
        if not invalid:
            return None
        next_index = feed.calendar.index(day) + 1
        if next_index >= len(feed.calendar):
            return SignalUnavailable("leader_type_risk_actual_next_session_unavailable")
        following = feed.calendar[next_index]
        required = set(held) | set(book.state.get("pending_targets") or {})
        if any(not _known_raw(feed.rows.get(date, {}).get(symbol))
               for date in (day, following) for symbol in required):
            return SignalUnavailable("leader_type_risk_relevant_raw_or_status_unavailable")
        return ReplayRiskExitIntent(book.context, day, following, tuple(sorted(invalid)),
            SIGNAL_SOURCE, "fixed_trend_structure_invalid_no_calendar_holding_expiry")


def risk_policy(type_id, *, candidate_by_day, feature_by_day, complete_feature_days=()):
    return TrendStructureOnlyRiskPolicy(type_id, candidate_by_day=candidate_by_day,
        feature_by_day=feature_by_day, complete_feature_days=complete_feature_days)


def target_policy(feed, state, config):
    """An explicit factory returning None; never fall back to fixed_exit_policy."""
    return None
