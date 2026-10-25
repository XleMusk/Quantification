"""Build a research-only adoption gate from comparable candidate evidence.

The gate joins the saved common-window comparison, turnover-cost pressure and
the latest research-selection receipt.  It records an explicit next decision
without promoting a candidate to paper or live execution.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_candidate_adoption_decision_v1"
COMMON_SCHEMA = "quantification_common_window_strategy_comparison_v1"
COST_SCHEMA = "quantification_common_window_cost_pressure_v1"
SELECTION_SCHEMA = "quantification_candidate_research_selection_v1"

DEFAULT_COMMON = Path("outputs/pipeline_receipts/20261026_common_window_strategy_comparison_v1.json")
DEFAULT_COST = Path("outputs/pipeline_receipts/20261026_common_window_cost_pressure_v1.json")
DEFAULT_SELECTION = Path("outputs/pipeline_receipts/20261026_candidate_research_selection_v2.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261026_candidate_adoption_decision_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261026_candidate_adoption_decision_v1.md")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"receipt must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _require_schema(receipt: Mapping[str, Any], schema: str, path: Path) -> None:
    if receipt.get("schema") != schema:
        raise ValueError(f"schema mismatch for {path}: expected {schema}")


def _selection_index(receipt: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = receipt.get("rows")
    if not isinstance(rows, list):
        raise ValueError("selection receipt rows must be a list")
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("selection receipt row must be an object")
        strategy_id = row.get("strategy_id")
        if isinstance(strategy_id, str) and strategy_id:
            indexed[strategy_id] = row
    return indexed


def _resolve_selection_row(strategy: Mapping[str, Any], selected: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    strategy_id = strategy.get("id")
    direct = selected.get(strategy_id)
    if direct is not None:
        return direct
    candidate_spec_id = strategy.get("candidate_spec_id")
    matches = [
        row for row in selected.values()
        if row.get("catalog_id") == candidate_spec_id
        or row.get("candidate_spec_id") == candidate_spec_id
    ]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"strategy missing from latest selection: {strategy_id}")


def _pressure_index(receipt: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    rows = receipt.get("strategies")
    if not isinstance(rows, list):
        raise ValueError("cost-pressure receipt strategies must be a list")
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            raise ValueError("cost-pressure strategy row is malformed")
        pressure = row.get("pressure")
        if not isinstance(pressure, list) or not pressure:
            raise ValueError(f"cost-pressure row has no pressure levels: {row.get('id')}")
        max_pressure = max(
            (item for item in pressure if isinstance(item, Mapping)),
            key=lambda item: float(item.get("bps", 0.0)),
            default=None,
        )
        if max_pressure is None:
            raise ValueError(f"cost-pressure row has no valid levels: {row.get('id')}")
        indexed[row["id"]] = {"base_net_return": row.get("base_net_return"), "max_pressure": max_pressure}
    return indexed


def _decision(selection: Mapping[str, Any], common: Mapping[str, Any]) -> tuple[str, str]:
    action = selection.get("research_action")
    if action == "STOP_RESEARCH_ONLY":
        return action, "latest selection preserves negative evidence"
    if action == "BLOCKED_COVERAGE":
        return action, "required common coverage is missing"
    if action == "UNASSESSED_SEPARATE_GATE":
        return action, "candidate has a separate timing, coverage or attribution gate"
    if action != "CONTINUE_RESEARCH_ONLY":
        raise ValueError(f"unsupported research action: {action}")
    if common.get("strategy_validated") is not True:
        return "CONTINUE_RESEARCH_ONLY", "common-window evidence is research-only and not strategy validated"
    return "CONTINUE_RESEARCH_ONLY", "predeclared research follow-up remains required"


def build_decision(common_path: Path = DEFAULT_COMMON, cost_path: Path = DEFAULT_COST,
                   selection_path: Path = DEFAULT_SELECTION) -> dict[str, Any]:
    common = _read(common_path)
    cost = _read(cost_path)
    selection = _read(selection_path)
    _require_schema(common, COMMON_SCHEMA, common_path)
    _require_schema(cost, COST_SCHEMA, cost_path)
    _require_schema(selection, SELECTION_SCHEMA, selection_path)
    if common.get("execution_admitted") is not False or cost.get("execution_admitted") is not False:
        raise ValueError("input evidence must remain execution_admitted=false")
    if common.get("common_window") != cost.get("common_window"):
        raise ValueError("common-window and cost-pressure windows differ")
    if common.get("stockdb_snapshot") != cost.get("stockdb_snapshot"):
        raise ValueError("common-window and cost-pressure snapshots differ")

    selected = _selection_index(selection)
    pressured = _pressure_index(cost)
    decisions: list[dict[str, Any]] = []
    for strategy in common.get("strategies", []):
        if not isinstance(strategy, Mapping):
            raise ValueError("common-window strategy row is malformed")
        strategy_id = strategy.get("id")
        if not isinstance(strategy_id, str):
            raise ValueError(f"strategy missing from latest selection: {strategy_id}")
        selection_row = _resolve_selection_row(strategy, selected)
        if strategy_id not in pressured:
            raise ValueError(f"strategy missing from cost-pressure receipt: {strategy_id}")
        action, reason = _decision(selection_row, strategy)
        pressure = pressured[strategy_id]["max_pressure"]
        decisions.append({
            "strategy_id": strategy_id,
            "label": strategy.get("label"),
            "candidate_spec_id": strategy.get("candidate_spec_id"),
            "research_action": action,
            "decision_reason": reason,
            "selection_strategy_id": selection_row.get("strategy_id"),
            "selection_catalog_id": selection_row.get("catalog_id"),
            "selection_research_followup_allowed": selection_row.get("research_followup_allowed") is True,
            "paper_allowed": False,
            "promotion_allowed": False,
            "common_window_metrics": strategy.get("metrics"),
            "max_cost_pressure": pressure,
            "strategy_validated": strategy.get("strategy_validated") is True,
        })
    return {
        "schema": SCHEMA,
        "status": "COMPLETED_RESEARCH_ONLY_ADOPTION_GATE",
        "execution_admitted": False,
        "paper_allowed": False,
        "promotion_allowed": False,
        "common_window": common.get("common_window"),
        "stockdb_snapshot": common.get("stockdb_snapshot"),
        "inputs": {
            "common_window_receipt": str(common_path.resolve()),
            "common_window_sha256": _sha256(common_path),
            "cost_pressure_receipt": str(cost_path.resolve()),
            "cost_pressure_sha256": _sha256(cost_path),
            "selection_receipt": str(selection_path.resolve()),
            "selection_sha256": _sha256(selection_path),
        },
        "strategies": decisions,
        "summary": {
            "continue_research_only": sum(row["research_action"] == "CONTINUE_RESEARCH_ONLY" for row in decisions),
            "stop_research_only": sum(row["research_action"] == "STOP_RESEARCH_ONLY" for row in decisions),
            "blocked_coverage": sum(row["research_action"] == "BLOCKED_COVERAGE" for row in decisions),
            "unassessed_separate_gate": sum(row["research_action"] == "UNASSESSED_SEPARATE_GATE" for row in decisions),
            "strategy_validated": sum(row["strategy_validated"] for row in decisions),
        },
        "limitations": [
            "This gate joins saved research receipts; it does not rerun a strategy or mutate a ledger.",
            "PIT completeness, forward validation, broker observations and execution authorization remain separate gates.",
            "A CONTINUE_RESEARCH_ONLY result is not paper or live adoption.",
        ],
    }


def render_markdown(receipt: Mapping[str, Any]) -> str:
    lines = [
        "# Candidate adoption gate (research only)",
        "",
        f"Common window: `{receipt['common_window']['start']}..{receipt['common_window']['end']}` ({receipt['common_window']['sessions']} sessions).",
        "",
        "This gate joins the common-window comparison, cost pressure and latest research selection. It never promotes a candidate.",
        "",
        "| Strategy | Decision | Reason | Max cost pressure | Strategy validated |",
        "|---|---|---|---:|---|",
    ]
    for row in receipt["strategies"]:
        pressure = row["max_cost_pressure"]
        lines.append(
            f"| {row['label']} (`{row['strategy_id']}`) | {row['research_action']} | {row['decision_reason']} | "
            f"{float(pressure.get('bps', 0.0)):.0f} bps | {'yes' if row['strategy_validated'] else 'no'} |"
        )
    lines += ["", "All rows keep `paper_allowed=false`, `promotion_allowed=false`, and `execution_admitted=false`.", ""]
    return "\n".join(lines)


def write_outputs(*, common_path: Path = DEFAULT_COMMON, cost_path: Path = DEFAULT_COST,
                  selection_path: Path = DEFAULT_SELECTION, output: Path = DEFAULT_OUTPUT,
                  report: Path = DEFAULT_REPORT) -> tuple[Path, Path]:
    receipt = build_decision(common_path, cost_path, selection_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(render_markdown(receipt), encoding="utf-8")
    return output, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common-window", type=Path, default=DEFAULT_COMMON)
    parser.add_argument("--cost-pressure", type=Path, default=DEFAULT_COST)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    output, report = write_outputs(common_path=args.common_window, cost_path=args.cost_pressure,
                                   selection_path=args.selection, output=args.output, report=args.report)
    print(json.dumps({"status": "COMPLETED_RESEARCH_ONLY_ADOPTION_GATE", "output": str(output), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
