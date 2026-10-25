"""Compare saved research order capacity and execution cost.

This package consumes terminal ``account.json`` files already produced by the
shared Engine/Ledger replays.  It measures requested versus filled quantity,
unfilled exposure and fixed slippage stress on the filled notional.  It never
reruns a market replay, changes a saved account, merges candidate orders into
the sole paper account, or creates broker/THS payloads.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "quantification_strategy_capacity_cost_comparison_v1"
VERIFIED = "VERIFIED_LOCAL_CAPACITY_COST_COMPARISON"
DEFAULT_COST_BANDS_BPS = (0, 25, 50)
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_p05_capacity_cost_comparison_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_p05_capacity_cost_comparison_v1.md")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected account JSON object: {path}")
    return value


def _int(value: Any, *, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _money(cents: int) -> float:
    return round(cents / 100.0, 2)


def _order_metrics(account: Mapping[str, Any], *, strategy_id: str, source_path: Path,
                   cost_bands_bps: Sequence[int]) -> dict[str, Any]:
    orders = account.get("orders") or {}
    fills = account.get("fills") or {}
    if not isinstance(orders, Mapping) or not isinstance(fills, Mapping):
        raise ValueError(f"{source_path} orders/fills must be objects")
    requested_qty = filled_qty = requested_notional_cents = filled_notional_cents = 0
    fees_cents = 0
    unfilled_order_count = 0
    for order_id, order in orders.items():
        if not isinstance(order, Mapping):
            raise ValueError(f"{source_path}.orders.{order_id} must be an object")
        qty = _int(order.get("qty", 0), name=f"{order_id}.qty")
        filled = _int(order.get("filled", 0), name=f"{order_id}.filled")
        if filled > qty:
            raise ValueError(f"{order_id}.filled exceeds qty")
        limit_price = _int(order.get("limit_price", 0), name=f"{order_id}.limit_price")
        if qty and not limit_price:
            raise ValueError(f"{order_id}.limit_price is required for non-empty orders")
        requested_qty += qty
        filled_qty += filled
        requested_notional_cents += qty * limit_price
        fees_cents += _int(order.get("fees", 0), name=f"{order_id}.fees")
        if filled < qty:
            unfilled_order_count += 1
    for fill_id, fill in fills.items():
        if not isinstance(fill, Mapping):
            raise ValueError(f"{source_path}.fills.{fill_id} must be an object")
        qty = _int(fill.get("qty", 0), name=f"{fill_id}.qty")
        price = _int(fill.get("price", 0), name=f"{fill_id}.price")
        filled_notional_cents += qty * price
    if filled_qty != sum(_int(fill.get("qty", 0), name="fill.qty") for fill in fills.values()):
        raise ValueError(f"{source_path} order and fill quantities disagree")
    unfilled_qty = requested_qty - filled_qty
    unfilled_notional_cents = max(0, requested_notional_cents -
                                  sum(_int(order.get("filled", 0), name="order.filled") *
                                      _int(order.get("limit_price", 0), name="order.limit_price")
                                      for order in orders.values()))
    utilization = filled_qty / requested_qty if requested_qty else 1.0
    stress_rows = []
    for bps in cost_bands_bps:
        if isinstance(bps, bool) or int(bps) < 0:
            raise ValueError("cost bands must be non-negative integers")
        stress_cost_cents = fees_cents + int(round(filled_notional_cents * int(bps) / 10000))
        stress_rows.append({
            "slippage_bps": int(bps),
            "stressed_cost_cny": _money(stress_cost_cents),
            "stressed_cost_rate": round(stress_cost_cents / filled_notional_cents, 8)
            if filled_notional_cents else None,
        })
    return {
        "strategy_id": strategy_id,
        "source_account": str(source_path.resolve()),
        "terminal_day": str(account.get("day") or ""),
        "order_count": len(orders),
        "fill_count": len(fills),
        "requested_qty": requested_qty,
        "filled_qty": filled_qty,
        "unfilled_qty": unfilled_qty,
        "capacity_utilization": round(utilization, 8),
        "unfilled_order_count": unfilled_order_count,
        "requested_notional_cny": _money(requested_notional_cents),
        "filled_notional_cny": _money(filled_notional_cents),
        "unfilled_exposure_cny": _money(unfilled_notional_cents),
        "base_fees_cny": _money(fees_cents),
        "cost_stress": stress_rows,
    }


def build_comparison(account_paths: Mapping[str, Path], *, cost_bands_bps: Sequence[int] = DEFAULT_COST_BANDS_BPS) -> dict[str, Any]:
    if not account_paths:
        raise ValueError("At least one saved research account is required")
    if len(set(int(bps) for bps in cost_bands_bps)) != len(tuple(cost_bands_bps)):
        raise ValueError("cost bands must be unique")
    rows = [_order_metrics(_read(Path(path)), strategy_id=str(strategy_id),
                           source_path=Path(path), cost_bands_bps=cost_bands_bps)
            for strategy_id, path in account_paths.items()]
    rows.sort(key=lambda row: row["strategy_id"])
    terminal_days = sorted({row["terminal_day"] for row in rows})
    return {
        "schema": SCHEMA,
        "status": VERIFIED,
        "strategy_count": len(rows),
        "strategy_rows": rows,
        "terminal_days": terminal_days,
        "alignment_status": "COMMON_TERMINAL_DAY" if len(terminal_days) == 1 else "MIXED_TERMINAL_DATES_RESEARCH_ONLY",
        "cost_bands_bps": [int(bps) for bps in cost_bands_bps],
        "aggregation_policy": "candidate_accounts_remain_alternatives; order notional is not merged into the sole paper Ledger",
        "execution_admitted": False,
        "orders_created": False,
        "fills_created": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "research_only": True,
        "limitations": [
            "Capacity utilization is measured from saved order/fill records, not current broker liquidity.",
            "Slippage bands are fixed diagnostic assumptions and are not selected after observing returns.",
            "Mixed terminal dates remain research-only and cannot become a current executable target.",
        ],
    }


def run(account_paths: Mapping[str, Path], *, cost_bands_bps: Sequence[int] = DEFAULT_COST_BANDS_BPS,
        output_path: Path = DEFAULT_OUTPUT, report_path: Path = DEFAULT_REPORT) -> dict[str, Any]:
    result = build_comparison(account_paths, cost_bands_bps=cost_bands_bps)
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    result["source_hashes"] = {str(strategy_id): _sha256(Path(path)) for strategy_id, path in account_paths.items()}
    result["source_paths"] = {str(strategy_id): str(Path(path).resolve()) for strategy_id, path in account_paths.items()}
    output_path = Path(output_path).resolve(); output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path = Path(report_path).resolve(); report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Strategy Capacity Cost Comparison", "", f"Strategies: {result['strategy_count']}; terminal days: {', '.join(result['terminal_days'])}; execution_admitted=false.", "", "|Strategy|Orders|Fills|Capacity utilization|Unfilled qty|Unfilled exposure CNY|Base fees CNY|", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in result["strategy_rows"]:
        lines.append(f"|{row['strategy_id']}|{row['order_count']}|{row['fill_count']}|{row['capacity_utilization']:.4%}|{row['unfilled_qty']}|{row['unfilled_exposure_cny']:.2f}|{row['base_fees_cny']:.2f}|")
    lines.extend(["", "Fixed slippage bands: " + ", ".join(f"{bps} bps" for bps in result["cost_bands_bps"]), "", "Saved research accounts are alternatives; their orders are not merged into the paper Ledger."])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result["report_path"] = str(report_path)
    result["receipt_path"] = str(output_path)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    from .virtual_strategy_sleeves import DEFAULT_ACCOUNTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--slippage-bps", nargs="+", type=int, default=list(DEFAULT_COST_BANDS_BPS))
    args = parser.parse_args(argv)
    result = run(DEFAULT_ACCOUNTS, cost_bands_bps=args.slippage_bps,
                 output_path=args.output, report_path=args.report)
    print(json.dumps({"status": result["status"], "strategy_count": result["strategy_count"],
                      "terminal_days": result["terminal_days"],
                      "execution_admitted": result["execution_admitted"]}, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "VERIFIED", "build_comparison", "run", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
