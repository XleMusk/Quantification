from datetime import date, datetime, timezone
import csv
import importlib.util
import json
import sqlite3
import shutil
import subprocess
import uuid
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import replay_reports, settings, store
from .config import StrategyConfig
from .feed import HistoricalFeed, freeze
from .report import detail
from .research import paper_account_continuity as paper_continuity
from .research import paper_preview_view
from .research import research_comparison_view

router = APIRouter(prefix="/api/trading")


def _broker_capability_snapshot():
    """Expose declared broker boundaries without opening a client or account."""
    from .ths.broker import THSAdapter

    ths = THSAdapter({}).capabilities.to_dict()
    return {
        "ths": {
            **ths,
            "client_verified_this_call": False,
            "account_observed": False,
            "quotes_observed": False,
            "execution_admitted": False,
        }
    }


def manifest():
    path = settings.DATASET / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "missing"}


def _normalize_blockers(value):
    """Keep persisted blocker fields usable when older artifacts stored text.

    Review artifacts historically used a semicolon-delimited string while the
    API contract exposes blocker arrays.  Treating that string as an iterable
    silently produced one-character blockers in the selected-plan view.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(";") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _latest_stockdb_forward_compare(strategy_id=None):
    """Expose the newest fixed StockDB forward diagnostic to desk consumers.

    These receipts are persistent research artifacts rather than package
    pointers, so the selected-plan view must discover them without turning a
    diagnostic into a promotion or execution decision.
    """
    root = (settings.DATA_ROOT / "research" / "stockdb_forward_compare").resolve()
    if not root.is_dir():
        return {
            "status": "missing",
            "schema": None,
            "source_file": None,
            "research_only": True,
            "execution_admitted": False,
            "comparison": None,
            "candidates": [],
        }
    valid_payloads = []
    for path in sorted(root.glob("*/result.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("schema") != "stockdb_forward_compare_v1":
            continue
        valid_payloads.append((path, payload))

    selected_payload = None
    selected_path = None
    if strategy_id:
        for path, payload in valid_payloads:
            if any(
                isinstance(row, dict) and str(row.get("candidate") or "") == str(strategy_id)
                for row in payload.get("candidates") or []
            ):
                selected_path, selected_payload = path, payload
                break
    if selected_payload is None and valid_payloads:
        selected_path, selected_payload = valid_payloads[0]
    if selected_payload is not None:
        path, payload = selected_path, selected_payload
        candidates = []
        for row in payload.get("candidates") or []:
            if not isinstance(row, dict):
                continue
            candidates.append({
                "candidate": row.get("candidate"),
                "independent_candidate": bool(row.get("independent_candidate")),
                "start_date": row.get("start_date"),
                "end_date": row.get("end_date"),
                "target_symbols": row.get("target_symbols"),
                "observed_symbols": row.get("observed_symbols"),
                "missing_symbols": row.get("missing_symbols"),
                "coverage": row.get("coverage"),
                "coverage_complete": row.get("status") == "complete" and row.get("coverage") == 1.0,
                "equal_weight_forward_return": row.get("equal_weight_forward_return"),
                "target_qty_value_weighted_forward_return": row.get("target_qty_value_weighted_forward_return"),
                "status": row.get("status"),
            })
        comparison = payload.get("comparison") or {}
        return {
            "status": "completed_research_only",
            "schema": payload.get("schema"),
            "source_file": str(path),
            "source_cutoff": payload.get("source_cutoff"),
            "research_only": payload.get("research_only") is True,
            "execution_admitted": payload.get("execution_admitted") is True,
            "comparison": {
                "start_date": comparison.get("start_date"),
                "end_date": comparison.get("end_date"),
                "selection_cutoff": comparison.get("selection_cutoff"),
                "post_cutoff_forward_window": comparison.get("post_cutoff_forward_window") is True,
                "coverage_status": comparison.get("coverage_status"),
                "calendar_session_count": comparison.get("calendar_session_count"),
                "price_field": comparison.get("price_field"),
            },
            "candidates": candidates,
            "selected_strategy_match": bool(
                strategy_id
                and any(str(row.get("candidate") or "") == str(strategy_id) for row in candidates)
            ),
            "basis": "fixed StockDB raw-close forward diagnostic; no promotion or broker admission",
        }
    return {
        "status": "missing",
        "schema": None,
        "source_file": None,
        "research_only": True,
        "execution_admitted": False,
        "comparison": None,
        "candidates": [],
    }


def _latest_saved_comparison_diagnostic(strategy_id=None, anchor=None):
    """Expose the newest complete same-ledger comparison for the desk view.

    The package pointer remains the admission evidence.  This discovery view is
    deliberately research-only so newly produced candidate comparisons become
    visible without silently changing strategy selection or execution state.
    """
    root = (settings.ROOT / "outputs" / "research_comparisons").resolve()
    if not root.is_dir():
        return {"status": "missing", "source_file": None, "selected_strategy_match": False, "cases": []}
    anchor_start = str((anchor or {}).get("start") or "")
    anchor_end = str((anchor or {}).get("end") or "")
    anchor_costs = bool((anchor or {}).get("same_cost_settings"))
    payloads = []
    for path in sorted(root.glob("*/comparison.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        cases = payload.get("cases") or []
        if not isinstance(cases, list) or not cases:
            continue
        if payload.get("status") not in {"compared_saved_accounts", "compared"}:
            continue
        if payload.get("comparable_cases", 0) != payload.get("requested_cases", 0):
            continue
        if strategy_id and not any(
            isinstance(case, dict) and str(case.get("signal") or "") == str(strategy_id)
            for case in cases
        ):
            continue
        if anchor_start and str(payload.get("start") or "") != anchor_start:
            continue
        if anchor_end and str(payload.get("end") or "") != anchor_end:
            continue
        if anchor and bool(payload.get("same_cost_settings")) != anchor_costs:
            continue
        payloads.append((path, payload))
    if not payloads:
        return {"status": "missing", "source_file": None, "selected_strategy_match": False, "cases": []}
    path, payload = payloads[0]
    cases = []
    for case in payload.get("cases") or []:
        if not isinstance(case, dict):
            continue
        metrics = case.get("metrics") or {}
        cases.append({
            "case_id": case.get("case_id"),
            "family": case.get("family"),
            "signal": case.get("signal"),
            "label": case.get("label"),
            "status": case.get("status"),
            "net_return": metrics.get("net_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "sharpe_zero_rf_252": metrics.get("sharpe_zero_rf_252"),
            "fees": metrics.get("fees"),
            "stale_valuation_days": metrics.get("stale_valuation_days"),
            "descriptive_nondominated": bool(case.get("descriptive_nondominated")),
            "strategy_validated": bool(case.get("strategy_validated")),
            "execution_admitted": bool(case.get("execution_admitted")),
        })
    return {
        "status": "completed_research_only",
        "source_file": str(path),
        "start": payload.get("start"),
        "end": payload.get("end"),
        "sessions": payload.get("sessions", 0),
        "requested_cases": payload.get("requested_cases", 0),
        "comparable_cases": payload.get("comparable_cases", 0),
        "same_cost_settings": bool(payload.get("same_cost_settings")),
        "selected_strategy_match": bool(
            strategy_id and any(row.get("signal") == str(strategy_id) for row in cases)
        ),
        "selected_case": next(
            (row for row in cases if row.get("signal") == str(strategy_id)), None
        ),
        "cases": cases,
        "descriptive_nondominated_case_ids": payload.get("descriptive_nondominated_case_ids") or [],
        "strategy_validated": bool(payload.get("strategy_validated")),
        "execution_admitted": False,
        "research_only": True,
        "basis": "latest complete same-ledger comparison; diagnostic only, no promotion or broker admission",
    }


def _latest_fixed_idea_frontier():
    """Expose the latest fixed-rule research frontier without selecting a winner."""
    root = (settings.ROOT / "outputs" / "auto_research_frontiers").resolve()
    if not root.is_dir():
        return {"status": "missing", "source_file": None, "candidates": [], "execution_admitted": False}
    for path in sorted(root.glob("*/run.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("schema") != "quant_research_idea_frontier_run_v1":
            continue
        frontier = payload.get("frontier") or {}
        candidates = []
        for item in frontier.get("candidates") or []:
            if not isinstance(item, dict):
                continue
            latest = item.get("latest") or {}
            candidates.append({
                "candidate": item.get("candidate"),
                "runner": item.get("runner"),
                "description": item.get("description"),
                "status": item.get("status"),
                "latest_run": latest.get("run"),
                "net_return": latest.get("return"),
                "max_drawdown": latest.get("max_drawdown"),
                "sharpe_zero_rf_252": latest.get("sharpe_zero_rf_252"),
                "error": latest.get("error"),
                "research_only": True,
                "execution_admitted": False,
            })
        return {
            "status": "completed_research_only",
            "source_file": str(path),
            "source_run": frontier.get("source_run"),
            "source_period": frontier.get("source_period"),
            "candidates": candidates,
            "research_only": True,
            "fresh_holdout_claimed": bool(payload.get("fresh_holdout_claimed")),
            "execution_admitted": False,
            "basis": "fixed-rule idea frontier; diagnostic only, no automatic selection or broker admission",
        }
    return {"status": "missing", "source_file": None, "candidates": [], "execution_admitted": False}


def _latest_research_exception_view():
    """Expose the saved research exception register without reopening a run."""
    root = (settings.ROOT / "outputs" / "research_decision_matrices").resolve()
    if not root.is_dir():
        return {
            "schema": "research_data_exception_view_v1",
            "status": "missing",
            "source_file": None,
            "research_only": True,
            "execution_admitted": False,
            "resume_allowed": False,
            "open_items": [],
            "completed_session_frontier": 0,
            "operator_next_action": "build a decision matrix after a pinned research artifact exists",
        }
    for path in sorted(root.glob("*/run.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("schema") != "research_decision_matrix_v1":
            continue
        register = payload.get("research_data_exceptions") or {}
        if register.get("schema") != "research_data_exception_register_v1":
            continue
        return {
            "schema": "research_data_exception_view_v1",
            "status": register.get("status", "unknown"),
            "source_file": str(path),
            "matrix_run_id": payload.get("id"),
            "research_only": True,
            "execution_admitted": False,
            "resume_allowed": False,
            "latest_attempt": register.get("latest_attempt"),
            "latest_failed_attempt": register.get("latest_failed_attempt"),
            "last_successful_projection": register.get("last_successful_projection"),
            "completed_session_frontier": register.get("completed_session_frontier", 0),
            "open_items": register.get("open_items") or [],
            "resume_policy": register.get("resume_policy"),
            "operator_next_action": register.get("operator_next_action"),
            "basis": "latest saved decision matrix; read-only exception metadata",
        }
    return {
        "schema": "research_data_exception_view_v1",
        "status": "missing",
        "source_file": None,
        "research_only": True,
        "execution_admitted": False,
        "resume_allowed": False,
        "open_items": [],
        "completed_session_frontier": 0,
        "operator_next_action": "build a decision matrix after a pinned research artifact exists",
    }


def _latest_fixed_profitability_diagnostic():
    """Expose the newest fixed reporting-calendar profitability replay."""
    root = (settings.ROOT / "outputs" / "market_replay").resolve()
    if not root.is_dir():
        return {"status": "missing", "source_file": None, "candidates": [], "execution_admitted": False}
    for path in sorted(root.glob("*/run.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("research_family") != "economic_profitability_reporting_calendar_v1":
            continue
        if payload.get("status") != "completed_profitability_diagnostics":
            continue
        protocol = payload.get("protocol") or {}
        receipt = protocol.get("financial_receipt") or {}
        candidates = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            candidates.append({
                "signal": item.get("signal"),
                "status": item.get("status"),
                "sessions": item.get("sessions"),
                "net_return": item.get("net_return"),
                "max_drawdown": item.get("max_drawdown"),
                "sharpe_zero_rf_252": item.get("sharpe_zero_rf_252"),
                "fees": item.get("fees"),
                "stale_valuation_days": item.get("stale_valuation_days"),
                "orders": item.get("orders"),
                "fills": item.get("fills"),
                "research_only": True,
                "execution_admitted": False,
            })
        return {
            "status": "completed_research_only",
            "source_file": str(path),
            "research_family": payload.get("research_family"),
            "start": payload.get("start"),
            "end": payload.get("end"),
            "financial_receipt": {
                "symbols": receipt.get("symbols"),
                "report_rows": receipt.get("report_rows"),
                "declared_symbols": receipt.get("declared_symbols"),
                "symbols_with_reports": receipt.get("symbols_with_reports"),
                "publication_policy": receipt.get("publication_policy"),
                "revision_safe_pit_verified": bool(receipt.get("revision_safe_pit_verified")),
                "execution_admitted": False,
            },
            "candidates": candidates,
            "research_only": True,
            "fresh_holdout_claimed": bool(receipt.get("fresh_holdout_claimed")),
            "execution_admitted": False,
            "basis": "fixed reporting-calendar profitability replay; diagnostic only, no promotion or broker admission",
        }
    return {"status": "missing", "source_file": None, "candidates": [], "execution_admitted": False}


def _latest_alpha158_linear_diagnostic():
    """Expose the newest Alpha158 same-basis model comparison to desk consumers.

    This is deliberately a research receipt: the run uses frozen local inputs
    and a shared Engine/Ledger replay, but its evaluation segment is not a
    fresh holdout and it has no broker or execution admission.
    """
    root = (settings.ROOT / "outputs" / "market_replay").resolve()
    if not root.is_dir():
        return {"status": "missing", "source_file": None, "candidates": [], "execution_admitted": False}
    for path in sorted(root.glob("*/run.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("status") != "completed_alpha158_linear_diagnostics":
            continue
        if payload.get("research_family") != "alpha158_linear_ensemble":
            continue
        protocol = payload.get("protocol") or {}
        segments = protocol.get("segments") or {}
        candidates = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            candidates.append({
                "signal": item.get("signal"),
                "status": item.get("status"),
                "sessions": item.get("completed_sessions"),
                "net_return": item.get("net_return"),
                "max_drawdown": item.get("max_drawdown"),
                "sharpe_zero_rf_252": item.get("sharpe_zero_rf_252"),
                "fees": item.get("fees"),
                "turnover_value": item.get("turnover_value"),
                "orders": item.get("orders"),
                "fills": item.get("fills"),
                "stale_valuation_days": item.get("stale_valuation_days"),
                "research_only": True,
                "execution_admitted": False,
            })
        predictions = []
        for item in payload.get("prediction_diagnostics") or []:
            if not isinstance(item, dict):
                continue
            predictions.append({
                "signal": item.get("signal"),
                "prediction_rows": item.get("prediction_rows"),
                "ic_days": item.get("ic_days"),
                "mean_rank_ic": item.get("mean_rank_ic"),
                "ic_std": item.get("ic_std"),
                "hac_t_lag5": item.get("hac_t_lag5"),
            })
        return {
            "status": "completed_research_only",
            "source_file": str(path),
            "research_family": payload.get("research_family"),
            "protocol_id": protocol.get("id"),
            "dataset": payload.get("dataset"),
            "scope": payload.get("scope"),
            "symbols": payload.get("symbols"),
            "start": payload.get("start"),
            "end": payload.get("end"),
            "sessions": payload.get("sessions"),
            "matched_rows": payload.get("matched_rows"),
            "training_dates": payload.get("training_dates"),
            "last_training_label_exit": payload.get("last_training_label_exit"),
            "segments": {
                "train": segments.get("train"),
                "valid": segments.get("valid"),
                "test": segments.get("test"),
            },
            "model_sources": protocol.get("model_sources") or {},
            "comparisons": payload.get("comparisons") or [],
            "prediction_diagnostics": predictions,
            "candidates": candidates,
            "fresh_holdout_claimed": bool(protocol.get("fresh_holdout_claimed")),
            "strategy_validated": bool(payload.get("strategy_validated")),
            "research_only": True,
            "execution_admitted": False,
            "blockers": [
                "2026 evaluation is a development segment, not a fresh independent holdout",
                "PIT membership, corporate actions and source completeness remain unresolved",
                "research-only replay; no broker or account admission",
            ],
            "basis": "frozen Alpha158 feature inputs + fixed linear/all-seed comparison on shared Engine/Ledger",
        }
    return {"status": "missing", "source_file": None, "candidates": [], "execution_admitted": False}


def _latest_alpha158_lightgbm_diagnostic():
    """Expose the pinned Alpha158+LightGBM replay without granting admission."""
    root = (settings.ROOT / "outputs" / "factor_mining").resolve()
    if not root.is_dir():
        return {"status": "missing", "source_file": None, "candidates": [], "execution_admitted": False}
    accepted_kinds = {"alpha158_ingestion_ledger_comparison", "alpha158_baseline_comparison"}
    for path in sorted(root.glob("*/run.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("experiment_kind") not in accepted_kinds:
            continue
        if payload.get("status") not in {"completed", "failed_ledger_comparison"}:
            continue
        config = payload.get("config") or {}
        segments = config.get("segments") or {}
        candidates = []
        for item in payload.get("portfolio_metrics") or []:
            if not isinstance(item, dict) or item.get("variant") != "B0":
                continue
            seed = item.get("seed")
            if seed is None:
                continue
            candidates.append({
                "signal": f"alpha158_seed{int(seed)}",
                "model": "LightGBM",
                "features": 158,
                "seed": int(seed),
                "status": item.get("status"),
                "sessions": item.get("completed_sessions", item.get("sessions")),
                "net_return": item.get("net_return"),
                "max_drawdown": item.get("max_drawdown"),
                "sharpe_zero_rf_252": item.get("sharpe_zero_rf_252"),
                "fees": item.get("fees"),
                "turnover_value": item.get("turnover_value"),
                "orders": item.get("orders"),
                "fills": item.get("fills"),
                "stale_valuation_days": item.get("stale_valuation_days"),
                "research_only": True,
                "execution_admitted": False,
            })
        model_metrics = []
        for item in payload.get("model_metrics") or []:
            if not isinstance(item, dict) or item.get("variant") != "B0":
                continue
            model_metrics.append({
                "seed": item.get("seed"),
                "split": item.get("split"),
                "features": item.get("features"),
                "best_iteration": item.get("best_iteration"),
                "ic_days": item.get("ic_days"),
                "mean_rank_ic": item.get("mean_rank_ic"),
                "ic_std": item.get("ic_std"),
                "hac_t_lag5": item.get("hac_t_lag5"),
            })
        if not candidates:
            continue
        data_quality = payload.get("data_quality") or {}
        return {
            "status": "completed_research_only",
            "source_file": str(path),
            "experiment_kind": payload.get("experiment_kind"),
            "model": "LightGBM",
            "features": 158,
            "dataset": config.get("dataset"),
            "scope": (payload.get("dataset_manifest") or {}).get("scope"),
            "symbols": data_quality.get("symbols"),
            "start": segments.get("test", [None, None])[0],
            "end": segments.get("test", [None, None])[1],
            "sessions": candidates[0].get("sessions"),
            "seeds": sorted(row["seed"] for row in candidates),
            "model_metrics": model_metrics,
            "candidates": candidates,
            "paired_deltas": payload.get("paired_deltas") or [],
            "fresh_holdout_claimed": bool(payload.get("fresh_holdout_claimed")),
            "strategy_validated": bool(payload.get("strategy_validated")),
            "research_only": True,
            "execution_admitted": False,
            "blockers": [
                "2026 evaluation is a development segment, not a fresh independent holdout",
                "PIT membership, corporate actions and source completeness remain unresolved",
                "high-turnover model replay; no broker or account admission",
            ],
            "basis": "pinned Qlib Alpha158 features + existing LightGBM seed replays on shared Engine/Ledger",
        }
    return {"status": "missing", "source_file": None, "candidates": [], "execution_admitted": False}


@router.get("/research/candidate-evidence")
def research_candidate_evidence():
    """Project saved registry/evidence only; never refresh research or accounts."""
    from .research_evidence_catalog import read_catalog
    return read_catalog(settings.ROOT, settings.DATA_ROOT)


@router.get("/research/leader-comparison")
def leader_research_comparison():
    """Latest fixed leader comparison; failures stay visible and never select orders."""
    root = settings.ROOT / "outputs" / "leader_daily_replay"
    paths = sorted(root.glob("*/result.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        return {"status": "missing", "source_file": None, "cases": [],
                "candidate_panel_only": True, "strategy_validated": False,
                "selection_enabled": False, "execution_admitted": False}
    path = paths[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"最新龙头研究结果尚不可读取：{error}") from error
    if (payload.get("schema") != "leader_daily_comparison_v2"
            or payload.get("candidate_panel_only") is not True
            or payload.get("execution_admitted") is not False
            or payload.get("strategy_validated") is not False
            or payload.get("selection_enabled") is not False):
        raise HTTPException(409, "龙头研究结果缺少候选面板边界")
    return {**payload, "source_file": str(path.resolve()), "research_only": True,
            "automatic_promotion": False, "execution_admitted": False}


@router.get("/research/market-scoreboard")
def market_forecast_scoreboard():
    """Read registered predictions and outcomes without fitting or updating them."""
    path = settings.ROOT / "outputs" / "market_regime_scoreboard" / "scoreboard.json"
    if not path.is_file():
        return {"status": "missing", "source_file": None, "summary_by_horizon": {},
                "selection_enabled": False, "strategy_validated": False, "execution_admitted": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"大盘事前核验结果尚不可读取：{error}") from error
    if (payload.get("schema") != "market_regime_scoreboard_v1"
            or payload.get("execution_admitted") is not False
            or payload.get("model_refit") is not False):
        raise HTTPException(409, "大盘核验结果缺少冻结模型与研究边界")
    return {**payload, "status": "saved_forecast_outcome_audit", "source_file": str(path.resolve()),
            "research_only": True, "automatic_promotion": False, "selection_enabled": False,
            "strategy_validated": False, "execution_admitted": False}


@router.get("/research/leader-close-publication")
def latest_leader_close_publication():
    """Read saved complete rule weights; never run a strategy or create an order."""
    from .research.leader_close_publication_view import leader_close_publication_view
    try:
        return leader_close_publication_view(settings.ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"最新规则目标发布证据尚不可读取：{error}") from error


@router.get("/research/close-workbench")
def latest_close_workbench():
    """Saved source progress and exact preview; no collection, replay or account write."""
    from .research.close_workbench import close_workbench
    try:
        return close_workbench(settings.DATA_ROOT, settings.ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"最新收盘处理证据尚不可读取：{error}") from error


@router.get("/research/recent-alpha158")
def latest_recent_alpha158():
    """Read latest saved frozen scores/publication/head without starting work."""
    from .research.recent_alpha158_view import recent_alpha158_view
    try:
        return recent_alpha158_view(settings.DATA_ROOT, project_root=settings.ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"新来源 Alpha158 批次证据尚不可读取：{error}") from error


@router.get("/research/recent-shortline")
def latest_recent_shortline():
    """Read saved new-source shortline counts; never train, replay or select."""
    from .research.recent_shortline_view import recent_shortline_view
    try:
        return recent_shortline_view(settings.ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"新来源短线批次证据尚不可读取：{error}") from error


@router.get("/research/native-preopen")
def native_preopen_status():
    """Read saved overnight plans and isolated checkpoints without advancing them."""
    from .research.shortline_native_preopen_status import native_preopen_status as read_status
    return read_status(settings.DATA_ROOT, settings.ROOT)


@router.get("/research/opening-daily")
def saved_opening_daily_status():
    """Project saved opening pointers; never register, run, recover or trade."""
    from .research.opening_daily_view import opening_daily_view
    try:
        return opening_daily_view(settings.DATA_ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"每日纸面开盘方案尚不可读取：{error}") from error


@router.get("/research/opening-daily/{route_id}")
def saved_opening_daily_detail(route_id: str):
    """Read one registered route's saved cases; never accept a file path."""
    from .research.opening_daily_detail import opening_daily_detail
    try:
        return opening_daily_detail(settings.DATA_ROOT, route_id)
    except KeyError as error:
        if error.args == ("Unknown registered opening route",):
            raise HTTPException(404, "未登记这个纸面方案路线") from error
        raise HTTPException(409, f"逐案例纸面方案字段不完整：{error}") from error
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise HTTPException(409, f"逐案例纸面方案尚不可读取：{error}") from error


@router.get("/research/opening-daily/{route_id}/cases/{case_id}/ledger")
def saved_opening_daily_ledger(route_id: str, case_id: str):
    """Read one pinned completed research book, separate from its next plan."""
    from .research.opening_daily_ledger import opening_daily_ledger
    try:
        return opening_daily_ledger(settings.DATA_ROOT, route_id, case_id)
    except KeyError as error:
        if error.args in (("Unknown registered opening route",), ("Unknown registered opening case",),
                          ("Unknown or ambiguous case in the registered opening route",)):
            raise HTTPException(404, "未登记这个纸面路线或案例") from error
        raise HTTPException(409, f"已保存模拟账本字段不完整：{error}") from error
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise HTTPException(409, f"已保存模拟账本尚不可读取：{error}") from error


@router.get("/research/opening-daily/{route_id}/cases/{case_id}/ths-review")
def saved_opening_ths_review(route_id: str, case_id: str):
    """Read one research selection and missing THS evidence; never hydrate orders.

    GET accepts no account, quote, file path or execution options. Evidence
    comparison is an explicit offline CLI operation, not a page-read effect.
    """
    from .ths.opening_review import build_review
    try:
        return build_review(settings.DATA_ROOT, route_id, case_id)
    except KeyError as error:
        if error.args in (("Unknown registered opening route",), ("Unknown registered opening case",)):
            raise HTTPException(404, "未登记这个纸面路线或案例") from error
        raise HTTPException(409, f"账户复核来源字段不完整：{error}") from error
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise HTTPException(409, f"账户复核来源尚不可读取：{error}") from error


@router.get("/research/native-preopen-comparison")
def native_preopen_comparison():
    """Compare saved independent policies without generating or consuming either plan."""
    from .research.shortline_native_preopen_status import native_preopen_comparison as read_comparison
    return read_comparison(settings.DATA_ROOT, settings.ROOT)


def _saved_trade_case_index():
    from .research.trade_case_learning_index import TradeCaseLearningIndex
    root = Path(settings.ROOT) / "outputs/trade_case_learning"
    return TradeCaseLearningIndex(
        root / "20261015_initial_v1/casebook_v19/manifest.json",
        root / "20261016_source1015_pending_v1/progress_T_completed.json",
    )


@router.get("/research/trade-case-learning")
def trade_case_learning(symbol: str | None = None, day: str | None = None,
                        scope: str | None = None, kind: str | None = None,
                        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    """Search saved interpretations; never collect, rebuild a casebook or trade."""
    try:
        return _saved_trade_case_index().list_cases(
            symbol=symbol.strip().upper() if symbol else None, day=day or None,
            scope=scope or None, kind=kind or None, offset=offset, limit=limit)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"已保存逐案资料尚不可读取：{error}") from error


@router.get("/research/trade-case-learning/{case_id}")
def trade_case_learning_detail(case_id: str):
    """Open one manifest-bound fact/interpretation without accepting a file path."""
    try:
        index = _saved_trade_case_index()
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"已保存逐案索引尚不可读取：{error}") from error
    try:
        return index.detail(case_id)
    except KeyError as error:
        if error.args == (case_id,):
            raise HTTPException(404, "已保存索引中没有这个案例") from error
        raise HTTPException(409, f"该案例的保存字段不完整：{error}") from error
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise HTTPException(409, f"该案例的原事实或解读尚不可读取：{error}") from error


@router.get("/research/leader-exit-hook")
def latest_leader_exit_hook():
    """Read the saved five-family exit comparison; never launch a replay."""
    from .research.leader_exit_hook_view import leader_exit_hook_view
    try:
        return leader_exit_hook_view(settings.ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"独立退出历史诊断尚不可读取：{error}") from error


@router.get("/research/leader-eight-type-paper")
def latest_leader_eight_type_paper():
    """Read all saved unbound type plans without selecting or running a strategy."""
    from .research.leader_eight_type_paper_view import leader_eight_type_paper_view
    try:
        result = leader_eight_type_paper_view(settings.ROOT)
        from .research.leader_candidate_intraday_view import leader_candidate_intraday_view
        try:
            result["intraday_tracking"] = leader_candidate_intraday_view(settings.ROOT, settings.DATA_ROOT, paper=result)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            result["intraday_tracking"] = {"status": "unavailable", "error": str(error),
                "provider_contacted": False, "engine_advanced": False, "execution_admitted": False}
        return result
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"八型纸面方案尚不可读取：{error}") from error


@router.get("/research/leader-eight-type-positions")
def latest_leader_eight_type_positions():
    """Read saved isolated research fills/positions/risk facts; never run a strategy."""
    from .research.leader_eight_type_position_view import leader_eight_type_position_view
    try:
        from .research.leader_position_intraday_view import attach_saved_position_quotes
        return attach_saved_position_quotes(leader_eight_type_position_view(settings.DATA_ROOT), settings.DATA_ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"八型研究持仓尚不可读取：{error}") from error


@router.get("/research/leader-eight-type-history")
def saved_leader_eight_type_history(window_id: str | None = None, case_id: str | None = None):
    """Read one saved historical case projection; never build or advance it."""
    from .research.leader_eight_type_history_view import read_history
    try:
        return read_history(settings.DATA_ROOT, window_id=window_id, case_id=case_id)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"八型历史环境与持仓响应尚不可读取：{error}") from error


@router.get("/research/leader-identity")
def leader_identity_view():
    """Read the fixed prospective cohort without collecting or registering it."""
    from .research.leader_identity_view import leader_identity_tracking_view
    try:
        return leader_identity_tracking_view(settings.DATA_ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"固定龙股身份观察尚不可读取：{error}") from error


@router.get("/research/leader-cohort-intraday")
def leader_cohort_intraday():
    """Read saved public quotes of the fixed cohort; never collect or generate orders."""
    from .research.leader_cohort_quote_view import leader_cohort_intraday_view
    try:
        return leader_cohort_intraday_view(settings.DATA_ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"固定龙股盘中观察尚不可读取：{error}") from error


@router.get("/research/market-roster-intraday")
def market_roster_intraday():
    """Read one prior-dated roster snapshot; never collect or select securities."""
    from .research.market_roster_quote_view import market_roster_quote_view
    try:
        return market_roster_quote_view(settings.DATA_ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"前日名册盘中观察尚不可读取：{error}") from error


@router.get("/research/leader-intraday-panel")
def leader_intraday_panel():
    """Read the fixed saved forward panel; never register, advance or collect."""
    from .research.leader_intraday_path_panel_view import leader_intraday_path_panel_view
    try:
        return leader_intraday_path_panel_view(settings.DATA_ROOT)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"固定龙型事前后继观察尚不可读取：{error}") from error


@router.get("/research/leader-environment")
def leader_environment_view(display_limit: int = Query(10, ge=1, le=100)):
    """Newest dated descriptive analysis, without running a model or choosing orders."""
    from .research.index_quote_view import index_quote_observation
    paths = sorted((settings.ROOT / "outputs" / "leader_environment").glob("*/result.json"),
                   key=lambda path: path.parent.name, reverse=True)
    boundary = {"candidate_panel_only": True, "strategy_validated": False,
                "selection_enabled": False, "execution_admitted": False,
                "orders_generated": False, "ledger_mutated": False}
    read_at = datetime.now().astimezone().isoformat()
    if not paths:
        return {"status": "waiting_for_complete_close_analysis", "analysis": None,
                "read_at": read_at, "source_file": None, **boundary}
    path = paths[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"最新龙头环境分析尚不可读取：{error}") from error
    if (payload.get("schema") != "leader_environment_analysis_run_v1"
            or any(payload.get(key) is not value for key, value in boundary.items())
            or any(payload.get("analysis", {}).get(key) is not value for key, value in boundary.items())
            or payload.get("day") != payload.get("analysis", {}).get("day")):
        raise HTTPException(409, "龙头环境分析缺少日期或研究边界")
    for archetype in payload["analysis"].get("archetypes", []):
        candidates = archetype.get("candidates") or []
        archetype["stored_candidate_count"] = len(candidates)
        archetype["candidates"] = candidates[:display_limit]
    return {**payload, "read_at": read_at, "source_file": str(path.resolve()),
            "intraday_index_observation": index_quote_observation(settings.ROOT, data_root=settings.DATA_ROOT),
            "display_limit": display_limit, "display_is_not_portfolio_selection": True,
            "intraday_observation_verified": False,
            "source_time_context": "complete_close_only_not_current_intraday",
            **boundary}


