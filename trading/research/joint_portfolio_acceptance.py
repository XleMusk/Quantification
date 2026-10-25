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


def build_joint_portfolio(account_paths: Mapping[str, Path] = DEFAULT_SELECTED, *,
                          initial_cash_cny: float = 140_000.0, gross_limit: float = 0.8,
                          single_limit: float = 0.2, lot_size: int = 100) -> dict[str, Any]:
    if not account_paths:
        raise ValueError("At least one strategy account is required")
    if initial_cash_cny <= 0 or not 0 < single_limit <= gross_limit <= 1 or lot_size <= 0:
        raise ValueError("Invalid portfolio constraints")
    snapshots = [_snapshot(strategy_id, Path(path)) for strategy_id, path in account_paths.items()]
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
        "accounts": snapshots, "target_rows": rows,
        "target_notional_cents": target_notional,
        "cash_residual_cents": pool_cents - target_notional,
        "orders_created": False, "fills_created": False, "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "blockers": {"lot_infeasible_symbols": blockers},
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
