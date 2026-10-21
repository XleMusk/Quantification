"""Build a read-only strategy decision matrix from saved sole-ledger artifacts.

This is a trader-facing research consumer, not another backtest.  It joins the
latest saved same-ledger comparison with already completed factor diagnostics
so an operator can see performance, cost, cadence, stability and evidence
boundaries in one place.  It never refits, searches, selects or admits a trade.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ..settings import ROOT


OUTPUT_ROOT = ROOT / "outputs" / "research_decision_matrices"


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _latest_completed(root: Path, statuses: set[str]) -> tuple[Path | None, dict | None]:
    candidates = []
    if not root.exists():
        return None, None
    for path in root.glob("*/run.json"):
        try:
            record = _load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record.get("status") in statuses:
            candidates.append((path.stat().st_mtime_ns, path, record))
    if not candidates:
        return None, None
    _, path, record = max(candidates, key=lambda item: item[0])
    return path, record


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _case_row(case: dict, comparison: dict) -> dict:
    metrics = case.get("metrics") or {}
    period = case.get("source_period") or {}
    return {
        "case_id": case.get("case_id"),
        "family": case.get("family"),
        "signal": case.get("signal"),
        "scope": case.get("scope"),
        "source_start": period.get("start"),
        "source_end": period.get("end"),
        "comparison_start": comparison.get("start"),
        "comparison_end": comparison.get("end"),
        "sessions": metrics.get("sessions"),
        "net_return": _number(metrics.get("net_return")),
        "max_drawdown": _number(metrics.get("max_drawdown")),
        "sharpe_zero_rf_252": _number(metrics.get("sharpe_zero_rf_252")),
        "fees": _number(metrics.get("fees")),
        "turnover_value": _number(metrics.get("turnover_value")),
        "stale_valuation_days": metrics.get("stale_valuation_days"),
        "source_status": case.get("source_status"),
        "strategy_validated": bool(case.get("strategy_validated")),
        "execution_admitted": bool(case.get("execution_admitted")),
        "decision_state": "descriptive_read_only",
    }


def _diagnostic(path: Path | None, record: dict | None, diagnostic_id: str, title: str) -> dict:
    if not path or not record:
        return {
            "diagnostic_id": diagnostic_id,
            "title": title,
            "status": "missing",
            "path": None,
            "run_id": None,
            "execution_admitted": False,
        }
    return {
        "diagnostic_id": diagnostic_id,
        "title": title,
        "status": record.get("status", "unknown"),
        "path": str(path),
        "run_id": record.get("id"),
        "execution_admitted": bool(record.get("execution_admitted")),
        "result": record.get("result") or {},
        "limitations": record.get("limitations") or [],
        "interpretation": record.get("interpretation"),
    }


def _latest_factor_family(source_cutoff: str) -> tuple[Path | None, dict | None]:
    root = ROOT / "outputs" / "factor_families"
    candidates = []
    if not root.exists():
        return None, None
    for path in root.glob("*/run.json"):
        try:
            record = _load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record.get("status") != "completed_research_diagnostics":
            continue
        segments = record.get("segments") or {}
        if str((segments.get("test") or [None, None])[-1]) != str(source_cutoff):
            continue
        candidates.append((path.stat().st_mtime_ns, path, record))
    if not candidates:
        return None, None
    _, path, record = max(candidates, key=lambda item: item[0])
    return path, record


def _latest_factor_library_ablation(source_cutoff: str) -> tuple[Path | None, dict | None]:
    """Load the newest completed leave-one-library-out diagnostic for this cutoff."""
    root = ROOT / "outputs" / "factor_candidate_library_ablations"
    candidates = []
    if not root.exists():
        return None, None
    for path in root.glob("*/run.json"):
        try:
            record = _load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record.get("status") != "completed_factor_candidate_library_ablation":
            continue
        if str(record.get("end") or "") != str(source_cutoff or ""):
            continue
        if not record.get("results"):
            continue
        finished = record.get("finished_at") or record.get("started_at") or ""
        candidates.append((finished, path.stat().st_mtime_ns, path, record))
    if not candidates:
        return None, None
    _, _, path, record = max(candidates, key=lambda item: (item[0], item[1], str(item[2])))
    return path, record


def _latest_saved_risk_views() -> dict[str, dict]:
    """Expose completed historical risk views without recomputing or selecting."""
    specs = (
        ("concentration", ROOT / "outputs" / "risk_concentration", {"completed_saved_account_concentration"}),
        ("stress_exposure", ROOT / "outputs" / "stress_exposure", {"completed_saved_stress_exposure"}),
        ("style_exposure", ROOT / "outputs" / "style_exposure", {"completed_dated_style_diagnostics"}),
    )
    views: dict[str, dict] = {}
    for name, root, statuses in specs:
        path, record = _latest_completed(root, statuses)
        if not path or not record:
            continue
        views[name] = {
            "status": record.get("status"),
            "path": str(path),
            "run_id": record.get("id"),
            "summary": record.get("summary") or {},
            "limitations": record.get("limitations") or [],
            "strategy_validated": bool(record.get("strategy_validated")),
            "execution_admitted": False,
        }
    return views


def _latest_model_stability(source_cutoff: str) -> dict | None:
    """Summarize the saved Alpha158/LightGBM seed spread for this cutoff."""
    root = ROOT / "outputs" / "factor_mining"
    candidates = []
    if not root.exists():
        return None
    for path in root.glob("*/run.json"):
        try:
            record = _load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record.get("status") != "completed":
            continue
        manifest = record.get("dataset_manifest") or {}
        if str(manifest.get("source_cutoff") or manifest.get("end") or "") != str(source_cutoff or ""):
            continue
        if not record.get("model_metrics") or not record.get("portfolio_metrics"):
            continue
        finished = record.get("finished_at") or record.get("started_at") or ""
        candidates.append((finished, path.stat().st_mtime_ns, path, record))
    if not candidates:
        return None
    _, _, path, record = max(candidates, key=lambda item: (item[0], item[1], str(item[2])))
    model_metrics = [
        row for row in record.get("model_metrics") or []
        if row.get("split") == "test" and row.get("mean_rank_ic") is not None
    ]
    portfolio_metrics = [
        row for row in record.get("portfolio_metrics") or []
        if row.get("seed") is not None and row.get("net_return") is not None
    ]
    if not model_metrics or not portfolio_metrics:
        return None
    ics = [float(row["mean_rank_ic"]) for row in model_metrics]
    returns = sorted(float(row["net_return"]) for row in portfolio_metrics)
    median = returns[len(returns) // 2] if len(returns) % 2 else (
        returns[len(returns) // 2 - 1] + returns[len(returns) // 2]
    ) / 2
    return {
        "status": "completed_saved_model_seed_stability",
        "path": str(path),
        "run_id": record.get("id"),
        "seed_count": len(portfolio_metrics),
        "test_rank_ic_mean": sum(ics) / len(ics),
        "test_rank_ic_min": min(ics),
        "test_rank_ic_max": max(ics),
        "test_rank_ic_positive_seed_count": sum(value > 0 for value in ics),
        "portfolio_net_return_median": median,
        "portfolio_net_return_min": min(returns),
        "portfolio_net_return_max": max(returns),
        "portfolio_positive_seed_count": sum(value > 0 for value in returns),
        "limitations": [
            "Seed dispersion is descriptive and does not establish model robustness or future returns.",
            "The saved run is not a fresh untouched holdout and execution admission remains disabled.",
        ],
        "execution_admitted": False,
    }


def _latest_factor_regime_overlay(source_cutoff: str) -> dict | None:
    """Load the completed fixed breadth-regime overlay for this cutoff."""
    root = ROOT / "outputs" / "factor_regime_overlays"
    candidates = []
    if not root.exists():
        return None
    for path in root.glob("*/run.json"):
        try:
            record = _load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record.get("status") != "completed_factor_breadth_regime_overlay":
            continue
        if str(record.get("end") or "") != str(source_cutoff or ""):
            continue
        result = record.get("result") or {}
        if result.get("status") != "completed":
            continue
        finished = record.get("finished_at") or record.get("started_at") or ""
        candidates.append((finished, path.stat().st_mtime_ns, path, record))
    if not candidates:
        return None
    _, _, path, record = max(candidates, key=lambda item: (item[0], item[1], str(item[2])))
    result = record.get("result") or {}
    control = result.get("control_metrics") or {}
    return {
        "status": record.get("status"),
        "path": str(path),
        "run_id": record.get("id"),
        "signal": record.get("signal"),
        "result": {
            "net_return": result.get("net_return"),
            "max_drawdown": result.get("max_drawdown"),
            "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
            "net_return_difference": result.get("net_return_difference"),
            "max_drawdown_difference": result.get("max_drawdown_difference"),
            "sharpe_difference": result.get("sharpe_difference"),
            "risk_off_decision_days": result.get("risk_off_decision_days"),
            "control_net_return": control.get("net_return"),
            "control_max_drawdown": control.get("max_drawdown"),
        },
        "limitations": record.get("limitations") or [],
        "selection_enabled": False,
        "execution_admitted": False,
    }


def _latest_independent_sample() -> tuple[Path | None, dict | None]:
    """Load the newest completed independent-provider sample comparison.

    The comparison is already a saved shared-ledger artifact.  Surface it in
    the operator matrix without recomputing it or allowing it to affect
    selection/admission.
    """
    root = ROOT / "outputs" / "stockdb_independent_sample_compare"
    candidates = []
    if not root.exists():
        return None, None
    for path in root.glob("*/run.json"):
        try:
            record = _load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record.get("schema") != "stockdb_independent_sample_compare_v1":
            continue
        if record.get("status") != "completed_research_only":
            continue
        if not record.get("independent_sample"):
            continue
        candidates.append((path.stat().st_mtime_ns, path, record))
    if not candidates:
        return None, None
    _, path, record = max(candidates, key=lambda item: item[0])
    evaluation = record.get("evaluation_window") or {}
    return path, {
        "status": record.get("status"),
        "path": str(path),
        "run_id": record.get("id"),
        "selection_cutoff": record.get("selection_cutoff"),
        "evaluation_start": evaluation.get("start"),
        "evaluation_end": evaluation.get("end"),
        "sessions": len(evaluation.get("sessions") or []),
        "target_universe_count": record.get("target_universe_count"),
        "target_basket_size": record.get("target_basket_size"),
        "alternate_provider": record.get("alternate_provider"),
        "provider_complete_paired_rows": record.get("provider_complete_paired_rows"),
        "ledger_results": record.get("ledger_results") or [],
        "independent_sample": True,
        "prior_forward_overlap": bool(record.get("prior_forward_overlap")),
        "research_only": True,
        "selection_enabled": False,
        "execution_admitted": False,
        "limitations": record.get("limitations") or [],
    }


def _factor_inventory() -> dict:
    root = ROOT / "outputs" / "factor_selection_review" / "20260929"
    formula_path = root / "formula_inventory.csv"
    operator_path = root / "operator_inventory.csv"
    if not formula_path.exists():
        return {"status": "missing", "formula_path": None, "operator_path": None,
                "library_counts": {}, "formula_count": 0, "operator_count": 0,
                "execution_admitted": False}
    import csv
    counts: dict[str, int] = {}
    with formula_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            library = row.get("library") or "unknown"
            counts[library] = counts.get(library, 0) + 1
    operator_count = 0
    if operator_path.exists():
        with operator_path.open("r", encoding="utf-8-sig", newline="") as handle:
            operator_count = sum(1 for _ in csv.DictReader(handle))
    return {
        "status": "completed_static_formula_inventory",
        "formula_path": str(formula_path),
        "operator_path": str(operator_path) if operator_path.exists() else None,
        "library_counts": counts,
        "formula_count": sum(counts.values()),
        "operator_count": operator_count,
        "execution_admitted": False,
        "limitations": [
            "Formula inventory is a definition catalog, not a market-data validation or portfolio result.",
            "Library counts do not imply independent information, tradability or incremental net value.",
        ],
    }


def _research_exception_register() -> dict:
    """Summarize resumable research blockers without reopening a run.

    Factor projections can stop after a valid ledger prefix when source-backed
    corporate-action terms are missing.  Keep the latest failed attempt and
    the last completed projection together so consumers can resume from the
    right evidence boundary instead of starting another blind replay.
    """
    root = ROOT / "outputs" / "factor_execution_projections"
    attempts = []
    if root.exists():
        for path in root.glob("*/run.json"):
            try:
                record = _load(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if record.get("research_family") != "factor_execution_projection":
                continue
            attempts.append((path.stat().st_mtime_ns, path, record))

    def newest(statuses):
        matching = [item for item in attempts if item[2].get("status") in statuses]
        if not matching:
            return None
        _, path, record = max(matching, key=lambda item: item[0])
        result = record.get("result") or {}
        return {
            "path": str(path),
            "run_id": record.get("id"),
            "status": record.get("status"),
            "signal": record.get("signal"),
            "start": record.get("start"),
            "end": record.get("end"),
            "completed_sessions": result.get("completed_sessions") or result.get("sessions") or 0,
            "error": record.get("error") or result.get("error"),
            "blocker": record.get("blocker") or {},
        }

    latest = newest({
        "completed_factor_execution_projection",
        "failed_factor_execution_projection",
        "failed_factor_execution_ledger",
    })
    completed = newest({"completed_factor_execution_projection"})
    failed = newest({"failed_factor_execution_projection", "failed_factor_execution_ledger"})
    completed_mtime = max(
        (item[0] for item in attempts
         if item[2].get("status") == "completed_factor_execution_projection"),
        default=-1,
    )
    failed_mtime = max(
        (item[0] for item in attempts
         if item[2].get("status") in {"failed_factor_execution_projection", "failed_factor_execution_ledger"}),
        default=-1,
    )
    unresolved_failure = bool(failed and failed_mtime > completed_mtime)
    blocker = (failed or {}).get("blocker") or {}
    open_items = []
    if unresolved_failure:
        message = blocker.get("message") or failed.get("error") or "factor execution projection failed"
        item = {
            "code": blocker.get("code") or "factor_execution_projection_blocked",
            "message": str(message),
            "symbol": blocker.get("symbol"),
            "decision_date": blocker.get("decision_date"),
            "missing_fields": blocker.get("missing_fields") or [],
            "source": failed["path"],
        }
        open_items.append(item)

    market_root = ROOT / "outputs" / "market_replay"
    market_attempts = []
    if market_root.exists():
        for path in market_root.glob("*/run.json"):
            try:
                record = _load(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if record.get("scope") != "full_source_sh_sz_research_only":
                continue
            run_key = (record.get("start"), record.get("end"))
            for result in record.get("results") or []:
                result_key = (result.get("signal"), *run_key)
                market_attempts.append({
                    "mtime": path.stat().st_mtime_ns,
                    "path": str(path),
                    "run_id": record.get("id"),
                    "signal": result.get("signal"),
                    "start": record.get("start"),
                    "end": record.get("end"),
                    "status": result.get("status"),
                    "completed_sessions": result.get("completed_sessions") or result.get("sessions") or 0,
                    "blocker": result.get("blocker") or {},
                    "result_key": result_key,
                })
    market_completed = {}
    for attempt in market_attempts:
        if attempt["status"] == "completed":
            market_completed[attempt["result_key"]] = max(
                market_completed.get(attempt["result_key"], -1), attempt["mtime"])
    market_failures = [
        attempt for attempt in market_attempts
        if attempt["status"] == "failed" and attempt["blocker"]
        and attempt["mtime"] >= market_completed.get(attempt["result_key"], -1)
    ]
    for failed_market in sorted(market_failures, key=lambda item: item["mtime"], reverse=True):
        blocker = failed_market["blocker"]
        open_items.append({
            "code": blocker.get("code") or "market_replay_blocked",
            "message": blocker.get("message") or "market replay failed",
            "symbol": blocker.get("symbol"),
            "decision_date": blocker.get("decision_date"),
            "missing_fields": blocker.get("missing_fields") or [],
            "source": failed_market["path"],
            "signal": failed_market["signal"],
            "window": {"start": failed_market["start"], "end": failed_market["end"]},
            "source_kind": "market_replay",
        })
    latest_market = max(market_attempts, key=lambda item: item["mtime"]) if market_attempts else None

    if not attempts and not market_attempts:
        status = "missing"
        next_action = "run the fixed projection only after its pinned source inputs are available"
    elif open_items:
        status = "open_blockers"
        next_action = "attach reviewed source evidence for the affected run, then resume from its last successful ledger session"
    else:
        status = "no_open_blocker"
        next_action = "retain the latest completed projection; do not refit or reopen it without a new source snapshot"
    return {
        "schema": "research_data_exception_register_v1",
        "status": status,
        "latest_attempt": latest,
        "latest_failed_attempt": failed,
        "latest_market_replay_attempt": latest_market,
        "last_successful_projection": completed,
        "completed_session_frontier": (completed or {}).get("completed_sessions", 0),
        "open_items": open_items,
        "resume_policy": "resume_after_reviewed_source_evidence_only",
        "resume_allowed": False,
        "execution_admitted": False,
        "evidence_scope": "research_only",
        "operator_next_action": next_action,
    }


def build(comparison_path: Path, out: Path | None = None) -> Path:
    comparison = _load(comparison_path)
    if comparison.get("status") != "compared_saved_accounts":
        raise ValueError(f"Unsupported comparison status: {comparison.get('status')}")
    cases = comparison.get("cases") or []

    diagnostics = []
    diagnostics.append(_diagnostic(*_latest_completed(
        ROOT / "outputs" / "factor_candidate_attributions",
        {"completed_factor_candidate_attribution"},
    ), "factor_attribution", "因子账本归因"))
    diagnostics.append(_diagnostic(*_latest_completed(
        ROOT / "outputs" / "factor_candidate_cost_replays",
        {"completed_factor_candidate_cost_replay"},
    ), "factor_cost_path", "固定成本路径"))
    diagnostics.append(_diagnostic(*_latest_completed(
        ROOT / "outputs" / "factor_candidate_cadence_replays",
        {"completed_factor_candidate_cadence_replay"},
    ), "factor_cadence", "固定双月频率"))
    diagnostics.append(_diagnostic(*_latest_completed(
        ROOT / "outputs" / "factor_candidate_cadence_stability",
        {"completed_saved_ledger_rolling_diagnostic"},
    ), "factor_stability", "滚动窗口稳定性"))
    factor_library_path, factor_library = _latest_factor_family(str(comparison.get("end") or ""))
    factor_library_view = {
        "status": factor_library.get("status") if factor_library else "missing",
        "path": str(factor_library_path) if factor_library_path else None,
        "run_id": factor_library.get("id") if factor_library else None,
        "definitions_completed": factor_library.get("definitions_completed", 0) if factor_library else 0,
        "redundancy_edges": factor_library.get("redundancy_edges", 0) if factor_library else 0,
        "redundancy_components": factor_library.get("redundancy_components", 0) if factor_library else 0,
        "quality": factor_library.get("quality") or {} if factor_library else {},
        "diagnostic_counts": factor_library.get("diagnostic_counts") or [] if factor_library else [],
        "limitations": (factor_library.get("quality") or {}).get("limitations") or [] if factor_library else [],
        "account_backtest": bool(factor_library.get("account_backtest")) if factor_library else False,
        "execution_admitted": False,
    }
    factor_inventory_view = _factor_inventory()
    factor_library_ablation_path, factor_library_ablation = _latest_factor_library_ablation(
        str(comparison.get("end") or "")
    )
    ablation_rows = []
    if factor_library_ablation:
        for library, result in sorted((factor_library_ablation.get("results") or {}).items()):
            control = result.get("control_metrics") or {}
            ablation_rows.append({
                "excluded_library": library,
                "status": result.get("status"),
                "selected_factor_count": result.get("selected_factor_count"),
                "net_return": result.get("net_return"),
                "net_return_difference": result.get("net_return_difference"),
                "max_drawdown": result.get("max_drawdown"),
                "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
                "sharpe_difference": result.get("sharpe_difference"),
                "fees": result.get("fees"),
                "control_net_return": control.get("net_return"),
            })
    factor_library_ablation_view = {
        "status": factor_library_ablation.get("status") if factor_library_ablation else "missing",
        "path": str(factor_library_ablation_path) if factor_library_ablation_path else None,
        "run_id": factor_library_ablation.get("id") if factor_library_ablation else None,
        "source_run": factor_library_ablation.get("source_run") if factor_library_ablation else None,
        "candidate_count": factor_library_ablation.get("candidate_count", 0) if factor_library_ablation else 0,
        "libraries": factor_library_ablation.get("libraries") or [] if factor_library_ablation else [],
        "rows": ablation_rows,
        "limitations": factor_library_ablation.get("limitations") or [] if factor_library_ablation else [],
        "selection_enabled": False,
        "execution_admitted": False,
    }
    risk_views = _latest_saved_risk_views()
    model_stability = _latest_model_stability(str(comparison.get("end") or ""))
    regime_overlay = _latest_factor_regime_overlay(str(comparison.get("end") or ""))
    independent_sample_path, independent_sample = _latest_independent_sample()
    research_exceptions = _research_exception_register()
    exception_sources = [
        (research_exceptions.get("latest_attempt") or {}).get("path"),
        (research_exceptions.get("latest_market_replay_attempt") or {}).get("path"),
    ]
    exception_sources = "; ".join(path for path in exception_sources if path) or None

    functions = [
        {"function_id": "candidate_comparison", "desk_function": "策略/组合比较", "status": "completed_saved_ledger", "source": str(comparison_path), "selection": "disabled"},
        {"function_id": "pnl_attribution", "desk_function": "损益与费用归因", "status": diagnostics[0]["status"], "source": diagnostics[0]["path"], "selection": "disabled"},
        {"function_id": "cost_sensitivity", "desk_function": "成本敏感性", "status": diagnostics[1]["status"], "source": diagnostics[1]["path"], "selection": "disabled"},
        {"function_id": "rebalance_cadence", "desk_function": "再平衡频率诊断", "status": diagnostics[2]["status"], "source": diagnostics[2]["path"], "selection": "disabled"},
        {"function_id": "window_stability", "desk_function": "滚动窗口稳定性", "status": diagnostics[3]["status"], "source": diagnostics[3]["path"], "selection": "disabled"},
        {"function_id": "factor_library_redundancy", "desk_function": "因子库覆盖与冗余", "status": factor_library_view["status"], "source": factor_library_view["path"], "selection": "diagnostic_only"},
        {"function_id": "factor_incremental_ablation", "desk_function": "因子家族增量消融", "status": factor_library_ablation_view["status"], "source": factor_library_ablation_view["path"], "selection": "diagnostic_only"},
        {"function_id": "factor_formula_inventory", "desk_function": "公式库目录与来源", "status": factor_inventory_view["status"], "source": factor_inventory_view["formula_path"], "selection": "catalog_only"},
        {"function_id": "portfolio_risk_exposure", "desk_function": "组合风险与暴露诊断", "status": "completed_saved_historical_risk_views" if risk_views else "missing", "source": "; ".join(view["path"] for view in risk_views.values()) or None, "selection": "diagnostic_only"},
        {"function_id": "factor_model_stability", "desk_function": "模型种子稳定性", "status": model_stability["status"] if model_stability else "missing", "source": model_stability["path"] if model_stability else None, "selection": "diagnostic_only"},
        {"function_id": "factor_regime_overlay", "desk_function": "市场状态覆盖诊断", "status": regime_overlay["status"] if regime_overlay else "missing", "source": regime_overlay["path"] if regime_overlay else None, "selection": "diagnostic_only"},
        {"function_id": "independent_sample_compare", "desk_function": "独立样本策略比较", "status": independent_sample["status"] if independent_sample else "missing", "source": independent_sample["path"] if independent_sample else None, "selection": "diagnostic_only"},
        {"function_id": "research_data_exception_management", "desk_function": "研究数据异常与恢复边界", "status": research_exceptions["status"], "source": exception_sources, "selection": "disabled"},
        {"function_id": "live_release", "desk_function": "实时/账户/成交释放", "status": "blocked_by_boundary", "source": None, "selection": "disabled", "reason": "No current quote, account observation or broker fill evidence"},
    ]

    record = {
        "schema": "research_decision_matrix_v1",
        "id": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed_read_only_research_decision_matrix",
        "comparison": {
            "path": str(comparison_path),
            "start": comparison.get("start"),
            "end": comparison.get("end"),
            "sessions": comparison.get("sessions"),
            "requested_cases": comparison.get("requested_cases"),
            "comparable_cases": comparison.get("comparable_cases"),
            "same_cost_settings": bool(comparison.get("same_cost_settings")),
        },
        "strategy_rows": [_case_row(case, comparison) for case in cases],
        "diagnostics": diagnostics,
        "factor_library": factor_library_view,
        "factor_inventory": factor_inventory_view,
        "factor_library_ablation": factor_library_ablation_view,
        "risk_views": risk_views,
        "model_stability": model_stability or {"status": "missing", "execution_admitted": False},
        "factor_regime_overlay": regime_overlay or {"status": "missing", "execution_admitted": False},
        "independent_sample": independent_sample or {"status": "missing", "path": None, "selection_enabled": False, "execution_admitted": False},
        "research_data_exceptions": research_exceptions,
        "desk_functions": functions,
        "selection_enabled": False,
        "execution_admitted": False,
        "limitations": [
            "Rows reuse saved sole-ledger daily marks; this is not a new backtest or holdout.",
            "Different source scopes, capital histories and opening equities remain visible and are not silently normalized.",
            "Diagnostic modules reuse saved rankings/model outputs and do not prove causal alpha or future returns.",
            "No current quote, account observation, broker response or fill evidence is present; live release is blocked.",
            "Research exception status is resumable evidence metadata only; it never authorizes a new replay, selection or order.",
        ],
        "operator_next_action": "Use the matrix to choose the next research question; keep all candidates paper-only until current-session and independent validation evidence exists.",
    }
    target = out or OUTPUT_ROOT / record["id"]
    target.mkdir(parents=True, exist_ok=False)
    json_path = target / "run.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    with (target / "strategy_matrix.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = list(record["strategy_rows"][0]) if record["strategy_rows"] else ["case_id", "decision_state"]
        handle.write(",".join(fields) + "\n")
        for row in record["strategy_rows"]:
            handle.write(",".join(json.dumps(row.get(field), ensure_ascii=False) for field in fields) + "\n")
    lines = [
        "# Research Decision Matrix",
        "",
        f"- Comparison: `{comparison.get('start')}..{comparison.get('end')}`; cases `{len(cases)}`; comparable `{comparison.get('comparable_cases')}`",
        "- Status: completed read-only research consumer; selection and execution admission disabled.",
        "",
        "## Institutional-style offline functions",
        "",
        "| Function | Status | Selection |",
        "|---|---|---|",
    ]
    lines.extend(f"| {row['desk_function']} | {row['status']} | {row['selection']} |" for row in functions)
    if ablation_rows:
        lines.extend([
            "",
            "## Factor-family incremental ablation",
            "",
            "| Excluded library | Net return | Delta vs full | Max drawdown | Sharpe | Sharpe delta | Fees |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        lines.extend(
            "| {excluded_library} | {net_return:.2%} | {net_return_difference:.2%} | {max_drawdown:.2%} | {sharpe_zero_rf_252:.4f} | {sharpe_difference:.4f} | {fees:.2f} |".format(
                excluded_library=row["excluded_library"],
                net_return=float(row["net_return"]),
                net_return_difference=float(row["net_return_difference"]),
                max_drawdown=float(row["max_drawdown"]),
                sharpe_zero_rf_252=float(row["sharpe_zero_rf_252"]),
                sharpe_difference=float(row["sharpe_difference"]),
                fees=float(row["fees"]),
            )
            for row in ablation_rows
        )
        lines.extend([
            "",
            "This is a fixed candidate-pool, fixed-cost, same-ledger diagnostic only; it is not causal attribution, model selection or execution approval.",
        ])
    if independent_sample:
        lines.extend([
            "",
            "## Independent sample comparison",
            "",
            f"- Window: `{independent_sample['evaluation_start']}..{independent_sample['evaluation_end']}`; selection cutoff `{independent_sample['selection_cutoff']}`",
            f"- Universe: `{independent_sample['target_universe_count']}`; basket `{independent_sample['target_basket_size']}`; paired rows `{independent_sample['provider_complete_paired_rows']}`; alternate provider `{independent_sample['alternate_provider']}`",
            "- This is a saved shared-ledger research diagnostic; it is not selection or execution approval.",
            "",
            "| Candidate | Net return | Max drawdown | Orders | Fills |",
            "|---|---:|---:|---:|---:|",
        ])
        for row in independent_sample["ledger_results"]:
            lines.append(
                "| {candidate} | {net_return:.2%} | {max_drawdown:.2%} | {orders} | {fills} |".format(
                    candidate=row.get("candidate"),
                    net_return=float(row.get("net_return") or 0),
                    max_drawdown=float(row.get("max_drawdown") or 0),
                    orders=row.get("orders"),
                    fills=row.get("fills"),
                )
            )
    lines.extend(["", "## Boundaries", ""])
    lines.extend(f"- {item}" for item in record["limitations"])
    (target / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    print(build(args.comparison, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
