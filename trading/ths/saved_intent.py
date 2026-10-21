"""Compare a saved research target with fresh THS evidence, without promotion.

This pure consumer reuses the manual adapter's share/order arithmetic.  Its
diagnostic order can never be passed to the prefill path as an eligible plan.
"""
from copy import deepcopy
from decimal import Decimal
import re

from ..config import StrategyConfig
from ..contracts import MARKET_TZ
from ..engine import shares_for_target
from ..research.saved_account_intent import read_request
from ..rules import execution_rule, fees
from .account_risk import evaluate_order_risk, package_from_account_observation
from .adapter import fresh, money, now, validate_cash_balance, validate_snapshot
from .broker import THSAdapter
from .ledger_import import marks_of


_MAIN = re.compile(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)")
_FLAGS = {"research_only": True, "candidate_panel_only": True,
          "execution_admitted": False, "selection_enabled": False,
          "strategy_validated": False, "prefill_allowed": False,
          "submitted": False, "client_interacted": False, "saved": False}


def _quote_records(quotes, snapshot, current):
    if (not isinstance(quotes, dict)
            or quotes.get("schema") != "ths_raw_quote_observation_v1"
            or quotes.get("source") not in {"ths_controls", "ths_user_export"}
            or quotes.get("account_alias") != snapshot["account_alias"]
            or quotes.get("trading_date") != snapshot["trading_date"]
            or quotes.get("price_basis") != "raw_rmb"
            or not isinstance(quotes.get("prices"), dict)):
        raise ValueError("Changes require same-account, same-day raw THS broker quotes")
    fresh(quotes["observed_at"], current)
    return quotes["prices"]


def _quote(symbol, quotes, records, snapshot, current):
    record = records.get(symbol)
    if not isinstance(record, dict):
        raise ValueError(f"Missing raw THS broker quote for {symbol}")
    if (record.get("source", quotes["source"]) not in {"ths_controls", "ths_user_export"}
            or record.get("account_alias", snapshot["account_alias"]) != snapshot["account_alias"]
            or record.get("symbol", symbol) != symbol
            or record.get("trading_date", quotes["trading_date"]) != snapshot["trading_date"]
            or record.get("price_basis", quotes["price_basis"]) != "raw_rmb"):
        raise ValueError(f"Broker quote identity/source mismatch for {symbol}")
    fresh(record.get("observed_at", quotes["observed_at"]), current)
    price = money(record["price_rmb"])
    limit = money(record.get("limit_price_rmb", record["price_rmb"]))
    preclose = money(record["preclose_rmb"])
    if (min(price, limit, preclose) <= 0
            or type(record.get("is_st")) is not bool
            or type(record.get("tradable")) is not bool):
        raise ValueError(f"Explicit positive price/preclose and boolean status required for {symbol}")
    return {**record, "price_cents": price, "limit_cents": limit,
            "preclose_rmb": f"{Decimal(preclose) / 100:.2f}"}


