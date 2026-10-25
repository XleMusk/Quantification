"""Small, deterministic contracts for the financial-factor F2 time rules.

The functions in this module deliberately have no clock, filesystem, network,
calendar-provider, or account dependency.  They are the narrow semantic layer
that a financial-factor producer can call before it hands a candidate to the
existing research and Engine/Ledger paths.

Two clocks must stay separate here:

* a source that only supplies a publication *day* has an interval of possible
  event times; its conservative trading availability uses the interval's
  later boundary;
* a reference value is a strict pre-event witness only when its own possible
  availability finishes before the event's earliest possible time.  A same-day
  exact timestamp therefore cannot prove ordering against a date-only event.

This module is research-only.  It never enables broker access and carries no
``execution_admitted`` state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


MARKET_TZ = timezone(timedelta(hours=8))
PRE_EVENT_PROVEN = "PRE_EVENT_PROVEN"
PRE_EVENT_UNPROVEN = "PRE_EVENT_UNPROVEN"
MATURE = "MATURE"
IMMATURE = "IMMATURE"


def _timestamp(value: datetime | str, *, field: str = "timestamp") -> datetime:
    """Parse an offset-aware timestamp without consulting the current clock."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        if "T" not in value:
            raise ValueError(f"{field} must include an explicit time and offset")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO timestamp") from exc
    else:
        raise TypeError(f"{field} must be an offset-aware datetime or ISO timestamp")
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must be offset-aware")
    return parsed


def _day(value: str, *, field: str = "session day") -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be YYYY-MM-DD")
    try:
        canonical = date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if canonical != value:
        raise ValueError(f"{field} must be canonical YYYY-MM-DD")
    return value


@dataclass(frozen=True)
class TimeWindow:
    """A possible availability/event window.

    ``end`` is exclusive for an interval.  ``end=None`` represents one exact
    instant.  Keeping the distinction makes an interval ending exactly at a
    later event's start provably earlier while preventing an exact timestamp
    at that same start from being treated as earlier.
    """

    start: datetime
    end: datetime | None = None

    def __post_init__(self) -> None:
        start = _timestamp(self.start, field="window start")
        object.__setattr__(self, "start", start)
        if self.end is not None:
            end = _timestamp(self.end, field="window end")
            if end <= start:
                raise ValueError("interval end must be after its start")
            object.__setattr__(self, "end", end)

    @classmethod
    def exact(cls, value: datetime | str) -> "TimeWindow":
        return cls(_timestamp(value))

    @classmethod
    def interval(cls, start: datetime | str, end: datetime | str) -> "TimeWindow":
        return cls(_timestamp(start), _timestamp(end))

    @property
    def kind(self) -> str:
        return "EXACT" if self.end is None else "INTERVAL"

    def to_dict(self) -> dict[str, str | None]:
        return {"kind": self.kind, "start": self.start.isoformat(),
                "end": self.end.isoformat() if self.end is not None else None}


def publication_interval(publication_day: str) -> TimeWindow:
    """Return ``[D 00:00, D+1 00:00)`` in Asia/Shanghai.

    A date-only disclosure is intentionally *not* converted into a point at
    midnight.  Midnight is the earliest possible event time, while the end of
    the interval is the conservative latest boundary used for trade timing.
    """

    day = _day(publication_day, field="publication day")
    start = datetime.combine(date.fromisoformat(day), time.min, tzinfo=MARKET_TZ)
    return TimeWindow.interval(start, start + timedelta(days=1))


def strictly_before(candidate: TimeWindow, event: TimeWindow) -> bool:
    """Whether ``candidate`` proves strict precedence over every event time.

    For an exact candidate and interval event, the candidate must be before the
    event interval's *start*.  For an interval candidate, its exclusive end
    may equal the event start; this is the half-open boundary that proves all
    candidate times occurred earlier.
    """

    if not isinstance(candidate, TimeWindow) or not isinstance(event, TimeWindow):
        raise TypeError("strictly_before requires two TimeWindow values")
    if candidate.end is None:
        return candidate.start < event.start
    return candidate.end <= event.start


