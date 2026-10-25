"""Explicit Decimal financial-unit normalization with no implicit guessing."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


UNIT_TO_YUAN = {
    "yuan": Decimal("1"),
    "thousand_yuan": Decimal("1000"),
    "ten_thousand_yuan": Decimal("10000"),
    "million_yuan": Decimal("1000000"),
}


def normalize_amount(value: Any, unit: str, *, target_unit: str = "yuan") -> dict[str, Any]:
    """Return raw and normalized values; unknown units are rejected."""

    if unit not in UNIT_TO_YUAN or target_unit not in UNIT_TO_YUAN:
        raise ValueError("Unknown financial unit; explicit source dictionary required")
    if isinstance(value, bool):
        raise ValueError("financial amount must be a finite Decimal")
    try:
        raw = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("financial amount must be a finite Decimal") from error
    if not raw.is_finite():
        raise ValueError("financial amount must be a finite Decimal")
    normalized = raw * UNIT_TO_YUAN[unit] / UNIT_TO_YUAN[target_unit]
    return {"raw_value": raw, "raw_unit": unit, "value": normalized, "unit": target_unit,
            "conversion": f"{unit}->{target_unit}", "conversion_verified": True}


__all__ = ["UNIT_TO_YUAN", "normalize_amount"]
