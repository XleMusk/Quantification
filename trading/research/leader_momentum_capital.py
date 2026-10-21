"""Past-close entry affordability in original rank order, not an execution allocator.

The caller supplies the full ranked list and this decision day's parsed raw rows.
Existing holdings bypass the new-entry budget test but receive no rank preference.
The Engine still determines quantities, available cash and executable orders.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from decimal import Decimal
import math
from numbers import Real

from ..rules import execution_rule, fees, notional_cents, quote_value


def affordable_ranked_candidates(ranked, equity_cents, held_quantities, rows, config, day):
    """Return complete candidates or an incomplete batch requiring NO_CHANGE.

    Only candidates needed to fill the fixed number of slots are examined.
    A known minimum-buy cost above the nominal slot budget permits looking
    farther down the same ranking. Unknown raw closes or rules never do.
    Slot budgets are whole cents; fees apply to new-entry minimum notionals.
    This does not establish PIT availability, tradability or account admission.
    """
    config.validate()
    if type(equity_cents) is not int or equity_cents <= 0:
        raise ValueError("Entry affordability requires positive integer-cent equity")
    if (not isinstance(ranked, list) or not isinstance(held_quantities, Mapping)
            or not isinstance(rows, Mapping) or not isinstance(day, str)
            or date.fromisoformat(day).isoformat() != day):
        raise ValueError("Ranked candidates, holdings and exact decision-day rows are required")
    nominal_weight = min(config.single_limit, config.gross_limit / config.holdings)
    budget = int(Decimal(equity_cents) * Decimal(str(nominal_weight)))
    selected, excluded, seen = [], [], set()
    previous_score = math.inf

    def result(status, reason):
        return {"status": status, "candidates": selected if status == "complete" else [],
                "excluded": excluded, "slot_budget_cents": budget, "reason": reason}

    for rank, candidate in enumerate(ranked, 1):
        if len(selected) == config.holdings:
            break
        if not isinstance(candidate, Mapping):
            raise ValueError("Every ranked candidate must be an original score mapping")
        symbol, score = candidate.get("symbol"), candidate.get("score")
        if (not isinstance(symbol, str) or symbol in seen or isinstance(score, bool)
                or not isinstance(score, Real) or not math.isfinite(score) or score > previous_score):
            raise ValueError("Distinct candidates in original finite descending-score order required")
        seen.add(symbol)
        previous_score = score
        quantity = held_quantities.get(symbol, 0)
        if type(quantity) is not int or quantity < 0:
            raise ValueError("Known nonnegative integer held quantities required")
        row = rows.get(symbol)
        close = row.get("close") if isinstance(row, Mapping) else None
        if (not isinstance(row, Mapping) or str(row.get("adjustflag")) != "3"
                or isinstance(close, bool) or not isinstance(close, Real)
                or not math.isfinite(close) or close <= 0):
            return result("incomplete", "candidate_raw_close_unknown:" + symbol)
        try:
            rule = execution_rule(symbol, day)
        except ValueError:
            return result("incomplete", "candidate_execution_rule_unknown:" + symbol)
        price = quote_value(close, rule.price_unit)
        if price <= 0:
            return result("incomplete", "candidate_raw_close_unknown:" + symbol)
        if quantity == 0:
            minimum_value = notional_cents(price, rule.quantity.minimum, rule.price_unit)
            buy_fees = fees(minimum_value, "buy", config, equity_etf=rule.is_fund, day=day)
            if minimum_value + buy_fees > budget:
                excluded.append({"symbol": symbol, "rank": rank,
                    "reason": "minimum_entry_cost_exceeds_slot_budget", "raw_close": close,
                    "price_unit": rule.price_unit, "minimum_buy_quantity": rule.quantity.minimum,
                    "minimum_buy_notional_cents": minimum_value, "buy_fees_cents": buy_fees,
                    "minimum_buy_cost_cents": minimum_value + buy_fees})
                continue
        selected.append(deepcopy(dict(candidate)))
    return result("complete", "affordable_ranked_candidates" if selected else "known_no_affordable_candidates")
