"""Pure pretrade comparison around the existing lot allocator.

This module explains the distance between economic target weights and an
executable target under current holdings, sellability, frozen orders, fees and
cash.  It never creates orders or mutates the Ledger.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .portfolio_allocation import stock_lot_allocation
from .rules import quote_value


SCHEMA = "portfolio_economic_executable_comparison_v1"


def compare_target(intent, book, day: str, rows: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if getattr(intent, "kind", None).value != "FULL_TARGET" or intent.context != book.context:
        raise ValueError("Portfolio comparison needs a matching full target intent")
    weights = dict(intent.weights)
    stock_weights = {}
    fund_weights = {}
    prices = {}
    rules = {}
    for symbol in weights:
        rule = book.rule(symbol, day)
        row = rows.get(symbol)
        if rule.is_fund:
            fund_weights[symbol] = weights[symbol]
            continue
        if row is None or rule.price_unit != "cent":
            raise ValueError(f"Comparison needs a raw cent-stock mark: {symbol}")
        prices[symbol] = quote_value(row.get("close"), rule.price_unit)
        rules[symbol] = rule
        stock_weights[symbol] = weights[symbol]
    equity = book.equity()
    entries, budget, spent = stock_lot_allocation(
        stock_weights, equity, prices, rules, book.config.single_limit, book.config.gross_limit) if stock_weights else ([], 0, 0)
    rows_out, blockers = [], []
    total_buy_cost = 0
    total_sell_value = 0
    for entry in entries:
        symbol, price = entry["symbol"], entry["price"]
        current = book.quantity(symbol)
        sellable = book.available(symbol, day)
        delta = entry["quantity"] - current
        estimated_fees = 0
        flags = []
        if delta > 0:
            estimated_cost = book.order_cost(delta, price, symbol, day)
            total_buy_cost += estimated_cost
            estimated_fees = book.fee(price * delta, "buy", symbol, day)
            if estimated_cost > book.cash_available():
                flags.append("cash_shortfall")
        elif delta < 0:
            sell_qty = -delta
            total_sell_value += price * sell_qty
            if sell_qty > sellable:
                flags.append("sellable_T1_shortfall")
        if flags:
            blockers.extend({"symbol": symbol, "flag": flag} for flag in flags)
        rows_out.append({
            "symbol": symbol, "economic_weight": weights[symbol],
            "estimated_target_quantity": entry["quantity"], "current_quantity": current,
            "sellable_quantity": sellable, "delta_quantity": delta,
            "mark_cents": price, "estimated_buy_fee_cents": estimated_fees,
            "constraint_flags": flags,
        })
    for symbol, weight in fund_weights.items():
        rows_out.append({"symbol": symbol, "economic_weight": weight,
                         "estimated_target_quantity": None, "current_quantity": book.quantity(symbol),
                         "sellable_quantity": book.available(symbol, day), "delta_quantity": None,
                         "mark_cents": None, "estimated_buy_fee_cents": 0,
                         "constraint_flags": ["fund_sleeve_unchanged"]})
    return {
        "schema": SCHEMA,
        "status": "EXECUTABLE_TARGET_READY" if not blockers else "BLOCKED_EXECUTION_CONSTRAINT",
        "day": day, "context": book.context.to_dict(),
        "economic_target_weights": deepcopy(weights),
        "rows": rows_out,
        "stock_budget_cents": int(budget), "estimated_floor_notional_cents": int(spent),
        "estimated_buy_cost_cents": int(total_buy_cost), "estimated_sell_value_cents": int(total_sell_value),
        "cash_available_cents": int(book.cash_available()), "blockers": blockers,
        "orders_created": False, "ledger_mutated": False, "execution_admitted": False,
    }


__all__ = ["SCHEMA", "compare_target"]
