"""Project amounts from saved Engine sizing events without reevaluating them.

The mark is the Engine's recorded sizing mark.  It is neither a broker quote
nor a new execution-price observation, and the reference amounts omit costs.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math


SCHEMA = "saved_target_execution_amount_projection_v1"


def _number(value, *, positive=False, integer=False):
    if type(value) not in (int, float) or (integer and type(value) is not int):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        return None
    return number


def _finite_float(value):
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def project_saved_target_execution(events, day):
    """Copy exact-D trace rows; absence means the source did not record them."""
    if not isinstance(events, list):
        return None
    rows = [deepcopy(event) for event in events if isinstance(event, dict)
        and event.get("day") == day and event.get("kind") == "target_execution_diagnostic"]
    if not rows:
        return None
    for row in rows:
        budget, minimum, weight = None, None, None
        if row.get("schema_version") == 1:
            equity = _number(row.get("open_equity_cents"), integer=True)
            weight = _number(row.get("target_weight"))
            if equity is not None and weight is not None:
                budget = _finite_float(equity * weight / 100)
            unit = {"cent": 100, "mill": 1000}.get(row.get("price_unit"))
            mark = _number(row.get("engine_mark"), positive=True, integer=True)
            quantity = _number(row.get("minimum_buy_quantity"), positive=True, integer=True)
            if unit is not None and mark is not None and quantity is not None:
                minimum = _finite_float(mark * quantity / unit)
        new_entry = (_number(row.get("held_before"), integer=True) == 0
            and weight is not None and weight > 0)
        row["projection"] = {"schema": SCHEMA, "target_budget_rmb": budget,
            "minimum_buy_notional_rmb": minimum,
            "budget_below_minimum_buy_notional": (budget < minimum
                if new_entry and budget is not None and minimum is not None else None),
            "basis": "saved_engine_mark_reference_not_execution_quote",
            "fees_included": False, "slippage_included": False, "is_BrokerQuote": False}
    return rows
