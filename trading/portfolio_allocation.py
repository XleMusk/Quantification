"""Optional close-price lot tracking for research; execution stays in the engine."""
from dataclasses import replace
from decimal import Decimal
import math

from .contracts import IntentKind, PortfolioIntent, TradingMode
from .rules import quote_value


LOT_ALLOCATION = "close_stock_lot_tracking_v1"
OPEN_LOT_ALLOCATION = "open_stock_lot_tracking_v1"
OBSERVED_LOT_ALLOCATION = "observed_stock_lot_tracking_v1"


def _allocate_entries(entries, cap):
    budget = sum((entry["target"] for entry in entries), Decimal(0))
    spent = sum(entry["price"] * entry["quantity"] for entry in entries)
    improvements = []
    for entry in entries:
        quantity = entry["quantity"]
        extra = entry["step"] if quantity else entry["minimum"]
        value = extra * entry["price"]
        after = (quantity + extra) * entry["price"]
        before_error = entry["target"] - quantity * entry["price"]
        benefit = before_error ** 2 - (entry["target"] - after) ** 2
        if benefit > 0 and after <= cap:
            improvements.append((benefit / value, entry["rank"], value, extra, entry))
    # One floor-to-ceiling move per name; ties follow the unchanged signal ranks.
    for _, _, value, extra, entry in sorted(improvements, key=lambda item: (-item[0], item[1])):
        if spent + value <= budget:
            entry["quantity"] += extra
            spent += value
    return budget, spent


def stock_lot_allocation(weights, equity, prices, rules, single_limit, gross_limit):
    """Pure integer targets at supplied marks; no account, orders or fills.

    Return sizing entries, the original weight budget and allocated notional.
    Input order is the tie-breaker. Fees and available cash remain execution checks.
    """
    if (type(equity) is not int or equity <= 0
            or any(isinstance(w, bool) or not isinstance(w, (int, float))
                   or not math.isfinite(w) or w < 0 or w > single_limit for w in weights.values())
            or sum(weights.values()) > gross_limit + 1e-12):
        raise ValueError("Stock lot allocation needs valid prior weights and positive equity")
    entries = []
    for rank, (symbol, weight) in enumerate(weights.items()):
        rule, price = rules[symbol], prices.get(symbol, 0)
        if rule.is_fund or rule.price_unit != "cent" or type(price) is not int or price <= 0:
            raise ValueError(f"Unavailable cent-stock allocation mark: {symbol}")
        target = Decimal(equity) * Decimal(str(weight))
        quantity = int(target / price / rule.quantity.step) * rule.quantity.step
        if quantity < rule.quantity.minimum:
            quantity = 0
        entries.append({"symbol": symbol, "rank": rank, "price": price, "target": target,
                        "step": rule.quantity.step, "minimum": rule.quantity.minimum,
                        "quantity": quantity, "floor_quantity": quantity, "original_weight": weight})
    cap = Decimal(equity) * Decimal(str(single_limit))
    budget, spent = _allocate_entries(entries, cap)
    if spent > budget or any(e["price"] * e["quantity"] > cap for e in entries):
        raise ValueError("Stock lot allocation exceeded original stock budget or single-name cap")
    return entries, budget, spent


