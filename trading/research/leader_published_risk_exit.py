"""Explicit forward-close queueing contract for the original eight-type risks.

The original historical policy still requires an observed successor raw bar.
This opt-in policy queues reductions from known D evidence and an exact E in
the calendar. The published-close consumer must validate E raw/reference/status
before that actual session may mutate its research Ledger. No future bar is
created, read or used to choose a reduction here.
"""
from ..contracts import SignalUnavailable, TradingMode
from ..engine import ReplayRiskExitIntent
from .leader_type_risk_exit import LeaderTypeRiskExitPolicy, RULE_SPEC as ORIGINAL_RULE_SPEC
from .shortline_decision_v2 import MAX_HOLDING_SESSIONS, holding_origin
from .shortline_decision_v3 import _known_raw


POLICY_ID = "leader_eight_type_published_close_structure_and_expiry_queue_v1"
SIGNAL_SOURCE = "research:leader_published_close_structure_and_expiry_queue_v1"
RULE_SPEC = {**ORIGINAL_RULE_SPEC, "id": POLICY_ID,
    "availability_contract": "known_D_close_exact_calendar_E_queue_then_validate_E_before_execution",
    "successor_raw_required_to_queue": False, "future_raw_fabricated": False,
    "original_historical_policy_modified": False}


class PublishedCloseLeaderTypeRiskExitPolicy(LeaderTypeRiskExitPolicy):
    """Reuse fixed entry anchors/evaluation; change only E-bar availability."""

    def __init__(self, type_id, **kwargs):
        super().__init__(type_id, **kwargs)
        self.policy_id = POLICY_ID + ":" + type_id

    def __call__(self, day, book, feed):
        if book.context.mode != TradingMode.REPLAY:
            raise ValueError("Published-close risk queueing is replay-only")
        indices = {session: index for index, session in enumerate(feed.calendar)}
        if day not in indices:
            raise ValueError("An exact current exchange session is required")
        held = sorted(symbol for symbol in book.state["positions"] if book.quantity(symbol))
        expired, invalid = [], []
        for symbol in held:
            origin = holding_origin(book.state, symbol)
            if origin not in indices or indices[origin] > indices[day]:
                raise ValueError("Expiry requires the continuous holding's actual buy-fill session")
            if indices[day] - indices[origin] + 1 >= MAX_HOLDING_SESSIONS:
                expired.append(symbol)
            result = self.evaluate(day, book, feed, symbol)
            book.emit(day, "leader_type_risk_assessed", policy_id=self.policy_id, **result)
            if result["status"] == "invalid":
                invalid.append(symbol)
            elif result["status"] == "unknown":
                book.emit(day, "leader_type_risk_deferred", symbol=symbol, type_id=self.type_id,
                    reasons=result["reasons"], independent_expiry_not_suppressed=True)
        symbols = tuple(sorted(set(expired) | set(invalid)))
        if not symbols:
            return None
        if indices[day] + 1 >= len(feed.calendar):
            return SignalUnavailable("published_risk_exact_next_calendar_session_unavailable")
        required = set(held) | set(book.state.get("pending_targets") or {})
        if any(not _known_raw(feed.rows.get(day, {}).get(symbol)) for symbol in required):
            return SignalUnavailable("published_risk_current_raw_or_status_unavailable")
        following = feed.calendar[indices[day] + 1]
        book.emit(day, "published_risk_reduction_queued_from_current_close", policy_id=self.policy_id,
            execution_not_before=following, symbols=list(symbols),
            successor_raw_inspected=False, successor_raw_fabricated=False,
            successor_execution_quality_pending=True)
        return ReplayRiskExitIntent(book.context, day, following, symbols, SIGNAL_SOURCE,
            "fixed_structure_or_five_session_expiry_queue_E_quality_checked_before_execution")
