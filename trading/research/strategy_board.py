"""Build a trader-facing offline strategy board from saved evidence."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from .paper_strategy_selection import _load_robustness
from .independent_window_evidence import by_strategy, latest_artifact


BOARD_COLUMNS = [
    "strategy_id",
    "family",
    "evidence_class",
    "comparability_group",
    "source_window",
    "source_cutoff",
    "net_return",
    "max_drawdown",
    "sharpe_zero_rf_252",
    "robustness_status",
    "robustness_60d_positive_window_ratio",
    "robustness_60d_median_return",
    "robustness_120d_positive_window_ratio",
    "robustness_120d_median_return",
    "robustness_extra_50bps_return",
    "saved_ledger_window_stability_status",
    "saved_ledger_window_count",
    "saved_ledger_positive_window_count",
    "saved_ledger_worst_return",
    "saved_ledger_worst_drawdown",
    "independent_sample_window_count",
    "independent_sample_window_start",
    "independent_sample_window_end",
    "independent_sample_alternate_provider",
    "independent_sample_source",
    "independent_sample_positive_window_count",
    "independent_sample_mean_return",
    "independent_sample_worst_return",
    "independent_sample_worst_drawdown",
    "independent_sample_paired_rows_all_windows",
    "independent_sample_non_overlapping",
    "independent_sample_calendar_window_coverage_verified",
    "independent_sample_calendar_continuity_verified",
    "independent_sample_calendar_gap_count",
    "independent_sample_calendar_source",
    "test_rank_ic",
    "fees_cny",
    "orders",
    "stale_valuation_days",
    "data_usability_status",
    "data_usability_reason",
    "status",
    "execution_admitted",
    "automatic_promotion",
    "paper_risk_status",
    "paper_buy_gross_rmb",
    "paper_cash_after_buys_rmb",
    "paper_gross_headroom_rmb",
    "paper_single_headroom_rmb",
    "paper_account_observed",
    "source_artifact",
    "next_evidence",
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _number(value):
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _factor_test_ic(factor: dict, seed) -> float | None:
    for metric in factor.get("model_metrics") or []:
        if metric.get("split") == "test" and metric.get("variant") == "B0":
            if metric.get("seed") == seed:
                return metric.get("mean_rank_ic")
    return None


def _data_usability(stale_days):
    if stale_days is None:
        return "paper_review_stale_valuation_unknown", "saved comparison does not expose stale valuation count"
    if stale_days > 0:
        return "paper_review_stale_valuation", f"saved comparison contains {stale_days:g} stale valuation day(s)"
    return "paper_review_marks_complete", "saved comparison reports no stale valuation days"


def _robustness_fields(strategy_id: str, robustness_by_signal: dict) -> dict:
    robustness = robustness_by_signal.get(strategy_id, {})
    if not robustness:
        return {
            "robustness_status": "unmatched_saved_mark_diagnostic" if robustness_by_signal else "not_supplied",
            "robustness_60d_positive_window_ratio": "",
            "robustness_60d_median_return": "",
            "robustness_120d_positive_window_ratio": "",
            "robustness_120d_median_return": "",
            "robustness_extra_50bps_return": "",
        }
    return {
        "robustness_status": "matched_saved_mark_diagnostic",
        "robustness_60d_positive_window_ratio": (robustness.get("60", {}) or {}).get("positive_window_ratio", ""),
        "robustness_60d_median_return": (robustness.get("60", {}) or {}).get("return_median", ""),
        "robustness_120d_positive_window_ratio": (robustness.get("120", {}) or {}).get("positive_window_ratio", ""),
        "robustness_120d_median_return": (robustness.get("120", {}) or {}).get("return_median", ""),
        "robustness_extra_50bps_return": (robustness.get("cost_50bps", {}) or {}).get("stressed_net_return", ""),
    }


def _opening_rows(comparison_path: Path, sizing_path: Path | None = None,
                  robustness_by_signal: dict | None = None,
                  portfolio_risk_path: Path | None = None,
                  window_stability: dict | None = None,
                  independent_windows_by_strategy: dict | None = None) -> list[dict]:
    robustness_by_signal = robustness_by_signal or {}
    window_stability = window_stability or {}
    independent_windows_by_strategy = independent_windows_by_strategy or {}
    stability_strategy_id = str(window_stability.get("selected_strategy_id") or "")
    sizing_by_id = {}
    if sizing_path and sizing_path.exists():
        sizing_by_id = {
            row.get("strategy_id", ""): row
            for row in _read_csv(sizing_path)
            if row.get("strategy_id")
        }
    portfolio_risk_by_id = {}
    if portfolio_risk_path and portfolio_risk_path.exists():
        risk_payload = _read_json(portfolio_risk_path)
        portfolio_risk_by_id = {
            str(row.get("strategy_id")): row
            for row in risk_payload.get("rows", []) or []
            if row.get("strategy_id")
        }
    rows = []
    for source in _read_csv(comparison_path):
        strategy_id = source.get("strategy_id") or "unknown_strategy"
        sizing = sizing_by_id.get(strategy_id, {})
        risk = portfolio_risk_by_id.get(strategy_id, {})
        stability = window_stability if strategy_id == stability_strategy_id else {}
        independent = independent_windows_by_strategy.get(strategy_id, {})
        stale_days = _number(source.get("stale_days") or source.get("stale_valuation_days"))
        data_usability_status, data_usability_reason = _data_usability(stale_days)
        rows.append({
            "strategy_id": strategy_id,
            "family": "opening_candidate",
            "evidence_class": "same_cutoff_paper_ledger",
            "comparability_group": "shared_paper_ledger",
            "source_window": "opening_pack_candidate_window",
            "source_cutoff": source.get("source_cutoff"),
            "net_return": _number(source.get("comparison_return")),
            "max_drawdown": _number(source.get("comparison_max_drawdown")),
            "sharpe_zero_rf_252": _number(source.get("comparison_sharpe")),
            **_robustness_fields(strategy_id, robustness_by_signal),
            "saved_ledger_window_stability_status": stability.get("status", ""),
            "saved_ledger_window_count": stability.get("evaluated_windows", ""),
            "saved_ledger_positive_window_count": stability.get("positive_windows", ""),
            "saved_ledger_worst_return": stability.get("worst_net_return", ""),
            "saved_ledger_worst_drawdown": stability.get("worst_max_drawdown", ""),
            "independent_sample_window_count": independent.get("window_count", ""),
            "independent_sample_window_start": independent.get("window_start", ""),
            "independent_sample_window_end": independent.get("window_end", ""),
            "independent_sample_alternate_provider": independent.get("alternate_provider", ""),
            "independent_sample_source": independent.get("source", ""),
            "independent_sample_positive_window_count": independent.get("positive_windows", ""),
            "independent_sample_mean_return": independent.get("mean_net_return", ""),
            "independent_sample_worst_return": independent.get("worst_net_return", ""),
            "independent_sample_worst_drawdown": independent.get("worst_max_drawdown", ""),
            "independent_sample_paired_rows_all_windows": independent.get("paired_rows_all_windows", ""),
            "independent_sample_non_overlapping": independent.get("non_overlapping", ""),
            "independent_sample_calendar_window_coverage_verified": independent.get(
                "calendar_window_coverage_verified", ""
            ),
            "independent_sample_calendar_continuity_verified": independent.get("calendar_continuity_verified", ""),
            "independent_sample_calendar_gap_count": independent.get("calendar_gap_count", ""),
            "independent_sample_calendar_source": independent.get("calendar_source", ""),
            "test_rank_ic": None,
            "fees_cny": None,
            "orders": _number(source.get("candidate_order_count")),
            "stale_valuation_days": stale_days,
            "data_usability_status": data_usability_status,
            "data_usability_reason": data_usability_reason,
            "status": source.get("handoff_status") or "research_preview_not_ready_for_prefill",
            "execution_admitted": False,
            "automatic_promotion": False,
            "paper_risk_status": "paper_reference_only" if risk else sizing.get("risk_status") or "unavailable",
            "paper_buy_gross_rmb": risk.get("paper_accepted_buy_gross_rmb") or sizing.get("accepted_buy_gross_rmb") or "",
            "paper_cash_after_buys_rmb": risk.get("paper_cash_after_buys_only_rmb") or sizing.get("cash_after_buys_only_rmb") or "",
            "paper_gross_headroom_rmb": sizing.get("gross_headroom_rmb") or "",
            "paper_single_headroom_rmb": sizing.get("single_headroom_rmb") or "",
            "paper_account_observed": sizing.get("account_observed") or "unknown",
            "source_artifact": str(comparison_path),
            "next_evidence": "fresh holdout plus current account and quote observations",
        })
    return rows


def _factor_rows(evidence_path: Path) -> tuple[list[dict], dict]:
    evidence = _read_json(evidence_path)
    factor_meta = evidence.get("factor_replay") or {}
    factor_path_text = factor_meta.get("path")
    if not factor_path_text:
        return [], {"status": "factor_replay_missing", "path": None}
    factor_path = Path(factor_path_text)
    if not factor_path.exists():
        return [], {"status": "factor_replay_missing", "path": str(factor_path)}
    factor = _read_json(factor_path)
    rows = []
    test_start, test_end = None, None
    segments = (factor.get("config") or {}).get("segments") or {}
    if segments.get("test"):
        test_start, test_end = segments["test"]
    source_window = f"{test_start}..{test_end}" if test_start and test_end else "factor_test_window"
    for metric in factor.get("portfolio_metrics") or []:
        variant = metric.get("variant") or "unknown_variant"
        seed = metric.get("seed")
        suffix = f"_seed{seed}" if seed is not None else ""
        strategy_id = f"alpha158_lightgbm_{variant.lower()}{suffix}"
        stale_days = _number(metric.get("stale_valuation_days"))
        data_usability_status, data_usability_reason = _data_usability(stale_days)
        rows.append({
            "strategy_id": strategy_id,
            "family": "alpha158_lightgbm" if variant == "B0" else "factor_replay_baseline",
            "evidence_class": "factor_replay_diagnostic",
            "comparability_group": "factor_diagnostic_not_comparable_to_opening_candidates",
            "source_window": source_window,
            "source_cutoff": test_end,
            "net_return": metric.get("net_return"),
            "max_drawdown": metric.get("max_drawdown"),
            "sharpe_zero_rf_252": metric.get("sharpe_zero_rf_252"),
            "test_rank_ic": _factor_test_ic(factor, seed) if variant == "B0" else None,
            "fees_cny": metric.get("fees"),
            "orders": metric.get("orders"),
            "stale_valuation_days": metric.get("stale_valuation_days"),
            "data_usability_status": data_usability_status,
            "data_usability_reason": data_usability_reason,
            "status": "research_benchmark_only" if variant == "B0" else "diagnostic_baseline_only",
            "execution_admitted": False,
            "automatic_promotion": False,
            "paper_risk_status": "not_applicable",
            "paper_buy_gross_rmb": "",
            "paper_cash_after_buys_rmb": "",
            "paper_gross_headroom_rmb": "",
            "paper_single_headroom_rmb": "",
            "paper_account_observed": "not_applicable",
            "source_artifact": str(factor_path),
            "next_evidence": "PIT/corporate-action repair and untouched holdout before account use",
        })
    return rows, {
        "status": factor_meta.get("status") or "factor_replay_found",
        "path": str(factor_path),
        "run_id": factor.get("id"),
        "cutoff_match": bool(factor_meta.get("cutoff_match", False)),
        "requested_cutoff": factor_meta.get("requested_cutoff"),
        "observed_cutoff": factor_meta.get("observed_cutoff") or test_end,
        "candidate_search_performed": bool(factor.get("candidate_search_performed")),
        "fresh_holdout_claimed": bool(factor.get("fresh_holdout_claimed")),
        "limitations": factor.get("limitations") or (factor.get("dataset_manifest") or {}).get("limitations") or [],
    }


def build(comparison_path: str | Path, evidence_path: str | Path,
          destination: str | Path, sizing_path: str | Path | None = None,
          robustness_path: str | Path | None = None,
          portfolio_risk_path: str | Path | None = None) -> tuple[Path, Path, dict]:
    """Write a layered board without replaying, tuning, or admitting execution."""
    comparison = Path(comparison_path).resolve()
    evidence = Path(evidence_path).resolve()
    out = Path(destination).resolve()
    sizing = Path(sizing_path).resolve() if sizing_path else None
    portfolio_risk = Path(portfolio_risk_path).resolve() if portfolio_risk_path else None
    evidence_payload = _read_json(evidence)
    window_stability = ((evidence_payload.get("saved_ledger_comparison") or {}).get("window_stability") or {})
    robustness_artifact, robustness_by_signal = _load_robustness(robustness_path)
    independent_window_artifact, independent_windows_by_strategy = by_strategy(
        latest_artifact()
    )
    opening_rows = _opening_rows(
        comparison, sizing, robustness_by_signal, portfolio_risk,
        window_stability=window_stability,
        independent_windows_by_strategy=independent_windows_by_strategy,
    )
    factor_rows, factor_meta = _factor_rows(evidence)
    rows = opening_rows + factor_rows
    board = {
        "schema": "offline_strategy_board_v1",
        "status": "descriptive_layered_comparison",
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_cutoff": (evidence_payload.get("opening_pack") or {}).get("source_cutoff"),
        "execution_admitted": False,
        "automatic_promotion": False,
        "comparability_policy": {
            "shared_paper_ledger": "Comparable only within the same frozen opening candidate ledger and cost policy.",
            "factor_diagnostic_not_comparable_to_opening_candidates": "Keep Alpha158/LightGBM diagnostic metrics visible but do not rank against paper candidates.",
        },
        "decision": "manual_review_only_no_promotion",
        "rows": rows,
        "source_artifacts": {
            "strategy_comparison": str(comparison),
            "strategy_evidence_card": str(evidence),
            "factor_replay": factor_meta,
            "strategy_account_sizing_matrix": str(sizing) if sizing else None,
            "offline_portfolio_risk_summary": str(portfolio_risk) if portfolio_risk else None,
            "research_robustness": robustness_artifact,
            "independent_window_stability": str(independent_window_artifact) if independent_window_artifact else None,
        },
        "limits": [
            "This board reuses saved artifacts; it performs no new replay, tuning, or holdout claim.",
            "Opening candidates are same-cutoff paper candidates and are not account-sized current orders.",
            "Factor replay rank IC and seed returns are diagnostics; disagreement across seeds is retained.",
            "No current account observation, raw RMB quote observation, broker acknowledgement, or fill is inferred.",
            "Paper sizing fields are capacity projections only; account_observed=false never becomes live account state.",
            "Robustness fields are saved-mark rolling diagnostics and cost stress only; they do not claim a fresh holdout or promotion.",
            "Saved-ledger window stability is a review field; it does not select or promote a strategy.",
            "Independent StockDB/Tencent windows are supplemental evidence; non-overlap does not prove calendar continuity or execution readiness.",
        ],
    }
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "strategy_board.json"
    csv_path = out / "strategy_board.csv"
    json_path.write_text(json.dumps(board, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BOARD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return csv_path, json_path, board
