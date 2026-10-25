"""Attribute several saved research strategies to one virtual cash pool.

This is a research-only portfolio composition step.  It reads terminal
``account.json`` files already produced by the shared Engine/Ledger replays,
apportions an equal virtual sleeve budget, and aggregates target weights.  It
does not revalue accounts across dates, create orders, mutate a Ledger, or
open a broker/THS channel.  Mixed terminal dates are retained explicitly so
the result cannot be mistaken for a current executable target.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_virtual_strategy_sleeves_v1"
VERIFIED = "VERIFIED_RESEARCH_ONLY_VIRTUAL_SLEEVES"
DEFAULT_INITIAL_CASH_CNY = 140_000.0
DEFAULT_GROSS_LIMIT = 0.8
DEFAULT_SINGLE_LIMIT = 0.2
DEFAULT_ACCOUNTS = {
    "fixed_low_volatility_252_20": Path(
        r"D:\QuantificationData\trading\research\stockdb_rolling_low_volatility\20261024_120755_bdd75bd2\low_volatility_252_20\account.json"
    ),
    "low_vol_half_momentum_20": Path(
        r"D:\QuantificationData\trading\research\stockdb_rolling_low_volatility\20261024_121737_d2456fd6\low_vol_half_momentum_20\account.json"
    ),
    "momentum_12_1_20": Path(
        r"D:\QuantificationData\trading\research\stockdb_rolling_low_volatility\20261024_121737_d2456fd6\momentum_12_1_20\account.json"
    ),
    "mature_factor_balanced_ridge": Path(
        r"D:\Quantification\outputs\stockdb_factor_monthly_ridge_replays\20261024_monthly_same_lineage_cap500\account.json"
    ),
    "alpha158_lgbm_cap500": Path(
        r"D:\Quantification\outputs\stockdb_alpha158_lgbm_panel_replays\20261024_full_native_2026_cap500\account.json"
    ),
}
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_virtual_strategy_sleeves_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_virtual_strategy_sleeves_v1.md")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected account JSON object: {path}")
    return value


def _number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _receivables_cents(account: Mapping[str, Any]) -> int:
    total = 0
    for key, value in (account.get("receivables") or {}).items():
        if isinstance(value, Mapping):
            value = value.get("amount", value.get("cents", 0))
        total += int(round(_number(value, name=f"receivables.{key}")))
    return total


def _holdings_mark_cents(account: Mapping[str, Any]) -> int:
    prices = account.get("prices") or {}
    total = 0
    for symbol, lots in (account.get("positions") or {}).items():
        if not isinstance(lots, list):
            raise ValueError(f"positions.{symbol} must be a list")
        price = int(round(_number(prices.get(symbol), name=f"prices.{symbol}")))
        if price <= 0:
            raise ValueError(f"prices.{symbol} must be positive")
        for lot in lots:
            if not isinstance(lot, Mapping):
                raise ValueError(f"positions.{symbol} has a non-object lot")
            qty = int(round(_number(lot.get("qty", 0), name=f"positions.{symbol}.qty")))
            if qty < 0:
                raise ValueError(f"positions.{symbol}.qty must be nonnegative")
            total += qty * price
    return total


def _account_snapshot(account: Mapping[str, Any], path: Path) -> dict[str, Any]:
    day = str(account.get("day") or "")
    cash = int(round(_number(account.get("cash"), name=f"{path}.cash")))
    if cash < 0:
        raise ValueError(f"{path}.cash must be nonnegative")
    holdings = _holdings_mark_cents(account)
    receivables = _receivables_cents(account)
    equity = cash + holdings + receivables
    targets = account.get("targets")
    if not isinstance(targets, Mapping) or not targets:
        raise ValueError(f"{path} has no terminal targets")
    target_weights = {}
    for symbol, weight in targets.items():
        value = _number(weight, name=f"targets.{symbol}")
        if value < 0:
            raise ValueError(f"targets.{symbol} must be nonnegative")
        target_weights[str(symbol)] = value
    if any(value > 1.0 for value in target_weights.values()):
        raise ValueError(f"{path} has an invalid target weight")
    return {
        "path": str(Path(path).resolve()),
        "day": day,
        "cash_cents": cash,
        "holdings_mark_cents": holdings,
        "receivables_cents": receivables,
        "equity_cents": equity,
        "target_weights": target_weights,
        "target_weight_sum": sum(target_weights.values()),
        "orders": len(account.get("orders") or {}),
        "fills": len(account.get("fills") or {}),
    }


def build_virtual_sleeves(
    account_paths: Mapping[str, Path],
    *,
    sleeve_weights: Mapping[str, float] | None = None,
    initial_cash_cny: float = DEFAULT_INITIAL_CASH_CNY,
    gross_limit: float = DEFAULT_GROSS_LIMIT,
    single_limit: float = DEFAULT_SINGLE_LIMIT,
) -> dict[str, Any]:
    """Build equal-sleeve virtual attribution from saved terminal accounts."""
    if not account_paths:
        raise ValueError("At least one saved strategy account is required")
    names = tuple(account_paths)
    weights = dict(sleeve_weights or {name: 1.0 / len(names) for name in names})
    if set(weights) != set(names):
        raise ValueError("Sleeve weights must cover exactly the supplied accounts")
    if any(weight <= 0 or not math.isfinite(weight) for weight in weights.values()):
        raise ValueError("Sleeve weights must be finite and positive")
    weight_sum = sum(weights.values())
    if abs(weight_sum - 1.0) > 1e-9:
        raise ValueError(f"Sleeve weights must sum to 1, got {weight_sum}")
    if initial_cash_cny <= 0 or gross_limit <= 0 or single_limit <= 0 or single_limit > gross_limit:
        raise ValueError("Invalid virtual pool or exposure limits")

    snapshots = {name: _account_snapshot(_read(Path(path)), Path(path))
                 for name, path in account_paths.items()}
    target_weights: dict[str, float] = {}
    for name, sleeve_weight in weights.items():
        for symbol, target in snapshots[name]["target_weights"].items():
            target_weights[symbol] = target_weights.get(symbol, 0.0) + sleeve_weight * target
    merged_gross = sum(target_weights.values())
    max_single = max(target_weights.values(), default=0.0)
    constraints = {
        "gross_limit": gross_limit,
        "single_limit": single_limit,
        "merged_gross_weight": merged_gross,
        "merged_max_single_weight": max_single,
        "gross_limit_pass": merged_gross <= gross_limit + 1e-9,
        "single_limit_pass": max_single <= single_limit + 1e-9,
    }
    if not constraints["gross_limit_pass"] or not constraints["single_limit_pass"]:
        raise ValueError("Virtual target violates exposure limits")

    initial_cash_cents = int(round(initial_cash_cny * 100))
    sleeves = []
    virtual_equity = 0
    virtual_cash = 0
    virtual_holdings = 0
    virtual_receivables = 0
    for name, sleeve_weight in weights.items():
        snapshot = snapshots[name]
        budget = int(round(initial_cash_cents * sleeve_weight))
        weighted_equity = int(round(snapshot["equity_cents"] * sleeve_weight))
        weighted_cash = int(round(snapshot["cash_cents"] * sleeve_weight))
        weighted_holdings = int(round(snapshot["holdings_mark_cents"] * sleeve_weight))
        weighted_receivables = int(round(snapshot["receivables_cents"] * sleeve_weight))
        sleeves.append({
            "strategy_id": name,
            "sleeve_weight": sleeve_weight,
            "initial_budget_cents": budget,
            "terminal_day": snapshot["day"],
            "terminal_cash_cents": snapshot["cash_cents"],
            "terminal_holdings_mark_cents": snapshot["holdings_mark_cents"],
            "terminal_receivables_cents": snapshot["receivables_cents"],
            "terminal_equity_cents": snapshot["equity_cents"],
            "weighted_cash_cents": weighted_cash,
            "weighted_holdings_mark_cents": weighted_holdings,
            "weighted_receivables_cents": weighted_receivables,
            "weighted_equity_cents": weighted_equity,
            "target_weight_sum": snapshot["target_weight_sum"],
            "orders": snapshot["orders"],
            "fills": snapshot["fills"],
            "source_account": snapshot["path"],
        })
        virtual_equity += weighted_equity
        virtual_cash += weighted_cash
        virtual_holdings += weighted_holdings
        virtual_receivables += weighted_receivables

    checks = {
        "sleeve_weights_sum_to_one": abs(weight_sum - 1.0) <= 1e-9,
        "initial_cash_conserved_cents": sum(row["initial_budget_cents"] for row in sleeves) == initial_cash_cents,
        "equity_attribution_conserved": virtual_equity == virtual_cash + virtual_holdings + virtual_receivables,
        "weighted_sleeve_equity_conserved": virtual_equity == sum(row["weighted_equity_cents"] for row in sleeves),
        "target_gross_within_limit": constraints["gross_limit_pass"],
        "target_single_within_limit": constraints["single_limit_pass"],
    }
    if not all(checks.values()):
        raise AssertionError(f"Virtual sleeve conservation failed: {checks}")
    terminal_days = sorted({row["terminal_day"] for row in sleeves})
    return {
        "schema": SCHEMA,
        "status": VERIFIED,
        "strategy_count": len(sleeves),
        "strategy_ids": [row["strategy_id"] for row in sleeves],
        "terminal_days": terminal_days,
        "common_terminal_day": len(terminal_days) == 1,
        "alignment_status": "COMMON_TERMINAL_DAY" if len(terminal_days) == 1 else "MIXED_TERMINAL_DATES_RESEARCH_ONLY",
        "initial_cash_cny": initial_cash_cny,
        "initial_cash_cents": initial_cash_cents,
        "sleeves": sleeves,
        "virtual_pool": {
            "terminal_equity_cents": virtual_equity,
            "terminal_cash_cents": virtual_cash,
            "terminal_holdings_mark_cents": virtual_holdings,
            "terminal_receivables_cents": virtual_receivables,
            "terminal_equity_cny": virtual_equity / 100.0,
            "terminal_cash_cny": virtual_cash / 100.0,
            "terminal_holdings_mark_cny": virtual_holdings / 100.0,
            "target_weights": dict(sorted(target_weights.items())),
            "target_weight_sum": merged_gross,
        },
        "constraints": constraints,
        "conservation_checks": checks,
        "execution_admitted": False,
        "orders_created": False,
        "fills_created": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "research_only": True,
        "limitations": [
            "Each sleeve is a saved terminal research account; no cross-date revaluation or new replay is performed.",
            "Mixed terminal dates are explicit and make this a virtual attribution, not an executable current target.",
            "Virtual aggregation creates no orders, fills or persistent Ledger state and cannot be sent to THS.",
        ],
    }


def write_outputs(result: dict[str, Any], output_path: Path = DEFAULT_OUTPUT,
                  report_path: Path = DEFAULT_REPORT) -> tuple[Path, Path]:
    output_path = Path(output_path).resolve()
    report_path = Path(report_path).resolve()
    result = {**result, "observed_at": datetime.now(timezone.utc).isoformat(),
              "receipt_path": str(output_path), "report_path": str(report_path)}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Virtual strategy sleeves (research-only)", "",
        f"Status: `{result['status']}`; execution admitted: `false`.",
        f"Strategies: {result['strategy_count']}; terminal days: `{', '.join(result['terminal_days'])}`.",
        f"Alignment: `{result['alignment_status']}`; this is not an executable current target.", "",
        "| Strategy | Sleeve | Terminal day | Terminal equity CNY | Weighted equity CNY | Orders | Fills |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for row in result["sleeves"]:
        lines.append(f"| {row['strategy_id']} | {row['sleeve_weight']:.2%} | {row['terminal_day']} | "
                     f"{row['terminal_equity_cents']/100:.2f} | {row['weighted_equity_cents']/100:.2f} | "
                     f"{row['orders']} | {row['fills']} |")
    pool = result["virtual_pool"]
    lines += ["", "## Virtual pool", "",
              f"Terminal equity: CNY {pool['terminal_equity_cny']:.2f}; cash: CNY {pool['terminal_cash_cny']:.2f}; "
              f"marked holdings: CNY {pool['terminal_holdings_mark_cny']:.2f}.",
              f"Merged target weight: {pool['target_weight_sum']:.4%}; gross limit pass: `{result['constraints']['gross_limit_pass']}`; "
              f"single-name limit pass: `{result['constraints']['single_limit_pass']}`.", "",
              "## Boundaries", ""]
    lines.extend(f"- {item}" for item in result["limitations"])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path, report_path


def run(output_path: Path = DEFAULT_OUTPUT, report_path: Path = DEFAULT_REPORT) -> dict[str, Any]:
    result = build_virtual_sleeves(DEFAULT_ACCOUNTS)
    write_outputs(result, output_path, report_path)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    result = run(args.output, args.report)
    print(json.dumps({"status": result["status"], "strategy_count": result["strategy_count"],
                      "terminal_days": result["terminal_days"],
                      "alignment_status": result["alignment_status"],
                      "execution_admitted": result["execution_admitted"]}, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "VERIFIED", "DEFAULT_ACCOUNTS", "build_virtual_sleeves", "write_outputs", "run", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