def _research_evidence_consumer_summary(selected_strategy_id=None):
    """Keep non-selected-plan desk consumers aligned with research boundaries."""
    linear = _latest_alpha158_linear_diagnostic()
    lightgbm = _latest_alpha158_lightgbm_diagnostic()

    def compact(diagnostic, model=None):
        candidates = diagnostic.get("candidates") or []
        selected = next(
            (
                row for row in candidates
                if selected_strategy_id
                and str(row.get("signal") or "") == str(selected_strategy_id)
            ),
            None,
        )
        return {
            "status": diagnostic.get("status"),
            "model": model or diagnostic.get("model"),
            "source_file": diagnostic.get("source_file"),
            "candidate_count": len(candidates),
            "candidate_signals": [row.get("signal") for row in candidates],
            "selected_strategy_id": selected_strategy_id,
            "selected_strategy_match": selected is not None,
            "selected_candidate": selected,
            "start": diagnostic.get("start"),
            "end": diagnostic.get("end"),
            "sessions": diagnostic.get("sessions"),
            "fresh_holdout_claimed": bool(diagnostic.get("fresh_holdout_claimed")),
            "research_only": True,
            "execution_admitted": False,
            "blockers": diagnostic.get("blockers") or [],
        }

    blockers = []
    for diagnostic in (linear, lightgbm):
        blockers.extend(diagnostic.get("blockers") or [])
    return {
        "status": "research_only",
        "selected_strategy_id": selected_strategy_id,
        "alpha158_linear": compact(linear, "fixed_linear_ensemble"),
        "alpha158_lightgbm": compact(lightgbm, "LightGBM"),
        "leader_daily": leader_research_comparison(),
        "market_scoreboard": market_forecast_scoreboard(),
        "blockers": list(dict.fromkeys(blockers)),
        "automatic_promotion": False,
        "execution_admitted": False,
        "basis": "latest fixed local replay receipts; no fresh holdout, broker admission or account admission",
    }


@router.get("/overview")
def overview():
    return {"worker": store.health(), "dataset": manifest(), "runs": store.runs(),
            "default_config": StrategyConfig().to_dict(), "server_date": date.today().isoformat(),
            "broker": {"sdk_installed": importlib.util.find_spec("xtquant") is not None, "connected": False,
                       "live_enabled": False, "status": "not_authorized",
                       "capabilities": _broker_capability_snapshot()},
            "ths_native_summary": ths_native_summary(),
            "ths_native_grids": ths_native_grids(),
            "ths_readonly_evidence": ths_readonly_evidence(),
            "ths_native_observation": ths_native_observation(),
            "paper_preview_latest": paper_preview_latest(),
            "data_root": str(settings.DATA_ROOT)}


@router.get("/broker-capabilities")
def broker_capabilities():
    """Return declared adapter capabilities; this endpoint performs no broker I/O."""
    return {
        "schema": "broker_capability_view_v1",
        "status": "declared_capabilities_only",
        "client_verified_this_call": False,
        "account_observed": False,
        "quotes_observed": False,
        "execution_admitted": False,
        "channels": _broker_capability_snapshot(),
    }


