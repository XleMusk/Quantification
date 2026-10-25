"""Build a fixed research-only robustness and stop/continue matrix.

This package joins already saved long-window, untouched-forward and capacity/cost
receipts.  It does not rerun market data and it does not create an order or
mutate an account.  A candidate can continue only when both the long window and
the untouched forward window are positive; missing or incomplete evidence stays
blocked.  The rule is deliberately predeclared so the table cannot be tuned to
the observed returns.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_strategy_robustness_matrix_v1"
VERIFIED = "VERIFIED_RESEARCH_ROBUSTNESS_MATRIX"
DEFAULT_LONG = Path("outputs/pipeline_receipts/20261024_long_window_strategy_comparison_v1.json")
DEFAULT_FORWARD = Path("outputs/pipeline_receipts/20261024_common_untouched_forward_comparison_v2.json")
DEFAULT_CAPACITY = Path("outputs/pipeline_receipts/20261024_p05_capacity_cost_comparison_v1.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_strategy_robustness_matrix_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_strategy_robustness_matrix_v1.md")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _long_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") or {}
    return {
        "sessions": int(metrics.get("sessions", 0) or 0),
        "orders": int(metrics.get("orders", 0) or 0),
        "fills": int(metrics.get("fills", 0) or 0),
        "net_return": float(metrics["net_return"]) if metrics.get("net_return") is not None else None,
        "max_drawdown": float(metrics["max_drawdown"]) if metrics.get("max_drawdown") is not None else None,
        "sharpe": float(metrics["sharpe"]) if metrics.get("sharpe") is not None else None,
        "blocked": int(metrics.get("blocked", 0) or 0),
    }


def _forward_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sessions": int(row.get("sessions", 0) or 0),
        "net_return": float(row["net_return"]) if row.get("net_return") is not None else None,
        "max_drawdown": float(row["max_drawdown"]) if row.get("max_drawdown") is not None else None,
        "sharpe": float(row["sharpe_zero_rf_252"]) if row.get("sharpe_zero_rf_252") is not None else None,
        "holdout_untouched": bool(row.get("holdout_untouched")),
        "future_scores_used": bool(row.get("future_scores_used")),
        "orders_generated": bool(row.get("orders_generated")),
        "fills_generated": bool(row.get("fills_generated")),
        "pit_verified": bool(row.get("pit_verified")),
        "company_actions_applied": bool(row.get("company_actions_applied")),
    }


def _capacity_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
    costs = {int(x["slippage_bps"]): {
        "cost_cny": float(x["stressed_cost_cny"]),
        "cost_rate": float(x["stressed_cost_rate"]),
    } for x in row.get("cost_stress", [])}
    return {
        "capacity_utilization": float(row["capacity_utilization"]),
        "unfilled_qty": int(row.get("unfilled_qty", 0) or 0),
        "unfilled_exposure_cny": float(row.get("unfilled_exposure_cny", 0.0) or 0.0),
        "unfilled_order_count": int(row.get("unfilled_order_count", 0) or 0),
        "cost_stress": costs,
    }


def classify_evidence(long: Mapping[str, Any] | None,
                      forward: Mapping[str, Any] | None,
                      capacity: Mapping[str, Any] | None) -> str:
    """Apply the fixed robustness rule to one candidate."""
    if long is None or forward is None:
        return "BLOCKED_COVERAGE"
    if not forward.get("holdout_untouched") or forward.get("future_scores_used"):
        return "BLOCKED_DATA"
    if long.get("sessions", 0) <= 0 or forward.get("sessions", 0) <= 0:
        return "BLOCKED_DATA"
    long_return, forward_return = long.get("net_return"), forward.get("net_return")
    if long_return is None or forward_return is None:
        return "BLOCKED_DATA"
    if long_return > 0 and forward_return > 0:
        return "CONTINUE_RESEARCH_ONLY"
    if long_return <= 0 and forward_return <= 0:
        return "STOP_RESEARCH_ONLY"
    return "MIXED_EVIDENCE_RESEARCH_ONLY"


def build_matrix(long_source: Mapping[str, Any], forward_source: Mapping[str, Any],
                 capacity_source: Mapping[str, Any]) -> dict[str, Any]:
    long_rows = {str(row["id"]): row for row in long_source.get("strategies", [])}
    forward_rows = {str(row["id"]): row for row in forward_source.get("strategies", [])}
    capacity_rows = {str(row["strategy_id"]): row for row in capacity_source.get("strategy_rows", [])}
    ids = list(long_rows)
    rows = []
    for strategy_id in ids:
        long = _long_metrics(long_rows[strategy_id])
        forward_id = strategy_id if strategy_id != "low_vol_half_momentum_20" else "trend_low_vol_half_momentum_20"
        forward = _forward_metrics(forward_rows[forward_id]) if forward_id in forward_rows else None
        capacity = _capacity_metrics(capacity_rows[strategy_id]) if strategy_id in capacity_rows else None
        decision = classify_evidence(long, forward, capacity)
        rows.append({
            "strategy_id": strategy_id,
            "forward_strategy_id": forward_id,
            "long_window": long,
            "untouched_forward": forward,
            "capacity_cost": capacity,
            "decision": decision,
            "promotion_allowed": False,
        })
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
    return {
        "schema": SCHEMA,
        "status": VERIFIED,
        "source_receipts": {
            "long_window": str(DEFAULT_LONG.resolve()),
            "untouched_forward": str(DEFAULT_FORWARD.resolve()),
            "capacity_cost": str(DEFAULT_CAPACITY.resolve()),
        },
        "policy": {
            "missing_long_or_forward": "BLOCKED_COVERAGE",
            "non_untouched_or_future_scores": "BLOCKED_DATA",
            "both_returns_positive": "CONTINUE_RESEARCH_ONLY",
            "both_returns_nonpositive": "STOP_RESEARCH_ONLY",
            "one_positive_one_nonpositive": "MIXED_EVIDENCE_RESEARCH_ONLY",
            "promotion_allowed": False,
        },
        "comparison_windows": {
            "long": {"strategy_count": len(long_rows), "source_status": long_source.get("status")},
            "untouched_forward": forward_source.get("comparison_window"),
            "capacity_cost": {"strategy_count": capacity_source.get("strategy_count"),
                              "cost_bands_bps": capacity_source.get("cost_bands_bps")},
        },
        "strategies": rows,
        "decision_counts": counts,
        "execution_admitted": False,
        "orders_created": False,
        "fills_created": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "research_only": True,
        "limitations": [
            "The untouched forward window is only eight sessions and is a diagnostic holdout, not validation.",
            "Long windows have different native session counts; no cross-window return normalization is inferred.",
            "Capacity and cost are copied from saved order/fill records; no current broker liquidity is measured.",
            "PIT membership, publication timing and corporate actions remain unverified, so promotion is closed.",
        ],
    }


def build_report(result: Mapping[str, Any]) -> str:
    lines = ["# Strategy robustness matrix", "",
             f"Status: `{result['status']}`; promotion: `false`; execution: `false`.", "",
             "The fixed rule joins saved long-window, untouched-forward and capacity/cost receipts. It is a research stop/continue decision, not a paper or live selection.", "",
             "| Strategy | Long return | Forward return | Utilization | Unfilled exposure | Decision |", "|---|---:|---:|---:|---:|---|"]
    for row in result["strategies"]:
        long_return = row["long_window"]["net_return"]
        forward = row["untouched_forward"]
        forward_return = None if forward is None else forward["net_return"]
        capacity = row["capacity_cost"] or {}
        lines.append(f"| {row['strategy_id']} | {long_return:.2%} | {'missing' if forward_return is None else f'{forward_return:.2%}'} | {capacity.get('capacity_utilization', 0):.2%} | {capacity.get('unfilled_exposure_cny', 0):.2f} | {row['decision']} |")
    lines += ["", "All candidates remain research-only. No target portfolio, orders, fills, broker or THS action was created.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--long-input", type=Path, default=DEFAULT_LONG)
    parser.add_argument("--forward-input", type=Path, default=DEFAULT_FORWARD)
    parser.add_argument("--capacity-input", type=Path, default=DEFAULT_CAPACITY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    result = build_matrix(_read(args.long_input.resolve()), _read(args.forward_input.resolve()), _read(args.capacity_input.resolve()))
    result["source_receipts"] = {"long_window": str(args.long_input.resolve()), "untouched_forward": str(args.forward_input.resolve()), "capacity_cost": str(args.capacity_input.resolve())}
    output, report = args.output.resolve(), args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(build_report(result), encoding="utf-8")
    print(json.dumps({"status": result["status"], "decision_counts": result["decision_counts"], "promotion_allowed": False, "execution_admitted": False}, ensure_ascii=False))
    return 0


__all__ = ["build_matrix", "build_report", "classify_evidence", "main", "VERIFIED"]


if __name__ == "__main__":
    raise SystemExit(main())
