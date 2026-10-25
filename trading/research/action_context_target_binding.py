"""Bind candidate target rows to dated corporate-action context.

The binding is a research diagnostic.  It proves target identity and action
coverage lineage, but it never applies cash/share adjustments or promotes a
row to PIT-safe.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

from ..settings import ROOT


SCHEMA = "quantification_action_context_target_binding_v1"
DEFAULT_TARGET = ROOT / "outputs/pipeline_receipts/20261025_continue_candidates_joint_paper_v3/joint_paper_receipt.json"
DEFAULT_COVERAGE = ROOT / "outputs/pipeline_receipts/20261024_candidate_action_publication_coverage_v1.json"
DEFAULT_RECEIPT = ROOT / "outputs/pipeline_receipts/20261026_action_context_target_binding_v1.json"
DEFAULT_REPORT = ROOT / "outputs/research_reports/20261026_action_context_target_binding_v1.md"
ALIASES = {"stockdb_factor_monthly_ridge_v1": "mature_factor_balanced_ridge"}


def _read(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _coverage_by_strategy(coverage: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for strategy in coverage.get("strategies", []):
        strategy_id = str(strategy.get("strategy_id"))
        rows = {}
        for row in strategy.get("symbol_breakdown", []):
            rows[str(row.get("symbol"))] = {
                "action_events": int(row.get("action_events", 0)),
                "plan_announce_present": int(row.get("plan_announce_present", 0)),
                "operate_date_present": int(row.get("operate_date_present", 0)),
                "source_artifact_complete": int(row.get("source_artifact_complete", 0)),
            }
        result[strategy_id] = rows
    return result


def build_binding(target: Mapping[str, Any], coverage: Mapping[str, Any]) -> dict[str, Any]:
    joint = target.get("joint") if isinstance(target.get("joint"), Mapping) else target
    target_rows = list(joint.get("target_rows") or [])
    if not target_rows:
        raise ValueError("joint target rows are required")
    by_strategy = _coverage_by_strategy(coverage)
    rows: list[dict[str, Any]] = []
    for target_row in target_rows:
        symbol = str(target_row.get("symbol"))
        contributors = [str(item) for item in target_row.get("contributors", [])]
        evidence = []
        for contributor in contributors:
            coverage_id = ALIASES.get(contributor, contributor)
            evidence.append({
                "strategy_id": contributor,
                "coverage_strategy_id": coverage_id,
                **by_strategy.get(coverage_id, {}).get(symbol, {
                    "action_events": 0,
                    "plan_announce_present": 0,
                    "operate_date_present": 0,
                    "source_artifact_complete": 0,
                }),
            })
        rows.append({
            "symbol": symbol,
            "contributors": contributors,
            "target_quantity": target_row.get("target_quantity"),
            "target_notional_cents": target_row.get("target_notional_cents"),
            "action_context": evidence,
            "pit_asof_safe": False,
            "company_action_applied": False,
            "status": "BOUND_CONTEXT_PIT_BLOCKED",
        })
    return {
        "schema": SCHEMA,
        "status": "BOUND_TARGET_CONTEXT_PIT_BLOCKED",
        "created_at": datetime.now().astimezone().isoformat(),
        "decision_day": joint.get("common_terminal_day"),
        "strategy_ids": joint.get("strategy_ids", []),
        "target_count": len(rows),
        "rows": rows,
        "summary": {
            "target_rows": len(rows),
            "rows_with_action_event": sum(bool(row["action_context"] and any(item["action_events"] for item in row["action_context"])) for row in rows),
            "pit_asof_safe_rows": 0,
            "company_action_applied_rows": 0,
            "source_artifact_complete_rows": sum(any(item["source_artifact_complete"] for item in row["action_context"]) for row in rows),
        },
        "execution_admitted": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "limitations": [
            "The binding joins target identity to saved action coverage only.",
            "No company-action cash/share adjustment is applied.",
            "Publication/as-of/revision evidence remains incomplete, so no row is PIT-safe.",
        ],
    }


def render_report(receipt: Mapping[str, Any]) -> str:
    summary = receipt["summary"]
    return "\n".join([
        "# Action context target binding",
        "",
        f"Targets bound: `{summary['target_rows']}`; rows with action events: `{summary['rows_with_action_event']}`; source-complete rows: `{summary['source_artifact_complete_rows']}`.",
        "",
        "All rows remain PIT-blocked and no company-action adjustment was applied.",
        "",
    ])


def write_outputs(target_path: str | Path = DEFAULT_TARGET, coverage_path: str | Path = DEFAULT_COVERAGE,
                  receipt_path: str | Path = DEFAULT_RECEIPT, report_path: str | Path = DEFAULT_REPORT) -> tuple[Path, Path]:
    receipt = build_binding(_read(target_path)["joint"], _read(coverage_path))
    receipt_file = Path(receipt_path).expanduser().resolve()
    report_file = Path(report_path).expanduser().resolve()
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_file.write_text(render_report(receipt), encoding="utf-8")
    return receipt_file, report_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    receipt, report = write_outputs(args.target, args.coverage, args.receipt, args.report)
    print(json.dumps({"receipt": str(receipt), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_binding", "main", "render_report", "write_outputs"]