def conservative_available_at(event: TimeWindow,
                              processing_delay: timedelta = timedelta(0)) -> datetime:
    """Return the conservative first availability after an event.

    For an interval event this uses its later boundary, never its midpoint or
    earliest boundary.  The delay is explicit and cannot be negative.
    """

    if not isinstance(event, TimeWindow):
        raise TypeError("event must be a TimeWindow")
    if not isinstance(processing_delay, timedelta) or processing_delay < timedelta(0):
        raise ValueError("processing_delay must be a non-negative timedelta")
    return (event.end if event.end is not None else event.start) + processing_delay


@dataclass(frozen=True)
class ReferenceObservation:
    """One candidate point-in-time reference value.

    ``period_end`` is used only to choose the newest *proven* value after
    filtering.  It is deliberately separate from availability: report-period
    selection must not be allowed to choose a later revision first.
    """

    reference_id: str
    period_end: str
    available: TimeWindow
    value: Any
    eligible: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.reference_id, str) or not self.reference_id:
            raise ValueError("reference_id is required")
        _day(self.period_end, field="period_end")
        if not isinstance(self.available, TimeWindow):
            raise TypeError("available must be a TimeWindow")
        if type(self.eligible) is not bool:
            raise TypeError("eligible must be bool")


@dataclass(frozen=True)
class ReferenceSelection:
    status: str
    selected: ReferenceObservation | None
    reason: str
    considered: int

    @property
    def reference_id(self) -> str | None:
        return self.selected.reference_id if self.selected is not None else None

    @property
    def value(self) -> Any:
        return self.selected.value if self.selected is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reference_id": self.reference_id,
                "reason": self.reason, "considered": self.considered}


def select_pre_event_reference(candidates: Iterable[ReferenceObservation],
                               event: TimeWindow) -> ReferenceSelection:
    """Select the latest report-period value proven available before ``event``.

    If a same-day candidate is not provably earlier, an older proven candidate
    may still be selected.  If none exists, the explicit
    ``PRE_EVENT_UNPROVEN`` status is returned.  No value is silently treated as
    an event-time reference in that case.
    """

    if not isinstance(event, TimeWindow):
        raise TypeError("event must be a TimeWindow")
    rows = tuple(candidates)
    if any(not isinstance(row, ReferenceObservation) for row in rows):
        raise TypeError("candidates must contain ReferenceObservation values")
    ids = [row.reference_id for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("reference_id values must be unique")
    eligible = [row for row in rows if row.eligible]
    proven = [row for row in eligible if strictly_before(row.available, event)]
    if not proven:
        return ReferenceSelection(PRE_EVENT_UNPROVEN, None,
                                  "no_eligible_reference_proven_before_event", len(rows))
    # Filter by time first, then select the newest comparable report period.
    # Availability is a deterministic tie-breaker and never changes proof.
    selected = max(proven, key=lambda row: (row.period_end, row.available.start, row.reference_id))
    return ReferenceSelection(PRE_EVENT_PROVEN, selected,
                              "reference_interval_ends_before_event_start", len(rows))


class SessionAnchorUnavailable(ValueError):
    """A requested session or policy anchor is absent from the pinned schedule."""


def _anchor_time(value: time | str, *, field: str) -> time:
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ValueError(f"{field} must be a local wall-clock time without timezone")
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field} must be HH:MM[:SS]")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be HH:MM[:SS]") from exc
    if parsed.tzinfo is not None:
        raise ValueError(f"{field} must not include a timezone")
    return parsed


def session_anchor(calendar: Sequence[str], source_day: str, delay_sessions: int,
                   *, entry_policy: str,
                   anchors: Mapping[str, Mapping[str, time | str]]) -> datetime:
    """Move by session identity, then use that day's named entry anchor.

    The target session must be present in the pinned, sorted calendar and the
    same policy must be present for that target day.  Missing anchors raise
    ``SessionAnchorUnavailable``; the function never jumps to a later day that
    happens to have a price.
    """

    if not isinstance(calendar, Sequence) or isinstance(calendar, (str, bytes)):
        raise TypeError("calendar must be a sequence of YYYY-MM-DD session ids")
    days = tuple(_day(day) for day in calendar)
    if not days or list(days) != sorted(days) or len(days) != len(set(days)):
        raise ValueError("calendar must be non-empty, sorted and duplicate-free")
    source = _day(source_day, field="source day")
    if source not in days:
        raise SessionAnchorUnavailable("source day is absent from pinned calendar")
    if type(delay_sessions) is not int or delay_sessions < 0:
        raise ValueError("delay_sessions must be a non-negative integer")
    if not isinstance(entry_policy, str) or not entry_policy:
        raise ValueError("entry_policy is required")
    target_index = days.index(source) + delay_sessions
    if target_index >= len(days):
        raise SessionAnchorUnavailable("delayed target session is absent from calendar")
    target = days[target_index]
    row = anchors.get(target) if isinstance(anchors, Mapping) else None
    if not isinstance(row, Mapping) or entry_policy not in row:
        raise SessionAnchorUnavailable(f"missing {entry_policy!r} anchor for {target}")
    wall_time = _anchor_time(row[entry_policy], field=f"anchor {target}/{entry_policy}")
    return datetime.combine(date.fromisoformat(target), wall_time, tzinfo=MARKET_TZ)


