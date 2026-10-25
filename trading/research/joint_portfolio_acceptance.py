"""Build a date-aligned research target from saved strategy accounts.

This package is the portfolio stage after individual Engine/Ledger replays. It
uses only accounts with the same terminal day, computes a fixed equal-sleeve
target and lot-rounded quantities, and explicitly excludes mixed-date sleeves.
It creates no orders, fills, Ledger state, broker calls, or THS payloads.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping

from ..rules import stock_quantity_rule
from .virtual_strategy_sleeves import DEFAULT_ACCOUNTS


SCHEMA = "quantification_joint_portfolio_acceptance_v1"
VERIFIED = "VERIFIED_RESEARCH_ONLY_JOINT_PORTFOLIO"
CAPACITY_POLICIES = {"strict", "budgeted"}
DEFAULT_SELECTED = {
    "mature_factor_balanced_ridge": DEFAULT_ACCOUNTS["mature_factor_balanced_ridge"],
    "alpha158_lgbm_cap500": DEFAULT_ACCOUNTS["alpha158_lgbm_cap500"],
}
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_joint_portfolio_acceptance_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_joint_portfolio_acceptance_v1.md")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected account JSON object: {path}")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite numeric")
    return float(value)


def _snapshot(strategy_id: str, path: Path) -> dict[str, Any]:
    account = _read(path)
    day = str(account.get("day") or "")
    if not day:
        raise ValueError(f"{strategy_id} has no terminal day")
    if account.get("execution_admitted") is True:
        raise ValueError(f"{strategy_id} is execution-admitted")
    comparison = account.get("portfolio_comparison") or {}
    if comparison.get("orders_created") is True or comparison.get("ledger_mutated") is True:
        raise ValueError(f"{strategy_id} has a mutating comparison artifact")
    targets = account.get("targets")
    if not isinstance(targets, Mapping) or not targets:
        raise ValueError(f"{strategy_id} has no terminal target weights")
    weights = {str(symbol): _finite(weight, f"{strategy_id}.targets.{symbol}") for symbol, weight in targets.items()}
    if any(weight < 0 or weight > 1 for weight in weights.values()):
        raise ValueError(f"{strategy_id} contains invalid target weight")
    prices = account.get("prices") or {}
    for symbol in weights:
        price = int(_finite(prices.get(symbol), f"{strategy_id}.prices.{symbol}"))
        if price <= 0:
            raise ValueError(f"{strategy_id} has no positive target mark for {symbol}")
    return {"strategy_id": strategy_id, "path": str(path.resolve()), "day": day,
            "targets": weights, "prices": {symbol: int(prices[symbol]) for symbol in weights},
            "account_cash_cents": int(account.get("cash", 0)),
            "account_equity_cents": int(account.get("cash", 0)) + sum(
                int(lot.get("qty", 0)) * int(prices[symbol])
                for symbol, lots in (account.get("positions") or {}).items()
                for lot in lots),
            "orders": len(account.get("orders") or {}), "fills": len(account.get("fills") or {})}


def _build_joint_from_snapshots(snapshots: list[dict[str, Any]], *,
                                initial_cash_cny: float = 140_000.0, gross_limit: float = 0.8,
                                single_limit: float = 0.2, lot_size: int = 100,
                                capacity_policy: str = "strict", max_target_rows: int = 22) -> dict[str, Any]:
    if (not snapshots or initial_cash_cny <= 0
            or not 0 < single_limit <= gross_limit <= 1
            or lot_size <= 0 or capacity_policy not in CAPACITY_POLICIES
            or type(max_target_rows) is not int or not 1 <= max_target_rows <= 22):
        raise ValueError("Invalid portfolio constraints")
    days = sorted({item["day"] for item in snapshots})
    common_day = days[0] if len(days) == 1 else None
    if common_day is None:
        raise ValueError("Joint portfolio requires a common terminal day")
    sleeve_weight = 1.0 / len(snapshots)
    weights: dict[str, float] = {}
    price_by_symbol: dict[str, int] = {}
    contributors: dict[str, list[str]] = {}
    for item in snapshots:
        for symbol, target in item["targets"].items():
            weights[symbol] = weights.get(symbol, 0.0) + sleeve_weight * target
            price_by_symbol.setdefault(symbol, item["prices"][symbol])
            contributors.setdefault(symbol, []).append(item["strategy_id"])
    gross = sum(weights.values())
    max_single = max(weights.values(), default=0.0)
    if gross > gross_limit + 1e-9 or max_single > single_limit + 1e-9:
        raise ValueError("Joint target violates portfolio limits")
    pool_cents = int(round(initial_cash_cny * 100))
    rows = []
    target_notional = 0
    for symbol, weight in sorted(weights.items()):
        desired_cents = int(round(pool_cents * weight))
        price = price_by_symbol[symbol]
        # Respect the instrument board rule in addition to the default A-share
        # lot size.  STAR-board equities require 200 shares per buy; emitting
        # 100-share targets would pass the portfolio math but fail in the
        # shared Orders/Ledger path.
        board_rule = stock_quantity_rule(symbol)
        # STAR-board buys have a 200-share minimum but then allow odd lots;
        # main/ChiNext retain the ordinary 100-share step.
        quantity_step = (board_rule.step if board_rule.step == 1
                         else max(lot_size, board_rule.step))
        minimum_quantity = board_rule.minimum
        quantity = desired_cents // price // quantity_step * quantity_step
        if quantity < minimum_quantity:
            quantity = 0
        rounding_policy = "FLOOR_TO_LOT"
        if (weight > 0 and quantity == 0
                and price * minimum_quantity <= int(round(pool_cents * single_limit))):
            quantity = minimum_quantity
            rounding_policy = "ROUND_UP_ONE_BOARD_LOT_WITHIN_SINGLE_LIMIT"
        notional = quantity * price
        target_notional += notional
        rows.append({"symbol": symbol, "contributors": sorted(set(contributors[symbol])),
                     "target_weight": weight, "mark_cents": price,
                     "desired_notional_cents": desired_cents, "target_quantity": quantity,
                     "target_notional_cents": notional, "actual_weight": notional / pool_cents,
                     "lot_rounding_policy": rounding_policy,
                     "quantity_step": quantity_step,
                     "lot_rounding_residual_cents": desired_cents - notional})
    economic_rows = list(rows)
    dropped_rows: list[dict[str, Any]] = []
    if capacity_policy == "budgeted":
        budget_cents = int(round(pool_cents * gross_limit))
        selected: list[dict[str, Any]] = []
        used_cents = 0
        candidates = sorted(
            (row for row in rows if row["target_quantity"] > 0),
            key=lambda row: (row["target_notional_cents"], -row["target_weight"], row["symbol"]),
        )
        for row in candidates:
            if len(selected) >= max_target_rows:
                dropped_rows.append({"symbol": row["symbol"], "reason": "capacity_max_target_rows"})
                continue
            if used_cents + row["target_notional_cents"] <= budget_cents:
                selected.append(row)
                used_cents += row["target_notional_cents"]
            else:
                dropped_rows.append({"symbol": row["symbol"], "reason": "capacity_gross_cash_budget"})
        selected_symbols = {row["symbol"] for row in selected}
        for row in rows:
            if row["symbol"] not in selected_symbols and not any(
                    item["symbol"] == row["symbol"] for item in dropped_rows):
                dropped_rows.append({
                    "symbol": row["symbol"],
                    "reason": "capacity_no_board_valid_quantity" if row["target_quantity"] <= 0
                    else "capacity_selection",
                })
        rows = selected
        target_notional = sum(row["target_notional_cents"] for row in rows)
    lot_feasible = all(row["target_quantity"] > 0 or row["target_weight"] == 0 for row in rows)
    actual_gross = target_notional / pool_cents
    actual_max_single = max((row["actual_weight"] for row in rows), default=0.0)
    constraints_pass = actual_gross <= gross_limit + 1e-9 and actual_max_single <= single_limit + 1e-9
    status = VERIFIED if lot_feasible and constraints_pass else "PARTIAL_JOINT_PORTFOLIO_LOT_BLOCKED"
    blockers = [row["symbol"] for row in rows if not (row["target_quantity"] > 0 or row["target_weight"] == 0)]
    for row in rows:
        row["lot_feasible"] = row["target_quantity"] > 0 or row["target_weight"] == 0
    return {
        "schema": SCHEMA, "status": status, "execution_admitted": False,
        "research_only": True, "common_terminal_day": common_day,
        "strategy_ids": [item["strategy_id"] for item in snapshots],
        "sleeve_weight": sleeve_weight, "initial_cash_cny": initial_cash_cny,
        "constraints": {"gross_limit": gross_limit, "single_limit": single_limit,
                        "merged_gross_weight": gross, "merged_max_single_weight": max_single,
                        "actual_gross_weight": actual_gross, "actual_max_single_weight": actual_max_single,
                        "gross_pass": constraints_pass and actual_gross <= gross_limit + 1e-9,
                        "single_pass": constraints_pass and actual_max_single <= single_limit + 1e-9,
                        "lot_size": lot_size},
        "accounts": snapshots, "target_rows": rows, "economic_target_rows": economic_rows,
        "dropped_rows": dropped_rows, "capacity_policy": capacity_policy,
        "max_target_rows": max_target_rows,
        "target_notional_cents": target_notional,
        "cash_residual_cents": pool_cents - target_notional,
        "orders_created": False, "fills_created": False, "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "blockers": {"lot_infeasible_symbols": blockers,
                     "capacity_dropped_symbols": [item["symbol"] for item in dropped_rows]},
        "lot_rounding_policy": "round_up_one_lot_if_positive_target_and_within_single_limit_else_block",
        "acceptance": {"common_day": True, "gross_pass": actual_gross <= gross_limit + 1e-9,
                        "single_pass": actual_max_single <= single_limit + 1e-9,
                        "integer_lots": all(row["target_quantity"] % lot_size == 0 for row in rows),
                        "all_positive_targets_lot_feasible": lot_feasible,
                        "cash_nonnegative": pool_cents >= target_notional,
                        "no_execution_side_effect": True},
        "limitations": [
            "Only aligned saved terminal accounts are included; other candidates with another day are excluded.",
            "The target is a research projection, not a current paper or broker order plan.",
            "PIT, fresh holdout and promotion gates remain separate from portfolio arithmetic.",
        ],
    }


def build_joint_portfolio(account_paths: Mapping[str, Path] = DEFAULT_SELECTED, *,
                          initial_cash_cny: float = 140_000.0, gross_limit: float = 0.8,
                          single_limit: float = 0.2, lot_size: int = 100,
                          capacity_policy: str = "strict", max_target_rows: int = 22) -> dict[str, Any]:
    """Build a joint target from saved Engine/Ledger account snapshots."""
    if not account_paths:
        raise ValueError("At least one strategy account is required")
    snapshots = [_snapshot(strategy_id, Path(path)) for strategy_id, path in account_paths.items()]
    return _build_joint_from_snapshots(
        snapshots, initial_cash_cny=initial_cash_cny, gross_limit=gross_limit,
        single_limit=single_limit, lot_size=lot_size,
        capacity_policy=capacity_policy, max_target_rows=max_target_rows,
    )


def build_joint_portfolio_from_payloads(
    payloads: Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]], *,
    initial_cash_cny: float = 140_000.0, gross_limit: float = 0.8,
    single_limit: float = 0.2, lot_size: int = 100,
    capacity_policy: str = "strict", max_target_rows: int = 22,
) -> dict[str, Any]:
    """Build the same target from explicit same-day Target and Quote payloads."""
    if not payloads:
        raise ValueError("At least one strategy target/quote payload is required")
    snapshots: list[dict[str, Any]] = []
    for strategy_id, pair in payloads.items():
        if not isinstance(strategy_id, str) or not strategy_id or not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("Payload mapping must contain strategy_id -> (target, quote) pairs")
        target, quote = pair
        if (target.get("execution_admitted") is not False or target.get("research_only") is not True
                or target.get("strategy_id") != strategy_id):
            raise ValueError(f"Target payload is not research-only or strategy_id mismatches: {strategy_id}")
        decision_day = target.get("decision_day")
        if not isinstance(decision_day, str) or not decision_day:
            raise ValueError(f"Target payload has no decision_day: {strategy_id}")
        observed_at = str(quote.get("observed_at") or "")
        quote_day = quote.get("snapshot_date") or observed_at[:10]
        if (quote.get("execution_admitted") is not False
                or quote.get("client_interacted") is not False
                or quote_day != decision_day
                or quote.get("price_basis") != "raw_rmb"):
            raise ValueError(f"Quote payload is not a same-day read-only raw-RMB observation: {strategy_id}")
        weights = target.get("weights")
        prices = quote.get("prices")
        if not isinstance(weights, Mapping) or not weights or not isinstance(prices, Mapping):
            raise ValueError(f"Target/quote payload is missing weights or prices: {strategy_id}")
        if set(weights) != set(prices):
            raise ValueError(f"Target/quote symbols do not match: {strategy_id}")
        price_cents: dict[str, int] = {}
        for symbol, quote_row in prices.items():
            try:
                price_cents[str(symbol)] = int(round(float(quote_row["price_rmb"]) * 100))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid quote price: {strategy_id}.{symbol}") from error
            if price_cents[str(symbol)] <= 0:
                raise ValueError(f"Quote price must be positive: {strategy_id}.{symbol}")
        snapshots.append({
            "strategy_id": strategy_id, "path": "payload://" + strategy_id,
            "day": decision_day,
            "targets": {str(symbol): _finite(weight, f"{strategy_id}.weights.{symbol}")
                         for symbol, weight in weights.items()},
            "prices": price_cents, "account_cash_cents": 0,
            "account_equity_cents": 0, "orders": 0, "fills": 0,
        })
    return _build_joint_from_snapshots(
        snapshots, initial_cash_cny=initial_cash_cny, gross_limit=gross_limit,
        single_limit=single_limit, lot_size=lot_size,
        capacity_policy=capacity_policy, max_target_rows=max_target_rows,
    )


def build_report(result: Mapping[str, Any]) -> str:
    lines = ["# Joint portfolio acceptance (research-only)", "",
             f"Status: `{result['status']}`; common terminal day: `{result['common_terminal_day']}`; execution admitted: `false`.",
             f"Strategies: {', '.join(result['strategy_ids'])}; sleeve weight: {result['sleeve_weight']:.2%}.", "",
             "| Symbol | Contributors | Target weight | Quantity | Mark cents | Notional CNY |", "|---|---|---:|---:|---:|---:|"]
    for row in result["target_rows"]:
        lines.append(f"| {row['symbol']} | {', '.join(row['contributors'])} | {row['target_weight']:.2%} | "
                     f"{row['target_quantity']} | {row['mark_cents']} | {row['target_notional_cents']/100:.2f} |")
    lines += ["", f"Target notional: CNY {result['target_notional_cents']/100:.2f}; "
              f"cash residual: CNY {result['cash_residual_cents']/100:.2f}.", "",
              "This output creates no orders, fills or persistent Ledger state."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    result = build_joint_portfolio()
    output, report = args.output.resolve(), args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(build_report(result), encoding="utf-8")
    print(json.dumps({"status": result["status"], "common_terminal_day": result["common_terminal_day"],
                      "strategy_count": len(result["strategy_ids"]), "target_rows": len(result["target_rows"]),
                      "execution_admitted": result["execution_admitted"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_joint_portfolio", "build_report", "main", "DEFAULT_SELECTED", "VERIFIED"]
