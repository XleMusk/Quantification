"""Build a read-only paper target exposure and blocked-action stress view.

The view consumes the already saved paper plan, its target reconciliation and
the native-vs-raw close reference.  It does not fetch prices, create orders or
mutate the account.  Scenarios deliberately expose the residual position when
T+1 selling is unavailable or when available cash is reduced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "quantification_paper_exposure_stress_v1"
VERIFIED = "VERIFIED_LOCAL_PAPER_EXPOSURE_STRESS"
DEFAULT_PLAN = Path("outputs/paper_plans/20261024_lowvol_current_paper_plan_receipt.json")
DEFAULT_RECON = Path("outputs/paper_plans/20261024_lowvol_current_paper_reconciliation/receipt.json")
DEFAULT_NATIVE = Path("outputs/paper_plans/20261024_lowvol_current_native_stockdb_reference_v2/receipt.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_p03_paper_exposure_stress_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_p03_paper_exposure_stress_v1.md")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _num(value: Any, *, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise ValueError(f"{name} must be finite")
    return result


def _int(value: Any, *, name: str) -> int:
    result = int(value)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _validate_inputs(
    plan: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    native_reference: Mapping[str, Any],
) -> tuple[str, list[Mapping[str, Any]], float, float]:
    if plan.get("execution_admitted") is not False or reconciliation.get("execution_admitted") is not False:
        raise ValueError("paper inputs must remain execution_admitted=false")
    if reconciliation.get("status") != "PAPER_CLOSE_REFERENCE_READY":
        raise ValueError("paper reconciliation is not a close reference")
    if native_reference.get("execution_admitted") is not False:
        raise ValueError("native close reference must remain research-only")
    day = str(reconciliation.get("day") or "")
    if not day or day != str(native_reference.get("reference_date") or day):
        raise ValueError("paper and native close reference days must match")
    comparison = native_reference.get("comparison") or {}
    if comparison.get("close_mismatches") != 0:
        raise ValueError("native close reference contains mismatches")
    rows = list(reconciliation.get("rows") or [])
    if not rows or reconciliation.get("target_count") != len(rows):
        raise ValueError("complete paper target rows are required")
    cash = _num((plan.get("account") or {}).get("cash_cny"), name="account.cash_cny")
    equity = _num((plan.get("account") or {}).get("equity_cny"), name="account.equity_cny")
    if cash < 0 or equity <= 0:
        raise ValueError("paper account cash/equity are invalid")
    symbols = {str(row.get("symbol")) for row in rows}
    native_symbols = {str(row.get("symbol")) for row in native_reference.get("rows") or []}
    if symbols != native_symbols:
        raise ValueError("native close reference symbols do not match paper target symbols")
    return day, rows, cash, equity


def _base_row(row: Mapping[str, Any]) -> dict[str, Any]:
    symbol = str(row.get("symbol"))
    price = _num(row.get("paper_close_rmb"), name=f"{symbol}.paper_close_rmb")
    current_qty = _int(row.get("current_qty"), name=f"{symbol}.current_qty")
    target_qty = _int(row.get("estimated_target_qty"), name=f"{symbol}.estimated_target_qty")
    sellable_qty = _int(row.get("sellable_qty"), name=f"{symbol}.sellable_qty")
    delta_qty = target_qty - current_qty
    buy_fee = _num(row.get("estimated_buy_fee_cny") or 0, name=f"{symbol}.estimated_buy_fee_cny")
    return {
        "symbol": symbol,
        "price_cny": price,
        "current_qty": current_qty,
        "target_qty": target_qty,
        "sellable_qty": sellable_qty,
        "delta_qty": delta_qty,
        "target_weight": _num(row.get("target_weight") or 0, name=f"{symbol}.target_weight"),
        "buy_fee_cny": buy_fee,
    }


def _scenario_rows(
    base: Sequence[Mapping[str, Any]],
    *,
    scenario_id: str,
    available_cash_cny: float,
    force_no_sell: bool = False,
    lot_size: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    remaining_cash = float(available_cash_cny)
    rows: list[dict[str, Any]] = []
    blocked_orders = 0
    blocked_qty = 0
    residual_exposure = 0.0
    projected_holdings = 0.0
    action_counts = {"BUY_REFERENCE": 0, "SELL_REFERENCE": 0, "HOLD": 0, "BLOCKED": 0, "PARTIAL": 0}
    for item in base:
        row = dict(item)
        delta = int(row["delta_qty"])
        sell_qty = max(0, -delta)
        buy_qty = max(0, delta)
        executable_sell = 0 if force_no_sell else min(sell_qty, int(row["sellable_qty"]))
        sell_blocked = sell_qty - executable_sell
        if sell_blocked:
            blocked_orders += 1
            blocked_qty += sell_blocked
        buy_cost = buy_qty * float(row["price_cny"]) + float(row["buy_fee_cny"])
        executable_buy = buy_qty
        buy_status = "BUY_REFERENCE" if buy_qty else ("SELL_REFERENCE" if sell_qty else "HOLD")
        if buy_qty and remaining_cash + 1e-9 < buy_cost:
            affordable_lots = max(0, int(max(0.0, remaining_cash - float(row["buy_fee_cny"])) // (float(row["price_cny"]) * lot_size)))
            executable_buy = min(buy_qty, affordable_lots * lot_size)
            if executable_buy < buy_qty:
                blocked_orders += 1
                blocked_qty += buy_qty - executable_buy
                buy_status = "PARTIAL_CASH" if executable_buy else "BLOCKED_CASH"
                action_counts["PARTIAL" if executable_buy else "BLOCKED"] += 1
        if executable_buy:
            remaining_cash -= executable_buy * float(row["price_cny"]) + float(row["buy_fee_cny"])
        if sell_blocked:
            action_counts["BLOCKED"] += 1
        elif sell_qty:
            action_counts["SELL_REFERENCE"] += 1
        elif buy_status == "BUY_REFERENCE":
            action_counts["BUY_REFERENCE"] += 1
        elif not buy_qty:
            action_counts["HOLD"] += 1
        projected_qty = int(row["current_qty"]) - executable_sell + executable_buy
        residual_qty = int(row["target_qty"]) - projected_qty
        residual_value = abs(residual_qty) * float(row["price_cny"])
        projected_holdings += projected_qty * float(row["price_cny"])
        residual_exposure += residual_value
        row.update({
            "scenario_id": scenario_id,
            "action_status": "BLOCKED_T1" if sell_blocked else buy_status,
            "executable_sell_qty": executable_sell,
            "executable_buy_qty": executable_buy,
            "blocked_qty": sell_blocked + (buy_qty - executable_buy),
            "projected_qty": projected_qty,
            "residual_qty": residual_qty,
            "residual_exposure_cny": round(residual_value, 2),
        })
        rows.append(row)
    summary = {
        "scenario_id": scenario_id,
        "available_cash_cny": round(available_cash_cny, 2),
        "remaining_cash_cny": round(remaining_cash, 2),
        "blocked_order_count": blocked_orders,
        "blocked_qty": blocked_qty,
        "residual_exposure_cny": round(residual_exposure, 2),
        "projected_holdings_cny": round(projected_holdings, 2),
        "action_counts": action_counts,
        "cash_conservation_check": remaining_cash >= -1e-7,
    }
    return rows, summary


def build_stress_view(
    plan: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    native_reference: Mapping[str, Any],
    *,
    cash_haircut: float = 0.5,
) -> dict[str, Any]:
    if not 0 <= cash_haircut < 1:
        raise ValueError("cash_haircut must be in [0, 1)")
    day, source_rows, cash, equity = _validate_inputs(plan, reconciliation, native_reference)
    base = [_base_row(row) for row in source_rows]
    baseline_rows, baseline = _scenario_rows(base, scenario_id="baseline_reference", available_cash_cny=cash)
    t1_rows, t1 = _scenario_rows(base, scenario_id="t1_sell_blocked", available_cash_cny=cash, force_no_sell=True)
    cash_rows, cash_stress = _scenario_rows(
        base, scenario_id=f"cash_haircut_{int(cash_haircut * 100)}pct",
        available_cash_cny=cash * (1 - cash_haircut),
    )
    scenarios = [baseline | {"rows": baseline_rows}, t1 | {"rows": t1_rows}, cash_stress | {"rows": cash_rows}]
    return {
        "schema": SCHEMA,
        "status": VERIFIED,
        "source_day": day,
        "target_count": len(base),
        "account_cash_cny": round(cash, 2),
        "account_equity_cny": round(equity, 2),
        "cash_haircut": cash_haircut,
        "scenarios": scenarios,
        "execution_admitted": False,
        "orders_created": False,
        "fills_created": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "research_only": True,
        "limitations": [
            "Scenarios are deterministic paper stress projections over the saved 2026-10-23 close reference.",
            "T+1 and cash stress expose residual target exposure; they do not claim current broker state or quote availability.",
            "No order, fill, Ledger, broker or THS operation is attempted.",
        ],
    }


def run(
    plan_path: Path = DEFAULT_PLAN,
    reconciliation_path: Path = DEFAULT_RECON,
    native_path: Path = DEFAULT_NATIVE,
    output_path: Path = DEFAULT_OUTPUT,
    report_path: Path = DEFAULT_REPORT,
) -> dict[str, Any]:
    paths = {
        "paper_plan": plan_path.resolve(),
        "reconciliation": reconciliation_path.resolve(),
        "native_reference": native_path.resolve(),
    }
    result = build_stress_view(_read_json(paths["paper_plan"]), _read_json(paths["reconciliation"]), _read_json(paths["native_reference"]))
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    result["source_paths"] = {name: str(path) for name, path in paths.items()}
    result["source_hashes"] = {name: _sha256(path) for name, path in paths.items()}
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Paper Exposure Stress View",
        "",
        f"Reference day: {result['source_day']}; target rows: {result['target_count']}; execution_admitted=false.",
        "",
        "|Scenario|Blocked orders|Blocked qty|Residual exposure CNY|Projected holdings CNY|Remaining cash CNY|",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for scenario in result["scenarios"]:
        lines.append(
            f"|{scenario['scenario_id']}|{scenario['blocked_order_count']}|{scenario['blocked_qty']}|"
            f"{scenario['residual_exposure_cny']:.2f}|{scenario['projected_holdings_cny']:.2f}|{scenario['remaining_cash_cny']:.2f}|"
        )
    lines.extend(["", "This is a research-only projection over saved paper artifacts. No order, fill, Ledger, broker or THS operation was performed."])
    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result["report_path"] = str(report_path)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--reconciliation", type=Path, default=DEFAULT_RECON)
    parser.add_argument("--native-reference", type=Path, default=DEFAULT_NATIVE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    result = run(args.paper_plan, args.reconciliation, args.native_reference, args.output, args.report)
    print(json.dumps({
        "status": result["status"],
        "source_day": result["source_day"],
        "target_count": result["target_count"],
        "scenarios": [{key: row[key] for key in ("scenario_id", "blocked_order_count", "blocked_qty", "residual_exposure_cny")} for row in result["scenarios"]],
        "execution_admitted": result["execution_admitted"],
    }, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "VERIFIED", "build_stress_view", "run", "main"]