def allocate_close_lots(intent, book, day, rows):
    """Greedily reduce squared stock-target errors within the original stock budget.

    This emits weights, not frozen share orders. The next open still determines
    quantities, fees, cash, sellability and execution through the existing ledger.
    """
    if (not isinstance(intent, PortfolioIntent) or intent.kind != IntentKind.FULL_TARGET
            or intent.context != book.context or book.context.mode != TradingMode.REPLAY
            or intent.decided_at[:10] != day):
        raise ValueError("Close lot allocation needs this session's replay full target")
    equity = book.equity()
    if equity <= 0:
        raise ValueError("Close lot allocation needs positive account equity")
    original = dict(intent.weights)
    entries, weights = [], dict(original)
    cap = Decimal(equity) * Decimal(str(book.config.single_limit))
    for rank, (symbol, weight) in enumerate(intent.weights):
        rule = book.rule(symbol, day)
        if rule.is_fund:
            continue
        row = rows.get(symbol)
        if (row is None or not math.isfinite(row.get("close", math.nan)) or row["close"] <= 0
                or str(row.get("adjustflag", "3")) != "3"):
            raise ValueError(f"Unavailable raw allocation close: {symbol} {day}")
        price = quote_value(row["close"], rule.price_unit)
        if rule.price_unit != "cent":
            raise ValueError("Stock lot allocation requires the existing cent stock profile")
        target = Decimal(equity) * Decimal(str(weight))
        step, minimum = rule.quantity.step, rule.quantity.minimum
        quantity = int(target / price / step) * step
        if quantity < minimum:
            quantity = 0
        entries.append({"symbol": symbol, "rank": rank, "price": price, "target": target,
                        "step": step, "minimum": minimum, "quantity": quantity,
                        "floor_quantity": quantity, "original_weight": weight})
    budget, spent = _allocate_entries(entries, cap)
    for entry in entries:
        weights[entry["symbol"]] = entry["quantity"] * entry["price"] / equity
    if (any(weight > book.config.single_limit + 1e-12 for weight in weights.values())
            or sum(weights.values()) > book.config.gross_limit + 1e-12):
        raise ValueError("Close lot allocation exceeded portfolio risk caps")
    book.emit(day, "close_lot_allocation", policy=LOT_ALLOCATION,
              stock_budget_cny=float(budget / 100), allocated_stock_cny=spent / 100,
              squared_error_before=float(sum((entry["target"] - entry["floor_quantity"] * entry["price"]) ** 2
                                             for entry in entries) / 10000),
              squared_error_after=float(sum((entry["target"] - entry["quantity"] * entry["price"]) ** 2
                                            for entry in entries) / 10000),
              estimates=[{"symbol": entry["symbol"], "original_weight": entry["original_weight"],
                          "target_weight": weights[entry["symbol"]], "close_rmb": entry["price"] / 100,
                          "quantity_at_decision_close": entry["quantity"], "floor_quantity": entry["floor_quantity"],
                          "minimum_buy_quantity": entry["minimum"]} for entry in entries],
              basis="Current raw close/account; estimated lot weights, NOT guaranteed next-open quantities")
    return replace(intent, weights=tuple(weights.items()))


def allocate_open_lots(weights, book, day, rows):
    """Use prior-decision weights and current attempt marks, not future closes.

    The returned absolute stock quantities still pass the engine's delta, T+1,
    slippage, cash, fees, volume and exposure checks. No fills are promised.
    """
    if book.context.mode != TradingMode.REPLAY:
        raise ValueError("Open lot allocation is replay-only")
    equity = book.equity()
    entries, budget, spent = stock_lot_allocation(
        weights, equity, book.state["prices"], {s: book.rule(s, day) for s in weights},
        book.config.single_limit, book.config.gross_limit)
    book.emit(day, "open_lot_allocation", policy=OPEN_LOT_ALLOCATION,
              open_equity_cents=equity, stock_budget_cny=float(budget / 100), allocated_stock_cny=spent / 100,
              squared_error_before=float(sum((e["target"] - e["floor_quantity"] * e["price"]) ** 2
                                             for e in entries) / 10000),
              squared_error_after=float(sum((e["target"] - e["quantity"] * e["price"]) ** 2
                                            for e in entries) / 10000),
              estimates=[{"symbol": e["symbol"], "original_weight": e["original_weight"],
                          "attempt_mark_rmb": e["price"] / 100,
                          "has_execution_open": bool(rows.get(e["symbol"], {}).get("open", 0) > 0),
                          "quantity_at_execution_mark": e["quantity"], "floor_quantity": e["floor_quantity"],
                          "minimum_buy_quantity": e["minimum"]} for e in entries],
              basis="Prior decision weights/current attempt marks; same allocator, NOT guaranteed fills")
    return {e["symbol"]: e["quantity"] for e in entries}