@router.get("/opening-window-status")
def opening_window_status():
    """Expose the idempotent opening coordinator state without client or ledger I/O."""
    root = settings.DATA_ROOT / "research" / "opening_session_runs"
    trading_date = date.today().isoformat()
    path = root / trading_date / "opening_window_coordinator.json"
    if not path.is_file():
        return {
            "schema": "opening_window_status_view_v1",
            "status": "not_started",
            "trading_date": trading_date,
            "source_file": str(path),
            "execution_admitted": False,
            "client_interacted": False,
            "network_probe_attempted": False,
            "ledger_mutated": False,
            "submission_attempted": False,
            "next_action": "等待开盘协调器首次运行",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {
            "schema": "opening_window_status_view_v1",
            "status": "invalid_receipt",
            "trading_date": trading_date,
            "source_file": str(path),
            "error": str(error),
            "execution_admitted": False,
            "client_interacted": False,
            "network_probe_attempted": False,
            "ledger_mutated": False,
            "submission_attempted": False,
        }
    if payload.get("schema") != "opening_window_coordinator_v1":
        return {
            "schema": "opening_window_status_view_v1",
            "status": "unsupported_receipt",
            "trading_date": trading_date,
            "source_file": str(path),
            "receipt_schema": payload.get("schema"),
            "execution_admitted": False,
            "client_interacted": False,
            "network_probe_attempted": False,
            "ledger_mutated": False,
            "submission_attempted": False,
        }
    return {
        "schema": "opening_window_status_view_v1",
        **payload,
        "source_file": str(path),
        "execution_admitted": False,
        "ledger_mutated": False,
        "submission_attempted": False,
    }


@router.get("/ths-native-summary")
def ths_native_summary():
    """Expose the newest native THS summary receipt without treating it as a full account read."""
    root = settings.DATA_ROOT / "ths"
    candidates = sorted(
        (path for path in root.glob("native_summary_*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {
            "schema": "ths_native_account_summary_view_v1",
            "status": "not_observed",
            "source": "ths_controls_summary",
            "execution_admitted": False,
            "client_operation": False,
            "positions_read": False,
            "orders_read": False,
            "trades_read": False,
            "full_account_observation": False,
            "next_action": "Put the exact inspected THS account window in foreground and run capture-summary; then read the holdings, orders and trades pages separately.",
        }
    path = candidates[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {
            "schema": "ths_native_account_summary_view_v1",
            "status": "invalid_receipt",
            "source_file": str(path),
            "error": str(error),
            "execution_admitted": False,
            "client_operation": False,
        }
    if payload.get("schema") != "ths_native_account_summary_v1":
        return {
            "schema": "ths_native_account_summary_view_v1",
            "status": "unsupported_receipt",
            "source_file": str(path),
            "receipt_schema": payload.get("schema"),
            "execution_admitted": False,
            "client_operation": False,
        }
    return {
        "schema": "ths_native_account_summary_view_v1",
        "status": payload.get("status", "read"),
        "source": payload.get("source", "ths_controls_summary"),
        "source_file": str(path),
        "observed_at": payload.get("observed_at"),
        "account_alias": payload.get("account_alias"),
        "metrics_raw": payload.get("metrics_raw") or {},
        "positions_read": False,
        "orders_read": False,
        "trades_read": False,
        "full_account_observation": False,
        "client_interacted": bool(payload.get("client_interacted")),
        "submitted": False,
        "execution_admitted": False,
        "next_action": "Use this summary for display only; capture the holdings, orders and trades views or import the four explicit THS exports before sizing or reconciliation.",
    }


@router.get("/ths-readonly-evidence")
def ths_readonly_evidence():
    """Expose the newest static THS evidence index without importing it."""
    root = settings.DATA_ROOT / "ths"
    candidates = sorted(
        (path for path in root.rglob("readonly_evidence_index_v1.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    base = {
        "schema": "ths_readonly_evidence_view_v1",
        "status": "not_observed",
        "execution_admitted": False,
        "ledger_mutated": False,
        "submitted": False,
    }
    if not candidates:
        return {**base, "next_action": "尚无静态同花顺只读证据索引。"}
    path = candidates[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {**base, "status": "invalid_receipt", "source_file": str(path), "error": str(error)}
    if payload.get("schema") != "ths_readonly_evidence_index_v1":
        return {**base, "status": "unsupported_receipt", "source_file": str(path),
                "receipt_schema": payload.get("schema")}
    return {
        **payload,
        "schema": "ths_readonly_evidence_view_v1",
        "source_file": str(path),
        "execution_admitted": False,
        "ledger_mutated": False,
        "submitted": False,
    }


def _ths_client_session_status():
    """Check the pinned THS process/window identity without focusing or navigating it."""
    root = settings.DATA_ROOT / "ths"
    profiles = sorted(
        (path for path in root.glob("summary_profile_*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not profiles:
        return {"status": "profile_not_observed", "identity_verified": False, "account_page_verified": False}
    path = profiles[0]
    try:
        profile = json.loads(path.read_text(encoding="utf-8-sig"))
        from .ths.windows import probe
        snapshot = probe()
    except (OSError, UnicodeError, json.JSONDecodeError, ImportError, RuntimeError) as error:
        return {
            "status": "probe_failed",
            "profile_file": str(path),
            "identity_verified": False,
            "account_page_verified": False,
            "error": str(error),
        }
    target = profile.get("target") or {}
    window = next((item for item in snapshot.get("windows", []) if int(item.get("hwnd", 0)) == int(target.get("hwnd", -1))), None)
    process = next((item for item in snapshot.get("processes", []) if int(item.get("pid", 0)) == int(target.get("pid", -1))), None)
    identity_verified = bool(
        window
        and process
        and int(window.get("pid", -1)) == int(target.get("pid", -2))
        and window.get("class_name") == target.get("class_name")
        and process.get("version") == target.get("version")
    )
    return {
        "status": "verified_client_window" if identity_verified else "profile_target_mismatch",
        "profile_file": str(path),
        "target": {key: target.get(key) for key in ("hwnd", "pid", "version", "class_name")},
        "window": window,
        "process": process,
        "identity_verified": identity_verified,
        "account_page_verified": False,
        "read_only": True,
        "next_action": "将已验证实例切换到明确的账户、委托或成交页后，再分别读取对应网格；当前标题只能证明客户端窗口身份。",
    }


@router.get("/ths-native-grids")
def ths_native_grids():
    """Expose the latest read-only THS virtual-grid captures without importing rows."""
    root = settings.DATA_ROOT / "ths"
    candidates = sorted(
        (path for path in root.glob("*_grid_receipt_*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    latest = {}
    invalid = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            invalid.append({"source_file": str(path), "error": str(error)})
            continue
        kind = str(payload.get("kind") or "unknown")
        if kind in latest:
            continue
        candidate = None
        candidate_paths = sorted(
            (item for item in root.glob(f"{kind}_ocr_candidate_*.json") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if candidate_paths:
            candidate_path = candidate_paths[0]
            try:
                candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8-sig"))
                candidate = {
                    "source_file": str(candidate_path),
                    "status": candidate_payload.get("status"),
                    "candidate_state": candidate_payload.get("candidate_state"),
                    "row_count": len(candidate_payload.get("rows") or []),
                    "rows": candidate_payload.get("rows") or [],
                    "checks": candidate_payload.get("checks") or {},
                    "structured_rows": bool(candidate_payload.get("structured_rows")),
                    "semantic_verified": bool(candidate_payload.get("semantic_verified")),
                }
            except (OSError, UnicodeError, json.JSONDecodeError):
                candidate = {"source_file": str(candidate_path), "status": "invalid_receipt"}
        latest[kind] = {
            "schema": payload.get("schema"),
            "status": payload.get("status"),
            "source": payload.get("source"),
            "source_file": str(path),
            "image_path": payload.get("image_path"),
            "observed_at": payload.get("observed_at"),
            "grid_handle": payload.get("grid_handle"),
            "grid_pid": payload.get("grid_pid"),
            "width": payload.get("width"),
            "height": payload.get("height"),
            "structured_rows": bool(payload.get("structured_rows")),
            "semantic_verified": bool(payload.get("semantic_verified")),
            "ocr_candidate": candidate,
            "read_only": bool(payload.get("read_only")),
            "focused": bool(payload.get("focused")),
            "submitted": bool(payload.get("submitted")),
        }
    return {
        "schema": "ths_native_grid_view_v1",
        "status": "read_only_evidence" if latest else "not_observed",
        "grids": latest,
        "invalid_receipts": invalid,
        "rows_imported": False,
        "ledger_mutated": False,
        "submitted": False,
        "execution_admitted": False,
        "next_action": "Use the image for operator review; import an explicit structured THS export before reconciliation or order sizing.",
    }


@router.get("/ths-native-observation")
def ths_native_observation():
    """Combine current THS read-only evidence into a non-admitting observation candidate."""
    summary = ths_native_summary()
    grids = ths_native_grids()
    readonly_evidence = ths_readonly_evidence()
    structured_package = _latest_structured_observation_package()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    holdings = (grids.get("grids") or {}).get("holdings") or {}
    candidate = holdings.get("ocr_candidate") or {}

    # Prefer the newest indexed evidence as one coherent read-only view. The
    # legacy top-level receipts remain a fallback for installations that have
    # not produced an index yet; they must not override a newer nested capture.
    indexed_summary = readonly_evidence.get("summary") or {}
    if readonly_evidence.get("status") == "partial_readonly_evidence" and indexed_summary:
        summary = {
            "schema": "ths_native_account_summary_view_v1",
            "status": indexed_summary.get("status", "read"),
            "source": "readonly_evidence_index",
            "source_file": indexed_summary.get("path"),
            "observed_at": indexed_summary.get("observed_at"),
            "account_alias": readonly_evidence.get("account_alias"),
            "metrics_raw": indexed_summary.get("metrics_raw") or {},
            "positions_read": bool(indexed_summary.get("positions_read")),
            "orders_read": bool(indexed_summary.get("orders_read")),
            "trades_read": bool(indexed_summary.get("trades_read")),
            "full_account_observation": bool(indexed_summary.get("full_account_observation")),
            "client_interacted": True,
            "submitted": False,
            "execution_admitted": False,
            "next_action": "Use the indexed receipts for display only; import explicit THS exports before sizing or reconciliation.",
        }
        indexed_holdings = ((readonly_evidence.get("holdings") or {}).get("candidate") or {})
        candidate_path = indexed_holdings.get("path")
        if candidate_path and Path(candidate_path).is_file():
            try:
                indexed_payload = json.loads(Path(candidate_path).read_text(encoding="utf-8-sig"))
                if isinstance(indexed_payload, dict):
                    candidate = indexed_payload
            except (OSError, UnicodeError, json.JSONDecodeError):
                # Keep the already captured legacy candidate if the indexed
                # candidate file has become unavailable or malformed.
                pass

    checks = candidate.get("checks") or {}
    holdings_ready = bool(
        candidate.get("candidate_state") == "symbol_rows_detected"
        and checks.get("summary_cross_checks_pass") is True
    )
    missing = []
    if summary.get("status") not in {"read", "read_background"}:
        missing.append("account_summary")
    if not holdings_ready:
        missing.append("verified_structured_holdings")
    missing.extend(["cash_total_and_cash_frozen", "structured_orders", "structured_trades"])
    return {
        "schema": "ths_native_observation_candidate_v1",
        "status": "candidate_only" if summary.get("status") in {"read", "read_background"} else "incomplete",
        "client_session": _ths_client_session_status(),
        "account_summary": summary,
        "readonly_evidence": readonly_evidence,
        "structured_observation_package": structured_package,
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "order_count": structured_reconciliation.get("order_count"),
        "trade_count": structured_reconciliation.get("trade_count"),
        "cashflow_count": structured_reconciliation.get("cashflow_count"),
        "structured_package_ready_for_ledger_reconciliation": bool(
            structured_package.get("ready_for_ledger_reconciliation")
        ),
        "position_candidate": {
            "rows": candidate.get("rows") or [],
            "row_count": candidate.get("row_count", 0),
            "candidate_state": candidate.get("candidate_state"),
            "checks": checks,
            "structured": False,
            "semantic_verified": False,
        },
        "missing_evidence": missing,
        "full_account_observation": False,
        "orders_read": False,
        "trades_read": False,
        "ledger_mutated": False,
        "client_interacted": False,
        "submitted": False,
        "execution_admitted": False,
        "next_action": "Use as a display candidate only; require complete cash, structured positions, orders and trades before sizing, reconciliation or prefill.",
    }


def _observation_age_seconds(observed_at):
    if not observed_at:
        return None
    try:
        stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds())


def _latest_gui_prefill_profile_status():
    """Report the newest local GUI map without opening or probing THS."""
    root = (settings.DATA_ROOT / "ths").resolve()
    candidates = sorted(root.glob("*profile*.json"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    from .ths.adapter import gui_prefill_profile_status
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return {"path": str(path), **gui_prefill_profile_status(payload)}
    return {"path": None, **gui_prefill_profile_status({})}


@router.get("/ths-observation-health")
def ths_observation_health():
    """Summarize saved THS observation freshness and gaps without broker I/O."""
    native = ths_native_observation()
    structured = _latest_structured_observation_package()
    same_ledger = _same_ledger_bundle_reconciliation_view()
    journal = _ths_journal_reconciliation_view()
    prefill_profile = _latest_gui_prefill_profile_status()
    client_session = _ths_client_session_status()
    max_age_seconds = 30 * 60
    observed_at = native.get("account_summary", {}).get("observed_at")
    age_seconds = _observation_age_seconds(observed_at)
    alerts = []
    if age_seconds is None:
        alerts.append("native_account_observation_missing")
    elif age_seconds > max_age_seconds:
        alerts.append("native_account_observation_stale")
    if not structured.get("ready_for_ledger_reconciliation"):
        alerts.append("structured_ths_bundle_incomplete")
    if not structured.get("orders_observed"):
        alerts.append("structured_orders_missing")
    if not structured.get("trades_observed"):
        alerts.append("structured_trades_missing")
    if not structured.get("cashflows_observed"):
        alerts.append("structured_cashflows_missing")
    if same_ledger.get("status") in {"reconciliation_required", "structured_package_not_ready"}:
        alerts.append("same_ledger_reconciliation_required")
    elif same_ledger.get("status") == "isolated_rehearsal_only":
        alerts.append("persistent_ths_export_required")
    if prefill_profile["status"] != "ready":
        alerts.append("ths_gui_prefill_profile_unverified")
    if client_session.get("status") == "profile_target_mismatch":
        alerts.append("ths_profile_target_mismatch")
    elif client_session.get("status") == "probe_failed":
        alerts.append("ths_client_probe_failed")
    alert_specs = {
        "native_account_observation_missing": ("critical", "native_account", "保存同一观察时点的账户导出"),
        "native_account_observation_stale": ("high", "native_account", "重新读取当前交易日账户摘要"),
        "structured_ths_bundle_incomplete": ("critical", "structured_bundle", "补齐账户、委托、成交、累计资金流水四表"),
        "structured_orders_missing": ("high", "structured_bundle", "导出同花顺委托表；空表也须显式导出"),
        "structured_trades_missing": ("high", "structured_bundle", "导出同花顺成交表；不能从空网格推断零成交"),
        "structured_cashflows_missing": ("high", "structured_bundle", "导出每个合同号的累计资金流水和实际费用"),
        "same_ledger_reconciliation_required": ("critical", "same_ledger", "解决订单、成交、费用或现金差异后再预检"),
        "persistent_ths_export_required": ("critical", "same_ledger", "用持久用户导出替换隔离演练包"),
        "ths_gui_prefill_profile_unverified": ("high", "ths_prefill", "提供经过本机验证的 fields.symbol/price/qty 和账户/页面 guards"),
        "ths_profile_target_mismatch": ("high", "ths_client", "重新验证当前同花顺实例和账户页面映射；不要使用旧窗口句柄"),
        "ths_client_probe_failed": ("high", "ths_client", "先处理同花顺只读探针故障，再读取账户和订单证据"),
    }
    alert_records = [
        {"code": code, "severity": alert_specs.get(code, ("high", "observation", "人工复核"))[0],
         "source": alert_specs.get(code, ("high", "observation", "人工复核"))[1],
         "next_action": alert_specs.get(code, ("high", "observation", "人工复核"))[2]}
        for code in dict.fromkeys(alerts)
    ]
    return {
        "schema": "ths_observation_health_v1",
        "status": "attention_required" if alerts else "read_only_observation_healthy",
        "transport": {
            "enabled": False,
            "broker_io_attempted": False,
            "client_interacted": False,
            "submission_enabled": False,
        },
        "freshness": {
            "observed_at": observed_at,
            "age_seconds": age_seconds,
            "stale_after_seconds": max_age_seconds,
            "status": "missing" if age_seconds is None else
            "stale" if age_seconds > max_age_seconds else "fresh",
        },
        "native_observation": {
            "status": native.get("status"),
            "source_scope": (native.get("structured_observation_package") or {}).get("source_scope"),
            "account_summary_status": (native.get("account_summary") or {}).get("status"),
            "orders_read": bool(native.get("orders_read")),
            "trades_read": bool(native.get("trades_read")),
        },
        "structured_package": {
            "status": structured.get("status"),
            "source_scope": structured.get("source_scope"),
            "ready_for_ledger_reconciliation": bool(
                structured.get("ready_for_ledger_reconciliation")
            ),
            "row_counts": structured.get("row_counts") or {},
        },
        "prefill_profile": prefill_profile,
        "client_session": client_session,
        "same_ledger": {
            "status": same_ledger.get("status"),
            "issue_count": (same_ledger.get("summary") or {}).get("issue_count", 0),
            "issues": same_ledger.get("issues") or [],
            "ledger_mutated": bool(same_ledger.get("ledger_mutated")),
            "journal_status": journal.get("status"),
            "detail": same_ledger,
        },
        "alerts": list(dict.fromkeys(alerts)),
        "alert_records": alert_records,
        "execution_admitted": False,
        "ledger_mutated": False,
        "next_action": (
            "保存完整的同花顺账户、委托、成交和资金流水导出，再运行结构化包对账"
            if alerts else "保留当前只读观测；不需要任何券商写操作"
        ),
    }


def _latest_paper_preview_path():
    return paper_preview_view.latest_receipt_path(settings.DATA_ROOT)


def _latest_paper_continuity_path():
    """Find the newest saved paper-account continuity receipt for the desk view."""
    return paper_continuity.latest_receipt_path(settings.ROOT)


@router.get("/paper-preview-latest")
def paper_preview_latest():
    """Expose the newest next-session paper preview without making it executable."""
    return paper_preview_view.build_view(settings.DATA_ROOT)


@router.get("/paper-account-continuity")
def paper_account_continuity():
    """Expose one saved paper-account continuity record without advancing it."""
    return paper_continuity.build_view(settings.ROOT)


def _latest_structured_ths_bundle():
    """Find the newest complete user-export bundle already in the THS journal."""
    from .ths import journal

    journal.initialize()
    with store.connection() as db:
        rows = db.execute(
            "SELECT id,account_alias,kind,observed_at,trading_date,payload "
            "FROM ths_observations ORDER BY observed_at DESC,id DESC"
        ).fetchall()
    groups = {}
    required = {"account", "orders", "trades", "cashflows"}
    for row in rows:
        key = (row["account_alias"], row["trading_date"])
        group = groups.setdefault(key, {})
        group.setdefault(row["kind"], row)
    complete = [group for group in groups.values() if required <= set(group)]
    if not complete:
        return {"status": "missing", "account_alias": None, "trading_date": None,
                "observed_at": {}, "row_counts": {}, "cash_fields": {}}

    group = max(complete, key=lambda value: max(row["observed_at"] for row in value.values()))
    payloads = {kind: json.loads(row["payload"]) for kind, row in group.items()}
    account = payloads["account"]
    positions = account.get("rows") or []
    if not positions and isinstance(account.get("positions"), dict):
        positions = list(account["positions"].values())
    sellable_complete = all(
        isinstance(row, dict) and row.get("sellable") is not None
        for row in positions
    )
    cash_fields = {
        "cash_available": account.get("cash_available") is not None,
        "cash_total": account.get("cash_total") is not None,
        "cash_frozen": account.get("cash_frozen") is not None,
    }
    return {
        "status": "complete" if sellable_complete and all(cash_fields.values()) else "partial",
        "account_alias": account.get("account_alias"),
        "trading_date": account.get("trading_date"),
        "observed_at": {kind: value.get("observed_at") for kind, value in payloads.items()},
        "source": {kind: value.get("source") for kind, value in payloads.items()},
        "row_counts": {
            "account": len(positions),
            "orders": len(payloads["orders"].get("rows") or []),
            "trades": len(payloads["trades"].get("rows") or []),
            "cashflows": len(payloads["cashflows"].get("rows") or []),
        },
        "cash_fields": cash_fields,
        "sellable_complete": sellable_complete,
    }


def _latest_structured_observation_package():
    """Expose the latest static THS evidence package beside journal exports.

    The package is a read-only evidence index, not an importable broker
    observation.  Keeping it visible lets the workbench distinguish missing
    native rows from a genuinely absent capture without weakening release gates.
    """
    root = settings.DATA_ROOT / "ths" / "observation_bundle" / "structured"
    bundle_root = settings.DATA_ROOT / "ths" / "observation_bundle"
    template_paths = {
        "root": str(bundle_root),
        "account": str(bundle_root / "account_observation.json"),
        "orders": str(bundle_root / "orders.csv"),
        "trades": str(bundle_root / "trades.csv"),
        "cashflows": str(bundle_root / "cashflows.csv"),
        "profile": str(bundle_root / "profile.json"),
        "preview": str(bundle_root / "preview_current_v2.json"),
    }
    from .ths.structured_observation import verify_source_manifest
    candidates = sorted(root.rglob("structured_observation_package_v1.json"),
                        key=lambda path: path.stat().st_mtime, reverse=True) if root.is_dir() else []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("schema") != "ths_structured_observation_package_v1":
            continue
        reconciliation = payload.get("order_reconciliation") or {}
        source = payload.get("source")
        source_scope = payload.get("source_scope")
        if source_scope not in {"persistent_user_export", "isolated_rehearsal", "native_readonly_receipt"}:
            source_scope = (
                "persistent_user_export"
                if source == "validated_ths_user_export_bundle"
                else "isolated_rehearsal"
                if source == "isolated_ths_export_rehearsal"
                else "native_readonly_receipt"
            )
        tables = payload.get("tables") or {}
        source_paths = payload.get("source_paths") or {}
        source_manifest = payload.get("source_manifest")
        source_manifest_check = verify_source_manifest(source_manifest)
        return {
            "status": payload.get("status", "partial_structured_observation_package"),
            "native_verified": bool(payload.get("native_verified")),
            "broker_reconciled": bool(payload.get("broker_reconciled")),
            "ledger_reconciled": bool(payload.get("ledger_reconciled")),
            "account_observed": payload.get("account_observed"),
            "account_summary_observed": payload.get("account_summary_observed"),
            "account_structured_observed": payload.get("account_structured_observed"),
            "orders_observed": payload.get("orders_observed"),
            "trades_observed": payload.get("trades_observed"),
            "cashflows_observed": payload.get("cashflows_observed"),
            "structured_receipts": payload.get("structured_receipts") or {},
            "holdings_candidate_observed": payload.get("holdings_candidate_observed"),
            "holdings_candidate_semantic_verified": payload.get("holdings_candidate_semantic_verified"),
            "account_summary": payload.get("account_summary"),
            "holdings_candidate": payload.get("holdings_candidate"),
            "order_count": payload.get("order_count", reconciliation.get("order_count")),
            "trade_count": payload.get("trade_count", reconciliation.get("trade_count")),
            "cashflow_count": payload.get("cashflow_count", reconciliation.get("cashflow_count")),
            "empty_activity_exports": payload.get("empty_activity_exports"),
            "source_file": str(path),
            "source": source,
            "source_scope": source_scope,
            "source_index": payload.get("source_index"),
            "source_paths": source_paths,
            "source_manifest": source_manifest,
            "source_manifest_status": source_manifest_check["status"],
            "source_manifest_issues": source_manifest_check["issues"],
            "source_paths_present": bool(
                all(isinstance(source_paths.get(kind), str) and Path(source_paths[kind]).is_file()
                    for kind in ("account", "orders", "trades", "cashflows"))
            ) if source_paths else False,
            "account_alias": payload.get("account_alias"),
            "account_alias_quality": payload.get("account_alias_quality", "unknown"),
            "observation_window": payload.get("observation_window"),
            "input_template": template_paths,
            "table_status": {
                kind: (tables.get(kind) or {}).get("status")
                for kind in ("account", "holdings", "orders", "trades", "cashflows")
            },
            "unknowns": payload.get("unknowns") or [],
            "order_reconciliation": {
                "status": reconciliation.get("status"),
                "broker_reconciled": bool(reconciliation.get("broker_reconciled")),
                "ledger_reconciled": bool(reconciliation.get("ledger_reconciled")),
                "order_count": reconciliation.get("order_count"),
                "trade_count": reconciliation.get("trade_count"),
                "cashflow_count": reconciliation.get("cashflow_count"),
                "empty_activity_exports": reconciliation.get("empty_activity_exports"),
                "zero_orders_inferred": bool(reconciliation.get("zero_orders_inferred")),
                "zero_trades_inferred": bool(reconciliation.get("zero_trades_inferred")),
                "ledger_mutated": bool(reconciliation.get("ledger_mutated")),
                "execution_admitted": bool(reconciliation.get("execution_admitted")),
                "next_action": reconciliation.get("next_action"),
            },
            "ready_for_ledger_reconciliation": bool(
                payload.get("ready_for_ledger_reconciliation")
                if "ready_for_ledger_reconciliation" in payload
                else reconciliation.get("status") == "ready_for_ledger_reconciliation"
            ),
            "execution_admitted": bool(payload.get("execution_admitted")),
            "ledger_mutated": bool(payload.get("ledger_mutated")),
        }
    return {
        "status": "missing",
        "native_verified": False,
        "broker_reconciled": False,
        "ledger_reconciled": False,
        "account_observed": None,
        "account_summary_observed": None,
        "orders_observed": None,
        "trades_observed": None,
        "cashflows_observed": None,
        "holdings_candidate_observed": None,
        "holdings_candidate_semantic_verified": None,
        "account_summary": None,
        "holdings_candidate": None,
        "order_count": None,
        "trade_count": None,
        "cashflow_count": None,
        "empty_activity_exports": None,
        "source_file": None,
        "source": None,
        "source_scope": None,
        "source_index": None,
        "source_paths": {},
        "source_manifest": None,
        "source_manifest_status": "not_recorded",
        "source_manifest_issues": [],
        "source_paths_present": False,
        "account_alias": None,
        "account_alias_quality": "missing",
        "observation_window": None,
        "input_template": template_paths,
        "table_status": {},
        "unknowns": [],
        "order_reconciliation": {
            "status": "missing",
            "broker_reconciled": False,
            "ledger_reconciled": False,
            "order_count": None,
            "trade_count": None,
            "cashflow_count": None,
            "zero_orders_inferred": False,
            "zero_trades_inferred": False,
            "ledger_mutated": False,
            "execution_admitted": False,
            "next_action": "capture or import explicit THS evidence before reconciliation",
        },
        "ready_for_ledger_reconciliation": False,
        "execution_admitted": False,
        "ledger_mutated": False,
    }


def _latest_complete_structured_observation_package():
    """Expose the newest validated four-table package separately.

    A native receipt and a validated export are different evidence levels.  A
    newer partial native receipt must not hide an older complete package, and
    a complete rehearsal must never be presented as native broker state.
    """
    native_export_root = (settings.DATA_ROOT / "ths" / "observation_bundle" / "structured").resolve()
    rehearsal_root = (settings.DATA_ROOT / "research" / "ths_bundle_rehearsal").resolve()
    roots = [native_export_root, rehearsal_root]
    from .ths.structured_observation import verify_source_manifest
    candidates = []
    for root in roots:
        if root.is_dir():
            candidates.extend(root.rglob("structured_observation_package_v1.json"))

    def source_priority(path):
        """Prefer account exports over synthetic rehearsals before mtime."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return 1
        source_scope = payload.get("source_scope")
        if source_scope == "persistent_user_export":
            return 0
        if source_scope is None and payload.get("source") == "validated_ths_user_export_bundle":
            return 0
        return 1

    ordered_candidates = sorted(
        candidates,
        key=lambda item: (source_priority(item), -item.stat().st_mtime),
    )
    for path in ordered_candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (payload.get("schema") != "ths_structured_observation_package_v1"
                or payload.get("source") not in {
                    "validated_ths_user_export_bundle",
                    "isolated_ths_export_rehearsal",
                }
                or payload.get("status") != "complete_structured_observation_package"
                or payload.get("holdings_candidate_semantic_verified") is not True
                or payload.get("ready_for_ledger_reconciliation") is not True
                or payload.get("execution_admitted")
                or payload.get("ledger_mutated")):
            continue
        source_paths = payload.get("source_paths") or {}
        if not all(isinstance(source_paths.get(kind), str)
                   and Path(source_paths[kind]).is_file()
                   for kind in ("account", "orders", "trades", "cashflows")):
            continue
        declared_scope = payload.get("source_scope")
        if declared_scope in {"persistent_user_export", "isolated_rehearsal"}:
            source_scope = declared_scope
        else:
            # Older packages predate source_scope.  Keep their historical
            # directory fallback, while refusing unknown new scopes.
            try:
                source_scope = (
                    "persistent_user_export"
                    if path.resolve().is_relative_to(native_export_root)
                    else "isolated_rehearsal"
                )
            except (OSError, ValueError):
                source_scope = "unknown"
        reconciliation = payload.get("order_reconciliation") or {}
        source_manifest = payload.get("source_manifest")
        source_manifest_check = verify_source_manifest(source_manifest)
        return {
            "status": payload.get("status"),
            "source_file": str(path),
            "source": payload.get("source"),
            "source_scope": source_scope,
            "current_ledger_import_candidate": source_scope == "persistent_user_export",
            "source_paths": source_paths,
            "source_manifest": source_manifest,
            "source_manifest_status": source_manifest_check["status"],
            "source_manifest_issues": source_manifest_check["issues"],
            "source_paths_present": True,
            "account_alias": payload.get("account_alias"),
            "account_alias_quality": payload.get("account_alias_quality", "unknown"),
            "native_verified": bool(payload.get("native_verified")),
            "account_summary": payload.get("account_summary"),
            "holdings_candidate": payload.get("holdings_candidate"),
            "observation_window": payload.get("observation_window") or {},
            "holdings_candidate_semantic_verified": bool(
                payload.get("holdings_candidate_semantic_verified")
            ),
            "account_observed": bool(payload.get("account_observed")),
            "orders_observed": bool(payload.get("orders_observed")),
            "trades_observed": bool(payload.get("trades_observed")),
            "cashflows_observed": bool(payload.get("cashflows_observed")),
            "order_count": payload.get("order_count", reconciliation.get("order_count")),
            "trade_count": payload.get("trade_count", reconciliation.get("trade_count")),
            "cashflow_count": payload.get("cashflow_count", reconciliation.get("cashflow_count")),
            "order_reconciliation": {
                "status": reconciliation.get("status"),
                "order_count": reconciliation.get("order_count"),
                "trade_count": reconciliation.get("trade_count"),
                "cashflow_count": reconciliation.get("cashflow_count"),
                "broker_reconciled": bool(reconciliation.get("broker_reconciled")),
                "ledger_reconciled": bool(reconciliation.get("ledger_reconciled")),
                "ledger_mutated": bool(reconciliation.get("ledger_mutated")),
                "execution_admitted": bool(reconciliation.get("execution_admitted")),
                "linkage": reconciliation.get("linkage") or {},
                "next_action": reconciliation.get("next_action"),
            },
            "ready_for_ledger_reconciliation": bool(
                payload.get("ready_for_ledger_reconciliation")
                or reconciliation.get("status") == "ready_for_ledger_reconciliation"
            ),
            "ledger_mutated": bool(payload.get("ledger_mutated")),
            "execution_admitted": bool(payload.get("execution_admitted")),
        }
    return {
        "status": "missing",
        "source_file": None,
        "source": None,
        "source_scope": "missing",
        "current_ledger_import_candidate": False,
        "source_paths": {},
        "source_manifest": None,
        "source_manifest_status": "not_recorded",
        "source_manifest_issues": [],
        "source_paths_present": False,
        "account_alias": None,
        "account_alias_quality": "missing",
        "native_verified": False,
        "account_summary": None,
        "holdings_candidate": None,
        "holdings_candidate_semantic_verified": False,
        "account_observed": False,
        "orders_observed": False,
        "trades_observed": False,
        "cashflows_observed": False,
        "order_count": None,
        "trade_count": None,
        "cashflow_count": None,
        "order_reconciliation": {"status": "missing", "ready_for_ledger_reconciliation": False},
        "ready_for_ledger_reconciliation": False,
        "ledger_mutated": False,
        "execution_admitted": False,
    }


def _latest_native_structured_observation_package():
    """Return the newest native read-only package, including partial state.

    The complete-package helper intentionally skips partial receipts for
    Ledger consumers. Account-risk display needs the opposite behavior: a
    newer incomplete native receipt must remain visible and must not be hidden
    by an older synthetic rehearsal.
    """
    root = (settings.DATA_ROOT / "ths" / "observation_bundle" / "structured").resolve()
    if not root.is_dir():
        return {}
    for path in sorted(root.glob("*/structured_observation_package_v1.json"),
                       key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        scope = payload.get("source_scope")
        source = payload.get("source")
        if scope not in {None, "native_readonly_receipt"} and source != "native_readonly_receipt":
            continue
        if source == "validated_ths_user_export_bundle" or scope == "persistent_user_export":
            continue
        payload = dict(payload)
        payload["source_scope"] = "native_readonly_receipt"
        payload["source_file"] = str(path)
        return payload
    return {}


def _quote_observation_paths():
    root = (settings.DATA_ROOT / "ths" / "quote_observation").resolve()
    return root, root / "quotes.csv", root / "metadata.json"


def _paper_quote_required_symbols():
    """Read the frozen paper order symbols that a current quote drop must cover."""
    preview = paper_preview_latest()
    path = (preview.get("artifacts") or {}).get("provisional_order_plan")
    if not path or not Path(path).is_file():
        return []
    required = []
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if str(row.get("side") or "").strip().lower() not in {"buy", "sell"}:
                    continue
                symbol = str(row.get("symbol") or "").strip()
                if symbol:
                    required.append(symbol)
    except (OSError, UnicodeError, csv.Error):
        return []
    return sorted(set(required))


def _quote_observation_preview():
    """Validate the fixed raw-RMB quote drop without touching a client."""
    from .ths.quotes import build_quote_observation

    root, csv_path, metadata_path = _quote_observation_paths()
    template_csv_path = root / "quotes.template.csv"
    template_metadata_path = root / "metadata.template.json"
    files = {
        "quotes": {
            "path": str(csv_path),
            "present": csv_path.is_file(),
            "bytes": csv_path.stat().st_size if csv_path.is_file() else 0,
            "modified_at": csv_path.stat().st_mtime if csv_path.is_file() else None,
        },
        "metadata": {
            "path": str(metadata_path),
            "present": metadata_path.is_file(),
            "bytes": metadata_path.stat().st_size if metadata_path.is_file() else 0,
            "modified_at": metadata_path.stat().st_mtime if metadata_path.is_file() else None,
        },
    }
    template_files = {
        "quotes": {
            "path": str(template_csv_path),
            "present": template_csv_path.is_file(),
            "bytes": template_csv_path.stat().st_size if template_csv_path.is_file() else 0,
            "modified_at": template_csv_path.stat().st_mtime if template_csv_path.is_file() else None,
        },
        "metadata": {
            "path": str(template_metadata_path),
            "present": template_metadata_path.is_file(),
            "bytes": template_metadata_path.stat().st_size if template_metadata_path.is_file() else 0,
            "modified_at": template_metadata_path.stat().st_mtime if template_metadata_path.is_file() else None,
        },
    }
    base = {
        "schema": "ths_raw_quote_observation_readiness_v1",
        "quote_root": str(root),
        "files": files,
        "template_files": template_files,
        "templates_ready": all(item["present"] for item in template_files.values()),
        "canonical_inputs_ready": csv_path.is_file() and metadata_path.is_file(),
        "execution_admitted": False,
        "ledger_mutated": False,
        "client_operation": False,
    }
    required_symbols = _paper_quote_required_symbols()
    base["required_symbols"] = required_symbols
    if not csv_path.is_file() or not metadata_path.is_file():
        return {
            **base,
            "status": "template_ready" if base["templates_ready"] else "missing_template",
            "missing": [str(path) for path in (csv_path, metadata_path) if not path.is_file()],
            "next_action": (
                "空白模板已生成；复制/填写 quotes.template.csv 和 metadata.template.json 为固定目录中的 "
                "quotes.csv、metadata.json，填入同一观察时刻的原始人民币行情后刷新。"
                if base["templates_ready"]
                else "将同一观察时刻的原始人民币行情 CSV 和 metadata.json 放入固定目录后刷新。"
            ),
        }
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        if not isinstance(metadata, dict):
            raise ValueError("metadata.json must contain an object")
        required = ("source", "observed_at", "trading_date")
        missing = [key for key in required if not str(metadata.get(key) or "").strip()]
        if missing:
            raise ValueError(f"metadata.json missing: {', '.join(missing)}")
        from .ths.quotes import build_quote_observation
        observation = build_quote_observation(
            csv_path,
            source=metadata["source"],
            observed_at=metadata["observed_at"],
            trading_date=metadata["trading_date"],
        )
        observation.update({
            "account_alias": str(metadata.get("account_alias") or "quote_observation").strip(),
            "kind": "quotes",
        })
        observed_symbols = sorted(observation["prices"])
        missing_symbols = sorted(set(required_symbols) - set(observed_symbols))
        coverage_status = "complete" if required_symbols and not missing_symbols else "unknown" if not required_symbols else "incomplete"
        observation.update({
            "required_symbols": required_symbols,
            "missing_symbols": missing_symbols,
            "coverage_status": coverage_status,
        })
        if coverage_status != "complete":
            return {
                **base,
                "status": "coverage_incomplete" if coverage_status == "incomplete" else "coverage_unknown",
                "account_alias": observation["account_alias"],
                "trading_date": observation["trading_date"],
                "observed_at": observation["observed_at"],
                "source": observation["source"],
                "price_count": len(observation["prices"]),
                "missing_symbols": missing_symbols,
                "next_action": "行情文件必须覆盖本次纸面买卖计划的全部证券；当前不会导入或解除行情阻断。",
            }
        return {
            **base,
            "status": "ready_for_import",
            "account_alias": observation["account_alias"],
            "trading_date": observation["trading_date"],
            "observed_at": observation["observed_at"],
            "source": observation["source"],
            "price_count": len(observation["prices"]),
            "missing_symbols": [],
            "coverage_status": coverage_status,
            "observation": observation,
            "next_action": "确认后追加到本地 THS 观察日志；仍不会操作客户端、预填、提交或修改 Ledger。",
        }
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        return {
            **base,
            "status": "validation_error",
            "error": str(error),
            "next_action": "修正 quotes.csv 的 symbol/market/price_rmb 或 metadata.json 的 source/observed_at/trading_date。",
        }


@router.get("/quote-observation/readiness")
def quote_observation_readiness():
    """Validate the fixed raw-RMB quote handoff without importing it."""
    return _quote_observation_preview()


class QuoteObservationInitializeInput(BaseModel):
    confirm_initialize: bool = False


def _refresh_blank_quote_template(path, required_symbols):
    """Refresh only an untouched template when the frozen order set changes."""
    if not path.is_file():
        should_write = True
    else:
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            has_values = any(str(row.get("price_rmb") or "").strip() for row in rows)
            current_symbols = sorted({
                f"{str(row.get('symbol') or '').strip()}.{str(row.get('market') or '').strip()}"
                for row in rows
                if str(row.get("symbol") or "").strip() and str(row.get("market") or "").strip()
            })
            should_write = not has_values and current_symbols != sorted(required_symbols)
        except (OSError, UnicodeError, csv.Error):
            should_write = True
    if should_write:
        lines = ["symbol,market,price_rmb,quote_source"]
        lines.extend(
            f"{symbol.split('.', 1)[0]},{symbol.split('.', 1)[1]},," for symbol in required_symbols
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return should_write


@router.post("/quote-observation/initialize")
def initialize_quote_observation(body: QuoteObservationInitializeInput):
    """Create non-data quote templates for the current frozen paper plan."""
    root, csv_path, metadata_path = _quote_observation_paths()
    required_symbols = _paper_quote_required_symbols()
    if body.confirm_initialize is not True:
        return {
            "schema": "ths_raw_quote_observation_initialize_v1",
            "status": "confirmation_required",
            "quote_root": str(root),
            "required_symbols": required_symbols,
            "execution_admitted": False,
            "ledger_mutated": False,
            "client_operation": False,
            "next_action": "确认后生成行情 CSV/元数据模板；不会写入行情值。",
        }
    root.mkdir(parents=True, exist_ok=True)
    csv_template = root / "quotes.template.csv"
    metadata_template = root / "metadata.template.json"
    template_refreshed = _refresh_blank_quote_template(csv_template, required_symbols)
    if not metadata_template.exists():
        metadata_template.write_text(json.dumps({
            "schema": "ths_raw_quote_observation_v1",
            "account_alias": "<fill-account-alias>",
            "source": "<fill-raw-rmb-source>",
            "observed_at": "<fill-timezone-timestamp>",
            "trading_date": "<fill-YYYY-MM-DD>",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "schema": "ths_raw_quote_observation_initialize_v1",
        "status": "initialized",
        "quote_root": str(root),
        "required_symbols": required_symbols,
        "template_refreshed": template_refreshed,
        "templates": {"quotes": str(csv_template), "metadata": str(metadata_template)},
        "canonical_inputs": {"quotes": str(csv_path), "metadata": str(metadata_path)},
        "execution_admitted": False,
        "ledger_mutated": False,
        "client_operation": False,
        "next_action": "复制模板为 quotes.csv 和 metadata.json，填入同一时刻的原始人民币行情后刷新。",
    }


class QuoteObservationImportInput(BaseModel):
    confirm_import: bool = False


@router.post("/quote-observation/import")
def import_quote_observation(body: QuoteObservationImportInput):
    """Append one validated quote observation; this is not an execution action."""
    preview = _quote_observation_preview()
    if preview.get("status") != "ready_for_import":
        return {**preview, "status": "not_imported"}
    if body.confirm_import is not True:
        return {
            **preview,
            "status": "confirmation_required",
            "next_action": "确认后再次调用 import；当前没有写入观察日志。",
        }
    from .ths import journal

    result = journal.observe_many([preview["observation"]])
    return {
        **preview,
        "status": "imported" if result["inserted_kinds"] else "already_present",
        "journal": result,
        "next_action": "行情观察已入日志；仍需同一交易日账户/可卖数量/委托/成交证据，才能进入人工复核。",
    }


def _latest_structured_quote():
    from .ths import journal

    journal.initialize()
    with store.connection() as db:
        rows = db.execute(
            "SELECT id,payload FROM ths_observations WHERE kind='quotes' "
            "ORDER BY julianday(observed_at) DESC,id DESC"
        ).fetchall()
    if not rows:
        return {"status": "missing", "trading_date": None, "observed_at": None, "price_count": 0,
                "coverage_status": "unknown", "missing_symbols": []}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        prices = payload.get("prices") or {}
        if isinstance(prices, dict) and prices:
            return {
                "status": "complete",
                "account_alias": payload.get("account_alias"),
                "trading_date": payload.get("trading_date"),
                "observed_at": payload.get("observed_at"),
                "source": payload.get("source"),
                "price_count": len(prices),
                "schema": payload.get("schema"),
                "coverage_status": payload.get("coverage_status", "unknown"),
                "missing_symbols": payload.get("missing_symbols") or [],
            }
    return {"status": "invalid", "trading_date": None, "observed_at": None, "price_count": 0,
            "coverage_status": "unknown", "missing_symbols": []}


@router.get("/paper-preview-readiness")
def paper_preview_readiness():
    """Join the latest paper preview and THS observation into one release decision."""
    preview = paper_preview_latest()
    observation = ths_native_observation()
    structured = _latest_structured_ths_bundle()
    structured_package = _latest_structured_observation_package()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    quotes = _latest_structured_quote()
    blockers = list(preview.get("release_blockers") or [])
    blockers.extend(observation.get("missing_evidence") or [])

    # A validated user-export bundle can satisfy evidence classes that OCR cannot.
    # It still does not verify the live page or authorize a desktop write.
    if structured.get("status") == "complete":
        resolved = {
            "current_user_account_observation_missing",
            "sellable_quantity_and_t1_status_missing",
            "cash_total_and_cash_frozen",
            "structured_orders",
            "structured_trades",
        }
        blockers = [item for item in blockers if item not in resolved]

    execution_day = preview.get("execution_day")
    if (quotes.get("status") == "complete"
            and quotes.get("coverage_status") == "complete"
            and quotes.get("trading_date") == execution_day):
        blockers = [item for item in blockers if item != "same_session_raw_rmb_quotes_missing"]
    elif quotes.get("status") == "complete" and execution_day and quotes.get("trading_date") != execution_day:
        blockers.append("quote_observation_trading_date_mismatch")
    elif quotes.get("status") == "complete" and quotes.get("coverage_status") != "complete":
        blockers.append("quote_observation_symbol_coverage_incomplete")

    session = observation.get("client_session") or {}
    if session.get("identity_verified") is not True:
        blockers.append("ths_client_identity_unverified")
    if session.get("account_page_verified") is not True:
        blockers.append("ths_account_page_unverified")
    if same_ledger_bundle_reconciliation.get("status") == "reconciliation_required":
        blockers.append("same_ledger_bundle_reconciliation_required")
    elif same_ledger_bundle_reconciliation.get("status") == "isolated_rehearsal_only":
        blockers.append("persistent_ths_export_required_for_same_ledger_reconciliation")
    elif same_ledger_bundle_reconciliation.get("status") == "structured_package_not_ready":
        blockers.append("complete_ths_bundle_required_for_same_ledger_reconciliation")

    unique_blockers = list(dict.fromkeys(blockers))
    preview_available = preview.get("status") not in {"not_observed", "invalid_receipt"}
    account_evidence_available = observation.get("account_summary", {}).get("status") in {
        "read",
        "read_background",
    }
    account_observation_complete = bool(
        observation.get("full_account_observation")
        and observation.get("orders_read")
        and observation.get("trades_read")
        and observation.get("account_summary", {}).get("status") == "read"
    )
    return {
        "schema": "paper_preview_release_readiness_v1",
        "status": "blocked" if unique_blockers or not preview_available else "manual_review_required",
        "preview_available": preview_available,
        "account_evidence_available": account_evidence_available or structured.get("status") == "complete",
        "account_observation_complete": account_observation_complete or structured.get("status") == "complete",
        "account_summary_only": account_evidence_available and not account_observation_complete,
        "release_ready": False,
        "execution_admitted": False,
        "prefill_admitted": False,
        "submitted": False,
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "client_interacted": False,
        "blockers": unique_blockers,
        "structured_bundle": structured,
        "structured_observation_package": structured_package,
        "structured_account_summary_observed": structured_package.get("account_summary_observed"),
        "structured_account_structured_observed": structured_package.get("account_structured_observed"),
        "structured_account_summary_observed_at": structured_package.get("account_summary", {}).get("observed_at")
        if isinstance(structured_package.get("account_summary"), dict) else None,
        "structured_holdings_candidate_observed": structured_package.get("holdings_candidate_observed"),
        "structured_holdings_candidate_semantic_verified": structured_package.get(
            "holdings_candidate_semantic_verified"
        ),
        "structured_orders_observed": structured_package.get("orders_observed"),
        "structured_trades_observed": structured_package.get("trades_observed"),
        "structured_cashflows_observed": structured_package.get("cashflows_observed"),
        "structured_receipts": structured_package.get("structured_receipts") or {},
        "structured_empty_activity_exports": structured_package.get("empty_activity_exports"),
        "structured_package_ready_for_ledger_reconciliation": bool(
            structured_package.get("ready_for_ledger_reconciliation")
        ),
        "structured_native_verified": bool(structured_package.get("native_verified")),
        "structured_broker_reconciled": bool(structured_package.get("broker_reconciled")),
        "structured_ledger_reconciled": bool(structured_package.get("ledger_reconciled")),
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "order_count": structured_reconciliation.get("order_count"),
        "trade_count": structured_reconciliation.get("trade_count"),
        "cashflow_count": structured_reconciliation.get("cashflow_count"),
        "quote_input": _quote_observation_preview(),
        "quote_observation": quotes,
        "paper_preview": preview,
        "ths_observation": observation,
        "next_action": "先补齐账户页、结构化持仓、现金冻结、委托和成交回读，再由人工复核；当前仅允许纸面预览。",
    }


@router.get("/operator-package/manual-statements")
def operator_package_manual_statements():
    """Return empty, dated THS export templates and their offline import contract."""
    package = operator_package()
    execution_day = str(package.get("execution_day"))
    run_root = _operator_package_session_root(package)
    template_specs = [
        {
            "kind": "core_observation_bundle",
            "basis": "account_positions_orders_trades_cashflows",
            "columns": ["account_json", "orders_csv", "trades_csv", "cashflows_csv"],
            "field_columns": {
                "account": ["account_alias", "observed_at", "trading_date", "cash_total", "cash_frozen", "cash_available", "positions"],
                "orders": ["symbol", "market", "side", "order_id", "qty", "price", "filled_qty", "status"],
                "trades": ["symbol", "market", "side", "order_id", "execution_id", "qty", "price"],
                "cashflows": ["symbol", "market", "side", "order_id", "gross_amount", "fees", "cash_change"],
            },
            "templates": {
                "account": run_root / "account_observation.template.json",
                "orders": run_root / "orders_observation.template.csv",
                "trades": run_root / "trades_observation.template.csv",
                "cashflows": run_root / "cashflows_observation.template.csv",
            },
            "follow_up": "refresh the workbench and click the explicit local import action after review",
        },
        {
            "kind": "cashflows",
            "basis": "cumulative_order_cashflow",
            "columns": ["symbol", "market", "side", "order_id", "gross_amount", "fees", "cash_change"],
            "template": run_root / "cashflows_observation.template.csv",
            "follow_up": None,
        },
        {
            "kind": "movements",
            "basis": "nontrade_cash_movements",
            "columns": ["movement_id", "kind", "cash_change"],
            "template": run_root / "movements_observation.template.csv",
            "follow_up": None,
        },
        {
            "kind": "restriction_releases",
            "basis": "opening_restriction_releases",
            "columns": ["release_id", "symbol", "market", "released_qty", "release_day", "reason"],
            "template": run_root / "restriction_releases_observation.template.csv",
            "follow_up": "reconcile-session --include-restriction-releases",
        },
        {
            "kind": "share_credits",
            "basis": "account_bonus_share_credits",
            "columns": ["credit_id", "symbol", "market", "credited_qty", "credit_day", "available_day", "kind"],
            "template": run_root / "share_credits_observation.template.csv",
            "follow_up": "reconcile-session --include-share-credits",
        },
    ]
    rows = []
    for spec in template_specs:
        if spec["kind"] == "core_observation_bundle":
            templates = spec["templates"]
            template = templates["account"]
            template_exists = all(path.is_file() for path in templates.values())
            bundle_root = (settings.DATA_ROOT / "ths" / "observation_bundle").resolve()
            command = (
                f"copy current THS exports into {bundle_root} as "
                "account_observation.json, orders.csv, trades.csv and cashflows.csv; "
                "refresh the workbench, review the bundle and click the explicit local import action"
            )
        else:
            template = spec["template"]
            template_exists = template.is_file()
            command = (
                "D:\\Quantification\\env\\python.exe D:\\Quantification\\run_committee.py ths "
                f"import-export --kind {spec['kind']} --profile <verified_mapping.json> "
                f"--csv {template} --trading-date {execution_day} --observed-at <actual_export_time+08:00>"
            )
        rows.append({
            "kind": spec["kind"],
            "source": "ths_user_export",
            "basis": spec["basis"],
            "columns": spec["columns"],
            "template": str(template),
            "templates": {key: str(path) for key, path in spec.get("templates", {}).items()},
            "template_exists": template_exists,
            "import_command": command,
            "follow_up": spec["follow_up"],
            "execution_admitted": False,
        })
    structured_package = _latest_structured_observation_package()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    return {
        "schema": "offline_manual_statement_view_v1",
        "status": "manual_export_templates_ready" if all(row["template_exists"] for row in rows) else "manual_export_templates_missing",
        "execution_day": execution_day,
        "source_cutoff": package.get("source_cutoff"),
        "rows": rows,
        "structured_observation_package": structured_package,
        "structured_package_ready_for_ledger_reconciliation": bool(
            structured_package.get("ready_for_ledger_reconciliation")
        ),
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "order_count": structured_reconciliation.get("order_count"),
        "trade_count": structured_reconciliation.get("trade_count"),
        "cashflow_count": structured_reconciliation.get("cashflow_count"),
        "client_interacted": False,
        "account_observed": False,
        "quotes_observed": False,
        "ledger_mutated": False,
        "execution_admitted": False,
        "basis": "empty operator templates only; no account values, client readback or ledger import",
    }


def _latest_operator_package_path():
    research_root = settings.DATA_ROOT / "research"
    roots = [research_root / "opening_session_runs"]
    roots.extend(sorted(research_root.glob("opening_session_runs_*")))
    candidates = []
    for root in roots:
        candidates.extend(root.rglob("offline_operator_package_v1.json"))
    def sort_key(path):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            execution_day = date.fromisoformat(str(payload.get("execution_day")))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            execution_day = date.min
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0
        return execution_day, modified

    return max(candidates, key=sort_key) if candidates else None


def _operator_package_session_root(package):
    """Resolve sibling session artifacts for whichever package was selected."""
    source_file = package.get("package_source_file")
    if isinstance(source_file, str) and Path(source_file).is_file():
        return Path(source_file).resolve().parent
    return settings.DATA_ROOT / "research" / "opening_session_runs" / str(package.get("execution_day"))


def _ths_journal_reconciliation_view():
    """Expose saved THS plan reconciliation beside the structured export view.

    The structured observation package describes broker evidence; ``ths_plans``
    describes what the same evidence did to the sole paper Ledger.  Keeping
    both in one read-only view prevents the workbench from showing a valid
    four-table package without also showing whether it was only staged,
    reconciled, or explicitly imported.  Missing tables are an empty local
    state, never an inferred broker result.
    """
    from .ths import journal

    try:
        current = journal.status()
    except sqlite3.OperationalError:
        current = {"paused": True, "submission_enabled": False, "plans": []}

    plans = []
    for plan in current.get("plans") or []:
        result = plan.get("result") or {}
        account_reconciliation = result.get("account_reconciliation") or {}
        ledger_import = result.get("ledger_import") or {}
        plans.append({
            "plan_id": plan.get("id"),
            "account_alias": plan.get("account_alias"),
            "trading_date": plan.get("trading_date"),
            "status": plan.get("status"),
            "contract_id": plan.get("contract_id"),
            "reconciliation_status": result.get("status") or plan.get("status"),
            "account_reconciliation_status": account_reconciliation.get("status"),
            "account_cash_reconciled": result.get("account_cash_reconciled"),
            "account_positions_reconciled": result.get("account_positions_reconciled"),
            "orders_observed_at": result.get("orders_observed_at"),
            "trades_observed_at": result.get("trades_observed_at"),
            "account_observed_at": result.get("account_observed_at"),
            "cashflows_observed_at": result.get("cashflows_observed_at"),
            "ledger_imported": ledger_import.get("ledger_imported") is True,
            "ledger_import_run_id": ledger_import.get("run_id"),
            "ledger_import_source_ref": ledger_import.get("source_ref"),
            "execution_admitted": False,
        })

    if not plans:
        status = "no_saved_plans"
    elif any(row["reconciliation_status"] == "reconciliation_required" for row in plans):
        status = "reconciliation_required"
    elif any(row["ledger_imported"] for row in plans):
        status = "ledger_imported"
    else:
        status = "plans_visible_pending_or_unimported"
    return {
        "schema": "ths_journal_reconciliation_view_v1",
        "status": status,
        "paused": bool(current.get("paused")),
        "kill_switch": current.get("kill_switch", "engaged" if current.get("paused") else "disengaged"),
        "submission_enabled": False,
        "execution_admitted": False,
        "client_operation": False,
        "ledger_mutated": any(row["ledger_imported"] for row in plans),
        "plan_count": len(plans),
        "plans": plans,
        "basis": "saved THS plan results and explicit Ledger import receipts; no broker/client read",
    }


def _same_ledger_bundle_reconciliation_view():
    """Compare one complete THS bundle with receipts in the same Ledger.

    The structured bundle is broker evidence and ``ths_plans`` is the sole
    application accounting path.  This view joins them by the broker contract
    ID and reports exact differences without importing, refreshing, or
    otherwise mutating either side.
    """
    package = _latest_complete_structured_observation_package()
    source_scope = package.get("source_scope", "missing")
    base = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "structured_package_not_ready",
        "source_scope": source_scope,
        "account_alias": package.get("account_alias"),
        "trading_date": (package.get("observation_window") or {}).get("trading_date"),
        "structured_package_source_file": package.get("source_file"),
        "source_manifest_status": package.get("source_manifest_status", "not_recorded"),
        "source_manifest_issues": package.get("source_manifest_issues") or [],
        "ledger_mutated": False,
        "execution_admitted": False,
        "issues": [],
        "rows": [],
        "summary": {
            "broker_order_count": 0,
            "broker_trade_count": 0,
            "broker_cashflow_count": 0,
            "ledger_plan_count": 0,
            "ledger_imported_count": 0,
            "matched_count": 0,
            "issue_count": 0,
        },
        "basis": "complete THS order/trade/cashflow bundle joined to saved same-Ledger plan receipts; read_only",
    }
    if package.get("status") != "complete_structured_observation_package" or not package.get("ready_for_ledger_reconciliation"):
        base["next_action"] = (
            "capture and validate the complete account/orders/trades/cashflows bundle before Ledger comparison"
        )
        return base

    if package.get("source_manifest_status") == "source_changed":
        base.update({
            "status": "reconciliation_required",
            "issues": [{"order_id": "<bundle>", "code": "source_bundle_changed",
                         "details": package.get("source_manifest_issues") or []}],
            "summary": {**base["summary"], "issue_count": 1},
            "next_action": "rebuild the structured package from unchanged four-table exports before Ledger comparison",
        })
        return base

    if (source_scope == "persistent_user_export"
            and package.get("source_manifest_status") != "verified"):
        base.update({
            "status": "reconciliation_required",
            "issues": [{
                "order_id": "<bundle>",
                "code": "source_manifest_unverified",
                "details": package.get("source_manifest_issues") or [],
            }],
            "summary": {**base["summary"], "issue_count": 1},
            "next_action": "rebuild the persistent structured package with a verified four-table source manifest before Ledger comparison",
        })
        return base

    linkage = (package.get("order_reconciliation") or {}).get("linkage") or {}
    summaries = [row for row in linkage.get("order_summaries") or [] if isinstance(row, dict)]
    alias = package.get("account_alias")
    trading_date = (package.get("observation_window") or {}).get("trading_date")
    try:
        from .ths import journal
        journal_payload = journal.status()
    except sqlite3.OperationalError:
        journal_payload = {"plans": []}
    raw_plans = [row for row in journal_payload.get("plans") or [] if isinstance(row, dict)]
    plans = [
        row for row in raw_plans
        if row.get("account_alias") == alias and row.get("trading_date") == trading_date
        and row.get("status") not in {"discarded", "closed_unsubmitted"}
    ]
    plan_by_contract = {
        str(row.get("contract_id")): row for row in plans if row.get("contract_id")
    }
    broker_ids = {str(row.get("order_id")) for row in summaries if row.get("order_id")}
    issues = []
    rows = []
    imported_count = 0

    def add_issue(order_id, code, details=None):
        issue = {"order_id": order_id, "code": code}
        if details:
            issue["details"] = details
        issues.append(issue)

    def tca_fields(summary, plan=None):
        """Expose fill/cost arithmetic without inventing a market benchmark."""
        try:
            order_qty = int(summary.get("order_qty"))
            filled_qty = int(summary.get("trade_filled_qty"))
            gross_cents = int(summary.get("trade_gross_cents"))
            fees_cents = int(summary.get("fees_cents"))
        except (TypeError, ValueError):
            return {}
        if order_qty <= 0 or filled_qty <= 0 or gross_cents < 0 or fees_cents < 0:
            return {}
        effective_price = Decimal(gross_cents) / Decimal(filled_qty) / Decimal(100)
        result = {
            "fill_rate": f"{Decimal(filled_qty) / Decimal(order_qty):.6f}",
            "effective_fill_price_rmb": f"{effective_price:.4f}",
            "fees_bps_on_gross": f"{Decimal(fees_cents) * Decimal(10000) / Decimal(gross_cents):.4f}"
            if gross_cents else None,
            "reference_price_rmb": None,
            "adverse_slippage_bps": None,
            "reference_source": None,
        }
        try:
            reference_cents = int((plan or {}).get("price_cents"))
        except (TypeError, ValueError):
            reference_cents = None
        if reference_cents and reference_cents > 0:
            reference_price = Decimal(reference_cents) / Decimal(100)
            adverse = (effective_price - reference_price) if summary.get("side") == "buy" else (
                reference_price - effective_price
            )
            result.update({
                "reference_price_rmb": f"{reference_price:.4f}",
                "adverse_slippage_bps": f"{adverse * Decimal(10000) / reference_price:.4f}",
                "reference_source": "ledger_plan_limit_price",
            })
        return result

    linkage_issue_fields = (
        ("orphan_trade_order_ids", "broker_orphan_trade_order"),
        ("orphan_cashflow_order_ids", "broker_orphan_cashflow_order"),
        ("orders_missing_cashflow_order_ids", "broker_cashflow_missing"),
        ("filled_quantity_mismatches", "broker_fill_quantity_mismatch"),
        ("trade_identity_mismatches", "broker_trade_identity_mismatch"),
        ("cashflow_identity_mismatches", "broker_cashflow_identity_mismatch"),
        ("status_fill_mismatches", "broker_status_fill_mismatch"),
    )
    for field, code in linkage_issue_fields:
        for detail in linkage.get(field) or []:
            order_id = detail if isinstance(detail, str) else (detail.get("order_id") if isinstance(detail, dict) else None)
            add_issue(order_id or "<bundle>", code, detail if isinstance(detail, dict) else None)
    for detail in linkage.get("order_level_issues") or []:
        if isinstance(detail, dict):
            add_issue(detail.get("order_id") or "<bundle>", "broker_order_level_issue", detail)

    for summary in summaries:
        order_id = str(summary.get("order_id") or "")
        plan = plan_by_contract.get(order_id)
        row = {
            "order_id": order_id,
            "symbol": summary.get("symbol"),
            "side": summary.get("side"),
            "broker": {
                "order_qty": summary.get("order_qty"),
                "filled_qty": summary.get("order_filled_qty"),
                "trade_filled_qty": summary.get("trade_filled_qty"),
                "gross_cents": summary.get("trade_gross_cents"),
                "fees_cents": summary.get("fees_cents"),
                "cash_change_cents": summary.get("cash_change_cents"),
                "broker_status": summary.get("broker_status"),
                "status": summary.get("status"),
            },
            "tca": tca_fields(summary, plan),
            "ledger": None,
            "status": "broker_order_without_ledger_plan",
            "issues": [],
        }
        if plan is None:
            add_issue(order_id, "broker_order_without_ledger_plan")
            rows.append(row)
            continue

        result = plan.get("result") or {}
        proof = result.get("confirmed_account_reconciliation") or {}
        ledger_import = result.get("ledger_import") or {}
        imported = ledger_import.get("ledger_imported") is True
        if imported:
            imported_count += 1
        ledger_row = {
            "plan_id": plan.get("id"),
            "status": plan.get("status"),
            "filled_qty": result.get("filled_qty"),
            "gross_cents": result.get("gross_executed_cents"),
            "fees_cents": proof.get("fees_cents"),
            "cash_change_cents": proof.get("cash_change_cents"),
            "ledger_imported": imported,
            "ledger_import_run_id": ledger_import.get("run_id"),
        }
        row["ledger"] = ledger_row
        comparisons = (
            ("symbol", summary.get("symbol"), plan.get("symbol")),
            ("side", summary.get("side"), plan.get("side")),
            ("order_qty", summary.get("order_qty"), plan.get("qty")),
            ("filled_qty", summary.get("order_filled_qty"), result.get("filled_qty")),
            ("gross_cents", summary.get("trade_gross_cents"), result.get("gross_executed_cents")),
            ("fees_cents", summary.get("fees_cents"), proof.get("fees_cents")),
            ("cash_change_cents", summary.get("cash_change_cents"), proof.get("cash_change_cents")),
        )
        for field, broker_value, ledger_value in comparisons:
            if broker_value != ledger_value:
                detail = {"broker": broker_value, "ledger": ledger_value}
                row["issues"].append({"code": f"{field}_mismatch", "details": detail})
                add_issue(order_id, f"{field}_mismatch", detail)
        if not proof:
            row["issues"].append({"code": "ledger_account_reconciliation_missing"})
            add_issue(order_id, "ledger_account_reconciliation_missing")
        if not imported:
            row["issues"].append({"code": "ledger_import_missing"})
            add_issue(order_id, "ledger_import_missing")
        row["status"] = "matched_imported" if not row["issues"] else "reconciliation_required"
        rows.append(row)

    for plan in plans:
        contract_id = str(plan.get("contract_id") or "")
        if contract_id and contract_id not in broker_ids:
            add_issue(contract_id, "ledger_plan_without_broker_order", {"plan_id": plan.get("id")})
            rows.append({
                "order_id": contract_id,
                "symbol": plan.get("symbol"),
                "side": plan.get("side"),
                "broker": None,
                "ledger": {"plan_id": plan.get("id"), "status": plan.get("status")},
                "status": "ledger_plan_without_broker_order",
                "issues": [{"code": "ledger_plan_without_broker_order"}],
            })

    summary_counts = {
        "broker_order_count": len(summaries),
        "broker_trade_count": linkage.get("trade_count", 0),
        "broker_cashflow_count": linkage.get("cashflow_count", 0),
        "ledger_plan_count": len(plans),
        "ledger_imported_count": imported_count,
        "matched_count": sum(row.get("status") == "matched_imported" for row in rows),
        "issue_count": len(issues),
    }
    if source_scope != "persistent_user_export":
        status = "isolated_rehearsal_only"
        next_action = "replace the isolated rehearsal with a persistent user export before account-level Ledger reconciliation"
    elif not summaries and not plans:
        status = "explicit_empty_activity_confirmed"
        next_action = "retain the explicit empty broker activity evidence; no order import is required"
    elif not issues:
        status = "ledger_reconciled"
        next_action = "retain the matched receipt; no Ledger mutation or broker action is needed"
    else:
        status = "reconciliation_required"
        next_action = "resolve the listed order/fill/cashflow differences, then run the existing explicit Ledger preview/import"
    base.update({
        "status": status,
        "ledger_mutated": imported_count > 0 and source_scope == "persistent_user_export",
        "issues": issues,
        "rows": rows,
        "summary": summary_counts,
        "linkage_status": (package.get("order_reconciliation") or {}).get("status"),
        "next_action": next_action,
    })
    return base


def _same_ledger_compat_fields(bundle, journal_reconciliation):
    """Expose one authoritative status while retaining the legacy journal view."""
    if isinstance(bundle, dict) and bundle.get("status"):
        return {
            "same_ledger_reconciliation_status": bundle["status"],
            "same_ledger_imported": bool(bundle.get("ledger_mutated")),
        }
    return {
        "same_ledger_reconciliation_status": journal_reconciliation.get("status"),
        "same_ledger_imported": bool(journal_reconciliation.get("ledger_mutated")),
    }


def _observed_fill_tca_summary(bundle):
    """Aggregate only explicit broker-fill TCA from the same-ledger bundle."""
    rows = bundle.get("rows") if isinstance(bundle, dict) else None
    rows = [row for row in (rows or []) if isinstance(row, dict) and row.get("broker")]
    base = {
        "schema": "ths_observed_fill_tca_v1",
        "status": "not_available",
        "source_scope": bundle.get("source_scope") if isinstance(bundle, dict) else None,
        "order_count": 0,
        "filled_quantity": 0,
        "gross_cents": None,
        "fees_cents": None,
        "fill_rate": None,
        "effective_fill_price_rmb": None,
        "reference_order_count": 0,
        "weighted_adverse_slippage_bps": None,
        "execution_admitted": False,
        "basis": "explicit broker order/trade/cumulative cashflow rows; no benchmark inference",
    }
    if not rows:
        return base
    gross = 0
    fees = 0
    order_qty = 0
    filled_qty = 0
    reference_gross = 0
    weighted_slippage = Decimal("0")
    reference_order_count = 0
    for row in rows:
        broker = row.get("broker") or {}
        try:
            order_qty += int(broker.get("order_qty"))
            filled_qty += int(broker.get("trade_filled_qty"))
            gross_value = int(broker.get("gross_cents"))
            fees += int(broker.get("fees_cents"))
        except (TypeError, ValueError):
            continue
        gross += gross_value
        tca = row.get("tca") or {}
        if tca.get("adverse_slippage_bps") not in (None, ""):
            try:
                weighted_slippage += Decimal(str(tca["adverse_slippage_bps"])) * Decimal(gross_value)
                reference_gross += gross_value
                reference_order_count += 1
            except (InvalidOperation, TypeError, ValueError):
                pass
    if not order_qty and not filled_qty:
        return base
    result = dict(base)
    result.update({
        "status": (
            "isolated_rehearsal_only"
            if bundle.get("source_scope") != "persistent_user_export"
            else "observed_fills_with_reference"
            if reference_order_count
            else "observed_fills_without_reference"
        ),
        "order_count": len(rows),
        "filled_quantity": filled_qty,
        "gross_cents": gross,
        "fees_cents": fees,
        "fill_rate": f"{Decimal(filled_qty) / Decimal(order_qty):.6f}" if order_qty else None,
        "effective_fill_price_rmb": (
            f"{Decimal(gross) / Decimal(filled_qty) / Decimal(100):.4f}"
            if filled_qty and gross >= 0 else None
        ),
        "reference_order_count": reference_order_count,
        "weighted_adverse_slippage_bps": (
            f"{weighted_slippage / Decimal(reference_gross):.4f}"
            if reference_gross else None
        ),
    })
    return result


def _broker_observation_alerts(bundle):
    """Turn verified broker/ledger differences into one operator alert view.

    This is deliberately a read-only projection.  It does not retry, cancel,
    submit, or mutate the Ledger.  A persistent verified export may produce
    broker-observation alerts; an isolated rehearsal is labelled as synthetic
    and an absent package remains an explicit wait state.
    """
    base = {
        "schema": "ths_broker_observation_alerts_v1",
        "status": "awaiting_verified_structured_observation",
        "source_scope": bundle.get("source_scope") if isinstance(bundle, dict) else None,
        "source_manifest_status": bundle.get("source_manifest_status") if isinstance(bundle, dict) else None,
        "live_alerts": False,
        "execution_admitted": False,
        "alert_count": 0,
        "open_count": 0,
        "alerts": [],
        "basis": "same-ledger structured broker observation; read-only and no automatic recovery",
    }
    if not isinstance(bundle, dict) or not bundle.get("rows"):
        return base

    source_scope = bundle.get("source_scope")
    verified = bundle.get("source_manifest_status") == "verified"
    live_alerts = source_scope == "persistent_user_export" and verified
    alerts = []

    def add(code, severity, order_id, message, details=None):
        row = {
            "alert_id": f"broker-observation-{len(alerts) + 1}",
            "code": code,
            "severity": severity,
            "state": "open",
            "order_id": order_id,
            "message": message,
            "live_alert": live_alerts,
            "execution_admitted": False,
        }
        if details:
            row["details"] = details
        alerts.append(row)

    for row in bundle.get("rows") or []:
        if not isinstance(row, dict):
            add("invalid_reconciliation_row", "P0", "<row>", "结构化对账行不是对象")
            continue
        order_id = row.get("order_id") or "<unknown-order>"
        for issue in row.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("code") or "reconciliation_required")
            add(code, "P0" if "mismatch" in code or "orphan" in code else "P1",
                order_id, f"券商回读与同一 Ledger 存在 {code}", issue.get("details"))
        if row.get("status") == "broker_order_without_ledger_plan":
            add("broker_order_without_ledger_plan", "P0", order_id,
                "发现未归属到本系统计划的券商委托，必须人工认领或解释")
        elif row.get("status") == "ledger_plan_without_broker_order":
            add("ledger_plan_without_broker_order", "P0", order_id,
                "系统计划没有对应券商委托回读，禁止重发并要求人工查询")
        elif row.get("status") not in {None, "matched_imported"} and not row.get("issues"):
            add("broker_receipt_not_imported", "P1", order_id,
                "券商回读已存在，但尚未进入同一 Ledger", {"status": row.get("status")})

    if source_scope == "persistent_user_export" and not verified:
        add("structured_source_manifest_unverified", "P0", "<bundle>",
            "持久导出包的来源清单未验证，不能作为账户级告警或对账结果")
    status = "broker_observation_alerts_ready" if live_alerts else "isolated_observation_alerts_only"
    base.update({
        "status": status,
        "live_alerts": live_alerts,
        "alert_count": len(alerts),
        "open_count": len(alerts),
        "alerts": alerts,
    })
    return base


@router.get("/operator-package")
def operator_package():
    """Return the newest read-only paper operator package for the trading desk."""
    path = _latest_operator_package_path()
    if path is None:
        raise HTTPException(404, "尚无离线运营包")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"离线运营包尚不可读取：{error}") from error
    if payload.get("schema") != "offline_operator_package_v1":
        raise HTTPException(409, "离线运营包版本不受支持")
    payload["package_source_file"] = str(path.resolve())
    execution_day = str(payload.get("execution_day") or "")
    today = date.today().isoformat()
    if execution_day and execution_day < today:
        payload["package_freshness"] = "stale_execution_day"
        payload["package_warning"] = (
            f"execution_day={execution_day} is before server_date={today}; "
            "do not reuse this package for a new paper selection or THS handoff"
        )
        payload["next_action"] = (
            "build a future-dated opening pack from a current source cutoff before selection"
        )
    else:
        payload["package_freshness"] = "current_or_future_execution_day"
    structured_package = _latest_structured_observation_package()
    complete_structured_package = _latest_complete_structured_observation_package()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    payload["structured_observation_package"] = structured_package
    payload["structured_observation_package_complete"] = complete_structured_package
    payload["structured_package_ready_for_ledger_reconciliation"] = bool(
        structured_package.get("ready_for_ledger_reconciliation")
    )
    payload["complete_structured_package_ready_for_ledger_reconciliation"] = bool(
        complete_structured_package.get("ready_for_ledger_reconciliation")
    )
    payload["order_reconciliation"] = structured_reconciliation
    payload["order_reconciliation_status"] = structured_reconciliation.get("status")
    payload["order_count"] = structured_reconciliation.get("order_count")
    payload["trade_count"] = structured_reconciliation.get("trade_count")
    payload["cashflow_count"] = structured_reconciliation.get("cashflow_count")
    payload["ths_journal_reconciliation"] = journal_reconciliation
    payload["same_ledger_bundle_reconciliation"] = same_ledger_bundle_reconciliation
    # Surface the historical, explicit same-Ledger receipt at the package
    # level.  This is a read-only projection of a prior import, not a request
    # to mutate the Ledger or operate the broker client.
    payload.update(_same_ledger_compat_fields(
        same_ledger_bundle_reconciliation, journal_reconciliation
    ))
    payload["execution_admitted"] = False
    payload["ledger_mutated"] = journal_reconciliation["ledger_mutated"]
    payload["client_interacted"] = False
    return payload


@router.get("/operator-package/journal-reconciliation")
def operator_package_journal_reconciliation():
    """Return the same-Ledger THS plan/import view without broker I/O."""
    return _ths_journal_reconciliation_view()


@router.get("/operator-package/ledger-reconciliation")
def operator_package_ledger_reconciliation():
    """Join the latest complete THS bundle with the sole Ledger, read-only."""
    return _same_ledger_bundle_reconciliation_view()


class PaperSelectionInput(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")


class PromotionReviewInput(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    decision: str = Field(pattern=r"^(accept_for_paper|reject|defer)$")
    reviewer: str = Field(min_length=1, max_length=120)
    reviewed_at: str = Field(min_length=1, max_length=80)
    notes: str = Field(min_length=1, max_length=2000)
    confirm_review: bool = False


def _promotion_review_receipt_path(package):
    return _operator_package_session_root(package) / "human_promotion_review_receipt_v1.json"


@router.get("/operator-package/promotion-review")
def operator_package_promotion_review():
    """Return the paper promotion template plus the latest explicit human receipt."""
    package = operator_package()
    review_path = _package_artifact(package, "strategy_promotion_review")
    if not review_path:
        raise HTTPException(404, "当前离线包缺少策略晋级审查模板")
    try:
        review = json.loads(review_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"策略晋级审查模板尚不可读取：{error}") from error
    if review.get("schema") != "strategy_promotion_review_record_v1":
        raise HTTPException(409, "策略晋级审查模板版本不受支持")
    receipt_path = _promotion_review_receipt_path(package)
    receipt = None
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"人工晋级审查回执尚不可读取：{error}") from error
    return {
        "schema": "offline_promotion_review_view_v1",
        "status": "human_review_recorded" if receipt else review.get("status"),
        "source_template": str(review_path),
        "receipt": receipt,
        "candidate_count": review.get("candidate_count", 0),
        "eligible_candidate_count": review.get("eligible_candidate_count", 0),
        "required_evidence": review.get("required_evidence") or [],
        "automatic_promotion": False,
        "execution_admitted": False,
        "next_action": (
            "保留人工审查记录；策略仍需独立证据、账户/行情观察和后续人工确认"
            if receipt else
            "提交明确的人审决定前，策略保持待审，不会自动晋级"
        ),
    }


@router.post("/operator-package/promotion-review", status_code=202)
def submit_operator_package_promotion_review(body: PromotionReviewInput):
    """Persist an explicit paper-only human review; never promotes or submits."""
    package = operator_package()
    if package.get("package_freshness") == "stale_execution_day":
        raise HTTPException(409, package.get("package_warning", "离线运营包已过期，不能记录新审查"))
    review_path = _package_artifact(package, "strategy_promotion_review")
    if not review_path:
        raise HTTPException(404, "当前离线包缺少策略晋级审查模板")
    try:
        review = json.loads(review_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"策略晋级审查模板尚不可读取：{error}") from error
    if review.get("schema") != "strategy_promotion_review_record_v1":
        raise HTTPException(409, "策略晋级审查模板版本不受支持")
    candidate = next(
        (row for row in review.get("rows") or []
         if str(row.get("strategy_id") or "") == body.strategy_id),
        None,
    )
    if candidate is None:
        raise HTTPException(400, "策略不在当前冻结晋级审查模板中")
    if body.confirm_review is not True:
        return {
            "schema": "human_promotion_review_receipt_v1",
            "status": "confirmation_required",
            "strategy_id": body.strategy_id,
            "decision": body.decision,
            "automatic_promotion": False,
            "execution_admitted": False,
            "next_action": "确认该纸面审查决定后，再次提交 confirm_review=true；不会自动下单。",
        }
    receipt = {
        "schema": "human_promotion_review_receipt_v1",
        "status": "human_review_recorded",
        "execution_day": package.get("execution_day"),
        "strategy_id": body.strategy_id,
        "decision": body.decision,
        "reviewer": body.reviewer,
        "reviewed_at": body.reviewed_at,
        "notes": body.notes,
        "candidate_snapshot": candidate,
        "source_template": str(review_path),
        "automatic_promotion": False,
        "execution_admitted": False,
        "submission_enabled": False,
    }
    target = _promotion_review_receipt_path(package)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return receipt


class RecoveryReviewInput(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    scenario_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    decision: str = Field(pattern=r"^(acknowledge_and_hold|manual_takeover|defer)$")
    reviewer: str = Field(min_length=1, max_length=120)
    reviewed_at: str = Field(min_length=1, max_length=80)
    notes: str = Field(min_length=1, max_length=2000)
    confirm_review: bool = False


def _recovery_review_receipt_path(package):
    return _operator_package_session_root(package)


def _recovery_review_receipt_file(package, strategy_id, scenario_id):
    root = _recovery_review_receipt_path(package)
    return root / f"human_recovery_review_{strategy_id}_{scenario_id}_v1.json"


def _recovery_review_receipts(package):
    root = _recovery_review_receipt_path(package)
    if not root.is_dir():
        return []
    receipts = []
    for path in sorted(root.glob("human_recovery_review_*_v1.json")):
        try:
            receipts.append(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"人工恢复审查回执尚不可读取：{error}") from error
    return receipts


def _recovery_review_candidates(package):
    index_path = _package_artifact(package, "paper_recovery_handoff_index")
    if not index_path:
        raise HTTPException(404, "当前离线包缺少恢复交接索引")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
        candidates = []
        for index_row in index.get("rows") or []:
            handoff_path = Path(str(index_row.get("handoff_json") or ""))
            if not handoff_path.is_file():
                continue
            handoff = json.loads(handoff_path.read_text(encoding="utf-8-sig"))
            for row in handoff.get("rows") or []:
                candidates.append({
                    "strategy_id": handoff.get("strategy_id") or index_row.get("strategy_id"),
                    "scenario_id": row.get("scenario_id"),
                    "lifecycle_status": row.get("lifecycle_status"),
                    "recovery_action": row.get("recovery_action"),
                    "required_observation": row.get("required_observation"),
                    "retry_permitted": False,
                    "execution_admitted": False,
                    "source_file": str(handoff_path),
                })
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise HTTPException(409, f"恢复交接模板尚不可读取：{error}") from error
    return index_path, candidates


@router.get("/operator-package/recovery-review")
def operator_package_recovery_review():
    """Return recovery scenarios and the latest explicit paper-only review."""
    package = operator_package()
    index_path, candidates = _recovery_review_candidates(package)
    receipts = _recovery_review_receipts(package)
    return {
        "schema": "offline_recovery_review_view_v1",
        "status": "human_recovery_review_recorded" if receipts else "manual_recovery_review_required",
        "source_template": str(index_path),
        "receipts": receipts,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "retry_permitted": False,
        "automatic_recovery": False,
        "execution_admitted": False,
        "next_action": (
            "保留人工接管决定，继续等待账户、委托和成交观察；不会自动重试或撤单"
            if receipts else
            "提交人工恢复审查决定前，系统保持停止和对账优先，不会自动重试"
        ),
    }


@router.post("/operator-package/recovery-review", status_code=202)
def submit_operator_package_recovery_review(body: RecoveryReviewInput):
    """Persist a recovery review without authorizing retry, cancel or release."""
    package = operator_package()
    if package.get("package_freshness") == "stale_execution_day":
        raise HTTPException(409, package.get("package_warning", "离线包已过期，不能记录新恢复审查"))
    index_path, candidates = _recovery_review_candidates(package)
    candidate = next(
        (row for row in candidates
         if str(row.get("strategy_id") or "") == body.strategy_id
         and str(row.get("scenario_id") or "") == body.scenario_id),
        None,
    )
    if candidate is None:
        raise HTTPException(400, "恢复场景不在当前冻结恢复交接模板中")
    if body.confirm_review is not True:
        return {
            "schema": "human_recovery_review_receipt_v1",
            "status": "confirmation_required",
            "strategy_id": body.strategy_id,
            "scenario_id": body.scenario_id,
            "decision": body.decision,
            "retry_permitted": False,
            "automatic_recovery": False,
            "execution_admitted": False,
            "next_action": "确认该人工处置决定后，再次提交 confirm_review=true；仍不会自动重试、撤单或提交。",
        }
    receipt = {
        "schema": "human_recovery_review_receipt_v1",
        "status": "human_recovery_review_recorded",
        "execution_day": package.get("execution_day"),
        "strategy_id": body.strategy_id,
        "scenario_id": body.scenario_id,
        "decision": body.decision,
        "reviewer": body.reviewer,
        "reviewed_at": body.reviewed_at,
        "notes": body.notes,
        "candidate_snapshot": candidate,
        "source_template": str(index_path),
        "retry_permitted": False,
        "automatic_recovery": False,
        "execution_admitted": False,
        "submission_enabled": False,
    }
    target = _recovery_review_receipt_file(package, body.strategy_id, body.scenario_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return receipt


@router.post("/operator-package/paper-selection", status_code=202)
def apply_paper_selection(body: PaperSelectionInput):
    """Materialize one explicitly chosen candidate as a paper-only desk."""
    package = operator_package()
    if package.get("package_freshness") == "stale_execution_day":
        raise HTTPException(409, package.get("package_warning", "离线运营包已过期，不能继续选择"))
    candidates = {str(row.get("strategy_id")) for row in package.get("strategy_candidates", [])}
    if body.strategy_id not in candidates:
        raise HTTPException(400, "候选策略不在当前冻结运营包中")
    opening_pack = Path(str(package.get("opening_pack", "")))
    script = Path(__file__).resolve().parents[1] / "scripts" / "apply_offline_strategy_selection.ps1"
    if not opening_pack.is_file() or not script.is_file():
        raise HTTPException(409, "纸面策略选择所需的冻结运营包或脚本不存在")
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-OpeningPack", str(opening_pack), "-StrategyId", body.strategy_id,
        "-OutputRoot", str(settings.DATA_ROOT / "research" / "opening_session_runs"),
    ]
    try:
        result = subprocess.run(command, cwd=str(script.parents[1]), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=180, check=False)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(504, "纸面策略选择超过 180 秒，未尝试任何真实交易动作") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "纸面策略选择失败").strip()[-4000:]
        raise HTTPException(409, detail)
    receipt_path = _operator_package_session_root(package) / "paper_strategy_application_receipt_v1.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig")) if receipt_path.is_file() else None
    return {"status": "offline_paper_strategy_applied", "strategy_id": body.strategy_id,
            "receipt": receipt, "stdout_tail": (result.stdout or "").strip()[-2000:]}


@router.get("/operator-package/paper-selection")
def operator_package_paper_selection():
    """Return the last explicit paper selection without rerunning the offline builder."""
    package = operator_package()
    receipt_path = _operator_package_session_root(package) / "paper_strategy_application_receipt_v1.json"
    if not receipt_path.is_file():
        return {"schema": "offline_paper_selection_view_v1", "status": "no_paper_strategy_selected", "strategy_id": None, "receipt": None, "capacity": None}
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"单策略纸面回执尚不可读取：{error}") from error
    if receipt.get("schema") != "paper_strategy_application_receipt_v1":
        raise HTTPException(409, "单策略纸面回执版本不受支持")
    if str(receipt.get("execution_day")) != str(package.get("execution_day")):
        return {"schema": "offline_paper_selection_view_v1", "status": "stale_paper_strategy_selection", "strategy_id": None, "receipt": None, "capacity": None}
    capacity = None
    risk_path = _package_artifact(package, "offline_portfolio_risk_summary")
    if risk_path:
        try:
            risk_payload = json.loads(risk_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"候选组合风险书尚不可读取：{error}") from error
        capacity = next((row for row in risk_payload.get("rows") or [] if str(row.get("strategy_id")) == str(receipt.get("strategy_id"))), None)
        if capacity is not None:
            capacity = {"row": capacity, "source_file": str(risk_path), "execution_admitted": False}
    return {"schema": "offline_paper_selection_view_v1", "status": receipt.get("status", "offline_paper_strategy_applied"), "strategy_id": receipt.get("strategy_id"), "receipt": receipt, "capacity": capacity}


@router.get("/operator-package/selected-plan")
def operator_package_selected_plan():
    """Join the explicitly selected paper strategy into one read-only desk view."""
    package = operator_package()
    structured_package = _latest_structured_observation_package()
    complete_structured_package = _latest_complete_structured_observation_package()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    ledger_import_preview = package.get("ths_ledger_import_preview_rehearsal") or {}
    execution_day = str(package.get("execution_day"))
    receipt_path = _operator_package_session_root(package) / "paper_strategy_application_receipt_v1.json"
    if not receipt_path.is_file():
        return {
            "schema": "offline_selected_plan_view_v1",
            "status": "no_paper_strategy_selected",
            "execution_day": execution_day,
            "selected_strategy_id": None,
            "execution_admitted": False,
            "account_observed": False,
            "quotes_observed": False,
            "client_interacted": False,
            "summary": None,
            "target_orders": [],
            "ths_reference": None,
            "paper": None,
            "risk_row": None,
            "account_risk": None,
            "account_order_risk": None,
            "saved_comparison": None,
            "decision_queue": None,
            "structured_observation_package": structured_package,
            "structured_observation_package_complete": complete_structured_package,
            "structured_package_ready_for_ledger_reconciliation": bool(
                structured_package.get("ready_for_ledger_reconciliation")
            ),
            "complete_structured_package_ready_for_ledger_reconciliation": bool(
                complete_structured_package.get("ready_for_ledger_reconciliation")
            ),
            "order_reconciliation": structured_reconciliation,
            "order_reconciliation_status": structured_reconciliation.get("status"),
            "ledger_import_preview": ledger_import_preview,
            "ledger_import_preview_status": ledger_import_preview.get("status"),
            "broker_capabilities": _broker_capability_snapshot(),
            "source_files": {},
            "next_action": "先明确选择一个候选策略生成纸面方案；当前没有任何实时或真实交易动作。",
        }
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"单策略纸面回执尚不可读取：{error}") from error
    if receipt.get("schema") != "paper_strategy_application_receipt_v1":
        raise HTTPException(409, "单策略纸面回执版本不受支持")
    if str(receipt.get("execution_day")) != execution_day:
        return {
            "schema": "offline_selected_plan_view_v1",
            "status": "stale_paper_strategy_selection",
            "execution_day": execution_day,
            "selected_strategy_id": None,
            "execution_admitted": False,
            "account_observed": False,
            "quotes_observed": False,
            "client_interacted": False,
            "summary": None,
            "target_orders": [],
            "ths_reference": None,
            "paper": None,
            "risk_row": None,
            "account_risk": None,
            "account_order_risk": None,
            "saved_comparison": None,
            "decision_queue": None,
            "structured_observation_package": structured_package,
            "structured_observation_package_complete": complete_structured_package,
            "structured_package_ready_for_ledger_reconciliation": bool(
                structured_package.get("ready_for_ledger_reconciliation")
            ),
            "complete_structured_package_ready_for_ledger_reconciliation": bool(
                complete_structured_package.get("ready_for_ledger_reconciliation")
            ),
            "order_reconciliation": structured_reconciliation,
            "order_reconciliation_status": structured_reconciliation.get("status"),
            "ledger_import_preview": ledger_import_preview,
            "ledger_import_preview_status": ledger_import_preview.get("status"),
            "broker_capabilities": _broker_capability_snapshot(),
            "source_files": {"receipt": str(receipt_path)},
            "next_action": "纸面回执与当前离线运营包不是同一交易日，保留待处理，不执行任何动作。",
        }

    def artifact_path(name):
        value = (package.get("artifact_pointers") or {}).get(name) or (receipt.get("artifact_pointers") or {}).get(name)
        path = Path(str(value)) if value else None
        return path if path and path.is_file() else None

    def read_csv(path, label):
        if not path:
            return []
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error) as error:
            raise HTTPException(409, f"{label}尚不可读取：{error}") from error

    def read_json(path, label):
        if not path:
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"{label}尚不可读取：{error}") from error

    desk_snapshot_path = Path(str(package.get("trader_desk_snapshot") or ""))
    desk_snapshot = read_json(desk_snapshot_path, "交易员离线快照") if desk_snapshot_path.is_file() else {}
    instrument_master_view = {
        "instrument_master": desk_snapshot.get("instrument_master") or {},
        "coverage": desk_snapshot.get("instrument_master_coverage") or {},
        "execution_admitted": False,
        "source_file": str(desk_snapshot_path) if desk_snapshot_path.is_file() else None,
    }

    target_path = artifact_path("target_order_reconciliation")
    lifecycle_path = artifact_path("paper_order_lifecycle")
    reconciliation_path = artifact_path("paper_reconciliation")
    release_path = artifact_path("paper_release_decision")
    carry_path = artifact_path("paper_next_session_carry")
    risk_path = artifact_path("offline_portfolio_risk_summary")
    saved_comparison_path = artifact_path("saved_ledger_comparison")
    decision_queue_path = artifact_path("offline_operator_decision_queue_json")
    target_rows = read_csv(target_path, "目标订单对账")
    lifecycle_rows = read_csv(lifecycle_path, "纸面订单生命周期")
    reconciliation_rows = read_csv(reconciliation_path, "纸面对账")
    release_rows = read_csv(release_path, "纸面释放决策")
    carry = read_json(carry_path, "纸面下一会话结转")
    risk_payload = read_json(risk_path, "候选组合风险书")
    risk_row = next(
        (row for row in risk_payload.get("rows") or [] if str(row.get("strategy_id")) == str(receipt.get("strategy_id"))),
        None,
    )
    saved_comparison = None
    if saved_comparison_path:
        comparison_payload = read_json(saved_comparison_path, "已保存同账本比较")
        selected_strategy_id = str(receipt.get("strategy_id") or "")
        selected_case = next(
            (
                case
                for case in comparison_payload.get("cases") or []
                if str(case.get("signal") or "") == selected_strategy_id
            ),
            None,
        )
        metrics = (selected_case or {}).get("metrics") or {}
        selected_case_summary = None
        if selected_case is not None:
            selected_case_summary = {
                "case_id": selected_case.get("case_id"),
                "family": selected_case.get("family"),
                "signal": selected_case.get("signal"),
                "label": selected_case.get("label"),
                "status": selected_case.get("status"),
                "net_return": metrics.get("net_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe_zero_rf_252": metrics.get("sharpe_zero_rf_252"),
                "fees": metrics.get("fees"),
                "stale_valuation_days": metrics.get("stale_valuation_days"),
                "descriptive_nondominated": bool(selected_case.get("descriptive_nondominated")),
                "strategy_validated": bool(selected_case.get("strategy_validated")),
                "execution_admitted": bool(selected_case.get("execution_admitted")),
            }
        comparison_cases = []
        for case in comparison_payload.get("cases") or []:
            case_metrics = case.get("metrics") or {}
            comparison_cases.append({
                "case_id": case.get("case_id"),
                "family": case.get("family"),
                "signal": case.get("signal"),
                "label": case.get("label"),
                "status": case.get("status"),
                "net_return": case_metrics.get("net_return"),
                "max_drawdown": case_metrics.get("max_drawdown"),
                "sharpe_zero_rf_252": case_metrics.get("sharpe_zero_rf_252"),
                "fees": case_metrics.get("fees"),
                "stale_valuation_days": case_metrics.get("stale_valuation_days"),
                "descriptive_nondominated": bool(case.get("descriptive_nondominated")),
                "strategy_validated": bool(case.get("strategy_validated")),
                "execution_admitted": bool(case.get("execution_admitted")),
            })
        saved_comparison = {
            "status": comparison_payload.get("status"),
            "source_artifact": str(saved_comparison_path),
            "start": comparison_payload.get("start"),
            "end": comparison_payload.get("end"),
            "sessions": comparison_payload.get("sessions", 0),
            "requested_cases": comparison_payload.get("requested_cases", 0),
            "comparable_cases": comparison_payload.get("comparable_cases", 0),
            "same_cost_settings": bool(comparison_payload.get("same_cost_settings")),
            "selected_case": selected_case_summary,
            "cases": comparison_cases,
            "descriptive_nondominated_case_ids": comparison_payload.get("descriptive_nondominated_case_ids") or [],
            "strategy_validated": bool(comparison_payload.get("strategy_validated")),
            "execution_admitted": bool(comparison_payload.get("execution_admitted")),
            "backtest_run": bool(comparison_payload.get("backtest_run")),
        }
    selected_strategy_id = str(receipt.get("strategy_id") or "")
    latest_comparison_diagnostic = _latest_saved_comparison_diagnostic(
        selected_strategy_id,
        saved_comparison or {},
    )
    fixed_idea_frontier = _latest_fixed_idea_frontier()
    fixed_profitability_diagnostic = _latest_fixed_profitability_diagnostic()
    alpha158_linear_diagnostic = _latest_alpha158_linear_diagnostic()
    alpha158_lightgbm_diagnostic = _latest_alpha158_lightgbm_diagnostic()
    decision_queue = None
    if decision_queue_path:
        decision_payload = read_json(decision_queue_path, "离线决策队列")
        selected_strategy_id = str(receipt.get("strategy_id") or "")
        decision_row = next(
            (
                row
                for row in decision_payload.get("rows") or []
                if str(row.get("strategy_id") or "") == selected_strategy_id
            ),
            None,
        )
        if decision_row is not None:
            recovery_handoff_path = Path(str(decision_row.get("recovery_handoff_json"))) if decision_row.get("recovery_handoff_json") else None
            recovery_handoff = read_json(recovery_handoff_path, "纸面恢复交接") if recovery_handoff_path and recovery_handoff_path.is_file() else {}
            recovery_scenarios = [
                {
                    "scenario_id": row.get("scenario_id"),
                    "lifecycle_status": row.get("lifecycle_status"),
                    "recovery_action": row.get("recovery_action"),
                    "operator_instruction": row.get("operator_instruction"),
                    "required_observation": row.get("required_observation"),
                }
                for row in recovery_handoff.get("rows") or []
                if str(row.get("lifecycle_status") or "") != "passed"
            ]
            reconciliation_template = recovery_handoff.get("reconciliation_template") or {}
            recovery_evidence_checks = [
                {
                    "scenario_id": check.get("scenario_id"),
                    "status": check.get("status"),
                    "required_evidence": check.get("required_evidence") or [],
                    "pending_evidence": check.get("pending_evidence") or [],
                    "execution_admitted": False,
                }
                for check in reconciliation_template.get("checks") or []
            ]
            recovery_pending_evidence = sorted({
                str(item)
                for check in recovery_evidence_checks
                for item in check["pending_evidence"]
                if str(item)
            })
            decision_queue = {
                "schema": decision_payload.get("schema"),
                "status": decision_payload.get("status"),
                "source_artifact": str(decision_queue_path),
                "decision": decision_row.get("decision"),
                "reason": decision_row.get("reason"),
                "next_operator_action": decision_row.get("next_operator_action"),
                "recovery_handoff_status": decision_row.get("recovery_handoff_status"),
                "recovery_unresolved_scenario_count": decision_row.get("recovery_unresolved_scenario_count", 0),
                "recovery_retry_permitted": str(decision_row.get("recovery_retry_permitted", "false")).lower() == "true",
                "recovery_scenarios": recovery_scenarios,
                "recovery_pending_evidence_count": len(recovery_pending_evidence),
                "recovery_pending_evidence": recovery_pending_evidence,
                "recovery_evidence_checks": recovery_evidence_checks,
                "recovery_handoff_source": str(recovery_handoff_path) if recovery_handoff_path and recovery_handoff_path.is_file() else None,
                "portfolio_merge_status": decision_row.get("portfolio_merge_status"),
                "execution_admitted": bool(decision_payload.get("execution_admitted")),
            }
    ths_reference = None
    try:
        ths_reference = ths_reference_payload(strategy_id=str(receipt.get("strategy_id")))
    except HTTPException:
        ths_reference = None

    order_rows = [row for row in target_rows if row.get("row_type", "order") == "order"]
    prefill_checks = []
    prefill_rows = (ths_reference or {}).get("rows") or []
    prefill_by_sequence = {str(row.get("sequence")): row for row in prefill_rows}
    for target in order_rows:
        sequence = str(target.get("sequence") or "")
        payload_row = prefill_by_sequence.get(sequence)
        mismatches = []
        route_unavailable = bool(payload_row and payload_row.get("route_status") != "route_available")
        if payload_row is None:
            mismatches.append("missing_payload_row")
        elif not route_unavailable:
            if payload_row.get("symbol") != target.get("symbol"):
                mismatches.append("symbol")
            if payload_row.get("side") != target.get("side"):
                mismatches.append("side")
            if payload_row.get("qty") != target.get("order_qty"):
                mismatches.append("qty")
            if payload_row.get("symbol_field") != str(target.get("symbol", "")).split(".")[0]:
                mismatches.append("symbol_field")
            if payload_row.get("qty_field") != str(target.get("order_qty")):
                mismatches.append("qty_field")
            if not payload_row.get("price_rmb") or payload_row.get("price_field") != payload_row.get("price_rmb"):
                mismatches.append("price_field")
        prefill_checks.append({
            "sequence": sequence,
            "symbol": target.get("symbol"),
            "status": "route_unavailable" if route_unavailable else "matched" if not mismatches else "mismatch",
            "mismatches": mismatches,
            "route_status": payload_row.get("route_status") if payload_row else None,
            "prefill_status": payload_row.get("prefill_status") if payload_row else None,
        })
    prefill_matched_count = sum(row["status"] == "matched" for row in prefill_checks)
    prefill_route_unavailable_count = sum(row["status"] == "route_unavailable" for row in prefill_checks)
    prefill_mismatch_count = sum(row["status"] == "mismatch" for row in prefill_checks)
    prefill_reconciliation = {
        "status": "payload_matches_supported_target_fields_with_route_limits" if prefill_checks and prefill_mismatch_count == 0 and prefill_route_unavailable_count else "payload_matches_target_order_fields" if prefill_checks and prefill_mismatch_count == 0 else "payload_target_mismatch",
        "target_order_count": len(order_rows),
        "payload_row_count": len(prefill_rows),
        "matched_count": prefill_matched_count,
        "route_unavailable_count": prefill_route_unavailable_count,
        "mismatch_count": prefill_mismatch_count,
        "rows": prefill_checks,
        "basis": "frozen paper target and offline THS payload only; this is not a client form readback",
    }
    prefill_template_path = artifact_path("ths_prefill_readback_template")
    prefill_template = read_json(prefill_template_path, "THS预填回读模板") if prefill_template_path else None
    prefill_readback_validation_path = artifact_path("ths_prefill_readback_validation")
    prefill_readback_validation = (
        read_json(prefill_readback_validation_path, "THS预填回读校验")
        if prefill_readback_validation_path
        else None
    )

    # Keep the selected-plan desk view self-contained: account risk and the
    # selected batch projection use the same evidence as the standalone
    # account-risk endpoints. Missing price/fee evidence remains a blocker.
    from .ths.account_risk import build_account_risk_view, evaluate_order_risk

    account_inputs = _operator_account_risk_inputs()
    account_risk = build_account_risk_view(
        account_inputs["package"],
        quote_observation=account_inputs["quote_observation"],
    )
    reference_by_sequence = {
        str(row.get("sequence")): row
        for row in (ths_reference or {}).get("rows") or []
    }
    proposed_orders = []
    for target in order_rows:
        sequence = str(target.get("sequence") or "")
        reference = reference_by_sequence.get(sequence) or {}
        proposed_orders.append({
            "order_id": f"selected-plan-{sequence or target.get('symbol')}",
            "symbol": target.get("symbol"),
            "side": target.get("side"),
            "qty": target.get("order_qty"),
            "price_rmb": reference.get("price_rmb"),
            "fee_cents": target.get("fee_cents"),
        })
    account_order_risk = evaluate_order_risk(
        account_inputs["package"],
        proposed_orders,
        quote_observation=account_inputs["quote_observation"],
    )
    account_order_risk["primary_evidence"] = account_inputs["package"].get("source_scope") or "missing"
    account_order_risk["native_observation"] = {
        "status": account_inputs["native_package"].get("status", "missing"),
        "source_scope": account_inputs["native_package"].get("source_scope", "missing"),
        "source_file": account_inputs["native_package"].get("source_file"),
        "account_observed": bool(account_inputs["native_package"].get("account_observed")),
        "orders_observed": bool(account_inputs["native_package"].get("orders_observed")),
        "trades_observed": bool(account_inputs["native_package"].get("trades_observed")),
        "cashflows_observed": bool(account_inputs["native_package"].get("cashflows_observed")),
        "ready_for_ledger_reconciliation": bool(
            account_inputs["native_package"].get("ready_for_ledger_reconciliation")
        ),
        "unknowns": account_inputs["native_package"].get("unknowns") or [],
    }
    stockdb_snapshot_path = artifact_path("stockdb_frozen_research_snapshot") or artifact_path("stockdb_frozen_snapshot")
    stockdb_independent_path = artifact_path("stockdb_independent_sample_compare") or artifact_path("stockdb_independent_sample")
    stockdb_stability_path = artifact_path("stockdb_independent_window_stability")
    selected_strategy_id = str(receipt.get("strategy_id") or "")
    stockdb_forward = _latest_stockdb_forward_compare(selected_strategy_id)
    frozen_snapshot_payload = read_json(stockdb_snapshot_path, "StockDB冻结快照") if stockdb_snapshot_path else {}
    frozen_snapshot_meta = package.get("stockdb_frozen_snapshot") or {}
    frozen_snapshot_view = dict(frozen_snapshot_meta) if frozen_snapshot_meta else frozen_snapshot_payload
    if frozen_snapshot_payload:
        frozen_snapshot_view.setdefault("snapshot_schema", frozen_snapshot_payload.get("schema"))
        frozen_snapshot_view.setdefault("snapshot_status", frozen_snapshot_payload.get("status"))
    research_evidence = {
        "frozen_stockdb_snapshot": frozen_snapshot_view or None,
        "independent_sample": read_json(stockdb_independent_path, "StockDB独立样本比较") if stockdb_independent_path else None,
        "window_stability": read_json(stockdb_stability_path, "StockDB窗口稳定性") if stockdb_stability_path else None,
        "forward_diagnostic": stockdb_forward,
        "latest_comparison_diagnostic": latest_comparison_diagnostic,
        "fixed_idea_frontier": fixed_idea_frontier,
        "fixed_profitability_diagnostic": fixed_profitability_diagnostic,
        "alpha158_linear_diagnostic": alpha158_linear_diagnostic,
        "alpha158_lightgbm_diagnostic": alpha158_lightgbm_diagnostic,
        "execution_admitted": False,
        "basis": "frozen research evidence only; no strategy promotion or broker admission",
    }
    promotion_review_path = artifact_path("strategy_promotion_review")
    if promotion_review_path:
        promotion_payload = read_json(promotion_review_path, "策略晋级审查")
        selected_strategy_id = str(receipt.get("strategy_id") or "")
        selected_review = next(
            (
                row for row in promotion_payload.get("rows") or []
                if str(row.get("strategy_id") or "") == selected_strategy_id
            ),
            None,
        )
        human_review_path = _promotion_review_receipt_path(package)
        human_review_receipt = (
            read_json(human_review_path, "人工晋级审查回执")
            if human_review_path.is_file() else None
        )
        research_evidence["promotion_review"] = {
            "status": promotion_payload.get("status"),
            "source_artifact": str(promotion_review_path),
            "selected_strategy": selected_review,
            "human_review_receipt": human_review_receipt,
            "fresh_holdout_evidence": promotion_payload.get("fresh_holdout_evidence") or [],
            "automatic_promotion": False,
            "execution_admitted": False,
        }
    else:
        research_evidence["promotion_review"] = None
    independent_sample = research_evidence["independent_sample"] or {}
    window_stability = research_evidence["window_stability"] or {}
    forward_diagnostic = research_evidence["forward_diagnostic"] or {}
    promotion_review = research_evidence["promotion_review"] or {}
    selected_review = promotion_review.get("selected_strategy") or {}
    selected_strategy_id = str(receipt.get("strategy_id") or "")
    independent_candidates = {
        str(row.get("candidate") or row.get("strategy_id") or "")
        for row in independent_sample.get("ledger_results") or []
        if isinstance(row, dict)
    }
    stability_candidates = {
        str(row.get("candidate") or row.get("strategy_id") or "")
        for row in window_stability.get("strategy_summaries") or []
        if isinstance(row, dict)
    }
    independent_strategy_covered = selected_strategy_id in independent_candidates
    stability_strategy_covered = selected_strategy_id in stability_candidates
    forward_candidates = {
        str(row.get("candidate") or "")
        for row in forward_diagnostic.get("candidates") or []
        if isinstance(row, dict)
    }
    forward_strategy_covered = selected_strategy_id in forward_candidates
    forward_row = next(
        (
            row for row in forward_diagnostic.get("candidates") or []
            if isinstance(row, dict) and str(row.get("candidate") or "") == selected_strategy_id
        ),
        None,
    )
    research_blockers = [
        "fresh_holdout_required",
        "explicit_human_review_required",
        "account_observation_required",
        "quote_observation_required",
    ]
    if not independent_strategy_covered:
        research_blockers.append("selected_strategy_independent_sample_missing")
    if not stability_strategy_covered:
        research_blockers.append("selected_strategy_window_stability_missing")
    if forward_row is not None and forward_row.get("coverage_complete") is not True:
        research_blockers.append("selected_strategy_forward_coverage_incomplete")
    research_evidence["admission_gate"] = {
        "status": "blocked_research_only",
        "strategy_id": selected_strategy_id,
        "same_cost_settings": bool((saved_comparison or {}).get("same_cost_settings")),
        "independent_sample_completed": independent_sample.get("independent_sample") is True,
        "independent_strategy_covered": independent_strategy_covered,
        "independent_strategy_candidates": sorted(independent_candidates),
        "independent_sample_status": independent_sample.get("status"),
        "window_stability_strategy_covered": stability_strategy_covered,
        "window_stability_strategy_candidates": sorted(stability_candidates),
        "window_stability_status": window_stability.get("status"),
        "forward_diagnostic_completed": forward_diagnostic.get("status") == "completed_research_only",
        "forward_strategy_covered": forward_strategy_covered,
        "forward_strategy_candidates": sorted(forward_candidates),
        "forward_strategy_coverage_complete": bool(
            forward_row and forward_row.get("coverage_complete") is True
        ),
        "forward_strategy_observed_symbols": forward_row.get("observed_symbols") if forward_row else None,
        "forward_strategy_target_symbols": forward_row.get("target_symbols") if forward_row else None,
        "forward_diagnostic_status": forward_diagnostic.get("status"),
        "fresh_holdout_claimed": False,
        "promotion_status": selected_review.get("promotion_status") or "blocked_pending_explicit_review",
        "automatic_promotion": False,
        "execution_admitted": False,
        "blockers": list(dict.fromkeys(research_blockers)),
    }
    release_readiness = paper_preview_readiness()
    # These flags describe evidence actually usable by the selected-plan
    # consumer.  They must not be hard-coded false: a complete user export can
    # be observed while the order gate still remains read-only or blocked.
    account_observed = bool(account_risk.get("account_ready"))
    quotes_observed = bool(account_inputs["quote_observation"])
    promotion_gate = {
        "status": promotion_review.get("status", "missing") if promotion_review else "missing",
        "strategy_id": receipt.get("strategy_id"),
        "promotion_status": selected_review.get(
            "promotion_status", "blocked_pending_explicit_review"
        ),
        "fresh_holdout_claimed": bool(
            promotion_review.get("fresh_holdout_claimed")
            if promotion_review else False
        ),
        "account_observed": bool(
            promotion_review.get("account_observed")
            if promotion_review else False
        ),
        "quotes_observed": bool(
            promotion_review.get("quotes_observed")
            if promotion_review else False
        ),
        "blockers": _normalize_blockers(selected_review.get("blockers")) if selected_review else [
            "strategy_promotion_review_missing"
        ],
        "execution_admitted": False,
    }
    research_gate = dict(research_evidence.get("admission_gate") or {})
    research_gate.setdefault("status", "blocked_research_only")
    research_gate["execution_admitted"] = False
    account_gate = {
        "status": account_risk.get("status"),
        "account_ready": bool(account_risk.get("account_ready")),
        "blockers": _normalize_blockers(
            account_risk.get("execution_blockers")
            or account_risk.get("blockers")
            or []
        ),
        "execution_admitted": False,
    }
    order_gate = {
        "status": account_order_risk.get("status"),
        "blockers": _normalize_blockers(
            account_order_risk.get("execution_blockers")
            or account_order_risk.get("blockers")
            or []
        ),
        "execution_admitted": False,
    }
    ledger_gate = {
        "status": structured_reconciliation.get("status", "missing"),
        "issue_count": (structured_reconciliation.get("summary") or {}).get(
            "issue_count", 0
        ),
        "ready_for_ledger_reconciliation": bool(
            structured_package.get("ready_for_ledger_reconciliation")
        ),
        "source_manifest_status": structured_package.get("source_manifest_status", "not_recorded"),
        "source_manifest_issues": structured_package.get("source_manifest_issues") or [],
        "execution_admitted": False,
    }
    release_gate = {
        "status": release_readiness.get("status"),
        "release_ready": bool(release_readiness.get("release_ready")),
        "blockers": _normalize_blockers(release_readiness.get("blockers") or []),
        "execution_admitted": False,
    }
    release_gates = {
        "schema": "offline_selected_plan_release_gates_v1",
        "status": "blocked",
        "execution_admitted": False,
        "gates": {
            "strategy_promotion": promotion_gate,
            "research_admission": research_gate,
            "account_risk": account_gate,
            "order_risk": order_gate,
            "same_ledger_reconciliation": ledger_gate,
            "paper_release": release_gate,
        },
        "priority": [
            "strategy_promotion",
            "research_admission",
            "account_risk",
            "order_risk",
            "same_ledger_reconciliation",
            "paper_release",
        ],
    }
    next_gate = next(
        (
            gate_name for gate_name in release_gates["priority"]
            if (
                release_gates["gates"][gate_name].get("blockers")
                or release_gates["gates"][gate_name].get("issue_count", 0)
                or release_gates["gates"][gate_name].get("ready_for_ledger_reconciliation") is False
                or release_gates["gates"][gate_name].get("release_ready") is False
                or release_gates["gates"][gate_name].get("account_ready") is False
                or release_gates["gates"][gate_name].get("status") in {
                    "missing", "not_available", "blocked", "blocked_research_only",
                    "structured_package_not_ready", "isolated_rehearsal_only",
                    "reconciliation_required",
                }
            )
        ),
        "paper_release",
    )
    release_gates["next_gate"] = next_gate
    release_gates["next_action"] = {
        "strategy_promotion": "完成未参与调参的新留出窗口，并由人工记录审查决定；不自动晋级。",
        "research_admission": "补齐选中策略的独立样本、窗口稳定性和前瞻覆盖；保持研究态。",
        "account_risk": "导入同日现金、持仓、可卖数量和冻结资金的持久账户证据。",
        "order_risk": "在账户证据通过后重新运行逐单 A 股规则、现金和 T+1 风控。",
        "same_ledger_reconciliation": "导入同花顺 orders/trades/cashflows 四表并与同一 ths_plans 对账。",
        "paper_release": "修正纸面释放决策中的阻塞行，仍不提交真实委托。",
    }[next_gate]
    return {
        "schema": "offline_selected_plan_view_v1",
        "status": "offline_selected_plan_ready",
        "execution_day": execution_day,
        "selected_strategy_id": receipt.get("strategy_id"),
        "execution_admitted": False,
        "account_observed": account_observed,
        "quotes_observed": quotes_observed,
        "client_interacted": False,
        "summary": {
            "target_order_count": len(order_rows),
            "target_status_counts": dict(Counter(row.get("status") or "unknown" for row in order_rows)),
            "active_target_count": sum(row.get("active_target", "false").lower() == "true" for row in order_rows),
            "ths_order_count": (ths_reference or {}).get("order_count", 0),
            "ths_route_available_count": (ths_reference or {}).get("route_available_count", 0),
            "paper_event_count": len(lifecycle_rows),
            "paper_state_counts": dict(Counter(row.get("state") or "unknown" for row in lifecycle_rows)),
            "paper_reconciliation_count": len(reconciliation_rows),
            "paper_reconciliation_status_counts": dict(Counter(row.get("status") or "unknown" for row in reconciliation_rows)),
            "paper_release_decision_counts": dict(Counter(row.get("paper_release_decision") or "unknown" for row in release_rows)),
            "prefill_reconciliation_status": prefill_reconciliation["status"],
            "prefill_matched_count": prefill_reconciliation["matched_count"],
            "prefill_route_unavailable_count": prefill_reconciliation["route_unavailable_count"],
            "prefill_mismatch_count": prefill_reconciliation["mismatch_count"],
            "prefill_readback_validation_status": (prefill_readback_validation or {}).get("status"),
            "prefill_readback_complete": bool((prefill_readback_validation or {}).get("readback_complete")),
            "prefill_readback_pending_count": (prefill_readback_validation or {}).get("pending_row_count"),
            "prefill_readback_mismatch_count": (prefill_readback_validation or {}).get("mismatch_row_count"),
            "account_risk_status": account_risk.get("status"),
            "account_risk_blocker_count": len(account_risk.get("blockers") or []),
            "account_order_risk_status": account_order_risk.get("status"),
            "account_order_risk_blocker_count": len(account_order_risk.get("blockers") or []),
            "release_readiness_status": release_readiness.get("status"),
            "release_readiness_blocker_count": len(release_readiness.get("blockers") or []),
            "release_ready": bool(release_readiness.get("release_ready")),
            "research_snapshot_status": (research_evidence["frozen_stockdb_snapshot"] or {}).get("status"),
            "research_independent_sample_status": (research_evidence["independent_sample"] or {}).get("status"),
            "research_window_stability_status": (research_evidence["window_stability"] or {}).get("status"),
            "research_forward_diagnostic_status": (research_evidence["forward_diagnostic"] or {}).get("status"),
            "research_forward_diagnostic_source_cutoff": (research_evidence["forward_diagnostic"] or {}).get("source_cutoff"),
            "research_fixed_idea_frontier_status": (research_evidence["fixed_idea_frontier"] or {}).get("status"),
            "research_fixed_idea_frontier_candidate_count": len(
                (research_evidence["fixed_idea_frontier"] or {}).get("candidates") or []
            ),
            "research_fixed_profitability_status": (research_evidence["fixed_profitability_diagnostic"] or {}).get("status"),
            "research_fixed_profitability_candidate_count": len(
                (research_evidence["fixed_profitability_diagnostic"] or {}).get("candidates") or []
            ),
            "research_alpha158_linear_status": (research_evidence["alpha158_linear_diagnostic"] or {}).get("status"),
            "research_alpha158_linear_candidate_count": len(
                (research_evidence["alpha158_linear_diagnostic"] or {}).get("candidates") or []
            ),
            "research_alpha158_lightgbm_status": (research_evidence["alpha158_lightgbm_diagnostic"] or {}).get("status"),
            "research_alpha158_lightgbm_candidate_count": len(
                (research_evidence["alpha158_lightgbm_diagnostic"] or {}).get("candidates") or []
            ),
            "structured_package_status": structured_package.get("status"),
            "structured_package_source_scope": structured_package.get("source_scope"),
            "structured_package_ready_for_ledger_reconciliation": bool(
                structured_package.get("ready_for_ledger_reconciliation")
            ),
            "complete_structured_package_status": complete_structured_package.get("status"),
            "complete_structured_package_source_scope": complete_structured_package.get("source_scope"),
            "complete_structured_package_ready_for_ledger_reconciliation": bool(
                complete_structured_package.get("ready_for_ledger_reconciliation")
            ),
            "ledger_import_preview_status": ledger_import_preview.get("status"),
            "ledger_import_preview_would_change": bool(ledger_import_preview.get("would_change")),
            "risk_flags": (risk_row or {}).get("risk_flags"),
            "paper_cash_after_buys_rmb": (risk_row or {}).get("paper_cash_after_buys_only_rmb"),
        },
        "target_orders": order_rows,
        "ths_reference": ths_reference,
        "prefill_reconciliation": prefill_reconciliation,
        "prefill_readback_template": prefill_template,
        "prefill_readback_validation": prefill_readback_validation,
        "research_evidence": research_evidence,
        "paper": {
            "lifecycle": {"row_count": len(lifecycle_rows), "state_counts": dict(Counter(row.get("state") or "unknown" for row in lifecycle_rows)), "rows": lifecycle_rows},
            "reconciliation": {"row_count": len(reconciliation_rows), "status_counts": dict(Counter(row.get("status") or "unknown" for row in reconciliation_rows)), "rows": reconciliation_rows},
            "release": {"row_count": len(release_rows), "decision_counts": dict(Counter(row.get("paper_release_decision") or "unknown" for row in release_rows)), "rows": release_rows},
            "next_session": carry,
        },
        "risk_row": risk_row,
        "account_risk": account_risk,
        "account_order_risk": account_order_risk,
        "release_readiness": release_readiness,
        "release_gates": release_gates,
        "saved_comparison": saved_comparison,
        "decision_queue": decision_queue,
        "structured_observation_package": structured_package,
        "structured_observation_package_complete": complete_structured_package,
        "source_manifest": complete_structured_package.get("source_manifest"),
        "source_manifest_status": complete_structured_package.get("source_manifest_status", "not_recorded"),
        "source_manifest_issues": complete_structured_package.get("source_manifest_issues") or [],
        "structured_package_ready_for_ledger_reconciliation": bool(
            structured_package.get("ready_for_ledger_reconciliation")
        ),
        "complete_structured_package_ready_for_ledger_reconciliation": bool(
            complete_structured_package.get("ready_for_ledger_reconciliation")
        ),
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "ledger_import_preview": ledger_import_preview,
        "ledger_import_preview_status": ledger_import_preview.get("status"),
        "broker_capabilities": _broker_capability_snapshot(),
        "instrument_master": instrument_master_view,
        "source_files": {
            "receipt": str(receipt_path),
            "target_orders": str(target_path) if target_path else None,
            "paper_lifecycle": str(lifecycle_path) if lifecycle_path else None,
            "paper_reconciliation": str(reconciliation_path) if reconciliation_path else None,
            "paper_release": str(release_path) if release_path else None,
            "paper_next_session": str(carry_path) if carry_path else None,
            "portfolio_risk": str(risk_path) if risk_path else None,
            "ths_prefill_readback_template": str(prefill_template_path) if prefill_template_path else None,
            "ths_prefill_readback_validation": str(prefill_readback_validation_path) if prefill_readback_validation_path else None,
            "stockdb_frozen_snapshot": str(stockdb_snapshot_path) if stockdb_snapshot_path else None,
            "stockdb_independent_sample": str(stockdb_independent_path) if stockdb_independent_path else None,
            "stockdb_independent_window_stability": str(stockdb_stability_path) if stockdb_stability_path else None,
            "saved_ledger_comparison": str(saved_comparison_path) if saved_comparison_path else None,
            "decision_queue": str(decision_queue_path) if decision_queue_path else None,
            "desk_snapshot": str(desk_snapshot_path) if desk_snapshot_path.is_file() else None,
        },
        "next_action": receipt.get("next_action") or "保留纸面方案，待账户与同日行情观察后再人工复核；当前不提交委托。",
    }


def _package_artifact(package, name):
    value = (package.get("artifact_pointers") or {}).get(name)
    path = Path(str(value)) if value else None
    return path if path and path.is_file() else None


def _operator_account_risk_inputs():
    """Select the same read-only account evidence for every desk consumer."""
    complete_package = _latest_complete_structured_observation_package()
    native_package = _latest_native_structured_observation_package()
    quote_preview = _quote_observation_preview()
    quote_observation = (
        quote_preview.get("observation")
        if quote_preview.get("status") == "ready_for_import"
        else None
    )
    if complete_package.get("source_scope") == "persistent_user_export":
        package = complete_package
    elif native_package:
        package = native_package
    else:
        package = complete_package
    return {
        "complete_package": complete_package,
        "native_package": native_package,
        "quote_preview": quote_preview,
        "quote_observation": quote_observation,
        "package": package,
    }


@router.get("/operator-package/instrument-master")
def operator_package_instrument_master():
    """Expose cutoff-matched security identity and research-scope diagnostics."""
    package = operator_package()
    coverage_path = _package_artifact(package, "instrument_master_coverage")
    snapshot_path = Path(str(package.get("trader_desk_snapshot") or ""))
    if not coverage_path or not snapshot_path.is_file():
        raise HTTPException(404, "当前离线包缺少证券主表覆盖诊断")
    try:
        coverage = json.loads(coverage_path.read_text(encoding="utf-8-sig"))
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"证券主表诊断尚不可读取：{error}") from error
    if (coverage.get("schema") != "instrument_master_coverage_v1"
            or coverage.get("execution_admitted") is not False):
        raise HTTPException(409, "证券主表覆盖诊断版本或执行边界不受支持")
    return {
        "schema": "offline_instrument_master_view_v1",
        "status": snapshot.get("instrument_master", {}).get("status", "unknown"),
        "source_cutoff": package.get("source_cutoff"),
        "instrument_master": snapshot.get("instrument_master") or {},
        "coverage": snapshot.get("instrument_master_coverage") or {
            "status": coverage.get("status"),
            "source_only": coverage.get("source_only") or [],
            "master_only": coverage.get("master_only") or [],
        },
        "source_files": {
            "snapshot": str(snapshot_path),
            "coverage": str(coverage_path),
            "master": (snapshot.get("instrument_master") or {}).get("master"),
        },
        "research_only": True,
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "basis": "cutoff-matched identity metadata and source-scope diagnostic; not PIT or broker authority",
    }


@router.get("/operator-package/lifecycle")
def operator_package_lifecycle():
    """Return normalized paper lifecycle, carry and recovery state for the desk UI."""
    package = operator_package()
    research_evidence_summary = _research_evidence_consumer_summary(package.get("strategy_id"))
    structured_package = _latest_structured_observation_package()
    complete_structured_package = _latest_complete_structured_observation_package()
    ledger_import_preview = package.get("ths_ledger_import_preview_rehearsal") or {}
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    release_readiness = paper_preview_readiness()
    lifecycle_path = _package_artifact(package, "paper_order_lifecycle")
    carry_path = _package_artifact(package, "paper_next_session_carry")
    ledger_path = _package_artifact(package, "paper_ledger_projection")
    reconciliation_path = _package_artifact(package, "paper_reconciliation")
    release_path = _package_artifact(package, "paper_release_decision")
    recovery_path = _package_artifact(package, "recovery_plan")
    target_reconciliation_path = _package_artifact(package, "target_order_reconciliation")
    settlement_projection_path = _package_artifact(package, "allocation_settlement_projection")

    def read_csv(path):
        if not path:
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    lifecycle_rows = read_csv(lifecycle_path)
    release_rows = [row for row in read_csv(release_path) if row.get("row_type", "order") == "order"]
    reconciliation_rows = read_csv(reconciliation_path)
    recovery_rows = read_csv(recovery_path)
    target_reconciliation_rows = [row for row in read_csv(target_reconciliation_path) if row.get("row_type", "order") == "order"]
    settlement_projection_rows = [row for row in read_csv(settlement_projection_path) if row.get("row_type", "order") == "order"]
    carry = json.loads(carry_path.read_text(encoding="utf-8")) if carry_path else {}
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path else {}
    release_decision_counts = Counter(row.get("paper_release_decision", "unknown") for row in release_rows)
    reconciliation_status_counts = Counter(row.get("status", "unknown") for row in reconciliation_rows)
    exception_queue = []
    exception_specs = [
        ("blocked_missing_live_inputs", "P0", "缺少同日账户/行情输入，保持订单不释放", "paper_release_decision"),
        ("rejected_preflight", "P1", "回到账本预检逐单修正数量、现金或规则原因", "paper_release_decision"),
        ("manual_review_target_delta", "P1", "人工复核目标持仓与订单映射差异", "paper_release_decision"),
        ("unobserved", "P1", "补充或确认内部对账所需的外部观察", "paper_reconciliation"),
    ]
    for value, priority, action, source in exception_specs:
        count = (release_decision_counts if source == "paper_release_decision" else reconciliation_status_counts).get(value, 0)
        if count:
            exception_queue.append({
                "priority": priority,
                "exception": value,
                "count": count,
                "action": action,
                "source": source,
                "execution_admitted": False,
            })
    return {
        "schema": "offline_paper_lifecycle_view_v1",
        "status": "paper_lifecycle_view",
        "execution_day": package.get("execution_day"),
        "strategy_id": package.get("strategy_id"),
        "lifecycle": {
            "order_count": len({row.get("sequence") for row in lifecycle_rows if row.get("sequence")}),
            "event_count": len(lifecycle_rows),
            "state_counts": dict(Counter(row.get("state", "unknown") for row in lifecycle_rows)),
            "real_broker_state_count": sum(row.get("real_broker_state", "false").lower() == "true" for row in lifecycle_rows),
        },
        "carry": {
            "pending_settlement_order_count": carry.get("pending_settlement_order_count", 0),
            "pending_t1_buy_count": len(carry.get("pending_t1_buys") or []),
            "sell_release_review_count": len(carry.get("sell_release_review") or []),
            "next_exchange_session": carry.get("next_exchange_session"),
            "next_exchange_session_status": carry.get("next_exchange_session_status"),
            "synthetic_cash_delta_rmb": carry.get("synthetic_cash_delta_rmb"),
        },
        "ledger": {
            "synthetic_full_fill_count": ledger.get("synthetic_full_fill_count", 0),
            "cash_delta_rmb": ledger.get("cash_delta_rmb"),
            "internal_reconciliation_status": ledger.get("internal_reconciliation_status"),
            "live_reconciliation_status": ledger.get("live_reconciliation_status"),
            "t1_policy": ledger.get("t1_policy"),
        },
        "release": {
            "decision_counts": dict(release_decision_counts),
            "reason_counts": dict(Counter(row.get("reason", "") for row in release_rows)),
            "rows": release_rows,
        },
        "reconciliation": {
            "status_counts": dict(reconciliation_status_counts),
            "checks": reconciliation_rows,
        },
        "exception_queue": exception_queue,
        "target_reconciliation": {
            "order_count": len(target_reconciliation_rows),
            "status_counts": dict(Counter(row.get("status", "unknown") for row in target_reconciliation_rows)),
            "active_target_count": sum(row.get("active_target", "false").lower() == "true" for row in target_reconciliation_rows),
            "account_holdings_observed_count": sum(row.get("account_holdings_observed", "false").lower() == "true" for row in target_reconciliation_rows),
            "rows": target_reconciliation_rows,
        },
        "settlement_projection": {
            "order_count": len(settlement_projection_rows),
            "state_counts": dict(Counter(row.get("settlement_state", "unknown") for row in settlement_projection_rows)),
            "t1_dependency_counts": dict(Counter(row.get("t1_dependency", "unknown") for row in settlement_projection_rows)),
            "rows": settlement_projection_rows,
        },
        "recovery": {"rows": recovery_rows, "retry_permitted": False},
        "structured_observation_package": structured_package,
        "structured_observation_package_complete": complete_structured_package,
        "structured_package_ready_for_ledger_reconciliation": bool(
            structured_package.get("ready_for_ledger_reconciliation")
        ),
        "complete_structured_package_ready_for_ledger_reconciliation": bool(
            complete_structured_package.get("ready_for_ledger_reconciliation")
        ),
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "order_count": structured_reconciliation.get("order_count"),
        "trade_count": structured_reconciliation.get("trade_count"),
        "cashflow_count": structured_reconciliation.get("cashflow_count"),
        "ledger_import_preview": ledger_import_preview,
        "ledger_import_preview_status": ledger_import_preview.get("status"),
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        "observed_fill_tca": _observed_fill_tca_summary(same_ledger_bundle_reconciliation),
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "release_readiness": release_readiness,
        "release_readiness_status": release_readiness.get("status"),
        "release_ready": bool(release_readiness.get("release_ready")),
        "research_evidence_summary": research_evidence_summary,
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
    }


@router.get("/operator-package/evidence")
def operator_package_evidence():
    """Return the saved offline evidence card and promotion review for operator comparison."""
    from .research.operator_evidence_view import build_view
    try:
        return build_view(operator_package(), _package_artifact)
    except FileNotFoundError as error:
        raise HTTPException(404, "当前离线包缺少策略证据卡或晋级审查产物") from error
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        raise HTTPException(409, f"离线策略证据尚不可读取：{error}") from error


@router.get("/operator-package/decision-queue")
def operator_package_decision_queue():
    """Expose the saved strategy-level paper decision queue without broker I/O."""
    from .research.operator_decision_queue_view import UnsupportedDecisionQueue, build_view
    try:
        return build_view(
            operator_package(), _package_artifact,
            _ths_journal_reconciliation_view(),
            _same_ledger_bundle_reconciliation_view(),
            _same_ledger_compat_fields,
        )
    except FileNotFoundError as error:
        raise HTTPException(404, "当前离线包缺少交易员决策队列") from error
    except UnsupportedDecisionQueue as error:
        raise HTTPException(409, "交易员决策队列版本不受支持") from error
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        raise HTTPException(409, f"交易员决策队列尚不可读取：{error}") from error


@router.get("/operator-package/research-exceptions")
def operator_package_research_exceptions():
    """Return the latest research exception boundary without rerunning research."""
    return _latest_research_exception_view()


@router.get("/operator-package/execution-evidence")
def operator_package_execution_evidence():
    """Expose saved paper TCA, risk and child-order diagnostics without broker I/O."""
    package = operator_package()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    pointers = {
        "paper_execution_cost_stress": _package_artifact(package, "paper_execution_cost_stress"),
        "tca_pretrade": _package_artifact(package, "tca_pretrade"),
        "risk_checks": _package_artifact(package, "risk_checks"),
        "child_order_schedule": _package_artifact(package, "child_order_schedule"),
        "paper_child_order_reconciliation": _package_artifact(
            package, "paper_child_order_reconciliation"
        ),
        "paper_child_order_reconciliation_summary": _package_artifact(
            package, "paper_child_order_reconciliation_summary"
        ),
        "factor_candidate_cost_replay": _package_artifact(package, "factor_candidate_cost_replay"),
        "factor_candidate_cadence_replay": _package_artifact(package, "factor_candidate_cadence_replay"),
    }
    if any(pointers[name] is None for name in (
        "paper_execution_cost_stress", "tca_pretrade", "risk_checks", "child_order_schedule"
    )):
        raise HTTPException(404, "当前离线包缺少纸面 TCA、风险或子单产物")

    def read_csv(path):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error) as error:
            raise HTTPException(409, f"纸面执行证据尚不可读取：{error}") from error

    tca_rows = read_csv(pointers["tca_pretrade"])
    stress_rows = read_csv(pointers["paper_execution_cost_stress"])
    risk_rows = read_csv(pointers["risk_checks"])
    child_rows = read_csv(pointers["child_order_schedule"])
    broker_observation_alerts = _broker_observation_alerts(same_ledger_bundle_reconciliation)
    child_reconciliation_rows = (
        read_csv(pointers["paper_child_order_reconciliation"])
        if pointers["paper_child_order_reconciliation"] is not None
        else []
    )
    child_reconciliation_summary = {
        "schema": "paper_child_order_reconciliation_v1",
        "status": "not_available",
        "parent_count": 0,
        "scheduled_parent_count": 0,
        "complete_synthetic_parent_count": 0,
        "rejected_parent_count": 0,
        "mismatch_count": 0,
        "real_broker_state": False,
        "execution_admitted": False,
    }
    summary_path = pointers["paper_child_order_reconciliation_summary"]
    if summary_path is not None:
        try:
            candidate_summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"父子单对账摘要尚不可读取：{error}") from error
        if (
            candidate_summary.get("schema") != "paper_child_order_reconciliation_v1"
            or candidate_summary.get("execution_admitted") is not False
            or candidate_summary.get("real_broker_state") is not False
        ):
            raise HTTPException(409, "父子单对账产物版本或执行边界不受支持")
        child_reconciliation_summary = candidate_summary
    factor_cost_payload = None
    factor_cost_path = pointers["factor_candidate_cost_replay"]
    if factor_cost_path is not None:
        try:
            factor_cost_payload = json.loads(factor_cost_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"因子成本路径尚不可读取：{error}") from error
        if factor_cost_payload.get("status") != "completed_factor_candidate_cost_replay":
            factor_cost_payload = None
    factor_cadence_payload = None
    factor_cadence_path = pointers["factor_candidate_cadence_replay"]
    if factor_cadence_path is not None:
        try:
            factor_cadence_payload = json.loads(factor_cadence_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"因子调仓频率路径尚不可读取：{error}") from error
        if factor_cadence_payload.get("status") != "completed_factor_candidate_cadence_replay":
            factor_cadence_payload = None
    risk_status_counts = Counter(
        f"{row.get('check', 'unknown')}|{row.get('status', 'unknown')}" for row in risk_rows
    )
    risk_attention_rows = [
        row for row in risk_rows
        if row.get("status", "").lower() in {"blocked", "review", "fail", "rejected"}
    ]
    risk_summary_specs = {
        "copied_ledger_preflight": "复用统一账本预检结果",
        "a_share_buy_lot": "复核整手买入与卖出零股规则",
        "same_session_account": "补齐同日账户观察后再复核",
        "same_session_raw_rmb_quote": "补齐同日不复权人民币行情后再复核",
        "execution_scope": "保持纸面/主板范围并人工确认执行范围",
    }
    risk_summary = []
    grouped_risk_statuses = {}
    for row in risk_rows:
        check = row.get("check") or "unknown"
        status = row.get("status") or "unknown"
        grouped_risk_statuses.setdefault(check, Counter())[status] += 1
    for check in sorted(grouped_risk_statuses):
        status_counts = grouped_risk_statuses[check]
        risk_summary.append({
            "check": check,
            "status_counts": dict(status_counts),
            "attention_count": sum(
                count for status, count in status_counts.items()
                if status.lower() in {"blocked", "review", "fail", "rejected"}
            ),
            "next_action": risk_summary_specs.get(check, "保留逐单结果并在纸面执行前人工复核"),
        })
    candidate_by_strategy = {
        str(row.get("strategy_id")): row for row in (package.get("strategy_candidates") or [])
        if row.get("strategy_id")
    }
    stress_by_strategy = {}
    for row in stress_rows:
        stress_by_strategy.setdefault(row.get("strategy_id") or "unknown", []).append(row)
    risk_by_strategy = {}
    for row in risk_rows:
        risk_by_strategy.setdefault(row.get("strategy_id") or "unknown", []).append(row)
    child_by_strategy = {}
    for row in child_rows:
        child_by_strategy.setdefault(row.get("strategy_id") or "unknown", []).append(row)
    child_reconciliation_by_strategy = {}
    for row in child_reconciliation_rows:
        strategy_id = row.get("strategy_id") or "unknown"
        summary = child_reconciliation_by_strategy.setdefault(strategy_id, {
            "parent_count": 0,
            "complete_synthetic_parent_count": 0,
            "rejected_parent_count": 0,
            "mismatch_count": 0,
        })
        summary["parent_count"] += 1
        if row.get("reconciliation_status") == "pass_synthetic_complete":
            summary["complete_synthetic_parent_count"] += 1
        if row.get("parent_execution_status") == "not_executed_preflight_rejected":
            summary["rejected_parent_count"] += 1
        if row.get("reconciliation_status") not in {
            "pass_synthetic_complete", "not_scheduled_preflight_rejected"
        }:
            summary["mismatch_count"] += 1

    def stress_row_for(rows, bps):
        return next((row for row in rows if row.get("additional_slippage_bps") == str(bps)), {})

    strategy_execution_summary = []
    for strategy_id in sorted(set(stress_by_strategy) | set(risk_by_strategy) | set(child_by_strategy)):
        stress_for_strategy = stress_by_strategy.get(strategy_id, [])
        baseline = stress_row_for(stress_for_strategy, 0)
        stressed = stress_row_for(stress_for_strategy, 50)
        risk_for_strategy = risk_by_strategy.get(strategy_id, [])
        attention_for_strategy = [
            row for row in risk_for_strategy
            if row.get("status", "").lower() in {"blocked", "review", "fail", "rejected"}
        ]
        candidate = candidate_by_strategy.get(strategy_id, {})
        strategy_execution_summary.append({
            "strategy_id": strategy_id,
            "paper_net_return": candidate.get("net_return"),
            "baseline_cost_rmb": baseline.get("estimated_total_execution_cost_rmb"),
            "cost_at_50bps_rmb": stressed.get("estimated_total_execution_cost_rmb"),
            "synthetic_filled_notional_rmb": baseline.get("synthetic_filled_notional_rmb"),
            "risk_attention_count": len(attention_for_strategy),
            "risk_row_count": len(risk_for_strategy),
            "child_order_count": len(child_by_strategy.get(strategy_id, [])),
            "child_parent_count": len({
                row.get("parent_sequence") for row in child_by_strategy.get(strategy_id, [])
                if row.get("parent_sequence")
            }),
            "child_reconciliation": child_reconciliation_by_strategy.get(strategy_id, {
                "parent_count": 0,
                "complete_synthetic_parent_count": 0,
                "rejected_parent_count": 0,
                "mismatch_count": 0,
            }),
            "execution_status": "paper_diagnostics_only",
        })
    return {
        "schema": "offline_execution_evidence_view_v2",
        "status": "paper_diagnostics_only",
        "execution_admitted": False,
        "real_broker_state": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        "observed_fill_tca": _observed_fill_tca_summary(same_ledger_bundle_reconciliation),
        "broker_observation_alerts": broker_observation_alerts,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "tca_pretrade": {"rows": tca_rows, "estimated_all_in_cost_rmb": next(
            (row.get("value") for row in tca_rows if row.get("metric") == "estimated_all_in_cost_rmb"), None)},
        "cost_stress": {"row_count": len(stress_rows), "rows": stress_rows},
        "risk_checks": {
            "row_count": len(risk_rows),
            "status_counts": dict(risk_status_counts),
            "attention_row_count": len(risk_attention_rows),
            "attention_rows": risk_attention_rows,
            "summary": risk_summary,
        },
        "strategy_execution_summary": strategy_execution_summary,
        "child_order_schedule": {
            "row_count": len(child_rows),
            "parent_count": len({row.get("parent_sequence") for row in child_rows if row.get("parent_sequence")}),
            "rows": child_rows,
        },
        "child_order_reconciliation": {
            "schema": child_reconciliation_summary.get("schema"),
            "status": child_reconciliation_summary.get("status"),
            "parent_count": child_reconciliation_summary.get("parent_count", 0),
            "scheduled_parent_count": child_reconciliation_summary.get("scheduled_parent_count", 0),
            "complete_synthetic_parent_count": child_reconciliation_summary.get(
                "complete_synthetic_parent_count", 0
            ),
            "rejected_parent_count": child_reconciliation_summary.get("rejected_parent_count", 0),
            "mismatch_count": child_reconciliation_summary.get("mismatch_count", 0),
            "real_broker_state": False,
            "execution_admitted": False,
            "row_count": len(child_reconciliation_rows),
            "rows": child_reconciliation_rows,
            "by_strategy": child_reconciliation_by_strategy,
            "summary_artifact": str(summary_path) if summary_path else None,
            "rows_artifact": (
                str(pointers["paper_child_order_reconciliation"])
                if pointers["paper_child_order_reconciliation"]
                else None
            ),
        },
        "factor_cost_replay": {
            "status": (factor_cost_payload or {}).get("status", "not_available"),
            "source_cutoff": (factor_cost_payload or {}).get("end"),
            "source_run": (factor_cost_payload or {}).get("source_run"),
            "signal": (factor_cost_payload or {}).get("signal"),
            "control_signal": (factor_cost_payload or {}).get("control_signal"),
            "execution_policy": (factor_cost_payload or {}).get("execution_policy"),
            "result": (factor_cost_payload or {}).get("result"),
            "limitations": (factor_cost_payload or {}).get("limitations") or [],
            "artifact": str(factor_cost_path) if factor_cost_path else None,
            "interpretation": "固定最低佣金内部交易死区诊断；改变持仓路径，不是固定成交费用加回，也不是新留出测试或晋级依据",
            "basis": "same-ledger fixed cost-path diagnostic; not a fixed-fill addback, fresh holdout or promotion decision",
            "execution_admitted": False,
        },
        "factor_cadence_replay": {
            "status": (factor_cadence_payload or {}).get("status", "not_available"),
            "source_cutoff": (factor_cadence_payload or {}).get("end"),
            "source_run": (factor_cadence_payload or {}).get("source_run"),
            "signal": (factor_cadence_payload or {}).get("signal"),
            "control_signal": (factor_cadence_payload or {}).get("control_signal"),
            "execution_policy": (factor_cadence_payload or {}).get("execution_policy"),
            "result": (factor_cadence_payload or {}).get("result"),
            "limitations": (factor_cadence_payload or {}).get("limitations") or [],
            "artifact": str(factor_cadence_path) if factor_cadence_path else None,
            "interpretation": "固定双月调仓频率诊断；复用原排名和账本，不按收益搜索频率，也不是策略晋级依据",
            "basis": "same-ledger fixed cadence diagnostic; not a return-selected parameter or promotion decision",
            "execution_admitted": False,
        },
        "basis": "saved frozen-reference paper projections; no fill, broker acknowledgement or live quote was observed",
    }


@router.get("/operator-package/broker-observation-alerts")
def operator_package_broker_observation_alerts():
    """Expose broker acknowledgement/fill alerts without requiring TCA files."""
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    alerts = _broker_observation_alerts(same_ledger_bundle_reconciliation)
    persistent_readiness = ths_observation_bundle_readiness()
    persistent_profile = persistent_readiness.get("profile") or {}
    persistent_mapping = persistent_profile.get("mapping_validation") or {}
    persistent_source_scope = same_ledger_bundle_reconciliation.get("source_scope")
    persistent_manifest_status = persistent_readiness.get("source_manifest_status")
    persistent_bundle_is_account_evidence = bool(
        persistent_readiness.get("status") == "bundle_ready_for_reconciliation"
        and persistent_manifest_status == "verified"
    )
    return {
        **alerts,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        "persistent_bundle_readiness": {
            "status": persistent_readiness.get("status"),
            "preview_status": (persistent_readiness.get("preview") or {}).get("status"),
            "source_manifest_status": persistent_manifest_status,
            "source_manifest_issues": persistent_readiness.get("source_manifest_issues") or [],
            "profile_mapping_status": persistent_mapping.get("status"),
            "profile_mapping_issues": persistent_mapping.get("issues") or [],
            "bundle_is_account_evidence": persistent_bundle_is_account_evidence,
            "execution_admitted": False,
            "ledger_mutated": False,
        },
        "alert_source_selection": {
            "current_persistent_bundle_status": persistent_readiness.get("status"),
            "reconciliation_source_scope": persistent_source_scope,
            "reconciliation_source_manifest_status": same_ledger_bundle_reconciliation.get(
                "source_manifest_status"
            ),
            "reconciliation_is_account_evidence": bool(
                persistent_source_scope == "persistent_user_export"
                and same_ledger_bundle_reconciliation.get("source_manifest_status") == "verified"
            ),
            "synthetic_source_is_not_account_evidence": persistent_source_scope != "persistent_user_export",
            "basis": "current persistent bundle readiness is authoritative for account evidence; isolated rehearsal remains diagnostic only",
        },
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "client_interacted": False,
        "next_action": (
            "treat the listed differences as synthetic rehearsal evidence; provide a verified persistent THS export before account-level recovery"
            if alerts["alerts"] and not alerts["live_alerts"] else
            "resolve the listed broker/Ledger differences before any recovery"
            if alerts["alerts"] else
            "retain the explicit wait state; provide a verified persistent THS export before treating alerts as broker evidence"
        ),
    }


@router.get("/operator-package/portfolio-controls")
def operator_package_portfolio_controls():
    """Expose frozen paper what-if and allocation projections as read-only desk controls."""
    package = operator_package()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    scenario_path = _package_artifact(package, "scenario_analysis")
    allocation_path = _package_artifact(package, "allocation_settlement_projection")
    if not scenario_path or not allocation_path:
        raise HTTPException(404, "当前离线包缺少纸面情景或分配结算投影")

    def read_csv(path):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error) as error:
            raise HTTPException(409, f"纸面组合控制尚不可读取：{error}") from error

    scenario_rows = read_csv(scenario_path)
    allocation_all_rows = read_csv(allocation_path)
    allocation_order_rows = [row for row in allocation_all_rows if row.get("row_type", "order") == "order"]
    allocation_total_rows = [row for row in allocation_all_rows if row.get("row_type") != "order"]
    return {
        "schema": "offline_portfolio_controls_view_v1",
        "status": "paper_projection_only",
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "scenario": {
            "row_count": len(scenario_rows),
            "rows": scenario_rows,
            "basis": "frozen research reference prices; paper what-if only; no quote or fill observed",
        },
        "allocation": {
            "order_count": len(allocation_order_rows),
            "total_rows": allocation_total_rows,
            "state_counts": dict(Counter(row.get("settlement_state", "unknown") for row in allocation_order_rows)),
            "t1_dependency_counts": dict(Counter(row.get("t1_dependency", "unknown") for row in allocation_order_rows)),
            "rows": allocation_order_rows,
            "basis": "frozen research reference price; no fill or account observation",
        },
        "source_files": {"scenario": str(scenario_path), "allocation": str(allocation_path)},
    }


@router.get("/operator-package/portfolio-risk")
def operator_package_portfolio_risk():
    """Expose the saved, paper-only candidate portfolio risk book."""
    package = operator_package()
    risk_path = _package_artifact(package, "offline_portfolio_risk_summary")
    if not risk_path:
        raise HTTPException(404, "当前离线包缺少候选组合风险书")
    try:
        payload = json.loads(risk_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"候选组合风险书尚不可读取：{error}") from error
    if payload.get("schema") != "offline_portfolio_risk_summary_v1":
        raise HTTPException(409, "候选组合风险书版本不受支持")
    return {
        "schema": "offline_portfolio_risk_view_v1",
        "status": payload.get("status", "offline_reference_risk_comparison"),
        "execution_day": payload.get("execution_day"),
        "candidate_count": payload.get("candidate_count", 0),
        "sort_basis": payload.get("sort_basis"),
        "shared_account_scenario": payload.get("shared_account_scenario") or {},
        "rows": payload.get("rows") or [],
        "account_observed": False,
        "quotes_observed": False,
        "execution_admitted": False,
        "source_file": str(risk_path),
        "basis": "冻结参考价格和配置现金的纸面风险比较；候选策略互斥，不合并到统一账户账本",
        "not_attempted": payload.get("not_attempted") or [],
    }


@router.get("/operator-package/account-risk")
def operator_package_account_risk():
    """Calculate account-level risk from the same validated THS package.

    This is a read-only consumer.  It does not open THS, submit an order, or
    mutate the Ledger.  Rehearsal data remains explicitly rehearsal-only.
    """
    from .ths.account_risk import build_account_risk_view

    account_inputs = _operator_account_risk_inputs()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    complete_package = account_inputs["complete_package"]
    native_package = account_inputs["native_package"]
    quote_preview = account_inputs["quote_preview"]
    quote_observation = account_inputs["quote_observation"]
    # A persistent user export is preferred; otherwise the newest native
    # receipt remains primary and any rehearsal stays visibly nested.
    package = account_inputs["package"]
    result = build_account_risk_view(
        package,
        quote_observation=quote_observation,
        reconciliation=same_ledger_bundle_reconciliation,
    )
    if complete_package.get("source_scope") == "isolated_rehearsal":
        result["isolated_rehearsal_view"] = build_account_risk_view(
            complete_package, quote_observation=quote_observation,
            reconciliation=same_ledger_bundle_reconciliation,
        )
    result["native_observation"] = {
        "status": native_package.get("status", "missing"),
        "source_scope": native_package.get("source_scope", "missing"),
        "source_file": native_package.get("source_file"),
        "account_observed": bool(native_package.get("account_observed")),
        "orders_observed": bool(native_package.get("orders_observed")),
        "trades_observed": bool(native_package.get("trades_observed")),
        "cashflows_observed": bool(native_package.get("cashflows_observed")),
        "ready_for_ledger_reconciliation": bool(
            native_package.get("ready_for_ledger_reconciliation")
        ),
        "unknowns": native_package.get("unknowns") or [],
    }
    result["primary_evidence"] = package.get("source_scope") or "missing"
    result.update({
        "source_file": package.get("source_file"),
        "source_paths": package.get("source_paths") or {},
        "quote_observation": {
            "status": quote_preview.get("status"),
            "source": quote_preview.get("source"),
            "observed_at": quote_preview.get("observed_at"),
            "trading_date": quote_preview.get("trading_date"),
            "price_count": quote_preview.get("price_count", 0),
            "missing_symbols": quote_preview.get("missing_symbols") or [],
            "canonical_inputs_ready": bool(quote_preview.get("canonical_inputs_ready")),
        },
        "basis": "validated THS account export plus optional raw RMB quote observation; read-only",
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "next_action": (
            "use the existing explicit same-Ledger preview/import for this persistent export"
            if result.get("source_scope") == "persistent_user_export"
            and result.get("ledger_binding", {}).get("ready_for_ledger_reconciliation")
            and not result.get("ledger_binding", {}).get("ledger_reconciled")
            else "capture a complete persistent THS account/orders/trades/cashflows export"
            if result.get("status") in {
                "blocked_missing_or_invalid_account_evidence",
                "isolated_rehearsal_only",
            }
            else "review risk flags; no order submission is enabled"
        ),
    })
    return result


class ThsAccountOrderRiskInput(BaseModel):
    """A proposed order batch for the read-only account risk endpoint."""

    orders: list[dict] = Field(default_factory=list)
    quote_observation: dict | None = None


@router.post("/operator-package/account-order-risk")
def operator_package_account_order_risk(body: ThsAccountOrderRiskInput):
    """Project one explicit order batch against the same THS account package.

    This endpoint does not open THS, submit, cancel, import, or mutate the
    unified Ledger.  It is an order-level companion to account-risk; the
    resulting receipt remains paper/read-only evidence.
    """
    from .ths.account_risk import evaluate_order_risk

    account_inputs = _operator_account_risk_inputs()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    package = account_inputs["package"]
    native_package = account_inputs["native_package"]
    result = evaluate_order_risk(
        package,
        body.orders,
        quote_observation=body.quote_observation,
        reconciliation=same_ledger_bundle_reconciliation,
    )
    result["primary_evidence"] = package.get("source_scope") or "missing"
    result["native_observation"] = {
        "status": native_package.get("status", "missing"),
        "source_scope": native_package.get("source_scope", "missing"),
        "source_file": native_package.get("source_file"),
        "account_observed": bool(native_package.get("account_observed")),
        "orders_observed": bool(native_package.get("orders_observed")),
        "trades_observed": bool(native_package.get("trades_observed")),
        "cashflows_observed": bool(native_package.get("cashflows_observed")),
        "ready_for_ledger_reconciliation": bool(
            native_package.get("ready_for_ledger_reconciliation")
        ),
        "unknowns": native_package.get("unknowns") or [],
    }
    result["ths_journal_reconciliation"] = journal_reconciliation
    result["same_ledger_bundle_reconciliation"] = same_ledger_bundle_reconciliation
    result.update(_same_ledger_compat_fields(
        same_ledger_bundle_reconciliation, journal_reconciliation
    ))
    result["ledger_mutated"] = journal_reconciliation["ledger_mutated"]
    return result


@router.get("/operator-package/release-controls")
def operator_package_release_controls():
    """Expose paper release controls and alerts without enabling a release path."""
    package = operator_package()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    alerts_path = _package_artifact(package, "exception_alerts")
    controls_path = _package_artifact(package, "execution_controls")
    if not alerts_path or not controls_path:
        raise HTTPException(404, "当前离线包缺少纸面告警或释放控制产物")

    def read_csv(path):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error) as error:
            raise HTTPException(409, f"纸面释放控制尚不可读取：{error}") from error

    alerts = read_csv(alerts_path)
    controls = read_csv(controls_path)
    return {
        "schema": "offline_release_controls_view_v1",
        "status": "paper_controls_only",
        "execution_admitted": False,
        "live_alerts": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "alerts": {
            "row_count": len(alerts),
            "open_count": sum(row.get("state") == "open" for row in alerts),
            "severity_counts": dict(Counter(row.get("severity", "unknown") for row in alerts)),
            "rows": alerts,
        },
        "controls": {"row_count": len(controls), "rows": controls},
        "source_files": {"alerts": str(alerts_path), "controls": str(controls_path)},
    }


@router.get("/operator-package/audit-events")
def operator_package_audit_events():
    """Expose the ordered paper event trail without treating it as broker state."""
    package = operator_package()
    event_path = _package_artifact(package, "paper_audit_event_log")
    jsonl_path = _package_artifact(package, "paper_audit_event_log_jsonl")
    if not event_path and not jsonl_path:
        raise HTTPException(404, "当前离线包缺少纸面审计事件日志")
    rows = []
    if event_path:
        try:
            with event_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error) as error:
            raise HTTPException(409, f"纸面审计事件日志尚不可读取：{error}") from error
    elif jsonl_path:
        try:
            for line in jsonl_path.read_text(encoding="utf-8-sig").splitlines():
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("审计 JSONL 行必须是对象")
                    rows.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise HTTPException(409, f"纸面审计 JSONL 尚不可读取：{error}") from error
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    return {
        "schema": "offline_audit_event_log_view_v1",
        "status": "paper_event_log_only",
        "execution_admitted": False,
        "live_alerts": False,
        "broker_state_observed": False,
        "client_interacted": False,
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "event_count": len(rows),
        "state_counts": dict(Counter(row.get("state") or "unknown" for row in rows)),
        "event_type_counts": dict(Counter(row.get("event_type") or "unknown" for row in rows)),
        "events": rows,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "source_files": {
            "csv": str(event_path) if event_path else None,
            "jsonl": str(jsonl_path) if jsonl_path else None,
        },
        "next_action": "纸面事件仅用于审计和人工交接；真实订单、成交、账户和资金流水仍需持久同花顺观察包。",
    }


@router.get("/operator-package/basket-schedule")
def operator_package_basket_schedule():
    """Expose the frozen sell-before-buy basket sequence without broker I/O."""
    package = operator_package()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    schedule_path = _package_artifact(package, "execution_schedule")
    if not schedule_path:
        raise HTTPException(404, "当前离线包缺少篮子执行计划")

    try:
        with schedule_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as error:
        raise HTTPException(409, f"纸面篮子执行计划尚不可读取：{error}") from error

    phase_sequence = []
    for row in rows:
        phase = row.get("phase") or "unknown"
        if phase not in phase_sequence:
            phase_sequence.append(phase)
    phase_rank = {phase: index for index, phase in enumerate(phase_sequence)}
    phase_order = [phase_rank.get(row.get("phase") or "unknown", -1) for row in rows]
    sell_index = next((index for index, row in enumerate(rows) if row.get("side") == "sell"), None)
    buy_index = next((index for index, row in enumerate(rows) if row.get("side") == "buy"), None)
    sell_before_buy = buy_index is None or (sell_index is not None and sell_index < buy_index)
    manual_only = all(row.get("submission_mode") == "manual_ths_prefill_only" for row in rows)
    live_disabled_count = sum(row.get("live_submission") == "disabled" for row in rows)
    manual_release = None
    manual_release_path = _package_artifact(package, "paper_manual_release_sequence_index")
    if manual_release_path:
        try:
            manual_release_payload = json.loads(manual_release_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"人工批次释放索引尚不可读取：{error}") from error
        if manual_release_payload.get("schema") != "paper_manual_release_sequence_index_v1":
            raise HTTPException(409, "人工批次释放索引版本不受支持")
        manual_rows = manual_release_payload.get("rows") or []
        detail_rows = []
        detail_by_key = {}
        for index_row in manual_rows:
            sequence_path = Path(str(index_row.get("sequence_json") or ""))
            if not sequence_path.is_file():
                continue
            try:
                sequence_payload = json.loads(sequence_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise HTTPException(409, f"人工逐单释放序列尚不可读取：{error}") from error
            for detail in sequence_payload.get("rows") or []:
                detail_rows.append(detail)
                detail_by_key[(detail.get("strategy_id"), detail.get("symbol"), detail.get("side"), detail.get("qty"))] = detail
        enriched_rows = []
        for row in rows:
            key = (row.get("strategy_id"), row.get("symbol"), row.get("side"), row.get("qty"))
            detail = detail_by_key.get(key)
            enriched = dict(row)
            if detail:
                for field in ("release_sequence", "release_phase", "release_gate", "required_observation", "release_blocker_reason", "target_reconciliation_status"):
                    enriched[field] = detail.get(field)
            enriched_rows.append(enriched)
        rows = enriched_rows
        manual_release = {
            "schema": manual_release_payload.get("schema"),
            "status": manual_release_payload.get("status", "paper_manual_release_sequences_indexed"),
            "candidate_count": manual_release_payload.get("candidate_count", len(manual_rows)),
            "ordering_verified_count": sum(bool(row.get("ordering_verified")) for row in manual_rows),
            "route_available_count": sum(int(row.get("route_available_count", 0) or 0) for row in manual_rows),
            "route_unavailable_count": sum(int(row.get("route_unavailable_count", 0) or 0) for row in manual_rows),
            "rows": manual_rows,
            "detail_rows": detail_rows,
            "automatic_strategy_selection": False,
            "execution_admitted": False,
            "source_file": str(manual_release_path),
            "basis": "selected-plan paper release index; sell review precedes buy release; no broker or account observation",
        }
    return {
        "schema": "offline_basket_schedule_view_v1",
        "status": "paper_manual_sequence_only",
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "schedule": {
            "row_count": len(rows),
            "phase_sequence": phase_sequence,
            "phase_counts": dict(Counter(row.get("phase") or "unknown" for row in rows)),
            "side_counts": dict(Counter(row.get("side") or "unknown" for row in rows)),
            "sell_before_buy": sell_before_buy,
            "sequence_monotonic": phase_order == sorted(phase_order),
            "manual_only": manual_only,
            "live_submission_disabled_count": live_disabled_count,
            "manual_release": manual_release,
            "rows": rows,
            "basis": "frozen target basket and reference prices; sell-before-buy paper sequence; no account, quote, fill or broker observation",
        },
        "source_file": str(schedule_path),
    }


@router.get("/operator-package/blotter")
def operator_package_blotter():
    """Expose the frozen paper order blotter without broker I/O or ledger writes."""
    package = operator_package()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    blotter_path = _package_artifact(package, "blotter")
    if not blotter_path:
        raise HTTPException(404, "当前离线包缺少纸面中央订单台")

    try:
        with blotter_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as error:
        raise HTTPException(409, f"纸面中央订单台尚不可读取：{error}") from error

    return {
        "schema": "offline_blotter_view_v1",
        "status": "paper_blotter_only",
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "blotter": {
            "row_count": len(rows),
            "preflight_status_counts": dict(Counter(row.get("preflight_status") or "unknown" for row in rows)),
            "route_status_counts": dict(Counter(row.get("route_status") or "unknown" for row in rows)),
            "lifecycle_counts": dict(Counter(row.get("lifecycle") or "unknown" for row in rows)),
            "side_counts": dict(Counter(row.get("side") or "unknown" for row in rows)),
            "route_available_count": sum(row.get("route_status") == "route_available" for row in rows),
            "rows": rows,
            "basis": "frozen paper order intent and reference prices; no broker acknowledgement, fill, account or quote observation",
        },
        "source_file": str(blotter_path),
    }


@router.get("/operator-package/execution-attribution")
def operator_package_execution_attribution():
    """Expose frozen parent/child execution attribution for paper TCA review."""
    package = operator_package()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    attribution_path = _package_artifact(package, "paper_execution_attribution")
    if not attribution_path:
        raise HTTPException(404, "当前离线包缺少纸面执行归因产物")

    try:
        payload = json.loads(attribution_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"纸面执行归因尚不可读取：{error}") from error

    return {
        "schema": "offline_execution_attribution_view_v1",
        "status": payload.get("status", "offline_reference_attribution"),
        "execution_admitted": False,
        "real_broker_state": False,
        "account_observed": False,
        "quotes_observed": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "parent_count": payload.get("parent_count", 0),
        "child_count": payload.get("child_count", 0),
        "completed_parent_count": payload.get("completed_parent_count", 0),
        "residual_parent_count": payload.get("residual_parent_count", 0),
        "tca_metrics": payload.get("tca_metrics", {}),
        "side_summary": payload.get("side_summary", []),
        "scenario_summary": payload.get("scenario_summary", []),
        "child_pressure_scenarios": payload.get("child_pressure_scenarios", []),
        "execution_cost_stress": payload.get("execution_cost_stress", []),
        "parents": payload.get("parents", []),
        "basis": payload.get("tca_basis", "frozen reference price and configured paper cost; no observed fill benchmark"),
        "not_attempted": payload.get("not_attempted", []),
        "source_file": str(attribution_path),
    }


@router.get("/operator-package/session-handoff")
def operator_package_session_handoff():
    """Expose paper recovery, reconciliation and next-session handoff state."""
    package = operator_package()
    research_evidence_summary = _research_evidence_consumer_summary(package.get("strategy_id"))
    structured_package = _latest_structured_observation_package()
    ledger_import_preview = package.get("ths_ledger_import_preview_rehearsal") or {}
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    release_readiness = paper_preview_readiness()
    recovery_path = _package_artifact(package, "recovery_plan")
    recovery_handoff_index_path = _package_artifact(package, "paper_recovery_handoff_index")
    carry_path = _package_artifact(package, "paper_next_session_carry")
    reconciliation_path = _package_artifact(package, "paper_reconciliation")
    target_path = _package_artifact(package, "target_order_reconciliation")
    if not all((recovery_path, carry_path, reconciliation_path, target_path)):
        raise HTTPException(404, "当前离线包缺少会话交接或对账产物")

    def read_csv(path, message):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error) as error:
            raise HTTPException(409, f"{message}尚不可读取：{error}") from error

    try:
        carry = json.loads(carry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"纸面下一会话结转尚不可读取：{error}") from error
    recovery_rows = read_csv(recovery_path, "纸面恢复计划")
    reconciliation_rows = read_csv(reconciliation_path, "纸面对账")
    target_rows = read_csv(target_path, "目标订单对账")
    recovery_reviews = _recovery_review_receipts(package)
    recovery_reconciliation = {
        "status": "unobserved_paper_reconciliation_template",
        "template_schema": "broker_state_reconciliation_receipt_v1",
        "candidate_count": 0,
        "rows": [],
        "source_file": str(recovery_handoff_index_path) if recovery_handoff_index_path else None,
    }
    if recovery_handoff_index_path and recovery_handoff_index_path.is_file():
        try:
            index = json.loads(recovery_handoff_index_path.read_text(encoding="utf-8"))
            for index_row in index.get("rows", []):
                handoff_path = Path(index_row.get("handoff_json", ""))
                if not handoff_path.is_file():
                    continue
                handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
                template = handoff.get("reconciliation_template") or {}
                checks = template.get("checks") or []
                pending_evidence = sorted({
                    evidence
                    for check in checks
                    for evidence in (check.get("pending_evidence") or [])
                })
                recovery_reconciliation["rows"].append({
                    "strategy_id": handoff.get("strategy_id"),
                    "scenario_count": handoff.get("scenario_count", 0),
                    "unresolved_scenario_count": handoff.get("unresolved_scenario_count", 0),
                    "status": template.get("status", "unobserved_paper_reconciliation_template"),
                    "pending_check_count": sum(check.get("status") == "pending_observation" for check in checks),
                    "pending_evidence_count": len(pending_evidence),
                    "pending_evidence": pending_evidence,
                    "retry_permitted": bool(template.get("retry_permitted", False)),
                    "execution_admitted": bool(template.get("execution_admitted", False)),
                    "source_file": str(handoff_path),
                })
            recovery_reconciliation["candidate_count"] = len(recovery_reconciliation["rows"])
        except (OSError, UnicodeError, json.JSONDecodeError):
            recovery_reconciliation["status"] = "unobserved_paper_reconciliation_template_unreadable"
    return {
        "schema": "offline_session_handoff_view_v1",
        "status": "paper_session_handoff_only",
        "execution_admitted": False,
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "account_observed": False,
        "broker_observed": False,
        "fills_observed": False,
        "client_interacted": False,
        "recovery": {"row_count": len(recovery_rows), "rows": recovery_rows},
        "human_recovery_review": recovery_reviews,
        "recovery_reconciliation": recovery_reconciliation,
        "next_session": {
            "status": carry.get("status", "paper_cross_session_state_only"),
            "execution_day": carry.get("execution_day"),
            "carry_row_count": len(carry.get("carry", [])),
            "synthetic_cash_delta_rmb": carry.get("synthetic_cash_delta_rmb"),
            "requirements": carry.get("next_session_requirements", []),
            "rows": carry.get("carry", []),
        },
        "paper_reconciliation": {
            "row_count": len(reconciliation_rows),
            "status_counts": dict(Counter(row.get("status") or "unknown" for row in reconciliation_rows)),
            "rows": reconciliation_rows,
        },
        "target_reconciliation": {
            "row_count": len(target_rows),
            "status_counts": dict(Counter(row.get("status") or "unknown" for row in target_rows)),
            "active_target_count": sum(row.get("active_target") == "True" for row in target_rows),
            "rows": target_rows,
        },
        "structured_observation_package": structured_package,
        "structured_package_ready_for_ledger_reconciliation": bool(
            structured_package.get("ready_for_ledger_reconciliation")
        ),
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "order_count": structured_reconciliation.get("order_count"),
        "trade_count": structured_reconciliation.get("trade_count"),
        "cashflow_count": structured_reconciliation.get("cashflow_count"),
        "ledger_import_preview": ledger_import_preview,
        "ledger_import_preview_status": ledger_import_preview.get("status"),
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "release_readiness": release_readiness,
        "release_readiness_status": release_readiness.get("status"),
        "release_ready": bool(release_readiness.get("release_ready")),
        "research_evidence_summary": research_evidence_summary,
        "basis": "paper recovery and reconciliation artifacts only; observed account, broker orders, fills and next-session release remain absent",
        "source_files": {"recovery": str(recovery_path), "next_session": str(carry_path), "paper_reconciliation": str(reconciliation_path), "target_reconciliation": str(target_path), "paper_recovery_handoff_index": str(recovery_handoff_index_path) if recovery_handoff_index_path else None},
    }


def _ths_reference_root():
    return settings.DATA_ROOT / "research" / "strategy_selection"


def _ths_reference_paths():
    root = _ths_reference_root().resolve()
    if not root.is_dir():
        return []
    paths = []
    for path in root.rglob("ths_prefill_payloads.json"):
        resolved = path.resolve()
        if resolved.is_file() and resolved.is_relative_to(root):
            paths.append(resolved)
    return paths


def _read_ths_reference_payload(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"THS参考载荷尚不可读取：{error}") from error
    if payload.get("schema") != "ths_prefill_payloads_preview_v1":
        raise HTTPException(409, "THS参考载荷版本不受支持")
    if payload.get("status") != "offline_reference_payload_only" or payload.get("execution_admitted") is not False:
        raise HTTPException(409, "THS参考载荷不是离线只读版本")
    rows = []
    for raw in payload.get("rows") or []:
        rows.append({
            "sequence": raw.get("sequence"),
            "strategy_id": raw.get("strategy_id"),
            "symbol": raw.get("symbol"),
            "side": raw.get("side"),
            "qty": raw.get("qty"),
            "price_rmb": raw.get("price_rmb"),
            "symbol_field": raw.get("symbol_field"),
            "price_field": raw.get("price_field"),
            "qty_field": raw.get("qty_field"),
            "preflight_status": raw.get("preflight_status"),
            "route_status": raw.get("route_status"),
            "route_reason": raw.get("route_reason"),
            "prefill_status": raw.get("prefill_status"),
            "execution_admitted": raw.get("execution_admitted", "false"),
        })
    return {
        "schema": payload["schema"],
        "status": payload["status"],
        "execution_day": payload.get("execution_day"),
        "strategy_id": next((row.get("strategy_id") for row in rows if row.get("strategy_id")), None),
        "price_basis": payload.get("price_basis"),
        "order_count": payload.get("order_count", len(rows)),
        "accepted_payload_count": payload.get("accepted_payload_count", 0),
        "rejected_or_missing_count": payload.get("rejected_or_missing_count", 0),
        "route_available_count": payload.get("route_available_count", 0),
        "route_unavailable_count": payload.get("route_unavailable_count", 0),
        "account_observed": payload.get("account_observed", False),
        "quotes_observed": payload.get("quotes_observed", False),
        "client_interacted": payload.get("client_interacted", False),
        "submitted": payload.get("submitted", False),
        "execution_admitted": False,
        "route_policy": payload.get("route_policy"),
        "rows": rows,
        "not_attempted": payload.get("not_attempted") or [],
    }


@router.get("/ths-reference-payload")
def ths_reference_payload(strategy_id: str | None = Query(default=None, min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")):
    """Return a frozen, read-only THS payload preview without touching the client."""
    package = operator_package()
    candidates = [str(row.get("strategy_id")) for row in package.get("strategy_candidates") or [] if row.get("strategy_id")]
    selected = (package.get("strategy_selection") or {}).get("selected_strategy_id")
    requested = strategy_id or selected
    rank = {value: index for index, value in enumerate(([requested] if requested else []) + candidates)}
    paths = _ths_reference_paths()
    if not paths:
        raise HTTPException(404, "尚无THS离线参考载荷")

    # The offline operator package records the exact selected-plan payload.
    # Prefer it over the historical route-v1 discovery fallback so a rebuilt
    # selected plan cannot be paired with an older payload for the same strategy.
    selected_package_path = None
    package_strategy_id = str(package.get("strategy_id") or "")
    if requested and requested in {str(selected or ""), package_strategy_id}:
        package_pointer = (package.get("artifact_pointers") or {}).get("ths_prefill_payloads_summary")
        if package_pointer:
            candidate = Path(str(package_pointer)).resolve()
            if candidate.is_file() and candidate.is_relative_to(settings.DATA_ROOT.resolve() / "research" / "strategy_selection"):
                selected_package_path = candidate

    def sort_key(path):
        strategy_hint = next((strategy_id for strategy_id in rank if strategy_id in path.parent.name), "")
        route_hint = 0 if path.parent.name.endswith("_route_v1") else 1
        return (rank.get(strategy_hint, len(rank)), route_hint, -path.stat().st_mtime, str(path))

    ordered = sorted(paths, key=sort_key)
    if selected_package_path is not None:
        selected_path = selected_package_path
    elif requested:
        selected_path = next((path for path in ordered if requested in path.parent.name), None)
        if selected_path is None:
            raise HTTPException(404, "当前离线包没有该策略的THS参考载荷")
    else:
        selected_path = ordered[0]
    current = _read_ths_reference_payload(selected_path)
    available = []
    seen_strategy_ids = set()
    for path in ordered:
        try:
            value = _read_ths_reference_payload(path)
        except HTTPException:
            continue
        if value["strategy_id"] in seen_strategy_ids:
            continue
        seen_strategy_ids.add(value["strategy_id"])
        available.append({
            "strategy_id": value["strategy_id"],
            "execution_day": value["execution_day"],
            "order_count": value["order_count"],
            "accepted_payload_count": value["accepted_payload_count"],
            "route_available_count": value["route_available_count"],
            "route_unavailable_count": value["route_unavailable_count"],
            "status": value["status"],
        })
    current["selection_status"] = "operator_view_selection" if not selected else "explicit_paper_candidate"
    current["available_payloads"] = available
    current["source_file"] = selected_path.name
    return current


class ThsPrefillReadbackInput(BaseModel):
    """One normalized THS form readback; this endpoint never touches the client."""

    strategy_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    sequence: int = Field(ge=1)
    symbol: str = Field(min_length=1, max_length=32)
    price: str = Field(min_length=1, max_length=32)
    qty: str = Field(min_length=1, max_length=32)


def _normalize_ths_readback(field: str, value: str) -> str:
    """Canonicalize harmless form formatting while preserving invalid text."""
    text = str(value).strip()
    if field == "symbol":
        return text.split(".", 1)[0].strip().upper()
    if field == "price":
        try:
            number = Decimal(text.replace(",", ""))
            if number.is_finite():
                return f"{number:.2f}"
        except (InvalidOperation, ValueError):
            pass
    if field == "qty":
        try:
            number = Decimal(text.replace(",", ""))
            if number.is_finite() and number == number.to_integral_value():
                return str(int(number))
        except (InvalidOperation, ValueError):
            pass
    return text


@router.post("/ths-reference-payload/readback")
def ths_reference_payload_readback(body: ThsPrefillReadbackInput):
    """Compare one captured form row with the frozen THS payload without submission."""
    payload = ths_reference_payload(strategy_id=body.strategy_id)
    row = next((item for item in payload.get("rows") or []
                if int(item.get("sequence") or 0) == body.sequence), None)
    if row is None:
        raise HTTPException(404, "当前离线载荷没有该序号的订单")
    base = {
        "schema": "ths_prefill_readback_view_v1",
        "strategy_id": body.strategy_id,
        "sequence": body.sequence,
        "execution_day": payload.get("execution_day"),
        "submitted": False,
        "ledger_mutated": False,
        "execution_admitted": False,
    }
    if row.get("route_status") != "route_available":
        return {
            **base,
            "status": "route_unavailable",
            "expected": None,
            "readback": {"symbol": body.symbol, "price": body.price, "qty": body.qty},
            "mismatches": [],
            "route_status": row.get("route_status"),
            "route_reason": row.get("route_reason"),
        }
    # Compare canonical field values rather than the serialized spelling in
    # the frozen payload.  THS commonly renders a raw RMB price as ``4.1``
    # while the payload may carry ``4.10``; those are the same form value.
    expected_raw = {
        "symbol": str(row.get("symbol") or "").split(".")[0],
        "price": str(row.get("price_rmb") or ""),
        "qty": str(row.get("qty") or ""),
    }
    expected = {
        field: _normalize_ths_readback(field, value)
        for field, value in expected_raw.items()
    }
    raw_received = {"symbol": body.symbol, "price": body.price, "qty": body.qty}
    received = {field: _normalize_ths_readback(field, value) for field, value in raw_received.items()}
    mismatches = [
        {"field": field, "expected": expected[field], "received": received[field]}
        for field in ("symbol", "price", "qty")
        if received[field] != expected[field]
    ]
    return {
        **base,
        "status": "readback_verified" if not mismatches else "readback_mismatch",
        "expected": expected,
        "expected_raw": expected_raw,
        "readback": raw_received,
        "normalized_readback": received,
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "route_status": row.get("route_status"),
        "route_reason": row.get("route_reason"),
    }


@router.get("/ths-observation-bundle/readiness")
def ths_observation_bundle_readiness():
    """Validate a local THS export bundle without importing it or touching a client."""
    from .ths.observation_preview import preview_bundle, validate_observation_profile
    from .ths.structured_observation import build_source_manifest, verify_source_manifest

    root = (settings.DATA_ROOT / "ths" / "observation_bundle").resolve()
    paths = {
        "account": root / "account_observation.json",
        "orders": root / "orders.csv",
        "trades": root / "trades.csv",
        "cashflows": root / "cashflows.csv",
    }
    statement_paths = {
        kind: root / f"{kind}.csv"
        for kind in ("movements", "restriction_releases", "share_credits")
    }
    profile_path = root / "profile.json"
    profile = None
    profile_status = "not_supplied"
    if profile_path.is_file():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
            if not isinstance(profile, dict):
                profile = None
                profile_status = "invalid: expected a JSON object"
            else:
                profile_status = "loaded"
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            profile_status = f"invalid: {error}"

    preview = preview_bundle(
        paths["account"], paths["orders"], paths["trades"], paths["cashflows"],
        profile=profile,
    )
    profile_validation = validate_observation_profile(profile)
    current_manifest = None
    current_manifest_status = "not_ready"
    current_manifest_issues = []
    if preview.get("status") == "bundle_ready_for_reconciliation":
        current_manifest = build_source_manifest(paths)
        current_manifest_check = verify_source_manifest(current_manifest)
        current_manifest_status = current_manifest_check["status"]
        current_manifest_issues = current_manifest_check["issues"]
    structured_package = _latest_structured_observation_package()
    complete_structured_package = _latest_complete_structured_observation_package()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    files = {
        kind: {
            "path": str(path),
            "present": path.is_file(),
            "bytes": path.stat().st_size if path.is_file() else 0,
            "modified_at": path.stat().st_mtime if path.is_file() else None,
        }
        for kind, path in paths.items()
    }
    statement_files = {
        kind: {
            "path": str(path),
            "present": path.is_file(),
            "bytes": path.stat().st_size if path.is_file() else 0,
            "modified_at": path.stat().st_mtime if path.is_file() else None,
            "template_path": str(path),
            "template_present": path.is_file(),
        }
        for kind, path in statement_paths.items()
    }
    return {
        "schema": "ths_observation_bundle_readiness_v1",
        "status": preview.get("status", "validation_error"),
        "execution_admitted": False,
        "ledger_mutated": False,
        "client_operation": False,
        "bundle_root": str(root),
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "profile": {
            "path": str(profile_path),
            "status": profile_status,
            "mapping_validation": profile_validation,
        },
        "files": files,
        "statement_files": statement_files,
        "preview": preview,
        "structured_observation_package": structured_package,
        "structured_observation_package_complete": complete_structured_package,
        "source_manifest": current_manifest,
        "source_manifest_status": current_manifest_status,
        "source_manifest_issues": current_manifest_issues,
        "structured_package_ready_for_ledger_reconciliation": bool(
            structured_package.get("ready_for_ledger_reconciliation")
        ),
        "complete_structured_package_ready_for_ledger_reconciliation": bool(
            complete_structured_package.get("ready_for_ledger_reconciliation")
        ),
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "order_count": structured_reconciliation.get("order_count"),
        "trade_count": structured_reconciliation.get("trade_count"),
        "cashflow_count": structured_reconciliation.get("cashflow_count"),
        "next_action": (
            "review the validated export bundle, then use explicit import/reconcile command"
            if preview.get("status") == "bundle_ready_for_reconciliation"
            else "place the four user exports in the bundle directory, then refresh"
            if preview.get("status") in {"missing_template", "template_ready"}
            else "replace the four blank templates with current THS exports, then refresh"
            if preview.get("status") == "templates_blank"
            else "correct the listed export or mapping issues; no import was performed"
        ),
    }


class ThsObservationBundleInitializeInput(BaseModel):
    confirm_initialize: bool = False


@router.post("/ths-observation-bundle/initialize")
def initialize_ths_observation_bundle(body: ThsObservationBundleInitializeInput):
    """Create blank, explicitly named export files without creating observations."""
    if body.confirm_initialize is not True:
        return {
            "schema": "ths_observation_bundle_initialize_v1",
            "status": "confirmation_required",
            "execution_admitted": False,
            "client_operation": False,
            "ledger_mutated": False,
            "next_action": "确认只生成空白模板后，再次提交 confirm_initialize=true",
        }
    root = (settings.DATA_ROOT / "ths" / "observation_bundle").resolve()
    targets = {
        "account": root / "account_observation.json",
        "orders": root / "orders.csv",
        "trades": root / "trades.csv",
        "cashflows": root / "cashflows.csv",
        "movements": root / "movements.csv",
        "restriction_releases": root / "restriction_releases.csv",
        "share_credits": root / "share_credits.csv",
        "profile": root / "profile.json",
    }
    core_targets = {kind: targets[kind] for kind in ("account", "orders", "trades", "cashflows")}
    optional_statement_kinds = ("movements", "restriction_releases", "share_credits")
    refresh_blank = False
    if any(path.exists() for path in core_targets.values()):
        # A prior opening run may have left only untouched blank templates with
        # an old trading date. Refresh that safe state, but never overwrite a
        # partially or fully filled operator export.
        from .ths.observation_preview import is_blank_bundle
        existing_profile = None
        profile_path = targets["profile"]
        if profile_path.is_file():
            try:
                existing_profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise HTTPException(409, detail=f"已有 profile.json 无法读取；不会覆盖用户文件: {error}") from error
            if not isinstance(existing_profile, dict):
                raise HTTPException(409, detail="已有 profile.json 不是对象；不会覆盖用户文件")
        if not is_blank_bundle(
            core_targets["account"], core_targets["orders"], core_targets["trades"], core_targets["cashflows"],
            profile=existing_profile if isinstance(existing_profile, dict) else None,
        ):
            raise HTTPException(409, detail="观察包已有已填写或校验失败的文件；不会覆盖用户文件")
        refresh_blank = True
    sources_root = settings.DATA_ROOT / "research" / "opening_session_runs"
    candidates = sorted(
        (path for path in sources_root.glob("*/*observation.template.*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    source_names = {
        "account": "account_observation.template.json",
        "orders": "orders_observation.template.csv",
        "trades": "trades_observation.template.csv",
        "cashflows": "cashflows_observation.template.csv",
        "movements": "movements_observation.template.csv",
        "restriction_releases": "restriction_releases_observation.template.csv",
        "share_credits": "share_credits_observation.template.csv",
    }
    statement_headers = {
        "movements": "movement_id,kind,cash_change\n",
        "restriction_releases": "release_id,symbol,market,released_qty,release_day,reason\n",
        "share_credits": "credit_id,symbol,market,credited_qty,credit_day,available_day,kind\n",
    }
    selected = {}
    for kind, filename in source_names.items():
        match = next((path for path in candidates if path.name == filename), None)
        if match is None:
            if kind in optional_statement_kinds:
                selected[kind] = None
                continue
            raise HTTPException(409, detail=f"缺少模板源文件: {filename}")
        selected[kind] = match
    profile_source = Path(__file__).parent / "ths" / "observation_bundle_profile.example.json"
    if not profile_source.is_file():
        raise HTTPException(409, detail=f"缺少观察包 profile 源文件: {profile_source}")
    selected["profile"] = profile_source
    root.mkdir(parents=True, exist_ok=True)
    try:
        for kind, target in targets.items():
            if refresh_blank and kind == "profile" and target.is_file():
                continue
            if kind in optional_statement_kinds and target.is_file():
                try:
                    with target.open(encoding="utf-8-sig", newline="") as stream:
                        rows = csv.DictReader(stream)
                        has_values = any(
                            any(str(value or "").strip() for value in row.values())
                            for row in rows
                        )
                except (OSError, UnicodeError, csv.Error) as error:
                    raise HTTPException(
                        409,
                        detail=f"已有 {kind}.csv 无法安全判断是否为空；不会覆盖用户文件: {error}",
                    ) from error
                if has_values:
                    continue
            if kind in optional_statement_kinds and selected[kind] is None:
                target.write_text(statement_headers[kind], encoding="utf-8")
                continue
            shutil.copyfile(selected[kind], target)
    except OSError as error:
        raise HTTPException(409, detail=f"空白模板创建失败: {error}") from error
    return {
        "schema": "ths_observation_bundle_initialize_v1",
        "status": "blank_templates_refreshed" if refresh_blank else "blank_templates_initialized",
        "execution_admitted": False,
        "client_operation": False,
        "ledger_mutated": False,
        "template_refreshed": refresh_blank,
        "files": {kind: str(path) for kind, path in targets.items()},
        "statement_kinds": list(optional_statement_kinds),
        "next_action": (
            "空白模板已按最新离线开盘包刷新；将四个文件替换为同花顺当前人工导出内容后刷新校验。"
            if refresh_blank else
            "将四个文件替换为同花顺当前人工导出内容后刷新校验；按实际表头修改 profile.json；不要把历史或截图值填入模板。"
        ),
    }


@router.get("/ths-observation-bundle/journal-status")
def ths_observation_bundle_journal_status():
    """Expose the local THS observation-plan index without broker or ledger writes."""
    from .ths import journal

    journal.initialize()
    current = journal.status()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    plans = []
    for plan in current.get("plans", []):
        result = plan.get("result") or {}
        account_reconciliation = result.get("account_reconciliation")
        ledger_import = result.get("ledger_import")
        plans.append({
            "plan_id": plan.get("id"),
            "account_alias": plan.get("account_alias"),
            "status": plan.get("status"),
            "contract_id": plan.get("contract_id"),
            "trading_date": plan.get("trading_date"),
            "updated_at": plan.get("updated_at"),
            "result_status": result.get("status"),
            "result_reason": result.get("reason") or result.get("error"),
            "execution_count": len(result.get("executions") or []) if isinstance(result.get("executions"), list) else 0,
            "account_reconciliation_status": account_reconciliation.get("status") if isinstance(account_reconciliation, dict) else None,
            "ledger_imported": bool(ledger_import.get("ledger_imported")) if isinstance(ledger_import, dict) else False,
        })
    return {
        "schema": "ths_observation_journal_status_v1",
        "paused": bool(current.get("paused")),
        "kill_switch": current.get("kill_switch", "engaged" if current.get("paused") else "disengaged"),
        "submission_enabled": False,
        "execution_admitted": False,
        "client_operation": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "plan_count": len(plans),
        "plans": plans,
        "basis": "local observation journal index only; no broker session or account read",
    }


class ThsObservationBundleImportInput(BaseModel):
    confirm_import: bool = False
    plan_id: str | None = Field(default=None, max_length=120)
    observed_at: str | None = None
    run_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    confirm_ledger_import: bool = False
    statement_kinds: list[str] = Field(default_factory=list)


@router.post("/ths-observation-bundle/import")
def import_ths_observation_bundle(body: ThsObservationBundleImportInput):
    """Append a validated bundle and optionally import its receipt into the same paper Ledger."""
    readiness = ths_observation_bundle_readiness()
    if not body.confirm_import:
        return {
            "schema": "ths_observation_bundle_import_v1",
            "status": "confirmation_required",
            "execution_admitted": False,
            "client_operation": False,
            "ledger_mutated": False,
            "readiness": readiness,
            "next_action": "确认文件来自同花顺人工导出且字段映射无误后，再次提交 confirm_import=true",
        }
    if body.run_id and not body.plan_id:
        raise HTTPException(422, "run_id requires plan_id so the reconciled receipt has an explicit plan")
    statement_kinds = list(body.statement_kinds or [])
    allowed_statement_kinds = {"movements", "restriction_releases", "share_credits"}
    if (len(statement_kinds) > 3 or len(set(statement_kinds)) != len(statement_kinds)
            or not set(statement_kinds) <= allowed_statement_kinds):
        raise HTTPException(422, "statement_kinds 只能选择 movements、restriction_releases、share_credits 且不得重复")
    if statement_kinds and body.run_id:
        raise HTTPException(
            422,
            "附加账户流水先进入观察日志；不能与四表交易观察包在同一原子 Ledger 计划导入中混用",
        )
    if body.run_id and body.confirm_ledger_import is not True:
        return {
            "schema": "ths_observation_bundle_import_v1",
            "status": "ledger_confirmation_required",
            "execution_admitted": False,
            "client_operation": False,
            "ledger_mutated": False,
            "readiness": readiness,
            "next_action": "确认该观察包已与计划逐项核对，并显式提交 confirm_ledger_import=true",
        }
    if readiness["status"] != "bundle_ready_for_reconciliation":
        raise HTTPException(409, detail={
            "message": "导出包尚未通过校验，未执行导入",
            "status": readiness["status"],
            "readiness": readiness,
        })
    from .ths import journal
    from .ths.observation_preview import build_observation_bundle, build_statement_observations

    root = Path(readiness["bundle_root"])
    profile_path = Path(readiness["profile"]["path"])
    profile = None
    if profile_path.is_file():
        profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    paths = {
        "account": root / "account_observation.json",
        "orders": root / "orders.csv",
        "trades": root / "trades.csv",
        "cashflows": root / "cashflows.csv",
    }
    try:
        preview, observations = build_observation_bundle(
            paths["account"], paths["orders"], paths["trades"], paths["cashflows"],
            profile=profile, observed_at=body.observed_at,
        )
        statement_paths = {
            kind: root / f"{kind}.csv"
            for kind in statement_kinds
        }
        statement_observations = build_statement_observations(
            observations[0], statement_paths, profile=profile, observed_at=body.observed_at,
        ) if statement_kinds else []
        all_observations = [*observations, *statement_observations]
        if body.plan_id and not body.run_id:
            from .ths import journal

            bound_plan = journal.get(body.plan_id)
            expected_day = str(
                bound_plan.get("trading_date")
                or (bound_plan.get("snapshot") or {}).get("trading_date")
                or ""
            )
            if any(
                value.get("account_alias") != bound_plan.get("account_alias")
                or value.get("trading_date") != expected_day
                for value in all_observations
            ):
                raise HTTPException(409, detail={
                    "message": "观察包与指定计划的账户或交易日不一致，未写入观察日志",
                    "status": "plan_observation_binding_mismatch",
                    "plan_id": body.plan_id,
                    "expected_account_alias": bound_plan.get("account_alias"),
                    "expected_trading_date": expected_day,
                    "observed_account_aliases": sorted({value.get("account_alias") for value in all_observations}),
                    "observed_trading_dates": sorted({value.get("trading_date") for value in all_observations}),
                    "execution_admitted": False,
                    "ledger_mutated": False,
                })
        ledger_preview = None
        if body.run_id:
            # The UI performs this preview first, but the API must enforce the
            # same boundary for direct callers before any journal mutation.
            from .ths.ledger_import import preview_plan
            ledger_preview = preview_plan(body.run_id, body.plan_id, observations=observations)
            if ledger_preview.get("status") not in {"ready_for_import", "already_imported_no_change"}:
                raise HTTPException(409, detail={
                    "message": "观察包未通过当前 paper Ledger 差额预览，未写入观察日志",
                    "status": "ledger_preview_blocked",
                    "ledger_preview": ledger_preview,
                    "preview": preview,
                })
        from .ths.structured_observation import build_structured_observation_package_from_bundle
        structured_out_dir = (
            settings.DATA_ROOT / "ths" / "observation_bundle" / "structured"
            / f"import_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
        )
        structured = build_structured_observation_package_from_bundle(
            preview,
            observations,
            structured_out_dir,
            source_paths={
                "account": str(paths["account"]),
                "orders": str(paths["orders"]),
                "trades": str(paths["trades"]),
                "cashflows": str(paths["cashflows"]),
            },
            source_scope="persistent_user_export",
        )
        atomic_import = None
        if body.run_id:
            from .ths.ledger_import import import_observation_bundle
            try:
                atomic_import = import_observation_bundle(
                    body.run_id, body.plan_id, observations, confirm=body.confirm_ledger_import)
            except (PermissionError, ValueError, KeyError, TypeError) as error:
                raise HTTPException(409, detail={
                    "message": f"观察包与 Ledger 原子入账失败，观察、计划和账本均未更新: {error}",
                    "status": "atomic_ledger_import_failed",
                    "ledger_mutated": False,
                    "journal_mutated": False,
                    "plan_mutated": False,
                    "execution_admitted": False,
                }) from error
            journal_receipt = atomic_import["journal"]
        else:
            journal.initialize()
            journal_receipt = journal.observe_many(all_observations)
        result = {
            "schema": "ths_observation_bundle_import_v1",
            "status": "imported",
            "execution_admitted": False,
            "client_operation": False,
            "ledger_mutated": False,
            "preview": preview,
            "structured_package": structured,
            "journal": journal_receipt,
            "idempotent_replay": bool(journal_receipt.get("idempotent_replay")),
            "statement_observations": {
                "kinds": statement_kinds,
                "rows": {value["kind"]: len(value["rows"]) for value in statement_observations},
                "source_files": {kind: str(path) for kind, path in statement_paths.items()},
                "ledger_mutated": False,
            },
        }
        if ledger_preview is not None:
            result["ledger_preview"] = ledger_preview
        if atomic_import is not None:
            result.update({key: atomic_import[key] for key in (
                "atomic", "plan", "ledger", "ledger_imported", "ledger_mutated", "idempotent_replay",
            )})
        elif body.plan_id:
            result["plan"] = journal.refresh(body.plan_id)
        # Return the same detailed join used by the read-only risk and health
        # consumers so an import caller can see the resulting account/order/
        # fill/cashflow state without a second, potentially divergent query.
        result["same_ledger_bundle_reconciliation"] = _same_ledger_bundle_reconciliation_view()
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise HTTPException(409, detail=f"导出包未导入：{error}") from error


def _ths_simulated_ledger_readiness():
    """Describe the explicit simulated-account bridge without changing the ledger."""
    from .ths import journal
    from .ths.adapter import validate_cash_balance, validate_snapshot

    structured_package = _latest_structured_observation_package()
    research_evidence_summary = _research_evidence_consumer_summary()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    release_readiness = paper_preview_readiness()
    journal.initialize()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    with store.connection() as db:
        observation_rows = db.execute(
            "SELECT account_alias,kind,observed_at,trading_date,payload "
            "FROM ths_observations ORDER BY account_alias,kind,observed_at DESC,id DESC"
        ).fetchall()
        daily_run_ids = {row[0] for row in db.execute("SELECT DISTINCT run_id FROM daily")}
        run_rows = db.execute(
            "SELECT id,name,status,mode,config,state,cursor,start,end,updated_at "
            "FROM runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()

    latest = {}
    for row in observation_rows:
        key = (row["account_alias"], row["kind"])
        if key not in latest:
            latest[key] = json.loads(row["payload"])
    aliases = sorted({row["account_alias"] for row in observation_rows})
    accounts = []
    observation_kinds = ("account", "orders", "trades", "cashflows",
                         "movements", "restriction_releases", "share_credits")
    for alias in aliases:
        views = {kind: latest[(alias, kind)] for kind in observation_kinds
                 if (alias, kind) in latest}
        account = views.get("account")
        issues = []
        if account is None:
            issues.append("缺少账户余额观察")
        else:
            try:
                validate_snapshot(account)
            except (KeyError, TypeError, ValueError) as error:
                issues.append(str(error))
            try:
                validate_cash_balance(account)
            except (KeyError, TypeError, ValueError) as error:
                if str(error) not in issues:
                    issues.append(str(error))
        if "orders" not in views:
            issues.append("缺少委托观察")
        if "trades" not in views:
            issues.append("缺少成交观察（无成交也要导出空表）")
        if "cashflows" not in views:
            issues.append("缺少累计资金流水观察")
        accounts.append({
            "account_alias": alias,
            "observed_kinds": sorted(views),
            "statement_observations": {
                kind: {"observed": kind in views, "row_count": len(views[kind]["rows"]) if kind in views else 0}
                for kind in ("movements", "restriction_releases", "share_credits")
            },
            "latest_observed_at": max((str(value.get("observed_at")) for value in views.values()), default=None),
            "trading_date": account.get("trading_date") if account else None,
            "status": "ready_for_explicit_binding" if not issues else "observation_incomplete",
            "issues": issues,
            "paper_ledger_mutation": False,
        })

    runs = []
    for row in run_rows:
        state = json.loads(row["state"])
        reasons = []
        if row["mode"] != "paper":
            reasons.append("仅允许 paper 运行")
        if row["status"] != "paused":
            reasons.append("运行必须先暂停")
        if state.get("broker_binding"):
            reasons.append("运行已经绑定账户")
        if (row["cursor"] or state.get("day") or state.get("cash") != int(round(json.loads(row["config"])["initial_cash"] * 100))
                or any(state.get(key) for key in ("orders", "fills", "events", "positions", "receivables", "entitlements", "prices", "targets", "candidates"))
                or state.get("pending_targets") is not None):
            reasons.append("运行已经产生纸面历史，不能覆盖为开盘余额")
        if row["id"] in daily_run_ids:
            reasons.append("运行已经有 daily 纸面历史")
        binding = state.get("broker_binding") or {}
        runs.append({
            "run_id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "mode": row["mode"],
            "day": state.get("day"),
            "updated_at": row["updated_at"],
            "account_alias": binding.get("account_alias"),
            "bindable": not reasons,
            "issues": reasons,
            "paper_ledger_mutation": False,
        })

    current = journal.status()
    journal_reconciliation = _ths_journal_reconciliation_view()
    plans = []
    for plan in current.get("plans", []):
        result = plan.get("result") or {}
        plans.append({
            "plan_id": plan.get("id"),
            "account_alias": plan.get("account_alias"),
            "status": plan.get("status"),
            "trading_date": plan.get("trading_date"),
            "ledger_imported": bool((result.get("ledger_import") or {}).get("ledger_imported")),
        })
    return {
        "schema": "ths_simulated_ledger_readiness_v1",
        "status": "ready_for_explicit_binding" if any(row["bindable"] for row in runs) and any(row["status"] == "ready_for_explicit_binding" for row in accounts) else "awaiting_verified_export_or_paused_run",
        "execution_admitted": False,
        "submission_enabled": False,
        "client_operation": False,
        "paper_ledger_mutation": False,
        "real_account_enabled": False,
        "structured_observation_package": structured_package,
        "structured_package_ready_for_ledger_reconciliation": bool(
            structured_package.get("ready_for_ledger_reconciliation")
        ),
        "release_readiness": release_readiness,
        "release_readiness_status": release_readiness.get("status"),
        "release_ready": bool(release_readiness.get("release_ready")),
        "research_evidence_summary": research_evidence_summary,
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "order_count": structured_reconciliation.get("order_count"),
        "trade_count": structured_reconciliation.get("trade_count"),
        "cashflow_count": structured_reconciliation.get("cashflow_count"),
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "accounts": accounts,
        "runs": runs,
        "plans": plans,
        "next_action": "先导出同花顺账户/委托/成交/资金流水，并准备一个未产生纸面历史且已暂停的 paper run；绑定和入账都需要显式确认。",
    }


@router.get("/ths-simulated-ledger/readiness")
def ths_simulated_ledger_readiness():
    """Show whether a verified simulated THS account can enter the sole paper ledger."""
    return _ths_simulated_ledger_readiness()


class ThsSimulatedLedgerBindInput(BaseModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    account_alias: str = Field(min_length=1, max_length=120)
    confirm_simulated_account: bool = False


class ThsSimulatedLedgerPrepareInput(BaseModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirm_prepare: bool = False


class ThsSimulatedLedgerCreateRunInput(BaseModel):
    account_alias: str = Field(min_length=1, max_length=120)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    confirm_create: bool = False


@router.post("/ths-simulated-ledger/create-run")
def create_ths_simulated_ledger_run(body: ThsSimulatedLedgerCreateRunInput):
    """Create a blank paper run using one complete simulated-account observation."""
    if body.confirm_create is not True:
        return {
            "schema": "ths_simulated_ledger_create_run_v1",
            "status": "confirmation_required",
            "execution_admitted": False,
            "submission_enabled": False,
            "client_operation": False,
            "paper_ledger_mutation": False,
            "real_account_enabled": False,
            "next_action": "确认账户观察已核对后，再次提交 confirm_create=true",
        }
    from .ths.ledger_import import create_paper_run_from_observation
    try:
        receipt = create_paper_run_from_observation(body.account_alias, confirm=True, name=body.name)
    except (PermissionError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, detail={"message": str(error), "readiness": _ths_simulated_ledger_readiness()}) from error
    return {"schema": "ths_simulated_ledger_create_run_v1", "status": "paper_run_created_from_observation", **receipt,
            "next_action": "继续在模拟账户接入区显式绑定该 paper run；仍不会提交客户端委托。"}


@router.post("/ths-simulated-ledger/prepare-run")
def prepare_ths_simulated_ledger_run(body: ThsSimulatedLedgerPrepareInput):
    """Pause an untouched paper run before an explicitly observed simulated account is bound."""
    if body.confirm_prepare is not True:
        return {
            "schema": "ths_simulated_ledger_prepare_v1",
            "status": "confirmation_required",
            "execution_admitted": False,
            "submission_enabled": False,
            "client_operation": False,
            "paper_ledger_mutation": False,
            "real_account_enabled": False,
            "next_action": "确认该 run 为空白 paper run 且只用于同花顺模拟账户观察后，再次提交 confirm_prepare=true",
        }
    from .ths.ledger_import import prepare_empty_paper_run
    try:
        receipt = prepare_empty_paper_run(body.run_id, confirm=True)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except (PermissionError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, detail={"message": str(error), "readiness": _ths_simulated_ledger_readiness()}) from error
    return {
        "schema": "ths_simulated_ledger_prepare_v1",
        **receipt,
        "next_action": "导出同花顺模拟账户的账户、委托、成交和资金流水观察，再回到本页完成绑定。",
    }


@router.post("/ths-simulated-ledger/bind")
def bind_ths_simulated_ledger(body: ThsSimulatedLedgerBindInput):
    """Bind an observed simulated THS account to an existing paused paper run only."""
    if body.confirm_simulated_account is not True:
        return {
            "schema": "ths_simulated_ledger_bind_v1",
            "status": "confirmation_required",
            "execution_admitted": False,
            "submission_enabled": False,
            "client_operation": False,
            "paper_ledger_mutation": False,
            "next_action": "确认该账户是同花顺模拟账户且导出观察已核对后，再次提交 confirm_simulated_account=true",
        }
    from .ths.ledger_import import bind_account
    try:
        receipt = bind_account(body.run_id, body.account_alias, confirm_simulated_account=True)
    except (PermissionError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, detail={"message": str(error), "readiness": _ths_simulated_ledger_readiness()}) from error
    return {
        "schema": "ths_simulated_ledger_bind_v1",
        "status": "simulated_account_bound_to_paper_ledger",
        "execution_admitted": False,
        "submission_enabled": False,
        "client_operation": False,
        "paper_ledger_mutation": True,
        "real_account_enabled": False,
        "receipt": receipt,
    }


class ThsSimulatedLedgerImportInput(BaseModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    plan_id: str = Field(min_length=1, max_length=120)
    confirm_import: bool = False


@router.post("/ths-simulated-ledger/import")
def import_ths_simulated_ledger(body: ThsSimulatedLedgerImportInput):
    """Import a reconciled THS simulated receipt into the same paper ledger."""
    if body.confirm_import is not True:
        return {
            "schema": "ths_simulated_ledger_import_v1",
            "status": "confirmation_required",
            "execution_admitted": False,
            "submission_enabled": False,
            "client_operation": False,
            "paper_ledger_mutation": False,
            "next_action": "确认委托、成交、手续费、现金和持仓已逐项对账后，再次提交 confirm_import=true",
        }
    from .ths.ledger_import import import_plan
    try:
        receipt = import_plan(body.run_id, body.plan_id)
    except (PermissionError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, detail={"message": str(error), "readiness": _ths_simulated_ledger_readiness()}) from error
    return {
        "schema": "ths_simulated_ledger_import_v1",
        "status": "simulated_receipt_imported_to_paper_ledger",
        "execution_admitted": False,
        "submission_enabled": False,
        "client_operation": False,
        "paper_ledger_mutation": True,
        "real_account_enabled": False,
        "receipt": receipt,
    }


class ThsSimulatedLedgerMovementsInput(BaseModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    observed_at: str | None = None
    confirm_import: bool = False


@router.post("/ths-simulated-ledger/import-movements")
def import_ths_simulated_ledger_movements(body: ThsSimulatedLedgerMovementsInput):
    """Import the verified cumulative non-trade cash statement into the same paper Ledger."""
    if body.confirm_import is not True:
        return {
            "schema": "ths_simulated_ledger_import_movements_v1",
            "status": "confirmation_required",
            "execution_admitted": False,
            "submission_enabled": False,
            "client_operation": False,
            "paper_ledger_mutation": False,
            "real_account_enabled": False,
            "next_action": "确认同花顺累计资金流水与账户余额已核对，且本交易日尚未产生新计划后，再次提交 confirm_import=true",
        }
    from .ths.ledger_import import import_movements
    try:
        receipt = import_movements(body.run_id, observed_at=body.observed_at)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, detail={"message": str(error), "readiness": _ths_simulated_ledger_readiness()}) from error
    return {
        "schema": "ths_simulated_ledger_import_movements_v1",
        "status": "cash_statement_imported_to_paper_ledger",
        "execution_admitted": False,
        "submission_enabled": False,
        "client_operation": False,
        "paper_ledger_mutation": bool(receipt.get("changed")),
        "real_account_enabled": False,
        "receipt": receipt,
        "same_ledger_bundle_reconciliation": _same_ledger_bundle_reconciliation_view(),
        "next_action": "继续使用同一 paper Ledger；新委托前若资金流水变化，先重新导出并按当前交易会话对账。",
    }


class ThsSimulatedLedgerPreviewInput(BaseModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    plan_id: str = Field(min_length=1, max_length=120)
    use_observation_bundle: bool = False
    observed_at: str | None = None


@router.post("/ths-simulated-ledger/preview-import")
def preview_ths_simulated_ledger_import(body: ThsSimulatedLedgerPreviewInput):
    """Preview exact Ledger import deltas; staged observations are always rolled back."""
    observations = None
    bundle_preview = None
    if body.use_observation_bundle:
        from .ths.observation_preview import build_observation_bundle, preview_bundle

        root = (settings.DATA_ROOT / "ths" / "observation_bundle").resolve()
        paths = {
            "account": root / "account_observation.json",
            "orders": root / "orders.csv",
            "trades": root / "trades.csv",
            "cashflows": root / "cashflows.csv",
        }
        profile = None
        profile_path = root / "profile.json"
        if profile_path.is_file():
            profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        bundle_preview = preview_bundle(
            paths["account"], paths["orders"], paths["trades"], paths["cashflows"],
            profile=profile, observed_at=body.observed_at,
        )
        if bundle_preview.get("status") != "bundle_ready_for_reconciliation":
            return {
                "schema": "ths_ledger_import_preview_v1",
                "status": "blocked_by_observation_bundle",
                "run_id": body.run_id,
                "plan_id": body.plan_id,
                "execution_admitted": False,
                "ledger_mutated": False,
                "journal_mutated": False,
                "plan_mutated": False,
                "bundle_preview": bundle_preview,
                "next_action": bundle_preview.get("next_action") or "补齐同花顺账户、委托、成交和累计资金流水四类导出后重新预览",
            }
        _, observations = build_observation_bundle(
            paths["account"], paths["orders"], paths["trades"], paths["cashflows"],
            profile=profile, observed_at=body.observed_at,
        )
    from .ths.ledger_import import preview_plan
    result = preview_plan(body.run_id, body.plan_id, observations=observations)
    if bundle_preview is not None:
        result["bundle_preview"] = bundle_preview
    return result


class ThsSimulatedLedgerReconcileInput(BaseModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    observed_at: str | None = None
    include_restriction_releases: bool = False
    include_share_credits: bool = False
    confirm_reconcile: bool = False


@router.post("/ths-simulated-ledger/reconcile-session")
def reconcile_ths_simulated_ledger_session(body: ThsSimulatedLedgerReconcileInput):
    """Reconcile the next exchange-session observation into the same paper ledger."""
    if body.confirm_reconcile is not True:
        return {
            "schema": "ths_simulated_ledger_reconcile_session_v1",
            "status": "confirmation_required",
            "execution_admitted": False,
            "submission_enabled": False,
            "client_operation": False,
            "paper_ledger_mutation": False,
            "real_account_enabled": False,
            "next_action": "确认下一交易日账户、委托、成交、资金流水及 T+1 释放已核对后，再次提交 confirm_reconcile=true",
        }
    from .ths.ledger_import import reconcile_session
    try:
        receipt = reconcile_session(
            body.run_id,
            observed_at=body.observed_at,
            restriction_releases=body.include_restriction_releases,
            share_credits=body.include_share_credits,
        )
    except (PermissionError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, detail={"message": str(error), "readiness": _ths_simulated_ledger_readiness()}) from error
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    return {
        "schema": "ths_simulated_ledger_reconcile_session_v1",
        "status": "simulated_session_reconciled_to_paper_ledger",
        "execution_admitted": False,
        "submission_enabled": False,
        "client_operation": False,
        "paper_ledger_mutation": bool(receipt.get("changed")),
        "real_account_enabled": False,
        "receipt": receipt,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        "next_action": "继续使用同一 paper Ledger；若出现未归属委托、资金流水或公司行动差异，先解决对账冲突，不覆盖历史。",
    }


class RunInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    mode: str = "replay"
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    config: dict = Field(default_factory=dict)
    reference_run: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


@router.post("/runs", status_code=202)
def create_run(body: RunInput, idempotency_key: str = Header(..., min_length=8, max_length=80)):
    import pandas as pd
    try:
        config = StrategyConfig(**body.config).validate()
    except TypeError as error:
        raise ValueError("Unknown strategy setting or invalid numeric type") from error
    reference = store.get(body.reference_run) if body.reference_run else None
    if body.reference_run and reference is None:
        raise ValueError("对照的原始运行不存在")
    feed = HistoricalFeed(reference["dataset"]) if reference else HistoricalFeed()
    start, end = pd.Timestamp(body.start), pd.Timestamp(body.end)
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if body.mode == "replay":
        if body.start < feed.manifest["start"] or body.end > feed.manifest["end"] or not any(body.start <= d <= body.end for d in feed.calendar):
            raise ValueError("回放日期超出数据范围或无交易日")
    elif body.mode == "paper":
        if body.start < date.today().isoformat():
            raise ValueError("日终纸面账户从今天或未来开始，不把历史回填冒充已发生交易")
    else:
        raise ValueError("仅支持历史回放和日终纸面模拟，实盘未启用")
    dataset = freeze(feed) if body.mode == "replay" else settings.DATASET
    return store.create(body.name, config.to_dict(), body.start, body.end, idempotency_key, body.mode, dataset)


@router.get("/runs/rejections")
def run_rejections(request_key: str | None = None):
    """Read persisted run admission rejections without mutating a run."""

    return store.rejection_receipts(request_key)


@router.get("/runs/{identifier}")
def run_detail(identifier: str):
    value = detail(identifier)
    if value is None:
        raise HTTPException(404, "运行不存在")
    return value


@router.post("/runs/{identifier}/{action}")
def run_control(identifier: str, action: str):
    return store.control(identifier, action)


class SignalPreviewInput(BaseModel):
    signal_request: dict


@router.get("/runs/{identifier}/broker/history")
def broker_history(identifier: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    from .ths.history import history
    try:
        return history(identifier, limit=limit, offset=offset)
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"保存回读尚不可读取：{error}") from error
    except LookupError as error:
        raise HTTPException(404, "运行不存在") from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error


@router.post("/runs/{identifier}/broker/signal-preview")
def broker_signal_preview(identifier: str, body: SignalPreviewInput):
    from .ths.broker import THSAdapter
    try:
        return THSAdapter({"account_alias": body.signal_request.get("account_alias")}).prepare_signal(
            identifier, body.signal_request)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"信号预览未生成：{error}") from error


@router.get("/comparison")
def research_comparison():
    return [comparison(run["id"]) for run in store.runs()]


@router.get("/research-replays")
def saved_replays():
    try:
        return replay_reports.catalog()
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(409, f"研究记录尚不可读取：{error}") from error


@router.get("/research-diagnostics")
def research_diagnostics():
    from . import research_diagnostics as diagnostics
    try:
        return diagnostics.catalog()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"研究诊断记录尚不可读取：{error}") from error


@router.get("/research-candidate-board")
def research_candidate_board():
    from . import research_diagnostics as diagnostics
    try:
        return diagnostics.candidate_board()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"研究候选复核板尚不可读取：{error}") from error


@router.get("/research/portfolio-realization")
def research_portfolio_realization(case_id: str | None = None):
    from .research_realization import view
    try:
        return view(case_id)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"保存的仓位实现记录尚不可读取：{error}") from error


@router.get("/research-replays/{identifier}/{signal}")
def saved_replay_detail(identifier: str, signal: str):
    try:
        return replay_reports.detail(identifier, signal)
    except FileNotFoundError as error:
        raise HTTPException(404, "研究回放结果不存在") from error
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(409, f"研究记录尚不可读取：{error}") from error


@router.get("/research-comparison")
def saved_window_comparison(runs: str, start: str, end: str):
    try:
        return research_comparison_view.window(runs, start, end)
    except FileNotFoundError as error:
        raise HTTPException(404, "研究回放结果不存在") from error
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(409, f"研究区间尚不可比较：{error}") from error


@router.get("/research-comparison-snapshots")
def saved_window_comparison_snapshots(limit: int = Query(12, ge=1, le=50)):
    try:
        return research_comparison_view.snapshots(limit=limit)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, f"保存的研究比较尚不可读取：{error}") from error


@router.get("/annual-quality")
def annual_quality_catalog():
    from .research.annual_quality_portfolios import saved_catalog
    try:
        return saved_catalog()
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(409, f"年度候选记录尚不可读取：{error}") from error


@router.get("/annual-quality/{identifier}/{signal}")
def annual_quality_detail(identifier: str, signal: str):
    from .research.annual_quality_portfolios import saved_detail
    try:
        return saved_detail(identifier, signal)
    except FileNotFoundError as error:
        raise HTTPException(404, "年度候选记录不存在") from error
    except (OSError, ValueError, KeyError, StopIteration) as error:
        raise HTTPException(409, f"年度候选记录尚不可读取：{error}") from error


@router.get("/research-replays/{identifier}/{signal}/valuation-gaps")
def saved_replay_valuation_gaps(identifier: str, signal: str):
    from .research.valuation_gaps import saved
    try:
        return saved(identifier, signal)
    except FileNotFoundError as error:
        raise HTTPException(404, "研究回放结果不存在") from error
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(409, f"估值缺口记录尚不可读取：{error}") from error


@router.get("/runs/{identifier}/files/{filename}")
def download(identifier: str, filename: str):
    item = store.get(identifier)
    if item is None or item["status"] != "completed" or filename not in {"daily.csv", "orders.csv", "fills.csv", "summary.json", "account.json"}:
        raise HTTPException(404, "已完成的结果文件不存在")
    path = settings.DATA_ROOT / "results" / identifier / filename
    if not path.is_file():
        raise HTTPException(409, "结果文件尚在写入")
    return FileResponse(path, filename=filename)
