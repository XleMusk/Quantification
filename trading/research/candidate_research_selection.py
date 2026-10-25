"""Turn the fixed robustness matrix into a reproducible research queue.

This is the strategy-library decision layer after replay evidence.  It keeps
stop, mixed and coverage-blocked candidates visible while allowing only
research-only follow-up work.  It never creates a target, order or paper
selection and never opens a broker/THS channel.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_candidate_research_selection_v1"
VERIFIED = "VERIFIED_CANDIDATE_RESEARCH_SELECTION"
DEFAULT_ROBUSTNESS = Path("outputs/pipeline_receipts/20261024_strategy_robustness_matrix_v1.json")
DEFAULT_CATALOG = Path("docs/architecture_v3/candidate_strategy_catalog_20261024.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_candidate_research_selection_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_candidate_research_selection_v1.md")

STRATEGY_TO_CATALOG = {
    "fixed_low_volatility_252_20": "stockdb_low_volatility_252_20",
    "low_vol_half_momentum_20": "stockdb_low_vol_half_momentum_20",
    "momentum_12_1_20": "stockdb_momentum_12_1_20",
    "mature_factor_balanced_ridge": "factor_candidate_family_balanced_ridge",
    "alpha158_lgbm_cap500": "alpha158_lgbm_candidate_panel_v1",
}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _action(decision: str) -> tuple[str, str]:
    if decision in {"CONTINUE_RESEARCH_ONLY", "MIXED_EVIDENCE_RESEARCH_ONLY"}:
        return "CONTINUE_RESEARCH_ONLY", "Run the next predeclared untouched or causal experiment; do not paper-promote."
    if decision == "STOP_RESEARCH_ONLY":
        return "STOP_RESEARCH_ONLY", "Preserve the negative evidence and do not repeat the same lineage without changed data or code."
    if decision == "BLOCKED_COVERAGE":
        return "BLOCKED_COVERAGE", "Obtain the missing common forward coverage before ranking or replay."
    return "UNASSESSED_SEPARATE_GATE", "Keep the candidate in its own data and timing gate; no selection or promotion."


def _next_step(action: str) -> str:
    """Return the smallest next decision that can change this candidate state."""
    if action == "STOP_RESEARCH_ONLY":
        return "Keep the negative evidence; reopen only after a materially changed snapshot or rule."
    if action == "BLOCKED_COVERAGE":
        return "Obtain the missing common forward coverage before ranking or replaying this candidate."
    if action == "CONTINUE_RESEARCH_ONLY":
        return "Run the next predeclared untouched or causal experiment with the existing cost and timing contract."
    return "Keep a separate gate; first satisfy the candidate timing, coverage and attribution conditions."


def build_selection_plan(robustness: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
    rows = robustness.get("strategies")
    entries = catalog.get("entries")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Robustness receipt has no strategy rows")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Candidate catalog has no entries")
    by_strategy = {str(row.get("strategy_id")): row for row in rows}
    catalog_ids = {str(row.get("id")) for row in entries}
    missing = sorted(set(STRATEGY_TO_CATALOG.values()) - catalog_ids)
    if missing:
        raise ValueError(f"Robustness mapping is absent from catalog: {missing}")
    selected: list[str] = []
    blocked: list[str] = []
    stopped: list[str] = []
    plan_rows: list[dict[str, Any]] = []
    for entry in entries:
        catalog_id = str(entry.get("id"))
        strategy_id = next((key for key, value in STRATEGY_TO_CATALOG.items() if value == catalog_id), None)
        source = by_strategy.get(strategy_id) if strategy_id else None
        decision = str(source.get("decision")) if source else "UNASSESSED_SEPARATE_GATE"
        action, rationale = _action(decision)
        row = {
            "catalog_id": catalog_id,
            "strategy_id": strategy_id,
            "catalog_state": entry.get("state"),
            "robustness_decision": decision,
            "research_action": action,
            "rationale": rationale,
            "next_step": _next_step(action),
            "research_followup_allowed": action == "CONTINUE_RESEARCH_ONLY",
            "paper_allowed": False,
            "promotion_allowed": False,
        }
        plan_rows.append(row)
        if action == "CONTINUE_RESEARCH_ONLY":
            selected.append(catalog_id)
        elif action == "BLOCKED_COVERAGE":
            blocked.append(catalog_id)
        elif action == "STOP_RESEARCH_ONLY":
            stopped.append(catalog_id)
    counts: dict[str, int] = {}
    for row in plan_rows:
        key = row["research_action"]
        counts[key] = counts.get(key, 0) + 1
    return {
        "schema": SCHEMA,
        "status": VERIFIED,
        "source_robustness_receipt": str(DEFAULT_ROBUSTNESS.resolve()),
        "source_catalog": str(DEFAULT_CATALOG.resolve()),
        "policy": {
            "continue_or_mixed": "research_followup_only",
            "stop": "preserve_negative_evidence_and_do_not_repeat_same_lineage",
            "blocked": "wait_for_missing_coverage",
            "unassessed": "separate_gate",
            "paper_allowed": False,
            "promotion_allowed": False,
        },
        "rows": plan_rows,
        "action_counts": counts,
        "research_followup_queue": selected,
        "coverage_blocked": blocked,
        "stopped": stopped,
        "selection_enabled": False,
        "execution_admitted": False,
        "orders_created": False,
        "fills_created": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "research_only": True,
        "limitations": [
            "The queue chooses research follow-ups from saved evidence; it does not select a paper or live strategy.",
            "Unassessed shortline, public-seat and leader candidates retain their separate timing, coverage and attribution gates.",
            "PIT, publication timing and company-action evidence remain promotion prerequisites.",
        ],
    }


def build_report(result: Mapping[str, Any]) -> str:
    lines = ["# Candidate research selection", "",
             f"Status: `{result['status']}`; paper selection: `false`; promotion: `false`.", "",
             "The strategy library now has a fixed research queue and an explicit next step for every candidate.", "",
             "| Catalog candidate | Decision | Research action | Next step |", "|---|---|---|---|"]
    for row in result["rows"]:
        lines.append(f"| {row['catalog_id']} | {row['robustness_decision']} | {row['research_action']} | {row['next_step']} |")
    lines += ["", f"Research follow-up queue: `{', '.join(result['research_followup_queue']) or 'empty'}`.",
              "No target, order, fill, broker or THS action was generated.", ""]
    return "\n".join(lines)


def run(robustness_path: Path = DEFAULT_ROBUSTNESS, catalog_path: Path = DEFAULT_CATALOG,
        output_path: Path = DEFAULT_OUTPUT, report_path: Path = DEFAULT_REPORT) -> dict[str, Any]:
    robustness_path, catalog_path = robustness_path.resolve(), catalog_path.resolve()
    result = build_selection_plan(_read(robustness_path), _read(catalog_path))
    result.update({"observed_at": datetime.now(timezone.utc).isoformat(),
                   "source_robustness_receipt": str(robustness_path),
                   "source_catalog": str(catalog_path),
                   "receipt_path": str(output_path.resolve()),
                   "report_path": str(report_path.resolve())})
    output_path, report_path = output_path.resolve(), report_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(build_report(result), encoding="utf-8")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robustness", type=Path, default=DEFAULT_ROBUSTNESS)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    result = run(args.robustness, args.catalog, args.output, args.report)
    print(json.dumps({"status": result["status"], "action_counts": result["action_counts"],
                      "research_followup_queue": result["research_followup_queue"],
                      "execution_admitted": False}, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "VERIFIED", "build_report", "build_selection_plan", "main", "run"]


if __name__ == "__main__":
    raise SystemExit(main())
