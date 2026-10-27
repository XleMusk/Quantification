"""Make a declared research-only decision from the full strategy panel.

This module joins the saved same-window comparison and its cost-pressure
receipt.  It does not rerun a strategy, change a Ledger, or permit paper/live
promotion.  A strategy can only continue to the next research experiment
when the base and the highest configured cost-pressure slice both have
positive return and positive Sharpe.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_full_panel_strategy_decision_v1"
PANEL_SCHEMA = "quantification_full_panel_common_window_strategy_comparison_v1"
COST_SCHEMA = "quantification_common_window_cost_pressure_v1"
DEFAULT_PANEL = Path("outputs/pipeline_receipts/20261027_full_panel_common_window_strategy_comparison_v1.json")
DEFAULT_COST = Path("outputs/pipeline_receipts/20261027_full_panel_common_window_cost_pressure_v1.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261027_full_panel_strategy_decision_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261027_full_panel_strategy_decision_v1.md")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"receipt must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _require_research_only(receipt: Mapping[str, Any], path: Path) -> None:
    for key in ("execution_admitted", "strategy_validated", "ledger_mutated", "paper_allowed", "promotion_allowed"):
        if receipt.get(key) is True:
            raise ValueError(f"input crossed research-only boundary: {path} ({key})")


def _pressure_index(receipt: Mapping[str, Any], path: Path) -> dict[str, dict[str, Any]]:
    rows = receipt.get("strategies")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"cost-pressure strategies are missing: {path}")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            raise ValueError(f"cost-pressure row is malformed: {path}")
        pressure = row.get("pressure")
        if not isinstance(pressure, list) or not pressure:
            raise ValueError(f"cost-pressure levels are missing: {path}")
        levels = [level for level in pressure if isinstance(level, Mapping)]
        if not levels:
            raise ValueError(f"cost-pressure levels are malformed: {path}")
        result[row["id"]] = {"base_net_return": row.get("base_net_return"), "pressure": levels}
    return result


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


def build_decision(panel_path: Path = DEFAULT_PANEL, cost_path: Path = DEFAULT_COST) -> dict[str, Any]:
    panel = _read(panel_path)
    cost = _read(cost_path)
    if panel.get("schema") != PANEL_SCHEMA:
        raise ValueError(f"full-panel schema mismatch: {panel_path}")
    if cost.get("schema") != COST_SCHEMA:
        raise ValueError(f"cost-pressure schema mismatch: {cost_path}")
    _require_research_only(panel, panel_path)
    _require_research_only(cost, cost_path)
    if panel.get("common_window") != cost.get("common_window"):
        raise ValueError("panel and cost-pressure windows differ")
    if panel.get("stockdb_snapshot") != cost.get("stockdb_snapshot"):
        raise ValueError("panel and cost-pressure snapshots differ")
    strategies = panel.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("full-panel strategies are missing")
    pressure = _pressure_index(cost, cost_path)
    decisions: list[dict[str, Any]] = []
    for strategy in strategies:
        if not isinstance(strategy, Mapping) or not isinstance(strategy.get("id"), str):
            raise ValueError("full-panel strategy row is malformed")
        strategy_id = strategy["id"]
        if strategy_id not in pressure:
            raise ValueError(f"strategy missing from cost-pressure receipt: {strategy_id}")
        metrics = strategy.get("metrics")
        if not isinstance(metrics, Mapping):
            raise ValueError(f"strategy metrics are missing: {strategy_id}")
        levels = sorted(pressure[strategy_id]["pressure"], key=lambda row: float(row.get("bps", 0.0)))
        worst = levels[-1]
        base_pass = _positive(metrics.get("net_return")) and _positive(metrics.get("sharpe_zero_rf_252"))
        pressure_pass = _positive(worst.get("net_return")) and _positive(worst.get("sharpe_zero_rf_252"))
        if base_pass and pressure_pass:
            action = "CONTINUE_RESEARCH_ONLY"
            reason = "base common-window and highest configured cost-pressure slice both pass the predeclared positive-return/Sharpe rule"
        elif not base_pass:
            action = "STOP_RESEARCH_ONLY"
            reason = "base common-window net_return or Sharpe is non-positive"
        else:
            action = "STOP_RESEARCH_ONLY"
            reason = "highest configured cost-pressure net_return or Sharpe is non-positive"
        decisions.append({
            "strategy_id": strategy_id,
            "label": strategy.get("label"),
            "base_metrics": dict(metrics),
            "highest_cost_pressure": dict(worst),
            "base_rule_passed": base_pass,
            "cost_pressure_rule_passed": pressure_pass,
            "research_action": action,
            "decision_reason": reason,
            "strategy_validated": False,
            "paper_allowed": False,
            "promotion_allowed": False,
        })
    return {
        "schema": SCHEMA,
        "status": "COMPLETED_RESEARCH_ONLY_FULL_PANEL_DECISION",
        "execution_admitted": False,
        "strategy_validated": False,
        "ledger_mutated": False,
        "paper_allowed": False,
        "promotion_allowed": False,
        "common_window": panel["common_window"],
        "stockdb_snapshot": panel["stockdb_snapshot"],
        "decision_rule": "continue only when base and highest configured cost-pressure net_return > 0 and Sharpe > 0",
        "inputs": {
            "panel": {"path": str(panel_path.resolve()), "sha256": _sha256(panel_path)},
            "cost_pressure": {"path": str(cost_path.resolve()), "sha256": _sha256(cost_path)},
        },
        "strategy_count": len(decisions),
        "decisions": decisions,
        "summary": {
            "continue_research_only": sum(row["research_action"] == "CONTINUE_RESEARCH_ONLY" for row in decisions),
            "stop_research_only": sum(row["research_action"] == "STOP_RESEARCH_ONLY" for row in decisions),
            "strategy_validated": 0,
        },
        "limitations": [
            "This decision reads saved same-window artifacts and does not rerun a strategy.",
            "It is not forward validation, PIT validation, paper adoption or live execution permission.",
        ],
    }


def render_markdown(receipt: Mapping[str, Any]) -> str:
    lines = [
        "# Full-panel strategy decision (research only)",
        "",
        f"Common window: `{receipt['common_window']['start']}..{receipt['common_window']['end']}` ({receipt['common_window']['sessions']} sessions).",
        "",
        f"Rule: `{receipt['decision_rule']}`.",
        "",
        "| Strategy | Base return | Base Sharpe | Highest cost bps | Cost return | Cost Sharpe | Decision |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in receipt["decisions"]:
        base = row["base_metrics"]
        pressure = row["highest_cost_pressure"]
        lines.append(
            f"| {row['label']} (`{row['strategy_id']}`) | {float(base.get('net_return', 0.0)):.4%} | {float(base.get('sharpe_zero_rf_252', 0.0)):.4f} | "
            f"{float(pressure.get('bps', 0.0)):.0f} | {float(pressure.get('net_return', 0.0)):.4%} | {float(pressure.get('sharpe_zero_rf_252', 0.0)):.4f} | {row['research_action']} |"
        )
    lines.extend(["", "All decisions retain `paper_allowed=false`, `promotion_allowed=false`, and `execution_admitted=false`.", ""])
    return "\n".join(lines)


def write_outputs(*, panel_path: Path = DEFAULT_PANEL, cost_path: Path = DEFAULT_COST,
                  output: Path = DEFAULT_OUTPUT, report: Path = DEFAULT_REPORT) -> tuple[Path, Path]:
    receipt = build_decision(panel_path, cost_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(render_markdown(receipt), encoding="utf-8")
    return output, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--cost-pressure", type=Path, default=DEFAULT_COST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    output, report = write_outputs(panel_path=args.panel, cost_path=args.cost_pressure,
                                   output=args.output, report=args.report)
    receipt = _read(output)
    print(json.dumps({"status": receipt["status"], "strategy_count": receipt["strategy_count"],
                      "summary": receipt["summary"], "output": str(output.resolve()),
                      "report": str(report.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_decision", "main", "render_markdown", "write_outputs"]
