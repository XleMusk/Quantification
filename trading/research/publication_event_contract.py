"""Point-in-time publication evidence for dated research events.

This contract is deliberately independent of any provider.  A source adapter
can map its event receipt into the row shape accepted here; missing fields stay
unknown and therefore cannot make an action context PIT-safe.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import re
from typing import Any

from .financial_factor_contracts import TimeWindow, publication_interval, strictly_before
from ..contracts import decision_time


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError(f"{field} must be an offset-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must be offset-aware")
    return parsed


def _publication_window(row: Mapping[str, Any]) -> TimeWindow:
    day = row.get("publication_date")
    if not isinstance(day, str) or not day:
        raise ValueError("publication_date is required")
    stamp = row.get("publication_time")
    if stamp in (None, ""):
        return publication_interval(day)
    if not isinstance(stamp, str) or "T" not in stamp:
        raise ValueError("publication_time must be an offset-aware ISO timestamp")
    return TimeWindow.exact(_timestamp(stamp, "publication_time"))


def evaluate_publication_evidence(row: Mapping[str, Any], decision_day: str) -> dict[str, Any]:
    """Classify one source receipt without filling or inferring metadata."""

    if not isinstance(row, Mapping):
        raise TypeError("publication evidence row must be a mapping")
    reasons: list[str] = []
    publication_proven = False
    retrieval_asof_proven = False
    revision_identity_proven = False
    source_provenance_proven = False
    try:
        publication = _publication_window(row)
        publication_proven = strictly_before(publication, TimeWindow.exact(decision_time(decision_day)))
        if not publication_proven:
            reasons.append("publication_not_proven_before_decision")
    except (TypeError, ValueError) as exc:
        reasons.append(f"publication_evidence_invalid:{exc}")

    try:
        retrieved = _timestamp(row.get("retrieved_at"), "retrieved_at")
        asof = _timestamp(row.get("asof_at"), "asof_at")
        cutoff = _timestamp(decision_time(decision_day), "decision_time")
        # A date-only disclosure is an interval, not midnight.  Retrieval
        # before its conservative end cannot prove that the disclosed
        # contents were available to the consumer.  Exact publication times
        # use the same boundary through ``publication.start``.
        publication_boundary = publication.end or publication.start
        if retrieved < publication_boundary or asof < publication_boundary:
            reasons.append("retrieval_or_asof_before_publication")
        elif asof < retrieved:
            reasons.append("asof_before_retrieval")
        elif retrieved <= cutoff and asof <= cutoff:
            retrieval_asof_proven = True
        else:
            reasons.append("retrieval_or_asof_after_decision")
    except (TypeError, ValueError) as exc:
        reasons.append(f"retrieval_asof_invalid:{exc}")

    revision_id = row.get("revision_id")
    if isinstance(revision_id, str) and revision_id.strip():
        revision_identity_proven = True
    else:
        reasons.append("revision_id_missing")

    source_file = row.get("source_file")
    source_sha256 = row.get("source_sha256")
    if isinstance(source_file, str) and source_file.strip() and isinstance(source_sha256, str) \
            and _SHA256.fullmatch(source_sha256.strip()):
        source_provenance_proven = True
    else:
        reasons.append("source_file_or_sha256_missing")

    return {
        "publication_evidence_present": True,
        "publication_event_proven": publication_proven,
        "retrieved_asof_proven": retrieval_asof_proven,
        "revision_identity_proven": revision_identity_proven,
        "source_provenance_proven": source_provenance_proven,
        "pit_asof_safe": not reasons,
        "publication_evidence_reasons": reasons,
    }


def missing_publication_evidence() -> dict[str, Any]:
    """Return the explicit unknown state used when no sidecar receipt exists."""

    return {
        "publication_evidence_present": False,
        "publication_event_proven": False,
        "retrieved_asof_proven": False,
        "revision_identity_proven": False,
        "source_provenance_proven": False,
        "pit_asof_safe": False,
        "publication_evidence_reasons": ["publication_evidence_missing"],
    }


__all__ = ["evaluate_publication_evidence", "missing_publication_evidence"]