def label_available_at(required_available_at: Iterable[datetime | str] | datetime | str,
                       processing_delay: timedelta = timedelta(0)) -> datetime:
    """Return the first time a label is actually computable.

    Every required dependency time must be explicit.  An unknown dependency is
    an error rather than an early/default timestamp, and the result is the
    maximum dependency time plus an explicit processing delay.
    """

    if isinstance(required_available_at, (str, datetime)):
        values = (required_available_at,)
    else:
        try:
            values = tuple(required_available_at)
        except TypeError as exc:
            raise TypeError("required_available_at must be a timestamp or iterable") from exc
    if not values:
        raise ValueError("at least one label dependency is required")
    if any(value is None for value in values):
        raise ValueError("label dependency availability is unknown")
    if not isinstance(processing_delay, timedelta) or processing_delay < timedelta(0):
        raise ValueError("processing_delay must be a non-negative timedelta")
    parsed = tuple(_timestamp(value, field="label dependency") for value in values)
    return max(parsed) + processing_delay


@dataclass(frozen=True)
class LabelMaturity:
    status: str
    mature: bool
    reasons: tuple[str, ...]
    fit_cutoff: datetime
    feature_available_at: datetime | None
    label_actual_end: datetime | None
    label_available_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "status": self.status,
            "mature": self.mature,
            "reasons": list(self.reasons),
            "fit_cutoff": iso(self.fit_cutoff),
            "feature_available_at": iso(self.feature_available_at),
            "label_actual_end": iso(self.label_actual_end),
            "label_available_at": iso(self.label_available_at),
        }


def assess_label_maturity(feature_available_at: datetime | str | None,
                          label_actual_end: datetime | str | None,
                          label_available_at: datetime | str | None,
                          fit_cutoff: datetime | str,
                          completion_status: str) -> LabelMaturity:
    """Check all timing and completion requirements for a training label.

    Equality at the cutoff is allowed.  A label whose price window has ended
    but whose required correction/source arrives later remains ``IMMATURE``.
    """

    cutoff = _timestamp(fit_cutoff, field="fit_cutoff")

    def optional(value: datetime | str | None, field: str) -> datetime | None:
        return None if value is None else _timestamp(value, field=field)

    feature = optional(feature_available_at, "feature_available_at")
    actual_end = optional(label_actual_end, "label_actual_end")
    available = optional(label_available_at, "label_available_at")
    reasons: list[str] = []
    if feature is None:
        reasons.append("feature_available_at_unknown")
    elif feature > cutoff:
        reasons.append("feature_after_fit_cutoff")
    if actual_end is None:
        reasons.append("label_actual_end_unknown")
    elif actual_end > cutoff:
        reasons.append("label_end_after_fit_cutoff")
    if available is None:
        reasons.append("label_available_at_unknown")
    elif available > cutoff:
        reasons.append("label_available_after_fit_cutoff")
    if not isinstance(completion_status, str) or completion_status.upper() != "COMPLETE":
        reasons.append("label_not_complete")
    mature = not reasons
    return LabelMaturity(MATURE if mature else IMMATURE, mature, tuple(reasons), cutoff,
                         feature, actual_end, available)


__all__ = [
    "IMMATURE", "MARKET_TZ", "MATURE", "PRE_EVENT_PROVEN", "PRE_EVENT_UNPROVEN",
    "LabelMaturity", "ReferenceObservation", "ReferenceSelection", "SessionAnchorUnavailable",
    "TimeWindow", "assess_label_maturity", "conservative_available_at", "label_available_at",
    "publication_interval", "select_pre_event_reference", "session_anchor", "strictly_before",
]
