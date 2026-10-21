"""Lookahead-safe event-time checks shared by replay and paper consumers.

The existing SignalTime contract validates one signal and SignalFrame validates
the session cutoff.  This module records the session-level frontier that the
replay and later broker consumers need to carry forward: the latest signal
availability, the decision timestamp, and the first executable session.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta


MARKET_TZ = timezone(timedelta(hours=8))


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Event frontier timestamps must be offset-aware ISO strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Event frontier timestamps must be offset-aware")
    return parsed


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Event frontier sessions must be YYYY-MM-DD")
    return value


@dataclass(frozen=True)
class FrontierReceipt:
    """Immutable session receipt for a validated signal-to-execution chain."""

    session_day: str
    decision_at: str
    execution_not_before: str
    signal_count: int
    signal_event_min: str | None
    signal_event_max: str | None
    signal_available_max: str | None
    availability_evidence: tuple[str, ...]

    def __post_init__(self):
        _day(self.session_day)
        decision = _timestamp(self.decision_at)
        execution_day = _day(self.execution_not_before)
        if execution_day <= self.session_day:
            raise ValueError("Event frontier execution must be after the decision session")
        if type(self.signal_count) is not int or self.signal_count < 0:
            raise ValueError("Event frontier signal count must be a non-negative integer")
        if self.signal_count == 0:
            if any(value is not None for value in (self.signal_event_min, self.signal_event_max,
                                                    self.signal_available_max)):
                raise ValueError("Empty event frontier cannot carry signal timestamps")
            return
        if not all(isinstance(value, str) for value in
                   (self.signal_event_min, self.signal_event_max, self.signal_available_max)):
            raise ValueError("Non-empty event frontier needs signal timestamps")
        event_min = _timestamp(self.signal_event_min)
        event_max = _timestamp(self.signal_event_max)
        available_max = _timestamp(self.signal_available_max)
        if event_min > event_max or available_max < event_max or available_max > decision:
            raise ValueError("Signal event/availability order violates the decision frontier")
        if event_min.astimezone(MARKET_TZ).date().isoformat() != self.session_day:
            raise ValueError("Signal event is outside its decision session")
        if decision.astimezone(MARKET_TZ).date().isoformat() != self.session_day:
            raise ValueError("Decision timestamp is outside its decision session")
        if not self.availability_evidence or any(not isinstance(value, str) or not value
                                                  for value in self.availability_evidence):
            raise ValueError("Availability evidence must be explicit")

    @property
    def status(self):
        return "validated_with_signals" if self.signal_count else "validated_no_signal"

    def to_dict(self):
        return {
            "schema": "event_time_frontier_v1",
            "status": self.status,
            "session_day": self.session_day,
            "decision_at": self.decision_at,
            "execution_not_before": self.execution_not_before,
            "signal_count": self.signal_count,
            "signal_event_min": self.signal_event_min,
            "signal_event_max": self.signal_event_max,
            "signal_available_max": self.signal_available_max,
            "availability_evidence": list(self.availability_evidence),
        }


def validate_signal_frontier(signal_frame, *, decision_at, execution_day):
    """Validate and return the session frontier for one typed SignalFrame.

    SignalFrame remains the source of per-signal validation.  This function
    adds the cross-object invariant used by the replay receipt and intentionally
    refuses a same-session execution date.
    """
    session_day = _day(signal_frame.day)
    decision = _timestamp(decision_at)
    execution_day = _day(execution_day)
    if decision.astimezone(MARKET_TZ).date().isoformat() != session_day:
        raise ValueError("Decision timestamp is outside the signal session")
    events, available, evidence = [], [], []
    for signal in signal_frame.signals:
        event = _timestamp(signal.time.event_time)
        stamp = _timestamp(signal.time.available_at)
        if event.astimezone(MARKET_TZ).date().isoformat() != session_day:
            raise ValueError("Signal event is outside the signal session")
        if stamp < event or stamp > decision:
            raise ValueError("Signal is unavailable at the portfolio decision")
        events.append(event)
        available.append(stamp)
        evidence.append(signal.time.availability_evidence)
    receipt = FrontierReceipt(
        session_day=session_day,
        decision_at=decision_at,
        execution_not_before=execution_day,
        signal_count=len(events),
        signal_event_min=min(events).isoformat() if events else None,
        signal_event_max=max(events).isoformat() if events else None,
        signal_available_max=max(available).isoformat() if available else None,
        availability_evidence=tuple(sorted(set(evidence))),
    )
    return receipt.to_dict()


def summarize_frontiers(frontiers):
    """Create a compact replay-level receipt without discarding session detail."""
    sessions = [dict(value) for value in frontiers]
    invalid = [value for value in sessions if value.get("status") not in
               {"validated_with_signals", "validated_no_signal"}]
    return {
        "schema": "event_time_frontier_replay_v1",
        "status": "invalid" if invalid else "validated",
        "sessions": sessions,
        "session_count": len(sessions),
        "signal_sessions": sum(value.get("signal_count", 0) > 0 for value in sessions),
        "assumption_sessions": sum("historical_eod_assumption" in value.get("availability_evidence", [])
                                    for value in sessions),
        "invalid_sessions": len(invalid),
        "live_feed_connected": False,
    }
