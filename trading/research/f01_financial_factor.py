"""Pure, point-in-time F01 financial surprise calculation.

F01 is deliberately a fact-level function.  It accepts versioned facts and an
explicit event window, never reads a latest directory or current time, and
returns a typed status when a value cannot support a research decision.  The
result is suitable for a later Signal/Portfolio adapter; this module does not
create orders or alter an account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .financial_factor_contracts import TimeWindow, strictly_before
from .financial_factor_registry import F01


OBSERVED = "OBSERVED"
READY = "READY"
MISSING_INPUT = "MISSING_INPUT"
INCOMPARABLE = "INCOMPARABLE"
NONPOSITIVE_DENOMINATOR = "NONPOSITIVE_DENOMINATOR"
PRE_EVENT_UNPROVEN = "PRE_EVENT_UNPROVEN"
CONFLICT = "UNRESOLVED_CONFLICT"


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{field} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


@dataclass(frozen=True)
class FinancialFact:
    fact_id: str
    issuer_id: str
    metric_id: str
    period_start: date
    period_end: date
    period_kind: str
    scope_group: str
    currency: str
    unit: str
    value: Decimal | None
    available: TimeWindow
    status: str = OBSERVED

    def __post_init__(self) -> None:
        for field in ("fact_id", "issuer_id", "metric_id", "period_kind", "scope_group", "currency", "unit"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} is required")
        if not isinstance(self.period_start, date) or not isinstance(self.period_end, date) or self.period_end < self.period_start:
            raise ValueError("period must be an ordered date range")
        if not isinstance(self.available, TimeWindow):
            raise TypeError("available must be a TimeWindow")
        if self.value is not None:
            object.__setattr__(self, "value", _decimal(self.value, "value"))
        if not isinstance(self.status, str) or not self.status:
            raise ValueError("status is required")


@dataclass(frozen=True)
class F01Result:
    status: str
    value: Decimal | None
    available_at: TimeWindow | None
    fact_ids: tuple[str, ...]
    definition_version: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": str(self.value) if self.value is not None else None,
            "available_at": self.available_at.to_dict() if self.available_at is not None else None,
            "fact_ids": list(self.fact_ids),
            "definition_version": self.definition_version,
            "reason": self.reason,
        }


def _validate_facts(current: FinancialFact, prior: FinancialFact, assets: FinancialFact) -> tuple[str, ...]:
    if not all(isinstance(value, FinancialFact) for value in (current, prior, assets)):
        raise TypeError("F01 inputs must be FinancialFact values")
    ids = (current.fact_id, prior.fact_id, assets.fact_id)
    if len(set(ids)) != len(ids):
        raise ValueError("F01 fact_ids must be unique")
    if current.issuer_id != prior.issuer_id or current.issuer_id != assets.issuer_id:
        return (INCOMPARABLE, "issuer_identity_conflict")
    if current.metric_id != "parent_net_profit" or prior.metric_id != "parent_net_profit" or assets.metric_id != "total_assets":
        return (INCOMPARABLE, "metric_identity_mismatch")
    if current.period_kind not in {"single_quarter", "cumulative"} or prior.period_kind not in {"single_quarter", "cumulative"}:
        return (INCOMPARABLE, "profit_period_kind_mismatch")
    if assets.period_kind != "point_in_time":
        return (INCOMPARABLE, "asset_period_kind_mismatch")
    if (current.scope_group, current.currency, current.unit) != (prior.scope_group, prior.currency, prior.unit):
        return (INCOMPARABLE, "profit_scope_currency_or_unit_conflict")
    if (current.scope_group, current.currency, current.unit) != (assets.scope_group, assets.currency, assets.unit):
        return (INCOMPARABLE, "asset_scope_currency_or_unit_conflict")
    if current.period_end == prior.period_end or current.period_end <= prior.period_end:
        return (INCOMPARABLE, "current_and_prior_period_not_ordered")
    return (READY, "")


def compute_f01(current: FinancialFact, prior: FinancialFact, assets: FinancialFact,
                event: TimeWindow, *, definition_version: str = F01.definition_version) -> F01Result:
    """Compute ``(current - prior) / assets`` only from proven PIT facts."""

    if not isinstance(event, TimeWindow):
        raise TypeError("event must be a TimeWindow")
    if not isinstance(definition_version, str) or not definition_version:
        raise ValueError("definition_version is required")
    facts = (current, prior, assets)
    ids = tuple(value.fact_id for value in facts)
    if any(not isinstance(value, FinancialFact) for value in facts):
        raise TypeError("F01 inputs must be FinancialFact values")
    if any(value.status == CONFLICT for value in facts):
        return F01Result(CONFLICT, None, None, ids, definition_version, "unresolved_fact_conflict")
    if any(value.status != OBSERVED or value.value is None for value in facts):
        return F01Result(MISSING_INPUT, None, None, ids, definition_version, "required_fact_missing_or_not_observed")
    if any(not strictly_before(value.available, event) for value in facts):
        return F01Result(PRE_EVENT_UNPROVEN, None, None, ids, definition_version, "fact_not_proven_before_event")
    status, reason = _validate_facts(current, prior, assets)
    if status != READY:
        return F01Result(status, None, None, ids, definition_version, reason)
    denominator = _decimal(assets.value, "assets.value")
    if denominator <= 0:
        return F01Result(NONPOSITIVE_DENOMINATOR, None, None, ids, definition_version, "event_pre_assets_not_positive")
    # Availability of a derived value is bounded by the latest dependency.
    available = max(facts, key=lambda value: value.available.end or value.available.start).available
    value = (_decimal(current.value, "current.value") - _decimal(prior.value, "prior.value")) / denominator
    return F01Result(READY, value, available, ids, definition_version, "all_facts_comparable_and_pre_event")


def cumulative_to_single_quarter(current_ytd: FinancialFact, prior_ytd: FinancialFact,
                                 *, derived_fact_id: str) -> FinancialFact:
    """Subtract two comparable cumulative flow facts without changing units."""

    if not isinstance(current_ytd, FinancialFact) or not isinstance(prior_ytd, FinancialFact):
        raise TypeError("cumulative inputs must be FinancialFact values")
    if current_ytd.metric_id != prior_ytd.metric_id or current_ytd.period_kind != "cumulative" or prior_ytd.period_kind != "cumulative":
        raise ValueError("single-quarter conversion requires comparable cumulative flows")
    if (current_ytd.issuer_id, current_ytd.scope_group, current_ytd.currency, current_ytd.unit) != (
            prior_ytd.issuer_id, prior_ytd.scope_group, prior_ytd.currency, prior_ytd.unit):
        raise ValueError("cumulative flows are not comparable")
    if current_ytd.period_start != prior_ytd.period_start or current_ytd.period_end <= prior_ytd.period_end:
        raise ValueError("cumulative periods must share a start and advance in time")
    if current_ytd.status != OBSERVED or prior_ytd.status != OBSERVED or current_ytd.value is None or prior_ytd.value is None:
        raise ValueError("cumulative flows must be observed")
    available = max((current_ytd, prior_ytd), key=lambda value: value.available.end or value.available.start).available
    return FinancialFact(derived_fact_id, current_ytd.issuer_id, current_ytd.metric_id,
                         prior_ytd.period_end, current_ytd.period_end, "single_quarter",
                         current_ytd.scope_group, current_ytd.currency, current_ytd.unit,
                         _decimal(current_ytd.value, "current_ytd.value") - _decimal(prior_ytd.value, "prior_ytd.value"),
                         available)


__all__ = ["CONFLICT", "FinancialFact", "F01Result", "INCOMPARABLE", "MISSING_INPUT",
           "NONPOSITIVE_DENOMINATOR", "OBSERVED", "PRE_EVENT_UNPROVEN", "READY",
           "compute_f01", "cumulative_to_single_quarter"]
