"""Bind the retained half-momentum and Ridge sleeves into one paper package.

The common-window receipt compares the sleeves independently.  This module
adds the next portfolio decision slice: reuse the saved terminal joint target,
run it through the shared paper target/order/fee path, and report what the
union adds under the same capacity policy.  A one-day target projection is
never treated as a return comparison with the multi-session sleeve accounts.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

from ..settings import ROOT
from .stockdb_paper_target_package import build_paper_target_package


SCHEMA = "quantification_retained_joint_candidate_comparison_v1"
COMMON_RECEIPT = ROOT / "outputs/pipeline_receipts/20261026_common_window_strategy_comparison_v1.json"
SOURCE_JOINT_RECEIPT = ROOT / "outputs/pipeline_receipts/20261025_continue_candidates_joint_paper_v3/joint_paper_receipt.json"
OUTPUT_DIR = ROOT / "outputs/pipeline_receipts/20261026_retained_joint_candidate_comparison_v1"
RECEIPT_NAME = "retained_joint_comparison_receipt.json"
REPORT_NAME = "retained_joint_comparison_report.md"
RETAINED_IDS = ("low_vol_half_momentum_20", "mature_factor_balanced_ridge")
JOINT_IDS = ("trend_low_vol_half_momentum_20", "stockdb_factor_monthly_ridge_v1")


def _read(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _retained_entries(common: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = {str(row.get("id")): row for row in common.get("strategies", [])}
    missing = [strategy_id for strategy_id in RETAINED_IDS if strategy_id not in entries]
    if missing:
        raise ValueError(f"Retained candidate missing from common-window receipt: {missing}")
    selected = [entries[strategy_id] for strategy_id in RETAINED_IDS]
    if any(row.get("research_decision") != "CONTINUE_RESEARCH_ONLY" for row in selected):
        raise ValueError("A retained candidate is not marked CONTINUE_RESEARCH_ONLY")
    return selected


def _paper_projection(joint: Mapping[str, Any], output_dir: Path) -> dict[str, Any]:
    target = joint.get("target_payload")
    quote = joint.get("quote_payload")
    if not isinstance(target, Mapping) or not isinstance(quote, Mapping):
        raise ValueError("source joint receipt has no target/quote payload")
    result = build_paper_target_package(
        target,
        quote,
        140_000.0,
        execution_day="2026-10-14",
        settlement_day="2026-10-15",
        output_dir=output_dir / "paper_package",
    )
    return result["result"]


def build_comparison(
    common: Mapping[str, Any],
    joint: Mapping[str, Any],
    paper_result: Mapping[str, Any],
) -> dict[str, Any]:
    entries = _retained_entries(common)
    joint_meta = joint.get("joint") or {}
    if tuple(joint_meta.get("strategy_ids") or ()) != JOINT_IDS:
        raise ValueError("source joint receipt does not contain the retained half-momentum/Ridge pair")
    if joint_meta.get("common_terminal_day") != "2026-10-13":
        raise ValueError("retained joint target is not the pinned 2026-10-13 terminal target")
    snapshot = str(common.get("stockdb_snapshot") or "")
    if not snapshot:
        raise ValueError("common-window receipt has no shared StockDB snapshot")
    target_rows = list(joint_meta.get("target_rows") or [])
    contributors = {
        strategy_id: sum(strategy_id in row.get("contributors", []) for row in target_rows)
        for strategy_id in JOINT_IDS
    }
    target_ids = {str(row.get("symbol")) for row in target_rows}
    retained_metrics = {
        str(row["id"]): {
            "net_return": float(row["metrics"]["net_return"]),
            "max_drawdown": float(row["metrics"]["max_drawdown"]),
            "sharpe_zero_rf_252": row["metrics"].get("sharpe_zero_rf_252"),
            "orders": int(row["metrics"]["orders"]),
            "fills": int(row["metrics"]["fills"]),
            "fees_rmb": float(row["metrics"]["fees_rmb"]),
        }
        for row in entries
    }
    sleeve_returns = [row["net_return"] for row in retained_metrics.values()]
    route_available = int(paper_result.get("route_available_count", 0))
    paper_orders = int(paper_result.get("paper_order_count", 0))
    return {
        "schema": SCHEMA,
        "status": "COMPLETED_RESEARCH_ONLY_RETAINED_JOINT_COMPARISON",
        "created_at": datetime.now().astimezone().isoformat(),
        "execution_admitted": False,
        "broker_or_ths_interacted": False,
        "persistent_ledger_mutated": False,
        "shared_stockdb_snapshot": snapshot,
        "common_window": common["common_window"],
        "retained_sleeves": retained_metrics,
        "sleeve_return_spread": max(sleeve_returns) - min(sleeve_returns),
        "sleeve_average_net_return": mean(sleeve_returns),
        "joint_target": {
            "source_receipt": str(SOURCE_JOINT_RECEIPT),
            "decision_day": joint_meta["common_terminal_day"],
            "strategy_ids": list(JOINT_IDS),
            "target_count": len(target_ids),
            "contributor_target_counts": contributors,
            "capacity_dropped_count": len(joint_meta.get("dropped_rows") or []),
            "actual_gross_weight": joint_meta.get("constraints", {}).get("actual_gross_weight"),
            "paper_order_count": paper_orders,
            "route_available_count": route_available,
            "route_blocked_count": int(paper_result.get("route_blocked_count", 0)),
            "modeled_fees_rmb": float(paper_result.get("modeled_fees_rmb", 0.0)),
            "projected_final_equity_rmb": float(paper_result.get("projected_final_equity_rmb", 0.0)),
        },
        "incremental_decision": {
            "decision": "CONTINUE_RESEARCH_ONLY",
            "union_target_evidence": "available",
            "return_incremental_status": "NOT_COMPARABLE_PROJECTION_ONLY",
            "reason": "The sleeves are 648-session account results while the joint artifact is a terminal target projection; no return uplift is inferred.",
            "promotion_allowed": False,
        },
        "limitations": [
            "The joint target reuses the saved 2026-10-13 terminal payload and shared paper target/order/fee path.",
            "The joint projection is not a forward account and cannot be ranked by return against the common-window sleeves.",
            "PIT completeness, fresh forward validation, broker/THS observation and adoption remain unproven.",
        ],
    }


def render_report(receipt: Mapping[str, Any]) -> str:
    joint = receipt["joint_target"]
    lines = [
        "# Retained candidate joint comparison",
        "",
        f"Common window: `{receipt['common_window']['start']}..{receipt['common_window']['end']}` ({receipt['common_window']['sessions']} sessions).",
        f"Joint terminal target day: `{joint['decision_day']}`; same StockDB snapshot: `{receipt['shared_stockdb_snapshot']}`.",
        "",
        "| Sleeve | Net return | Max drawdown | Sharpe | Orders | Fills | Fees CNY |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy_id, metrics in receipt["retained_sleeves"].items():
        sharpe = metrics["sharpe_zero_rf_252"]
        lines.append(
            f"| `{strategy_id}` | {metrics['net_return']:.4%} | {metrics['max_drawdown']:.4%} | "
            f"{('null' if sharpe is None else f'{sharpe:.4f}')} | {metrics['orders']} | {metrics['fills']} | {metrics['fees_rmb']:.2f} |"
        )
    lines += [
        "",
        f"Joint target: {joint['target_count']} rows, {joint['paper_order_count']} paper orders, {joint['route_available_count']} routes available, modeled fees CNY {joint['modeled_fees_rmb']:.2f}.",
        f"Contributor rows: {joint['contributor_target_counts']}; capacity dropped: {joint['capacity_dropped_count']}.",
        "",
        "The sleeve return spread is a common-window diagnostic. A joint return increment is deliberately not calculated because the joint artifact is a terminal target projection, not a forward account.",
        "",
        "`execution_admitted=false`; no broker or Tonghuashun interaction occurred.",
        "",
    ]
    return "\n".join(lines)


def write_outputs(
    common_path: str | Path = COMMON_RECEIPT,
    joint_path: str | Path = SOURCE_JOINT_RECEIPT,
    output_dir: str | Path = OUTPUT_DIR,
) -> tuple[Path, Path]:
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    common = _read(common_path)
    joint = _read(joint_path)
    paper_result = _paper_projection(joint, output_root)
    receipt = build_comparison(common, joint, paper_result)
    receipt_path = output_root / RECEIPT_NAME
    report_path = output_root / REPORT_NAME
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(receipt), encoding="utf-8")
    return receipt_path, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common-receipt", type=Path, default=COMMON_RECEIPT)
    parser.add_argument("--joint-receipt", type=Path, default=SOURCE_JOINT_RECEIPT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    receipt_path, report_path = write_outputs(args.common_receipt, args.joint_receipt, args.output_dir)
    print(json.dumps({"receipt": str(receipt_path), "report": str(report_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_comparison", "main", "render_report", "write_outputs"]
