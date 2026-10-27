"""Read-only operator view for saved strategy evidence artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


ArtifactResolver = Callable[[dict[str, Any], str], Path | None]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"offline strategy evidence is not readable: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"offline strategy evidence must be an object: {path}")
    return value


def build_view(package: dict[str, Any], artifact_resolver: ArtifactResolver) -> dict[str, Any]:
    """Build the saved evidence-card view without rerunning research or I/O."""

    evidence_path = artifact_resolver(package, "strategy_evidence_card")
    promotion_path = artifact_resolver(package, "strategy_promotion_review")
    board_path = artifact_resolver(package, "strategy_board_json")
    if not evidence_path or not promotion_path or not board_path:
        raise FileNotFoundError("strategy evidence card or promotion review artifact is missing")

    evidence = _read_json(evidence_path)
    promotion = _read_json(promotion_path)
    board = _read_json(board_path)
    robustness_path = artifact_resolver(package, "research_robustness_json")
    robustness = _read_json(robustness_path) if robustness_path else None
    if robustness is not None and (
        robustness.get("status") != "saved_mark_robustness_diagnostic"
        or bool(robustness.get("fresh_holdout_claimed"))
        or bool(robustness.get("strategy_validated"))
        or bool(robustness.get("execution_admitted"))
    ):
        raise ValueError("robustness diagnostic version or execution boundary is unsupported")

    explainability_path = artifact_resolver(package, "strategy_explainability")
    explainability = _read_json(explainability_path) if explainability_path else None
    explainability_cases = []
    if explainability:
        source_run = Path(str(explainability.get("source_run", "")))
        if source_run.is_file():
            try:
                source_payload = _read_json(source_run)
            except ValueError:
                source_payload = {}
            for case in source_payload.get("cases") or []:
                case_id = str(case.get("case_id") or "")
                explainability_cases.append({
                    "strategy_id": case_id.rsplit(":", 1)[-1] or case_id,
                    "case_id": case_id,
                    "status": case.get("status"),
                    "net_pnl_cny": case.get("net_pnl_cny"),
                    "fees_cny": (case.get("costs") or {}).get("fees_cny"),
                    "open_slippage_cny": (case.get("raw_open_slippage") or {}).get("slippage_cny"),
                    "turnover_value_cny": (case.get("metrics") or {}).get("turnover_value"),
                    "filled_orders": case.get("filled_orders"),
                    "execution_admitted": False,
                })

    factor = evidence.get("factor_replay") or {}
    interpretation = evidence.get("interpretation") or {}
    saved_robustness = None
    if robustness is not None:
        saved_robustness = {
            "status": robustness.get("status"), "path": str(robustness_path),
            "comparison": robustness.get("comparison"), "start": robustness.get("start"),
            "end": robustness.get("end"), "sessions": robustness.get("sessions", 0),
            "cases": robustness.get("cases", 0),
            "window_lengths": robustness.get("window_lengths") or [],
            "cost_stress_bps": robustness.get("cost_stress_bps") or [],
            "rolling_window_summaries": robustness.get("rolling_window_summaries") or [],
            "cost_sensitivity": robustness.get("cost_sensitivity") or [],
            "fresh_holdout_claimed": False, "strategy_validated": False,
            "execution_admitted": False, "basis": robustness.get("basis"),
            "limits": robustness.get("limits") or [],
        }
    return {
        "schema": "offline_strategy_evidence_view_v1",
        "status": evidence.get("status"),
        "source_cutoff": evidence.get("opening_pack", {}).get("source_cutoff"),
        "execution_admitted": False, "automatic_promotion": False,
        "opening_candidates": evidence.get("opening_pack", {}).get("candidates") or [],
        "factor_replay": {
            "status": factor.get("status"), "run_id": factor.get("run_id"),
            "segments": factor.get("segments") or {},
            "model_test_rank_ic": factor.get("model_test_rank_ic") or [],
            "portfolio_metrics": factor.get("portfolio_metrics") or [],
            "paired_deltas": factor.get("paired_deltas") or [],
            "limitations": factor.get("limitations") or [],
        },
        "capacity_rows": [row for row in (board.get("rows") or []) if row.get("family") == "opening_candidate"],
        "promotion": {
            "status": promotion.get("status"), "candidate_count": promotion.get("candidate_count", 0),
            "eligible_candidate_count": promotion.get("eligible_candidate_count", 0),
            "fresh_holdout_evidence": promotion.get("fresh_holdout_evidence") or [],
            "required_evidence": promotion.get("required_evidence") or [],
            "rows": promotion.get("rows") or [],
        },
        "saved_ledger_comparison": evidence.get("saved_ledger_comparison") or None,
        "saved_robustness": saved_robustness,
        "factor_attribution": evidence.get("factor_attribution") or None,
        "factor_cost_replay": evidence.get("factor_cost_replay") or None,
        "factor_cadence_replay": evidence.get("factor_cadence_replay") or None,
        "factor_cadence_stability": evidence.get("factor_cadence_stability") or None,
        "research_decision_matrix": evidence.get("research_decision_matrix") or None,
        "interpretation": {
            "opening_strategy_state": interpretation.get("opening_strategy_state"),
            "factor_strategy_state": interpretation.get("factor_strategy_state"),
            "why": interpretation.get("why") or [],
            "seed_return_range": interpretation.get("seed_return_range") or {},
        },
        "explainability": explainability,
        "explainability_cases": explainability_cases,
        "limitations": evidence.get("limits") or evidence.get("limitations") or [],
    }


__all__ = ["build_view"]
