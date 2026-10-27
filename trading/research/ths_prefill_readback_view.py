"""Pure comparison view for one offline THS prefill readback row."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


SCHEMA = "ths_prefill_readback_view_v1"


def normalize(field: str, value: str) -> str:
    """Normalize harmless form formatting while preserving invalid text."""

    text = str(value).strip()
    if field == "symbol":
        return text.split(".", 1)[0].strip().upper()
    if field == "price":
        try:
            number = Decimal(text.replace(",", ""))
            if number.is_finite():
                return f"{number:.2f}"
        except (InvalidOperation, ValueError):
            pass
    if field == "qty":
        try:
            number = Decimal(text.replace(",", ""))
            if number.is_finite() and number == number.to_integral_value():
                return str(int(number))
        except (InvalidOperation, ValueError):
            pass
    return text


def build_view(
    *,
    strategy_id: str,
    sequence: int,
    symbol: str,
    price: str,
    qty: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare one captured form row with a frozen paper-only payload."""

    row = next((item for item in payload.get("rows") or []
                if int(item.get("sequence") or 0) == sequence), None)
    if row is None:
        raise KeyError("payload sequence missing")
    base = {
        "schema": SCHEMA,
        "strategy_id": strategy_id,
        "sequence": sequence,
        "execution_day": payload.get("execution_day"),
        "submitted": False,
        "ledger_mutated": False,
        "execution_admitted": False,
    }
    raw_received = {"symbol": symbol, "price": price, "qty": qty}
    if row.get("route_status") != "route_available":
        return {
            **base,
            "status": "route_unavailable",
            "expected": None,
            "readback": raw_received,
            "mismatches": [],
            "route_status": row.get("route_status"),
            "route_reason": row.get("route_reason"),
        }
    expected_raw = {
        "symbol": str(row.get("symbol") or "").split(".")[0],
        "price": str(row.get("price_rmb") or ""),
        "qty": str(row.get("qty") or ""),
    }
    expected = {field: normalize(field, value) for field, value in expected_raw.items()}
    received = {field: normalize(field, value) for field, value in raw_received.items()}
    mismatches = [
        {"field": field, "expected": expected[field], "received": received[field]}
        for field in ("symbol", "price", "qty") if received[field] != expected[field]
    ]
    return {
        **base,
        "status": "readback_verified" if not mismatches else "readback_mismatch",
        "expected": expected,
        "expected_raw": expected_raw,
        "readback": raw_received,
        "normalized_readback": received,
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "route_status": row.get("route_status"),
        "route_reason": row.get("route_reason"),
    }


__all__ = ["SCHEMA", "build_view", "normalize"]
