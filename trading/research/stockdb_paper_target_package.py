"""Build a StockDB paper target/order package without broker interaction.

The package is deliberately paper-only: ``execution_admitted`` is always
false and all fills are local projections on the shared :class:`Ledger`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import date
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..engine import shares_for_target
from ..ledger import Ledger, new_state
from ..routing import route_order
from ..rules import execution_rule


def _write_csv(path: Path, records: list[dict]) -> None:
    fields = list(records[0]) if records else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def build_paper_target_package(
    target_payload: dict,
    quote_payload: dict,
    initial_cash: float,
    *,
    execution_day: str,
    settlement_day: str,
    output_dir: str | Path | None = None,
) -> dict:
    """Build target, order, projected-account and THS reference payloads.

    ``target_payload`` must be an explicitly research-only signal-frame target;
    ``quote_payload`` must be a raw RMB quote observation covering every target
    symbol. No THS client, broker adapter, or persistent ledger is touched.
    """
    if target_payload.get("execution_admitted") is not False or target_payload.get("research_only") is not True:
        raise ValueError("StockDB target must be explicitly research-only and unadmitted")
    if quote_payload.get("schema") != "ths_raw_quote_observation_v1" or quote_payload.get("price_basis") != "raw_rmb":
        raise ValueError("Current raw RMB quote observation required")
    if type(initial_cash) not in (int, float) or not math.isfinite(initial_cash) or initial_cash < 1000:
        raise ValueError("initial_cash must be at least 1000 RMB")
    if date.fromisoformat(execution_day).isoformat() != execution_day or date.fromisoformat(settlement_day).isoformat() != settlement_day or settlement_day <= execution_day:
        raise ValueError("Explicit canonical execution and later settlement dates required")

    weights = target_payload.get("weights") or {}
    signals = target_payload.get("signal_frame", {}).get("signals") or []
    ranks = {
        f"{row['security']['code']}.{row['security']['exchange']}": row.get("diagnostics", {}).get("rank")
        for row in signals
        if row.get("diagnostics", {}).get("rank") is not None
    }
    ranks = {symbol: rank for symbol, rank in ranks.items() if symbol in weights}
    if not weights or set(weights) != set(ranks) or any(type(rank) is not int or rank <= 0 for rank in ranks.values()) or len(set(ranks.values())) != len(ranks):
        raise ValueError("Target weights and ranked signal-frame symbols must match")
    if any(type(weight) not in (int, float) or not math.isfinite(weight) or not 0 < weight <= 0.2 for weight in weights.values()):
        raise ValueError("Target weights must be positive, finite and within the 20% single-name cap")
    if sum(float(weight) for weight in weights.values()) > 0.8 + 1e-9:
        raise ValueError("Target gross weight exceeds the 80% paper cap")
    prices = quote_payload.get("prices") or {}
    if set(prices) != set(weights):
        raise ValueError("Quote coverage must exactly match target symbols")
    if any(not math.isfinite(float(quote["price_rmb"])) or float(quote["price_rmb"]) <= 0 for quote in prices.values()):
        raise ValueError("Positive finite raw RMB prices required")

    holdings = len(weights)
    config = StrategyConfig(
        signal="equal_weight", holdings=holdings, retain_rank=max(holdings, 30),
        gross_limit=0.8, single_limit=min(0.2, max(float(w) for w in weights.values())),
        initial_cash=float(initial_cash),
    ).validate()
    equity_cents = int(round(float(initial_cash) * 100))
    state = new_state(float(initial_cash))
    context = RunContext("stockdb-paper-target-package", TradingMode.REPLAY)
    book = Ledger(state, config, context)
    rows = []
    for symbol, weight in sorted(weights.items(), key=lambda item: (ranks[item[0]], item[0])):
        price = int(round(float(prices[symbol]["price_rmb"]) * 100))
        rule = execution_rule(symbol, execution_day)
        quantity = shares_for_target(equity_cents, float(weight), price, rule)
        if quantity <= 0:
            raise ValueError(f"zero target qty for {symbol}")
        route = route_order(symbol)
        order_id = hashlib.sha256(f"{execution_day}|{symbol}|buy|{quantity}|{price}".encode()).hexdigest()[:32]
        order = book.submit(order_id, execution_day, symbol, "buy", int(quantity), price)
        if order["status"] != "submitted":
            raise ValueError(f"paper order rejected for {symbol}: {order.get('reason')}")
        book.mark_price(symbol, price, execution_day)
        fill_id = hashlib.sha256(f"fill|{order_id}".encode()).hexdigest()[:32]
        book.fill(fill_id, order_id, int(quantity), price, execution_day, settlement_day)
        notional = price * quantity / 100
        rows.append({
            "rank": int(ranks[symbol]), "symbol": symbol, "target_weight": float(weight),
            "price_rmb": f"{price / 100:.2f}", "target_qty": int(quantity),
            "target_notional_rmb": round(notional, 2), "realized_weight": round(notional / float(initial_cash), 8),
            "side": "buy", "order_id": order_id, "paper_order_status": order["status"],
            "paper_fill_status": state["orders"][order_id]["status"],
            "route_id": route.route_id, "route_status": route.status, "route_reason": route.reason,
            "execution_admitted": False,
        })

    for symbol, quote in prices.items():
        book.mark_price(symbol, int(round(float(quote["price_rmb"]) * 100)), execution_day)
    fees = sum(order["fees"] for order in state["orders"].values()) / 100
    final_equity = book.equity() / 100
    positions = {symbol: book.quantity(symbol) for symbol in weights}
    target_holdings = rows
    order_plan = rows
    ths_reference = [{
        "sequence": row["rank"], "symbol": row["symbol"], "side": row["side"],
        "qty_field": row["target_qty"] if row["route_status"] == "route_available" else "",
        "price_field": row["price_rmb"] if row["route_status"] == "route_available" else "",
        "symbol_field": row["symbol"].split(".")[0] if row["route_status"] == "route_available" else "",
        "route_id": row["route_id"], "route_status": row["route_status"],
        "prefill_status": "reference_payload_only" if row["route_status"] == "route_available" else "blocked_unsupported_instrument_route",
        "price_basis": "raw_rmb_quote_observation; paper reference only", "execution_admitted": False,
    } for row in rows]
    projected_account = {
        "schema": "paper_projected_account_v1", "execution_admitted": False,
        "initial_cash_rmb": float(initial_cash), "cash_after_modeled_fills_rmb": round(state["cash"] / 100, 2),
        "fees_rmb": round(fees, 2), "positions": positions, "sellable_next_session": positions,
        "marks_rmb": {symbol: quote["price_rmb"] for symbol, quote in prices.items()},
        "equity_at_observed_marks_rmb": round(final_equity, 2),
        "t1_policy": "ordinary-stock fills available on the next session; this is a paper assumption",
    }
    result = {
        "schema": "stockdb_paper_target_order_package_v1", "status": "paper_target_order_package_complete",
        "strategy_id": target_payload.get("strategy_id"), "decision_day": target_payload.get("decision_day"),
        "execution_day": execution_day, "quote_observed_at": quote_payload.get("observed_at"),
        "quote_source": quote_payload.get("source"), "cash_assumption_rmb": float(initial_cash),
        "weight_basis": "StockDB signal-frame target weights; maximum 80% gross and 20% single-name cap",
        "lot_rounding": "existing execution_rule and shares_for_target",
        "target_count": len(rows), "paper_order_count": len(rows),
        "route_available_count": sum(row["route_status"] == "route_available" for row in rows),
        "route_blocked_count": sum(row["route_status"] != "route_available" for row in rows),
        "estimated_target_notional_rmb": round(sum(row["target_notional_rmb"] for row in rows), 2),
        "modeled_fees_rmb": round(fees, 2), "projected_final_cash_rmb": round(state["cash"] / 100, 2),
        "projected_final_equity_rmb": round(final_equity, 2), "execution_admitted": False,
        "account_observed": False, "client_interacted": False, "submitted": False, "cancelled": False,
        "ledger_persisted": False,
        "limitations": [
            "Initial cash is an independent research assumption, not an observed THS account balance.",
            "Captured raw RMB quotes are references, not broker fills or submission instructions.",
            "Paper fills use the shared local Ledger solely to project cash, fees and T+1 positions.",
            "Unsupported routes stay in the research projection but have no THS form reference fields.",
            "Settlement day is explicitly supplied by the caller; exchange-calendar admission is not inferred.",
            "No THS interaction, order submission, cancellation, transfer or persistent Ledger mutation occurred.",
        ],
    }
    package = {"target_holdings": target_holdings, "order_plan": order_plan,
               "ths_prefill_payloads": ths_reference, "projected_account": projected_account, "result": result}
    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name in ("target_holdings", "order_plan", "ths_prefill_payloads"):
            _write_csv(out / f"{name}.csv", package[name])
        result["artifacts"] = {name: str(out / f"{name}.{'csv' if name in {'target_holdings', 'order_plan', 'ths_prefill_payloads'} else 'json'}")
                                for name in package}
        for name in ("projected_account", "result"):
            (out / f"{name}.json").write_text(json.dumps(package[name], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return package


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--quotes", type=Path, required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--settlement-day", required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    package = build_paper_target_package(json.loads(args.target.read_text(encoding="utf-8")),
                                         json.loads(args.quotes.read_text(encoding="utf-8")),
                                         args.initial_cash, execution_day=args.execution_day,
                                         settlement_day=args.settlement_day, output_dir=args.output_dir)
    print(json.dumps(package["result"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