def prepare_saved_intent(handoff, snapshot, quotes, *, current=None, config=None,
                         execution_scope="strict"):
    """Return an unsaveable research-only account comparison; no I/O or fills."""
    current = current or now()
    intent = read_request(handoff["saved_account_intent"], current=current)
    if any(handoff.get(key) != intent[value] for key, value in (
            ("strategy_id", "strategy_id"), ("source_cutoff", "source_day"),
            ("execution_day", "execution_day"))):
        raise ValueError("Saved intent strategy/source/execution day differs from its handoff")
    validate_snapshot(snapshot, current)
    if not validate_cash_balance(snapshot):
        raise ValueError("Saved intent comparison requires total/frozen/available cash")
    if snapshot["trading_date"] != intent["execution_day"]:
        raise ValueError("Observation must be from the saved intent's exact execution day")
    if execution_scope not in {"strict", "mainboard_only"}:
        raise ValueError("Unknown saved-intent execution scope")
    expected = StrategyConfig(**intent["execution_config"]).validate()
    if config is not None:
        supplied = (StrategyConfig(**config) if isinstance(config, dict) else config).validate()
        if supplied.to_dict() != expected.to_dict():
            raise ValueError("Saved-intent execution configuration cannot be replaced")
    config = expected
    positions = snapshot["positions"]
    held = {symbol: row["quantity"] for symbol, row in positions.items() if row["quantity"]}
    marks = marks_of(snapshot)
    held_value = sum(marks[symbol] * qty for symbol, qty in held.items())
    equity = snapshot["cash_total"] + held_value
    if equity <= 0:
        raise ValueError("Observed equity must be positive")
    kind, weights = intent["kind"], intent["weights"]
    if kind not in {"FULL_TARGET", "NO_CHANGE"} or (kind == "NO_CHANGE" and weights is not None):
        raise ValueError("Saved intent kind/weights disagree")
    if kind == "FULL_TARGET":
        if (not isinstance(weights, dict)
                or any(type(w) not in {int, float} or not 0 <= w <= config.single_limit
                       for w in weights.values())
                or sum(weights.values()) > config.gross_limit + 1e-12):
            raise ValueError("Saved full weights exceed their fixed configuration")
    exits = set(intent["remaining_exit_symbols"])
    if kind == "FULL_TARGET" and any(weights.get(s, 0) > 0 for s in exits):
        raise ValueError("Saved full target conflicts with its remaining full exits")
    symbols = set(held) | exits | (set(weights) if weights is not None else set())
    targets, sizing, omitted, validated_quotes = [], [], [], {}
    records = None

    def broker_quote(symbol):
        nonlocal records
        if symbol not in validated_quotes:
            records = _quote_records(quotes, snapshot, current) if records is None else records
            validated_quotes[symbol] = _quote(symbol, quotes, records, snapshot, current)
        return validated_quotes[symbol]

    for symbol in sorted(symbols):
        quantity = held.get(symbol, 0)
        if not re.fullmatch(r"\d{6}\.(?:SSE|SZSE|BSE)", symbol):
            raise ValueError("Saved target requires exchange-qualified security identities")
        supported = bool(_MAIN.fullmatch(symbol))
        if execution_scope == "mainboard_only" and not supported:
            desired = quantity
            omitted.append({"symbol": symbol, "held_qty": quantity,
                            "saved_weight": weights.get(symbol) if weights is not None else None,
                            "remaining_exit": symbol in exits,
                            "reason": "outside_mainboard_scope_preserved_without_redistribution"})
        elif kind == "NO_CHANGE":
            desired = 0 if symbol in exits else quantity
        else:
            weight = weights.get(symbol, 0)
            # An unsupported held security may remain unchanged under NC, but
            # a new/full rebalance cannot quietly trade it or drop its weight.
            if not supported and (weight > 0 or quantity > 0):
                raise ValueError(f"Unsupported saved target requires a change: {symbol}")
            price = marks[symbol] if quantity else broker_quote(symbol)["price_cents"] if weight else 0
            desired = (shares_for_target(equity, weight, price, execution_rule(symbol, intent["execution_day"]))
                       if weight else 0)
            sizing.append({"symbol": symbol, "target_weight": weight, "target_qty": desired,
                           "valuation_price_cents": price or None,
                           "valuation_source": "account_mark" if quantity else "broker_quote" if weight else None})
        target = {"symbol": symbol, "target_qty": desired, "price_basis": "raw_rmb"}
        if desired != quantity:
            if not supported:
                raise ValueError(f"Unsupported saved target requires a change: {symbol}")
            quote = broker_quote(symbol)
            target["limit_price_rmb"] = f"{Decimal(quote['limit_cents']) / 100:.2f}"
        targets.append(target)

    portfolio = {"mode": "current_preview", "strategy_id": intent["strategy_id"],
                 "account_alias": snapshot["account_alias"], "generated_at": current.isoformat(),
                 "targets": targets}
    result = THSAdapter({"account_alias": snapshot["account_alias"]}).prepare_portfolio(
        snapshot, portfolio, current=current, config=config)
    result["portfolio"] = portfolio
    result["weight_sizing"] = {"equity_cents": equity, "held_value_cents": held_value,
        "cash_total_cents": snapshot["cash_total"], "cash_frozen_cents": snapshot["cash_frozen"],
        "rounding": "engine_floor_to_quantity_step", "targets": sizing}
    # The risk engine receives preclose/status but retains observed account
    # marks for equity; order limits must not revalue existing holdings.
    risk_quotes = {"prices": {s: {k: q[k] for k in ("preclose_rmb", "is_st", "tradable")}
                              for s, q in validated_quotes.items()}}
    package = package_from_account_observation(snapshot)
    risks = {}
    for step in result["adjustments"]:
        delta = step["delta_qty"]
        step["remaining_qty"] = abs(delta)
        step["remaining_exit_qty"] = held.get(step["symbol"], 0) if step["symbol"] in exits else 0
        if not delta:
            continue
        symbol, side = step["symbol"], step["side"]
        price = validated_quotes[symbol]["limit_cents"]
        risk = evaluate_order_risk(package, [{"symbol": symbol, "side": side, "qty": abs(delta),
            "price_rmb": f"{Decimal(price) / 100:.2f}",
            "fee_cents": fees(price * abs(delta), side, config, day=intent["execution_day"])}],
            quote_observation=risk_quotes, max_single_weight=Decimal(str(config.single_limit)),
            max_gross_weight=Decimal(str(config.gross_limit)),
            minimum_cash_buffer=Decimal(1) - Decimal(str(config.gross_limit)))
        risks[symbol] = risk
        step["risk_blockers"] = list(risk["blockers"])
        if risk["blockers"]:
            step["status"] = "blocked"
    result["adapter_status"] = result["status"]
    diagnostic = result.pop("next_plan")
    if diagnostic is not None:
        if risks[diagnostic["symbol"]]["blockers"]:
            result["adapter_status"] = "blocked"
        diagnostic.update(_FLAGS)
        diagnostic.update(status="research_only_not_prefill_eligible", submission="research_only")
    result.update(_FLAGS)
    result.update(status="research_only_not_prefill_eligible", next_plan=None,
        diagnostic_next_plan=diagnostic, submission="research_only", execution_scope=execution_scope,
        saved_account_intent=deepcopy(intent), scope_exclusions=omitted, order_risk=risks,
        remaining_exit_symbols=sorted(exits), source_tasks_mutated=False,
        source_basis="saved_research_account_not_a_current_broker_signal")
    return result
