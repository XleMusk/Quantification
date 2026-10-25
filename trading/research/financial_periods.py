"""Comparable cumulative-flow transformations for financial-factor inputs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class FlowValue:
    value: Decimal
    period: str
    period_kind: str
    scope_group: str
    currency: str
    unit: str
    version_id: str

    def __post_init__(self):
        try:
            value = self.value if isinstance(self.value, Decimal) else Decimal(str(self.value))
        except (InvalidOperation, ValueError, TypeError) as error:
            raise ValueError("flow value must be finite Decimal") from error
        if not value.is_finite():
            raise ValueError("flow value must be finite Decimal")
        object.__setattr__(self, "value", value)
        for field in ("period", "period_kind", "scope_group", "currency", "unit", "version_id"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} is required")


def _comparable(*values: FlowValue) -> None:
    if any(not isinstance(value, FlowValue) for value in values):
        raise TypeError("flow inputs must be FlowValue values")
    identity = {(value.scope_group, value.currency, value.unit) for value in values}
    if len(identity) != 1:
        raise ValueError("flow inputs have incompatible scope, currency or unit")


def ttm(current_ytd: FlowValue, prior_full_year: FlowValue, prior_ytd: FlowValue,
        *, version_id: str) -> FlowValue:
    """Compute current YTD + prior FY - prior YTD for comparable flow values."""

    _comparable(current_ytd, prior_full_year, prior_ytd)
    if (current_ytd.period_kind, prior_full_year.period_kind, prior_ytd.period_kind) != ("YTD", "FY", "YTD"):
        raise ValueError("TTM requires current YTD, prior FY and prior YTD")
    if current_ytd.period == prior_ytd.period or not current_ytd.period > prior_ytd.period:
        raise ValueError("current and prior YTD periods must be ordered")
    if not isinstance(version_id, str) or not version_id:
        raise ValueError("derived version_id is required")
    return FlowValue(current_ytd.value + prior_full_year.value - prior_ytd.value,
                     current_ytd.period, "TTM", current_ytd.scope_group,
                     current_ytd.currency, current_ytd.unit, version_id)


__all__ = ["FlowValue", "ttm"]
