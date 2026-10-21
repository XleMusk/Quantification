"""Current weight targets into the existing manual-only share-plan consumer."""
from copy import deepcopy
from datetime import datetime, timedelta
import re

from ..config import StrategyConfig
from ..contracts import IntentKind, MARKET_TZ, PortfolioIntent, Price, PriceBasis
from ..engine import shares_for_target
from ..portfolio_allocation import OBSERVED_LOT_ALLOCATION, stock_lot_allocation
from ..rules import execution_rule
from .adapter import fresh, from_portfolio, money, now, validate_cash_balance, validate_snapshot
from .ledger_import import marks_of


def from_weights(snapshot, weights, *, current=None, config=None):
    current = current or now()
    validate_snapshot(snapshot, current)
    if not validate_cash_balance(snapshot):
        raise ValueError("Weight sizing requires observed total and frozen cash, not available cash alone")
    if (weights.get("mode") != "current_preview"
            or not isinstance(weights.get("strategy_id"), str) or not weights["strategy_id"].strip()):
        raise ValueError("Only explicitly reviewed current-preview weights are accepted")
    if weights.get("account_alias") != snapshot["account_alias"]:
        raise ValueError("Weight portfolio account alias mismatch")
    if weights.get("weight_unit") != "equity_fraction":
        raise ValueError("Weights must explicitly use equity_fraction, not percentages or share quantities")
    fresh(weights["generated_at"], current)
    entries = weights.get("targets")
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ValueError("Weight targets must be a complete list")
    if any("symbol" not in item or "target_weight" not in item for item in entries):
        raise ValueError("Every weight target requires symbol and target_weight")
    intent = PortfolioIntent(IntentKind.FULL_TARGET,
                             tuple((item["symbol"], item["target_weight"]) for item in entries))
    held = {symbol for symbol, position in snapshot["positions"].items() if position["quantity"]}
    missing = held - {symbol for symbol, _ in intent.weights}
    if missing:
        raise ValueError(f"Explicit weights required for all held securities: {', '.join(sorted(missing))}")
    config = (config or StrategyConfig()).validate()
    policy = weights.get("lot_allocation")
    if policy not in (None, OBSERVED_LOT_ALLOCATION):
        raise ValueError("Current weight plans require the observed-mark lot allocation policy, not a replay policy")
    if (any(weight > config.single_limit + 1e-12 for _, weight in intent.weights)
            or sum(weight for _, weight in intent.weights) > config.gross_limit + 1e-12):
        raise ValueError("Weight targets exceed the existing single/gross position caps; no silent clipping")

    marks = marks_of(snapshot)
    held_value = sum(marks[symbol] * snapshot["positions"][symbol]["quantity"] for symbol in held)
    equity = snapshot["cash_total"] + held_value
    if equity <= 0:
        raise ValueError("Observed portfolio equity must be positive")
    day = current.astimezone(MARKET_TZ).date().isoformat()
    targets, sizing, quote_times, valuations, rules = [], [], [], {}, {}
    for item in entries:
        symbol, weight = item["symbol"], item["target_weight"]
        if not re.fullmatch(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)", symbol):
            raise ValueError("Weight plans support only the existing main-board instrument scope")
        if item.get("price_basis") != "raw_rmb":
            raise ValueError("Adjusted/research prices cannot enter weight sizing or the RMB order form")
        price, observed_at, source = 0, None, None
        if symbol in held:
            if "quote" in item:
                raise ValueError("Held securities use observed account marks, not a separate quote")
            price, observed_at, source = marks[symbol], snapshot["observed_at"], "account_mark"
        elif weight > 0:
            quote = item.get("quote")
            if (not isinstance(quote, dict) or quote.get("symbol") != symbol
                    or quote.get("price_basis") != "raw_rmb" or quote.get("trading_date") != day):
                raise ValueError("New holdings require a same-symbol, current-date raw RMB quote")
            fresh(quote["observed_at"], current)
            price = money(quote["price_rmb"])
            Price(price, PriceBasis.RAW)
            observed_at, source = quote["observed_at"], "explicit_current_quote"
            quote_times.append(datetime.fromisoformat(observed_at))
        valuations[symbol] = (price, observed_at, source)
        rules[symbol] = execution_rule(symbol, day)

    allocation = None
    if policy is not None:
        lot_entries, budget, spent = stock_lot_allocation(
            {s: w for s, w in intent.weights if w > 0}, equity,
            {s: v[0] for s, v in valuations.items()}, rules, config.single_limit, config.gross_limit)
        quantities = {e["symbol"]: e["quantity"] for e in lot_entries}
        allocation = {"policy": policy, "stock_budget_cny": float(budget / 100),
                      "allocated_stock_cny": spent / 100,
                      "squared_error_before": float(sum((e["target"] - e["floor_quantity"] * e["price"]) ** 2
                                                         for e in lot_entries) / 10000),
                      "squared_error_after": float(sum((e["target"] - e["quantity"] * e["price"]) ** 2
                                                        for e in lot_entries) / 10000),
                      "estimates": [{"symbol": e["symbol"], "floor_quantity": e["floor_quantity"],
                                     "target_quantity": e["quantity"], "valuation_price_cents": e["price"]}
                                    for e in lot_entries],
                      "basis": "Observed account marks/explicit quotes; NOT opens, reserved cash or guaranteed fills"}
    for item in entries:
        symbol, weight = item["symbol"], item["target_weight"]
        price, observed_at, source = valuations[symbol]
        qty = (quantities.get(symbol, 0) if policy is not None
               else shares_for_target(equity, weight, price, rules[symbol]))
        target = {"symbol": symbol, "target_qty": qty, "price_basis": "raw_rmb"}
        if "limit_price_rmb" in item:
            target["limit_price_rmb"] = item["limit_price_rmb"]
        targets.append(target)
        sizing.append({"symbol": symbol, "target_weight": weight, "target_qty": qty,
                       "valuation_price_cents": price or None, "valuation_observed_at": observed_at,
                       "valuation_source": source, "rounded_weight": price * qty / equity})

    portfolio = {"mode": weights["mode"], "strategy_id": weights["strategy_id"],
                 "account_alias": weights["account_alias"], "generated_at": weights["generated_at"],
                 "targets": targets}
    result = from_portfolio(snapshot, portfolio, current=current, config=config)
    result["portfolio"] = portfolio
    result["weight_sizing"] = {"equity_cents": equity, "held_value_cents": held_value,
                               "cash_total_cents": snapshot["cash_total"],
                               "cash_frozen_cents": snapshot["cash_frozen"],
                               "single_limit": config.single_limit, "gross_limit": config.gross_limit,
                               "rounding": "engine_floor_to_quantity_step", "targets": sizing}
    if policy is not None:
        result["weight_sizing"].update(rounding=policy, lot_allocation=policy, allocation=allocation)
    if result["next_plan"] is not None:
        plan = result["next_plan"]
        plan["weight_portfolio"] = deepcopy(weights)
        plan["weight_sizing"] = deepcopy(result["weight_sizing"])
        # A late save cannot give an older valuation quote a new five-minute life.
        expiry = datetime.fromisoformat(plan["expires_at"])
        plan["expires_at"] = min([expiry, *(stamp + timedelta(minutes=5) for stamp in quote_times)]).isoformat()
    return result
