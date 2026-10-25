"""Point-in-time query dependency records, including misses and unknowns.

F2 requires a later source arrival to invalidate a prior ``MISS`` query even
when the old result did not contain a fact id.  This module is a small pure
dependency index; persistence and provider ingestion remain separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Any, Mapping


QUERY_STATES = ("FOUND", "MISS", "UNKNOWN")
IMPACTS = ("NO_IMPACT", "RECOMPUTE_EXPLANATION", "REPLAY_ACCOUNT")


def _day(value: str, field: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be canonical YYYY-MM-DD")
    return value


def _stamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include an offset")
    return parsed.isoformat()


@dataclass(frozen=True)
class QueryDependency:
    issuer_id: str
    metric_or_notice_type: str
    period_start: str
    period_end: str
    cutoff: str
    information_view: str
    coverage_version: str
    result_state: str
    query_id: str = ""

    def __post_init__(self):
        for field in ("issuer_id", "metric_or_notice_type", "information_view", "coverage_version"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} is required")
        _day(self.period_start, "period_start")
        _day(self.period_end, "period_end")
        if self.period_end < self.period_start:
            raise ValueError("period range is reversed")
        object.__setattr__(self, "cutoff", _stamp(self.cutoff, "cutoff"))
        if self.result_state not in QUERY_STATES:
            raise ValueError("unknown query result state")

    def identity(self) -> dict[str, Any]:
        return {"issuer_id": self.issuer_id, "metric_or_notice_type": self.metric_or_notice_type,
                "period_start": self.period_start, "period_end": self.period_end,
                "cutoff": self.cutoff, "information_view": self.information_view,
                "coverage_version": self.coverage_version}

    @property
    def dependency_id(self) -> str:
        encoded = json.dumps(self.identity(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity(), "result_state": self.result_state,
                "query_id": self.query_id or self.dependency_id}


def fact_reopens_query(dependency: QueryDependency, fact: Mapping[str, Any]) -> bool:
    """Return whether a newly observed fact falls into a prior MISS/UNKNOWN query."""

    if not isinstance(dependency, QueryDependency) or dependency.result_state not in {"MISS", "UNKNOWN"}:
        return False
    if not isinstance(fact, Mapping):
        raise TypeError("fact must be a mapping")
    if fact.get("issuer_id") != dependency.issuer_id or fact.get("metric_or_notice_type") != dependency.metric_or_notice_type:
        return False
    start = _day(str(fact.get("period_start")), "fact.period_start")
    end = _day(str(fact.get("period_end")), "fact.period_end")
    if end < dependency.period_start or start > dependency.period_end:
        return False
    available_at = fact.get("available_at")
    if available_at is None:
        return True
    return _stamp(str(available_at), "fact.available_at") <= dependency.cutoff


def decision_impact(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    """Classify whether a revised fact needs a full shared-account replay."""

    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        raise TypeError("old and new decisions must be mappings")
    economic = ("selected_symbols", "target_weights", "orders", "fees", "cash", "positions")
    if any(old.get(field) != new.get(field) for field in economic):
        return "REPLAY_ACCOUNT"
    explanatory = ("explanation_factors", "diagnostics", "coverage")
    if any(old.get(field) != new.get(field) for field in explanatory):
        return "RECOMPUTE_EXPLANATION"
    return "NO_IMPACT"


__all__ = ["IMPACTS", "QUERY_STATES", "QueryDependency", "decision_impact", "fact_reopens_query"]
