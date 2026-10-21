"""Read-only catalog for research artifacts that are not shared-ledger replays."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import time

from . import replay_reports
from . import settings
from .report import metrics


_CANDIDATE_BOARD_CACHE: tuple[float, object, dict] | None = None


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _research_comparison_eligible(diagnostic: dict) -> bool:
    """Identify comparable research evidence without enabling selection."""
    family = diagnostic.get("family")
    if family == "stockdb_independent_sample_compare":
        return (
            diagnostic.get("independent_sample") is True
            and diagnostic.get("prior_forward_overlap") is not True
            and int(diagnostic.get("provider_complete_paired_rows") or 0) > 0
        )
    if family == "stockdb_independent_window_stability":
        return (
            diagnostic.get("non_overlapping") is True
            and int(diagnostic.get("window_count") or 0) >= 2
            and int(diagnostic.get("paired_rows_all_windows") or 0) > 0
        )
    return False


def _qlib_row(path: Path, record: dict) -> dict | None:
    if record.get("status") not in {"completed", "completed_partial_data"}:
        return None
    portfolio = record.get("portfolio") or {}
    daily_path = path / "portfolio_daily.csv"
    if not daily_path.is_file() or not portfolio.get("sessions"):
        return None
    daily = []
    previous_cost = 0.0
    with daily_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            cumulative_cost = float(row["total_cost"])
            daily.append({
                "date": row["date"],
                "equity": float(row["account"]),
                "cash": float(row["cash"]),
                "fees": cumulative_cost - previous_cost,
                "turnover_value": float(row["total_turnover"]),
                "stale_symbols": [],
            })
            previous_cost = cumulative_cost
    if not daily:
        return None
    initial = float(record["config"]["portfolio"]["capital"])
    calculated = metrics(daily, initial)
    test_start, test_end = record["config"]["segments"]["test"]
    quality = record.get("data_quality") or {}
    return {
        "id": path.name,
        "family": "qlib_partial_data_diagnostic",
        "label": "Qlib 质量门控动量基线",
        "status": "诊断完成（部分数据）" if record["status"] == "completed_partial_data" else "诊断完成",
        "start": test_start,
        "end": test_end,
        "sessions": calculated["sessions"],
        "symbols": int(quality.get("symbols", 0)),
        "net_return": calculated["net_return"],
        "max_drawdown": calculated["max_drawdown"],
        "sharpe_zero_rf_252": calculated["sharpe_zero_rf_252"],
        "benchmark_return": portfolio.get("benchmark_total_return"),
        "relative_wealth_return": portfolio.get("relative_wealth_return"),
        "fees": calculated["fees"],
        "excluded_symbols": len(portfolio.get("excluded_symbols") or []),
        "data_quality_gate": portfolio.get("data_quality_gate", "unknown"),
        "blockers": portfolio.get("data_quality_blockers") or [],
        "source": "real_local_historical_data",
        "report_path": str(path / "REPORT.md"),
        "execution_admitted": False,
        "strategy_validated": False,
        "live_ready": False,
        "limitations": record.get("limitations", []),
    }


def _stockdb_rows(path: Path, record: dict) -> list[dict]:
    if record.get("status") not in {"completed_factor_ledger_diagnostics", "blocked_factor_ledger_diagnostics"}:
        return []
    plan = record.get("plan") or {}
    report_path = path / "REPORT.md"
    rows = []
    for result in record.get("results") or []:
        if result.get("status") != "completed":
            continue
        rows.append({
            "id": path.name,
            "family": "stockdb_factor_diagnostic",
            "label": result.get("signal", "StockDB factor diagnostic"),
            "status": "诊断完成" if result.get("status") == "completed" else result.get("status", "未完成"),
            "start": record.get("start", plan.get("start")),
            "end": record.get("end", plan.get("end")),
            "sessions": result.get("completed_sessions", record.get("sessions")),
            "symbols": None,
            "net_return": result.get("net_return"),
            "max_drawdown": result.get("max_drawdown"),
            "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": result.get("fees"),
            "excluded_symbols": None,
            "data_quality_gate": "StockDB source diagnostic; PIT incomplete",
            "blockers": ["retrospective development; not validation or current signal"],
            "source": "local StockDB replay",
            "report_path": str(report_path) if report_path.is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": [
                "StockDB fields remain research-only; no promotion to HistoricalFeed or account.",
                "Point-in-time membership and current signal validity are not established.",
            ],
        })
    return rows


def _stockdb_ledger_replay_rows(path: Path, record: dict) -> list[dict]:
    """Index shared-Ledger StockDB baskets, including explicit no-trade reasons."""
    if (record.get("schema") != "stockdb_shared_ledger_replay_v1"
            or record.get("status") != "completed_research_only"):
        return []
    dates = record.get("common_path_dates") or []
    snapshot = record.get("snapshot")
    snapshot_meta = {}
    if snapshot:
        snapshot_path = Path(str(snapshot)) / "snapshot.json"
        if snapshot_path.is_file():
            try:
                snapshot_payload = _load(snapshot_path)
                effective_dates = snapshot_payload.get("effective_session_dates") or dates
                snapshot_meta = {
                    "schema": snapshot_payload.get("schema"),
                    "captured_at": snapshot_payload.get("captured_at"),
                    "effective_session_start": effective_dates[0] if effective_dates else None,
                    "effective_session_end": effective_dates[-1] if effective_dates else None,
                    "rows": snapshot_payload.get("rows") or {},
                    "research_only": snapshot_payload.get("research_only"),
                    "execution_admitted": snapshot_payload.get("execution_admitted"),
                    "file_hashes": {
                        name: value.get("sha256")
                        for name, value in (snapshot_payload.get("files") or {}).items()
                        if isinstance(value, dict) and value.get("sha256")
                    },
                }
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                snapshot_meta = {}
    rows = []
    for candidate in record.get("candidates") or []:
        if candidate.get("candidate") is None:
            continue
        rows.append({
            "id": path.name,
            "family": "stockdb_shared_ledger_replay",
            "label": candidate["candidate"],
            "status": "诊断完成",
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
            "sessions": candidate.get("sessions"),
            "symbols": candidate.get("target_count"),
            "net_return": candidate.get("net_return"),
            "max_drawdown": candidate.get("max_drawdown"),
            "sharpe_zero_rf_252": candidate.get("sharpe_zero_rf_252"),
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": candidate.get("fees"),
            "order_count": candidate.get("order_count"),
            "fill_count": candidate.get("fill_count"),
            "symbols_with_orders": candidate.get("symbols_with_orders"),
            "symbols_without_orders": candidate.get("symbols_without_orders"),
            "no_trade_reason_counts": candidate.get("no_trade_reason_counts") or {},
            "data_quality_gate": "StockDB raw-close shared Ledger; research-only",
            "blockers": [
                "StockDB point-in-time membership and corporate actions are not established",
                "execution admission is disabled",
            ],
            "source": "production Engine/Ledger StockDB replay",
            "snapshot": snapshot,
            "snapshot_meta": snapshot_meta,
            "report_path": str(path / candidate["candidate"] / "REPORT.md") if (
                path / candidate["candidate"] / "REPORT.md"
            ).is_file() else str(path / "REPORT.md"),
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": [
                "Frozen raw closes are a research basis, not broker observations or executable quotes.",
                "No-order rows retain production-engine diagnostics and must not be read as zero-return evidence.",
            ],
        })
    return rows


def _public_seat_rows(path: Path, record: dict) -> list[dict]:
    """Index public seat-disclosure pilots without treating them as accounts."""
    if record.get("status") != "completed_exploratory":
        return []
    coverage = record.get("coverage") or {}
    collection = coverage.get("collection") or {}
    config = record.get("config") or {}
    evaluation = config.get("evaluation") or [record.get("start"), record.get("end")]
    if len(evaluation) != 2 or not all(evaluation):
        return []
    rows = []
    limitations = [
        "公开龙虎榜席位列示不是个人交割单、账户持仓或资金规模。",
        "供应商分页完整不等于交易所披露完整，历史披露时间未验证。",
        "席位选择来自前置披露频次，不是收益调参；本轮不是外部留出。",
    ]
    common_blockers = [
        "public_disclosure_not_personal_account",
        "publication_time_unverified",
        "provider_completeness_only",
        "matched_industry_size_control_unavailable",
    ]
    for result in record.get("metrics") or []:
        mode = result.get("mode")
        lag = result.get("lag")
        if not mode or lag is None:
            continue
        label = f"{mode} disclosed-seat diagnostic (lag {lag})"
        rows.append({
            "id": f"{path.name}:{mode}:lag{lag}",
            "family": "public_seat_disclosure_diagnostic",
            "label": label,
            "status": "探索性完成",
            "start": result.get("period_start", evaluation[0]),
            "end": result.get("period_end", evaluation[1]),
            "sessions": result.get("sessions"),
            "symbols": (record.get("data_checks") or {}).get("symbols"),
            "net_return": result.get("net_return"),
            "max_drawdown": result.get("max_drawdown"),
            "sharpe_zero_rf_252": None,
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": result.get("fees"),
            "turnover_value": result.get("turnover_value"),
            "orders": result.get("orders"),
            "fills": result.get("fills"),
            "stale_valuation_days": result.get("stale_valuation_days"),
            "blocked_attempts": result.get("blocked_attempts"),
            "selected_seats": (coverage.get("selected_seats") or []),
            "test_selected_events": coverage.get("test_selected_events"),
            "public_disclosure_only": True,
            "personal_account_inference": False,
            "selection_is_point_in_time": False,
            "fresh_holdout_claimed": False,
            "data_quality_gate": "public seat disclosure diagnostic; not a personal account",
            "blockers": common_blockers,
            "source": "local public seat disclosure dataset + shared Engine/Ledger replay",
            "report_path": str(path / "REPORT.md") if (path / "REPORT.md").is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": limitations,
            "artifacts_path": record.get("artifacts"),
            "collection_status": collection.get("status"),
            "raw_seat_rows": coverage.get("raw_seat_rows"),
            "normalized_rows": coverage.get("normalized_rows"),
        })
    return rows


def _public_seat_bulk_rows(path: Path, record: dict) -> list[dict]:
    """Index completed full-source seat replays on the same review board.

    The bulk runner stores one run-level ``partial_*`` status because some
    hypotheses can stop on unresolved corporate actions or price references.
    Completed hypotheses are still valid research receipts and should remain
    discoverable; failed hypotheses are left in their run artifact rather than
    being converted into zero-return rows.
    """
    if record.get("research_family") != "full_source_public_seat_donchian_v1":
        return []
    coverage = record.get("coverage") or {}
    rows = []
    limitations = [
        "公开龙虎榜席位列示不是个人交割单、账户持仓或资金规模。",
        "供应商分页完整不等于交易所披露完整，历史披露时间未验证。",
        "席位集合在研究窗口前按披露频次固定，未按评估期收益选择。",
        "全源回放仍受未解决公司行动、价格参考和缺失披露约束。",
    ]
    blockers = [
        "public_disclosure_not_personal_account",
        "publication_time_unverified",
        "provider_completeness_only",
        "pit_membership_unverified",
        "execution_not_admitted",
    ]
    for result in record.get("results") or []:
        if result.get("status") != "completed":
            continue
        signal = result.get("signal")
        mode = result.get("mode")
        lag = result.get("lag")
        if not signal or mode is None or lag is None:
            continue
        rows.append({
            "id": f"{path.name}:{signal}",
            "family": "public_seat_disclosure_diagnostic",
            "label": f"{signal} full-source disclosed-seat diagnostic (lag {lag})",
            "status": "review_only",
            "start": record.get("start"),
            "end": record.get("end"),
            "sessions": result.get("sessions"),
            "symbols": (record.get("stock_manifest") or {}).get("canonical_codes"),
            "net_return": result.get("net_return"),
            "max_drawdown": result.get("max_drawdown"),
            "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
            "fees": result.get("fees"),
            "turnover_value": result.get("turnover_value"),
            "orders": result.get("orders"),
            "fills": result.get("fills"),
            "stale_valuation_days": result.get("stale_valuation_days"),
            "blocked_attempts": result.get("blocked_attempts"),
            "selected_seats": record.get("selected_seats") or [],
            "public_disclosure_only": True,
            "personal_account_inference": False,
            "selection_is_point_in_time": False,
            "fresh_holdout_claimed": False,
            "data_quality_gate": "public seat disclosure diagnostic; not a personal account",
            "blockers": blockers,
            "source": "local public seat disclosure dataset + shared Engine/Ledger replay",
            "report_path": str(path / "REPORT.md") if (path / "REPORT.md").is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": limitations,
            "artifacts_path": record.get("artifacts"),
            "raw_seat_rows": coverage.get("raw_listings_in_requested_interval"),
            "normalized_rows": coverage.get("in_scope_raw_listings"),
            "bulk_run_status": record.get("status"),
        })
    return rows


def _seat_candidate_panel_replay_rows(path: Path, record: dict) -> list[dict]:
    """Expose candidate-panel public-seat replays without promotion."""
    if record.get("schema") != "seat_hypothesis_candidate_panel_replay_v1":
        return []
    lag = record.get("lag")
    if lag not in {1, 2}:
        return []
    metrics_value = record.get("metrics") or {}
    return [{
        "id": f"{path.name}:lag{lag}",
        "family": "seat_candidate_panel_replay_diagnostic",
        "label": f"{record.get('hypothesis_id', 'public-seat')} candidate-panel lag {lag}",
        "status": "review_only",
        "start": record.get("start_date"),
        "end": record.get("end_date"),
        "sessions": record.get("replay_sessions"),
        "symbols": record.get("symbols"),
        "net_return": metrics_value.get("net_return"),
        "max_drawdown": metrics_value.get("max_drawdown"),
        "sharpe_zero_rf_252": metrics_value.get("sharpe_zero_rf_252"),
        "fees": metrics_value.get("fees"),
        "turnover_value": metrics_value.get("turnover_value"),
        "orders": len(record.get("orders") or []),
        "fills": len(record.get("fills") or []),
        "candidate_panel_only": True,
        "engine_ledger_replay_completed": record.get("engine_ledger_replay_completed") is True,
        "data_quality_gate": "public-seat candidate panel; shared Engine/Ledger rehearsal only",
        "blockers": [
            "candidate_panel_only",
            "pit_membership_unverified",
            "company_actions_unverified",
            "public_disclosure_time_unverified",
            "execution_not_admitted",
        ],
        "source": "frozen public-seat hypothesis + StockDB candidate bars + shared Engine/Ledger",
        "report_path": str(path / "REPORT.md") if (path / "REPORT.md").is_file() else None,
        "execution_admitted": False,
        "strategy_validated": False,
        "live_ready": False,
        "limitations": list(record.get("limitations") or []),
    }]


def _shortline_candidate_panel_replay_rows(path: Path, record: dict) -> list[dict]:
    """Expose shortline candidate-panel replays without enabling selection."""
    if record.get("schema") != "a_share_shortline_candidate_panel_replay_v1":
        return []
    metrics_value = record.get("metrics") or {}
    cap = record.get("max_symbols")
    variant = f"max{int(cap)}" if isinstance(cap, int) and cap > 0 else "uncapped"
    strategy_id = record.get("strategy_id", "")
    label = f"{strategy_id or 'shortline'} candidate-panel replay ({variant})"
    source = (
        "fixed signal-date emotion rule + StockDB raw SH/SZ bars + shared Engine/Ledger"
        if strategy_id == "shortline_fixed_emotion_rule_v1"
        else "corrected shortline model scores + StockDB raw SH/SZ bars + shared Engine/Ledger"
    )
    return [{
        "id": f"{path.name}:{variant}",
        "family": "shortline_candidate_panel_replay_diagnostic",
        "label": label,
        "status": "review_only",
        "start": record.get("start_date"),
        "end": record.get("end_date"),
        "sessions": record.get("replay_sessions"),
        "symbols": record.get("symbols"),
        "net_return": metrics_value.get("net_return"),
        "max_drawdown": metrics_value.get("max_drawdown"),
        "sharpe_zero_rf_252": metrics_value.get("sharpe_zero_rf_252"),
        "fees": metrics_value.get("fees"),
        "turnover_value": metrics_value.get("turnover_value"),
        "orders": len(record.get("orders") or []),
        "fills": len(record.get("fills") or []),
        "candidate_panel_only": record.get("candidate_panel_only") is True,
        "panel_variant": variant,
        "max_symbols": cap,
        "engine_ledger_replay_completed": record.get("engine_ledger_replay_completed") is True,
        "future_labels_used_for_score": record.get("future_labels_used_for_score") is True,
        "selection_enabled": False,
        "data_quality_gate": "shortline candidate panel; shared Engine/Ledger rehearsal only",
        "blockers": [
            "candidate_panel_only",
            "pit_membership_unverified",
            "company_actions_unverified",
            "intraday_queue_and_fill_unavailable",
            "execution_not_admitted",
        ],
        "source": source,
        "report_path": str(path / "REPORT.md") if (path / "REPORT.md").is_file() else None,
        "execution_admitted": False,
        "strategy_validated": False,
        "live_ready": False,
        "limitations": list(record.get("limitations") or []),
    }]


def _shortline_stage_selection_rows(path: Path, record: dict) -> list[dict]:
    """Expose stage-transition receipts without enabling selection."""
    if record.get("schema") != "a_share_shortline_stage_selection_diagnostic_v1":
        return []
    return [{
        "id": path.name,
        "family": "shortline_stage_selection_diagnostic",
        "label": "shortline stage selection diagnostic",
        "status": "review_only",
        "start": None,
        "end": None,
        "stages": record.get("stages") or [],
        "future_labels_used_for_selection": record.get("future_labels_used_for_selection") is True,
        "candidate_generated": record.get("candidate_generated") is True,
        "target_generated": record.get("target_generated") is True,
        "orders_generated": record.get("orders_generated") is True,
        "data_quality_gate": "stage baseline and touchability diagnostic only",
        "blockers": [
            "future_labels_diagnostic_only",
            "touchability_proxy_not_fill_evidence",
            "candidate_generation_disabled",
            "execution_not_admitted",
        ],
        "source": "corrected shortline stage baseline + touchability proxy + dated LightGBM folds",
        "report_path": str(path / "REPORT.md") if (path / "REPORT.md").is_file() else None,
        "execution_admitted": False,
        "strategy_validated": False,
        "live_ready": False,
        "limitations": list(record.get("limitations") or []),
    }]


def _stockdb_alpha158_lgbm_candidate_panel_rows(path: Path, record: dict) -> list[dict]:
    """Expose Alpha158/LightGBM candidate-panel replays without promotion."""
    if record.get("schema") != "a_share_stockdb_alpha158_lgbm_candidate_panel_replay_v1":
        return []
    metrics_value = record.get("metrics") or {}
    raw_end = record.get("raw_bar_end")
    variant = "nextbar" if raw_end and str(raw_end) > str(record.get("end_date", "")) else "score_window_end"
    label = f"{record.get('strategy_id', 'stockdb-alpha158-lgbm')} candidate-panel replay ({variant})"
    return [{
        "id": f"{path.name}:{variant}",
        "family": "stockdb_alpha158_lgbm_candidate_panel_replay_diagnostic",
        "label": label, "status": "review_only",
        "start": record.get("start_date"), "end": record.get("end_date"),
        "sessions": record.get("replay_sessions"), "symbols": record.get("symbols"),
        "net_return": metrics_value.get("net_return"), "max_drawdown": metrics_value.get("max_drawdown"),
        "sharpe_zero_rf_252": metrics_value.get("sharpe_zero_rf_252"),
        "fees": metrics_value.get("fees"), "turnover_value": metrics_value.get("turnover_value"),
        "orders": len(record.get("orders") or []), "fills": len(record.get("fills") or []),
        "candidate_panel_only": True, "panel_variant": variant,
        "raw_bar_end": raw_end, "engine_ledger_replay_completed": record.get("engine_ledger_replay_completed") is True,
        "future_labels_used_for_score": record.get("future_labels_used_for_score") is True,
        "selection_enabled": False, "data_quality_gate": "Alpha158/LightGBM candidate panel; shared Engine/Ledger rehearsal only",
        "blockers": ["candidate_panel_only", "pit_membership_unverified", "company_actions_unverified",
                     "intraday_queue_and_fill_unavailable", "execution_not_admitted"],
        "source": "dated frozen Alpha158/LightGBM scores + StockDB raw bars + shared Engine/Ledger",
        "report_path": str(path / "REPORT.md") if (path / "REPORT.md").is_file() else None,
        "execution_admitted": False, "strategy_validated": False, "live_ready": False,
        "limitations": list(record.get("limitations") or []),
    }]


def _full_market_alpha158_rows(path: Path, record: dict) -> list[dict]:
    """Index current-cutoff Alpha158 proxy replays without treating them as accounts."""
    if record.get("status") != "completed_research_only" or not str(record.get("source", "")).startswith("StockDB_"):
        return []
    metrics_path = path / "candidate_proxy_metrics.csv"
    if not metrics_path.is_file():
        return []
    def date_label(value):
        text = str(value or "")
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text

    start = date_label(record.get("analysis_start") or "20260105")
    end = str(record.get("analysis_end") or "")
    if not end:
        snapshot = Path(str(record.get("snapshot") or ""))
        manifest_path = snapshot / "manifest.json"
        if manifest_path.is_file():
            try:
                end = str(json.loads(manifest_path.read_text(encoding="utf-8-sig")).get("end") or "")
            except (OSError, UnicodeError, json.JSONDecodeError):
                end = ""
    end = date_label(end)
    if not start or not end:
        return []
    rows = []
    limitations = [
        "StockDB provider-return proxy, not executable cash or share accounting.",
        "Historical membership, corporate actions and PIT financial semantics remain unresolved.",
        "The later segment is a checked development segment, not a fresh external holdout.",
    ]
    with metrics_path.open(encoding="utf-8-sig", newline="") as stream:
        for item in csv.DictReader(stream):
            variant = str(item.get("variant") or "")
            period = str(item.get("period") or "")
            if not variant or not period:
                continue
            rows.append({
                "id": f"{path.name}:{variant}:{period}",
                "family": "full_market_alpha158_diagnostic",
                "label": f"Alpha158 {variant} ({period})",
                "status": "诊断完成",
                "start": start,
                "end": end,
                "sessions": int(item.get("sessions") or 0),
                "symbols": (record.get("source_coverage") or {}).get("codes"),
                "net_return": float(item["return"]) if item.get("return") else None,
                "max_drawdown": float(item["max_drawdown"]) if item.get("max_drawdown") else None,
                "sharpe_zero_rf_252": float(item["sharpe_zero_rf_252"]) if item.get("sharpe_zero_rf_252") else None,
                "benchmark_return": None,
                "relative_wealth_return": None,
                "fees": float(item["cost_haircut_cny"]) if item.get("cost_haircut_cny") else None,
                "excluded_symbols": None,
                "data_quality_gate": "StockDB Alpha158 provider-return proxy; research-only",
                "blockers": ["PIT未建立", "代理收益非统一现金账本", "未完成独立留出", "未获交易准入"],
                "source": "local StockDB snapshot + Qlib Alpha158 proxy replay",
                "report_path": str(path / "REPORT.md") if (path / "REPORT.md").is_file() else None,
                "execution_admitted": False,
                "strategy_validated": False,
                "live_ready": False,
                "limitations": limitations,
                "factor_variant": variant,
                "period": period,
            })
    return rows


def _alpha158_linear_rows(path: Path, record: dict) -> list[dict]:
    """Index matched Alpha158 method comparisons without promotion."""
    if (record.get("status") != "completed_alpha158_linear_diagnostics"
            or record.get("research_family") != "alpha158_linear_ensemble"):
        return []
    protocol = record.get("protocol") or {}
    blockers = [
        "仅研究回放，未完成独立留出",
        "StockDB/重建事件非PIT确认",
        "策略未验证",
        "未获交易准入",
    ]
    limitations = [
        "Ridge 与既有 LightGBM 使用同一 Alpha158 特征、标签、成本和共享 Engine/Ledger 回放。",
        "2026 评估段为已观察开发样本，不是新鲜独立测试；模型未使用 2026 标签反馈。",
        "原始行情、事件和上市主表仍是研究源，不构成券商账户或实盘证据。",
    ]
    rows = []
    for result in record.get("results") or []:
        signal = str(result.get("signal") or "")
        if not signal:
            continue
        rows.append({
            "id": f"{path.name}:{signal}",
            "family": "alpha158_linear_ensemble_diagnostic",
            "label": f"Alpha158 method comparison: {signal}",
            "status": "诊断完成",
            "start": record.get("start"),
            "end": record.get("end"),
            "sessions": result.get("completed_sessions"),
            "symbols": record.get("symbols"),
            "net_return": result.get("net_return"),
            "max_drawdown": result.get("max_drawdown"),
            "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": result.get("fees"),
            "turnover_value": result.get("turnover_value"),
            "orders": result.get("orders"),
            "fills": result.get("fills"),
            "stale_valuation_days": result.get("stale_valuation_days"),
            "data_quality_gate": "Alpha158 同口径方法比较；研究-only",
            "blockers": blockers,
            "source": "local Alpha158 native features + shared Engine/Ledger",
            "report_path": str(path / "REPORT.md") if (path / "REPORT.md").is_file() else None,
            "artifacts_path": record.get("artifacts"),
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "comparison_eligible": False,
            "fresh_holdout_claimed": False,
            "selection_is_point_in_time": False,
            "limitations": limitations,
            "method_protocol": protocol.get("id"),
        })
    return rows


def _stockdb_rolling_rows(path: Path, record: dict) -> list[dict]:
    """Index the longer raw-StockDB rolling comparison without promotion."""
    if record.get("schema") != "stockdb_rolling_low_volatility_ledger_v1":
        return []
    rows = []
    limitations = [
        "StockDB raw provider prices and retrieval-time universe remain research-only.",
        "Point-in-time membership, listing master and corporate actions are not verified.",
        "This is a same-rule longer diagnostic, not an untouched holdout.",
    ]
    report_path = settings.ROOT / "outputs" / "stockdb_rolling_low_volatility" / path.name / "REPORT.md"
    for result in record.get("results") or []:
        status = result.get("status")
        if status not in {"completed", "failed"}:
            continue
        blockers = ["PIT未建立", "公司行为/挂牌主表未核验", "未获交易准入", "非独立留出"]
        if status != "completed":
            blockers.append("StockDB缺行导致账本在最后成功日中止")
        rows.append({
            "id": f"{path.name}:{result.get('signal')}",
            "family": "stockdb_rolling_low_volatility_diagnostic",
            "label": result.get("signal", "StockDB rolling diagnostic"),
            "status": "诊断完成" if status == "completed" else "数据缺口中止",
            "start": record.get("replay_start"),
            "end": record.get("replay_end"),
            "sessions": result.get("completed_sessions", record.get("sessions")),
            "symbols": record.get("symbols"),
            "net_return": result.get("net_return"),
            "max_drawdown": result.get("max_drawdown"),
            "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": result.get("fees"),
            "orders": result.get("orders"),
            "fills": result.get("fills"),
            "data_quality_gate": "StockDB滚动规则 + 统一Engine/Ledger；研究-only",
            "blockers": blockers,
            "source": "local StockDB snapshot + shared Engine/Ledger",
            "report_path": str(report_path) if report_path.is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": limitations,
        })
    return rows


def _stockdb_segment_compare_rows(path: Path, record: dict) -> list[dict]:
    """Index saved full-vs-segment comparisons without making them selectable."""
    if record.get("schema") != "stockdb_rolling_segment_compare_v1":
        return []
    report_path = settings.ROOT / "outputs" / "stockdb_segment_compare" / path.name / "REPORT.md"
    limitations = list(record.get("limitations") or [])
    rows = []
    for result in record.get("rows") or []:
        signal = result.get("signal")
        if not signal:
            continue
        rows.append({
            "id": f"{path.name}:{signal}",
            "family": "stockdb_segment_window_comparison",
            "label": f"{signal} full-vs-segment",
            "status": "区间对比完成",
            "start": result.get("segment_start"),
            "end": result.get("segment_end"),
            "sessions": result.get("segment_sessions"),
            "symbols": None,
            "net_return": result.get("segment_net_return"),
            "max_drawdown": result.get("segment_max_drawdown"),
            "sharpe_zero_rf_252": result.get("segment_sharpe_zero_rf_252"),
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": result.get("segment_fees"),
            "orders": result.get("segment_orders"),
            "fills": result.get("segment_fills"),
            "stale_valuation_days": result.get("segment_stale_valuation_days"),
            "data_quality_gate": "StockDB统一Engine/Ledger区间对比；研究-only",
            "blockers": ["非独立留出", "未获交易准入", "仅区间诊断，不纳入候选选择"],
            "source": "saved StockDB full-vs-segment comparison",
            "report_path": str(report_path) if report_path.is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": limitations,
            "full_window": {
                "start": result.get("full_start"),
                "end": result.get("full_end"),
                "sessions": result.get("full_sessions"),
                "net_return": result.get("full_net_return"),
                "max_drawdown": result.get("full_max_drawdown"),
            },
            "segment_minus_full_net_return": result.get("segment_minus_full_net_return"),
        })
    return rows


def _stockdb_coverage_rows(path: Path, record: dict) -> list[dict]:
    """Expose source coverage gaps without turning them into strategy rows."""
    if record.get("schema") != "stockdb_source_coverage_diagnostic_v1":
        return []
    report_path = settings.ROOT / "outputs" / "stockdb_source_coverage" / path.name / "REPORT.md"
    rows = []
    for item in record.get("observations") or []:
        if item.get("observed_by_any_source"):
            continue
        symbol = item.get("symbol") or item.get("code") or "unknown"
        day = item.get("date") or "unknown"
        rows.append({
            "id": f"{path.name}:{symbol}:{day}",
            "family": "stockdb_source_coverage_diagnostic",
            "label": f"{symbol} {day} source coverage gap",
            "status": "数据覆盖缺口",
            "start": day,
            "end": day,
            "sessions": 0,
            "symbols": 1,
            "net_return": None,
            "max_drawdown": None,
            "sharpe_zero_rf_252": None,
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": None,
            "data_quality_gate": "StockDB source coverage diagnostic; no imputation",
            "blockers": ["checked sources contain no explicit row", "affected replay remains blocked"],
            "source": "local StockDB and alternate-source coverage evidence",
            "report_path": str(report_path) if report_path.is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "comparison_eligible": False,
            "observed_by_any_source": False,
            "missing_observations": record.get("missing_observations"),
            "limitations": [
                "No last-price carry-forward, zero-return or synthetic bar was used.",
                "A source gap does not identify suspension, delisting or tradability.",
            ],
        })
    return rows


def _stockdb_independent_source_rows(path: Path, record: dict) -> list[dict]:
    """Index fixed-target checks against a separate raw provider."""
    if record.get("schema") != "stockdb_independent_source_compare_v1":
        return []
    report_path = settings.ROOT / "outputs" / "stockdb_independent_source_compare" / path.name / "REPORT.md"
    rows = []
    limitations = list(record.get("limitations") or [])
    strict_holdout = record.get("strict_independent_holdout") is True
    prior_overlap = record.get("prior_forward_overlap") is True
    independent_sample = record.get("independent_sample") is True
    blockers = []
    if prior_overlap:
        blockers.append("此前前向窗口重叠")
    if not strict_holdout:
        blockers.append("严格独立留出仍未完成")
    else:
        blockers.append("仅相对本次目标截点的严格留出")
    blockers.extend(["未获交易准入", "仅数据一致性诊断"])
    for result in record.get("summaries") or []:
        strategy = result.get("strategy")
        if not strategy:
            continue
        alternate = result.get("alternate_source") or {}
        rows.append({
            "id": f"{path.name}:{strategy}",
            "family": "stockdb_independent_source_compare",
            "label": f"{strategy} independent-provider check",
            "status": "独立源对比完成",
            "start": record.get("start"),
            "end": record.get("end"),
            "sessions": None,
            "symbols": result.get("target_symbols"),
            "net_return": alternate.get("equal_weight_return"),
            "max_drawdown": None,
            "sharpe_zero_rf_252": None,
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": None,
            "orders": None,
            "fills": None,
            "data_quality_gate": "固定目标 + Tencent/StockDB 原始收盘价对比；研究-only",
            "blockers": blockers,
            "source": "local StockDB + separate Tencent raw daily candidate",
            "report_path": str(report_path) if report_path.is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": limitations,
            "alternate_provider": record.get("alternate_provider"),
            "stockdb_source": record.get("snapshot_source"),
            "paired_complete_symbols": result.get("paired_complete_symbols"),
            "complete_paired_rows": record.get("complete_paired_rows"),
            "selection_cutoff": record.get("selection_cutoff"),
            "strict_independent_holdout": strict_holdout,
            "prior_forward_overlap": prior_overlap,
            "independent_sample": independent_sample,
            "independent_sample_basis": record.get("independent_sample_basis"),
            "selection_performed": record.get("selection_performed") is True,
            "alternate_minus_stockdb_equal_return": result.get("alternate_minus_stockdb_equal_return"),
        })
    return rows


def _stockdb_independent_sample_rows(path: Path, record: dict) -> list[dict]:
    """Index non-overlapping shared-Ledger sample comparisons."""
    if record.get("schema") != "stockdb_independent_sample_compare_v1":
        return []
    report_path = settings.ROOT / "outputs" / "stockdb_independent_sample_compare" / path.name / "REPORT.md"
    limitations = list(record.get("limitations") or [])
    evaluation = record.get("evaluation_window") or {}
    sessions = evaluation.get("sessions") or []
    blockers = ["未获交易准入", "研究诊断，不纳入自动选择"]
    if record.get("prior_forward_overlap") is True:
        blockers.insert(0, "此前前向窗口重叠")
    elif record.get("independent_sample") is not True:
        blockers.insert(0, "独立样本边界未确认")
    rows = []
    for result in record.get("ledger_results") or []:
        candidate = result.get("candidate")
        if not candidate:
            continue
        rows.append({
            "id": f"{path.name}:{candidate}",
            "family": "stockdb_independent_sample_compare",
            "label": f"{candidate} independent sample",
            "status": "独立样本共享账本完成",
            "start": evaluation.get("start"),
            "end": evaluation.get("end"),
            "sessions": len(sessions),
            "symbols": record.get("target_universe_count"),
            "net_return": result.get("net_return"),
            "max_drawdown": result.get("max_drawdown"),
            "sharpe_zero_rf_252": None,
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": None,
            "orders": result.get("orders"),
            "fills": result.get("fills"),
            "data_quality_gate": "冻结 StockDB + Tencent 配对 + 统一 Engine/Ledger；研究-only",
            "blockers": blockers,
            "source": "saved non-overlapping StockDB/Tencent shared-Ledger comparison",
            "report_path": str(report_path) if report_path.is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": limitations,
            "alternate_provider": record.get("alternate_provider"),
            "target_basket_size": record.get("target_basket_size"),
            "target_universe_count": record.get("target_universe_count"),
            "provider_complete_paired_rows": record.get("provider_complete_paired_rows"),
            "selection_cutoff": record.get("selection_cutoff"),
            "independent_sample": record.get("independent_sample") is True,
            "prior_forward_overlap": record.get("prior_forward_overlap") is True,
            "independent_sample_basis": record.get("independent_sample_basis"),
        })
    return rows


def _stockdb_independent_window_stability_rows(path: Path, record: dict) -> list[dict]:
    """Index saved cross-window stability summaries without promoting them."""
    if record.get("schema") != "stockdb_independent_window_stability_v1":
        return []
    report_path = settings.ROOT / "outputs" / "stockdb_independent_window_stability" / path.name / "REPORT.md"
    window_count = record.get("window_count")
    rows = []
    for summary in record.get("strategy_summaries") or []:
        candidate = summary.get("candidate")
        if not candidate:
            continue
        positive_windows = summary.get("positive_windows")
        stability_status = (
            "all_windows_positive"
            if positive_windows == window_count
            else "mixed_window_results"
        )
        rows.append({
            "id": f"{path.name}:{candidate}",
            "family": "stockdb_independent_window_stability",
            "label": f"{candidate} independent window stability",
            "status": "独立窗口稳定性完成",
            "start": record.get("window_start"),
            "end": record.get("window_end"),
            "sessions": summary.get("total_sessions"),
            "symbols": None,
            "net_return": summary.get("mean_net_return"),
            "max_drawdown": summary.get("worst_max_drawdown"),
            "sharpe_zero_rf_252": None,
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": None,
            "orders": summary.get("total_orders"),
            "fills": summary.get("total_fills"),
            "data_quality_gate": "冻结 StockDB + Tencent 配对 + 多窗口共享账本；研究-only",
            "blockers": ["未获交易准入", "跨窗口仅研究诊断", "选择与执行仍关闭"],
            "source": "saved non-overlapping independent StockDB/Tencent shared-Ledger windows; calendar continuity not asserted",
            "report_path": str(report_path) if report_path.is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": list(record.get("limitations") or []),
            "alternate_provider": record.get("alternate_provider"),
            "window_count": window_count,
            "total_sessions": summary.get("total_sessions"),
            "non_overlapping": record.get("non_overlapping") is True,
            "calendar_continuity_verified": record.get("calendar_continuity_verified") is True,
            "positive_windows": positive_windows,
            "stability_status": stability_status,
            "mean_net_return": summary.get("mean_net_return"),
            "median_net_return": summary.get("median_net_return"),
            "worst_net_return": summary.get("worst_net_return"),
            "best_net_return": summary.get("best_net_return"),
            "worst_max_drawdown": summary.get("worst_max_drawdown"),
            "paired_rows_all_windows": summary.get("paired_rows_all_windows"),
        })
    return rows


def _factor_execution_row(path: Path, record: dict) -> dict | None:
    if record.get("research_family") != "factor_execution_projection":
        return None
    result = record.get("result") or {}
    blocker = record.get("blocker") or {}
    status = record.get("status", "unknown")
    blockers = []
    if blocker.get("symbol"):
        missing = ", ".join(blocker.get("missing_fields") or []) or "source terms"
        blockers.append(
            f"{blocker.get('symbol')} {blocker.get('decision_date', '')} missing {missing}"
        )
    elif blocker.get("message"):
        blockers.append(str(blocker["message"]))
    elif result.get("status") != "completed":
        blockers.append(str(result.get("error") or "ledger projection did not complete"))
    completed_sessions = result.get("completed_sessions") or result.get("sessions") or 0
    return {
        "id": path.name,
        "family": "factor_execution_projection",
        "label": record.get("signal") or record.get("score_label") or "factor execution projection",
        "status": "诊断完成" if status == "completed_factor_execution_projection" else "投影阻断",
        "start": record.get("start"),
        "end": record.get("end"),
        "sessions": completed_sessions,
        "symbols": None,
        "net_return": result.get("net_return"),
        "max_drawdown": result.get("max_drawdown"),
        "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
        "benchmark_return": None,
        "relative_wealth_return": None,
        "fees": result.get("fees"),
        "excluded_symbols": None,
        "data_quality_gate": "固定分数 + 主板范围 + 统一账本",
        "blockers": blockers,
        "source": "local fixed-score Engine/Ledger projection",
        "report_path": str(path / "REPORT.md") if (path / "REPORT.md").is_file() else None,
        "execution_admitted": False,
        "strategy_validated": False,
        "live_ready": False,
        "limitations": [
            "Fixed score projection is research-only and does not refit or claim a fresh holdout.",
            "Unresolved held actions stop the ledger and are never imputed to zero.",
        ],
    }


def _stockdb_forward_rows(path: Path, record: dict) -> list[dict]:
    """Index fixed StockDB forward results without treating them as live signals."""
    if record.get("schema") != "stockdb_forward_compare_v1":
        return []
    comparison = record.get("comparison") or {}
    blockers = [
        "研究-only，未获交易准入",
        "前向窗口使用 raw StockDB close，非券商可交易报价",
        "未完成新鲜留出与人工晋级审查",
    ]
    if comparison.get("coverage_status") != "frozen_forward_window_complete":
        blockers.insert(1, "前向窗口覆盖未冻结完整")
    rows = []
    for result in record.get("candidates") or []:
        candidate = result.get("candidate")
        if not candidate:
            continue
        rows.append({
            "id": f"{path.name}:{candidate}",
            "family": "stockdb_forward_diagnostic",
            "label": f"{candidate} fixed forward diagnostic",
            "status": "固定前向诊断完成" if result.get("status") == "complete" else "前向诊断不完整",
            "start": result.get("start_date") or comparison.get("start_date"),
            "end": result.get("end_date") or comparison.get("end_date"),
            "sessions": comparison.get("calendar_session_count"),
            "symbols": result.get("target_symbols"),
            "net_return": result.get("target_qty_value_weighted_forward_return"),
            "max_drawdown": None,
            "sharpe_zero_rf_252": None,
            "benchmark_return": None,
            "relative_wealth_return": None,
            "fees": None,
            "orders": None,
            "fills": None,
            "data_quality_gate": "固定 StockDB raw-close 前向窗口；研究-only",
            "blockers": blockers + (["候选目标覆盖不完整，路径指标不可用"]
                                     if result.get("status") != "complete" or result.get("coverage") != 1.0
                                     else []),
            "source": "persistent StockDB forward comparison",
            "report_path": str(path.parent / "REPORT.md") if (path.parent / "REPORT.md").is_file() else None,
            "execution_admitted": False,
            "strategy_validated": False,
            "live_ready": False,
            "limitations": [
                "Forward diagnostic does not establish broker quotes, fills, fees, or account eligibility.",
                "A candidate result cannot promote a different selected strategy.",
            ],
            "independent_candidate": result.get("independent_candidate") is True,
            "selection_cutoff": comparison.get("selection_cutoff"),
            "source_cutoff": record.get("source_cutoff"),
            "coverage": result.get("coverage"),
            "forward_window_complete": comparison.get("coverage_status") == "frozen_forward_window_complete",
        })
    return rows


def _native_lowvol_summary_rows(directory: Path) -> list[dict]:
    from .research.native_lowvol_replay_summary import FAMILY, load_summary
    rows = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        saved = load_summary(path)
        ready = saved["status"] == "saved_native_shared_ledger_summary"
        values = saved.get("metrics") or {}
        blockers = ["PIT及完整总回报未验证", "已观察历史窗口，非独立留出", "当前信号及交易权限关闭"]
        if not ready:
            blockers.insert(0, "保存来源已变化，原收益不可用：" + saved.get("error", "unknown"))
        rows.append({"id": path.stem, "family": FAMILY,
            "label": "原生低波共享账本（独立原价研究）",
            "status": "研究完成" if ready else "来源已变化，不可用",
            "start": saved["start"], "end": saved["end"], "sessions": saved["sessions"],
            "symbols": saved["symbols"],
            **{key: values.get(key) for key in ("net_return", "max_drawdown", "sharpe_zero_rf_252", "fees", "stale_valuation_days")},
            "benchmark_return": None, "relative_wealth_return": None,
            "orders": saved["orders"] if ready else None, "fills": saved["fills"] if ready else None,
            "order_count": saved["orders"] if ready else None, "fill_count": saved["fills"] if ready else None,
            "order_count": saved["orders"] if ready else None, "fill_count": saved["fills"] if ready else None,
            "reused_sessions": saved["reused_sessions"], "newly_completed_sessions": saved["newly_completed_sessions"],
            "data_quality_gate": "独立原生原价 + 公司行动组成项 + 共用Engine/Ledger；完整回报未验证",
            "source": "independent native raw quotes + shared Engine/Ledger",
            "report_path": str(path), "source_summary": str(path), "source_refs": saved["source_refs"],
            "blockers": blockers, "limitations": saved["limitations"],
            "returns_reportable": False, "execution_admitted": False,
            "strategy_validated": False, "live_ready": False})
    return rows


def catalog(*, include_shared_native: bool = False) -> list[dict]:
    root = settings.ROOT / "outputs"
    rows: list[dict] = []
    # Persistent StockDB factor replays live outside the repository. Index
    # them before the legacy output tree so the newest same-window receipt is
    # retained by the de-duplication below without changing any execution path.
    persistent_factor_root = settings.DATA_ROOT / "research" / "stockdb_factor_replay"
    for path in sorted(persistent_factor_root.glob("*/result.json"), reverse=True):
        rows.extend(_stockdb_rows(path.parent, _load(path)))
    shared_ledger_root = settings.DATA_ROOT / "research" / "stockdb_ledger_replays"
    for path in sorted(shared_ledger_root.glob("*/summary.json"), reverse=True):
        rows.extend(_stockdb_ledger_replay_rows(path.parent, _load(path)))
    forward_root = settings.DATA_ROOT / "research" / "stockdb_forward_compare"
    for path in sorted(forward_root.glob("*/result.json"), reverse=True):
        rows.extend(_stockdb_forward_rows(path.parent, _load(path)))
    for path in sorted((root / "quant_research").glob("*/run.json"), reverse=True):
        row = _qlib_row(path.parent, _load(path))
        if row is not None:
            rows.append(row)
    for path in sorted((root / "stockdb_factor_replay").glob("*/run.json"), reverse=True):
        rows.extend(_stockdb_rows(path.parent, _load(path)))
    for path in sorted((root / "seat_trend").glob("*/run.json"), reverse=True):
        rows.extend(_public_seat_rows(path.parent, _load(path)))
    for path in sorted((root / "seat_bulk_replay").glob("*/run.json"), reverse=True):
        rows.extend(_public_seat_bulk_rows(path.parent, _load(path)))
    for path in sorted((root / "seat_public_probe").glob("*/candidate_panel_replay_lag*/result.json"), reverse=True):
        rows.extend(_seat_candidate_panel_replay_rows(path.parent, _load(path)))
    for path in sorted((root / "shortline_research").glob("*/candidate_panel_replay*/result.json"), reverse=True):
        rows.extend(_shortline_candidate_panel_replay_rows(path.parent, _load(path)))
    # Fixed, non-model shortline rules use a separate durable output prefix so
    # their negative/diagnostic replays remain visible on the same review
    # board.  Keep the same schema gate and promotion blockers as model
    # candidate panels.
    for path in sorted((root / "shortline_research").glob("*/fixed_emotion_rule_replay*/result.json"), reverse=True):
        rows.extend(_shortline_candidate_panel_replay_rows(path.parent, _load(path)))
    for path in sorted((root / "shortline_research").glob("*/stage_selection_diagnostic*/result.json"), reverse=True):
        rows.extend(_shortline_stage_selection_rows(path.parent, _load(path)))
    for path in sorted((root / "stockdb_alpha158_lgbm_panel_replays").glob("*/result.json"), reverse=True):
        rows.extend(_stockdb_alpha158_lgbm_candidate_panel_rows(path.parent, _load(path)))
    alpha158_root = settings.DATA_ROOT / "research" / "full_market_alpha158"
    for path in sorted(alpha158_root.glob("*/protocol.json"), reverse=True):
        rows.extend(_full_market_alpha158_rows(path.parent, _load(path)))
    rolling_root = settings.DATA_ROOT / "research" / "stockdb_rolling_low_volatility"
    for path in sorted(rolling_root.glob("*/run.json"), reverse=True):
        rows.extend(_stockdb_rolling_rows(path.parent, _load(path)))
    segment_compare_root = settings.DATA_ROOT / "research" / "stockdb_segment_compare"
    for path in sorted(segment_compare_root.glob("*/comparison.json"), reverse=True):
        rows.extend(_stockdb_segment_compare_rows(path.parent, _load(path)))
    coverage_root = settings.DATA_ROOT / "research" / "stockdb_source_coverage"
    for path in sorted(coverage_root.glob("*/run.json"), reverse=True):
        rows.extend(_stockdb_coverage_rows(path.parent, _load(path)))
    independent_source_root = settings.DATA_ROOT / "research" / "stockdb_independent_source_compare"
    for path in sorted(independent_source_root.glob("*/comparison.json"), reverse=True):
        rows.extend(_stockdb_independent_source_rows(path.parent, _load(path)))
    independent_sample_root = root / "stockdb_independent_sample_compare"
    for path in sorted(independent_sample_root.glob("*/run.json"), reverse=True):
        rows.extend(_stockdb_independent_sample_rows(path.parent, _load(path)))
    stability_root = root / "stockdb_independent_window_stability"
    for path in sorted(stability_root.glob("*/run.json"), reverse=True):
        rows.extend(_stockdb_independent_window_stability_rows(path.parent, _load(path)))
    alpha158_linear_root = root / "market_replay"
    for path in sorted(alpha158_linear_root.glob("*/run.json"), reverse=True):
        rows.extend(_alpha158_linear_rows(path.parent, _load(path)))
    for path in sorted((root / "factor_execution_projections").glob("*/run.json"), reverse=True):
        row = _factor_execution_row(path.parent, _load(path))
        if row is not None:
            rows.append(row)
    # The legacy diagnostics UI calls this without the opt-in and labels its
    # section non-shared-ledger. Native shared accounts belong only on the
    # candidate review board, never under that incompatible heading.
    if include_shared_native:
        rows.extend(_native_lowvol_summary_rows(settings.DATA_ROOT / "research" / "native_lowvol_replay_summaries"))
    # Keep the newest completed artifact for a repeated diagnostic case; failed
    # attempts remain in their original output folders and are not hidden from
    # the research record, but do not crowd the operator comparison.
    unique: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        key = (row["family"], row["label"], row["start"], row["end"])
        unique.setdefault(key, row)
    return list(unique.values())


def candidate_board(*, limit: int = 60) -> dict:
    """Return a compact, read-only review view over completed replay results.

    This intentionally does not rank or promote candidates.  The comparison
    catalog contains repeated runs and several research-only families; the
    board keeps the newest artifact for each fixed family/signal/window so an
    operator can review validation boundaries without mistaking a screen for
    an executable selection.
    """
    global _CANDIDATE_BOARD_CACHE
    # The board is read-only and backed by immutable saved receipts.  Rebuilding
    # it on every UI refresh scans every historical replay and made the desk
    # appear stalled.  Keep a short-lived cache and invalidate it when the
    # output tree changes so a newly completed run becomes visible promptly.
    output_root = settings.ROOT / "outputs"
    diagnostic_roots = [
        output_root,
        settings.DATA_ROOT / "research" / "stockdb_factor_replay",
        settings.DATA_ROOT / "research" / "stockdb_ledger_replays",
        settings.DATA_ROOT / "research" / "full_market_alpha158",
        settings.DATA_ROOT / "research" / "stockdb_rolling_low_volatility",
        settings.DATA_ROOT / "research" / "stockdb_segment_compare",
        settings.DATA_ROOT / "research" / "stockdb_independent_source_compare",
        output_root / "stockdb_independent_sample_compare",
        output_root / "stockdb_independent_window_stability",
        settings.DATA_ROOT / "research" / "stockdb_forward_compare",
        output_root / "seat_trend",
        output_root / "seat_bulk_replay",
        output_root / "seat_public_probe",
    ]
    try:
        output_mtime_ns = max(root.stat().st_mtime_ns for root in diagnostic_roots if root.exists())
    except (OSError, ValueError):
        output_mtime_ns = 0
    from .research.native_lowvol_replay_summary import catalog_fingerprint
    source_revision = (output_mtime_ns, catalog_fingerprint(
        settings.DATA_ROOT / "research" / "native_lowvol_replay_summaries"))
    now = time.monotonic()
    if _CANDIDATE_BOARD_CACHE is not None:
        expires_at, cached_mtime_ns, cached = _CANDIDATE_BOARD_CACHE
        if now < expires_at and cached_mtime_ns == source_revision:
            return {**cached, "rows": cached["rows"][:limit], "truncated": len(cached["rows"]) > limit}

    grouped: dict[tuple[str, str, str, str], dict] = {}
    for run in replay_reports.catalog():
        protocol = run.get("protocol") or {}
        family = run.get("research_family", "rules")
        for result in run.get("results") or []:
            if result.get("status") != "completed":
                continue
            signal = result.get("signal")
            if not signal:
                continue
            start, end = run.get("start"), run.get("end")
            if not start or not end:
                continue
            pit = protocol.get("selection_is_point_in_time")
            fresh = protocol.get("fresh_holdout_claimed")
            execution_admitted = run.get("execution_admitted", protocol.get("execution_admitted", False))
            strategy_validated = run.get("strategy_validated", False)
            live_ready = run.get("live_ready", False)
            blockers = []
            if pit is not True:
                blockers.append("PIT未建立")
            if fresh is not True:
                blockers.append("未完成独立留出")
            if execution_admitted is not True:
                blockers.append("未获交易准入")
            if strategy_validated is not True:
                blockers.append("策略未验证")
            stale_days = result.get("stale_valuation_days")
            if stale_days:
                blockers.append(f"陈旧估值 {stale_days}日")
            model_diagnostics = run.get("model_diagnostics") or []
            seed = result.get("seed")
            model_diagnostic = next(
                (
                    item for item in model_diagnostics
                    if item.get("seed") == seed and item.get("split") == "test"
                ),
                None,
            )
            paired_delta = next(
                (item for item in (run.get("paired_deltas") or []) if item.get("seed") == seed),
                None,
            )
            key = (family, signal, start, end)
            grouped.setdefault(key, {
                "id": run["id"],
                "family": family,
                "signal": signal,
                "label": result.get("label") or signal,
                "status": "review_only",
                "start": start,
                "end": end,
                "sessions": run.get("sessions"),
                "symbols": run.get("symbols"),
                "net_return": result.get("net_return"),
                "max_drawdown": result.get("max_drawdown"),
                "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
                "fees": result.get("fees"),
                "stale_valuation_days": stale_days,
                "model": run.get("model"),
                "model_seed": seed,
                "model_features": (model_diagnostic or {}).get("features"),
                "model_split": (model_diagnostic or {}).get("split"),
                "mean_rank_ic": (model_diagnostic or {}).get("mean_rank_ic"),
                "ic_std": (model_diagnostic or {}).get("ic_std"),
                "hac_t_lag5": (model_diagnostic or {}).get("hac_t_lag5"),
                "paired_delta_net_return": (paired_delta or {}).get("delta_net_return"),
                "paired_delta_max_drawdown": (paired_delta or {}).get("delta_max_drawdown"),
                "paired_delta_turnover_over_initial": (paired_delta or {}).get("delta_turnover_over_initial"),
                "model_diagnostic_available": model_diagnostic is not None,
                "paired_delta_available": paired_delta is not None,
                "selection_is_point_in_time": pit,
                "fresh_holdout_claimed": fresh,
                "strategy_validated": strategy_validated is True,
                "execution_admitted": execution_admitted is True,
                "live_ready": live_ready is True,
                "research_comparison_eligible": False,
                "blockers": blockers,
                "limitations": protocol.get("limitations") or [],
            })
    # The persistent StockDB and public-seat diagnostics are intentionally not
    # part of replay_reports.catalog(). Put them on the same read-only review
    # board, but keep their non-comparability and non-admission explicit.
    for diagnostic in catalog(include_shared_native=True):
        if diagnostic.get("family") not in {
            "stockdb_factor_diagnostic",
            "stockdb_shared_ledger_replay",
            "public_seat_disclosure_diagnostic",
            "seat_candidate_panel_replay_diagnostic",
            "shortline_candidate_panel_replay_diagnostic",
            "shortline_stage_selection_diagnostic",
            "stockdb_alpha158_lgbm_candidate_panel_replay_diagnostic",
            "full_market_alpha158_diagnostic",
            "stockdb_rolling_low_volatility_diagnostic",
            "stockdb_segment_window_comparison",
            "stockdb_independent_source_compare",
            "stockdb_independent_sample_compare",
            "stockdb_independent_window_stability",
            "stockdb_forward_diagnostic",
            "alpha158_linear_ensemble_diagnostic",
            "native_lowvol_shared_ledger_diagnostic",
        }:
            continue
        start, end = diagnostic.get("start"), diagnostic.get("end")
        signal = diagnostic.get("label")
        if not start or not end or not signal:
            continue
        family = diagnostic["family"]
        key = (family, signal, start, end)
        grouped.setdefault(key, {
            "id": diagnostic.get("id"),
            "family": family,
            "signal": signal,
            "label": signal,
            "status": "unavailable" if diagnostic.get("status") == "来源已变化，不可用" else "review_only",
            "start": start,
            "end": end,
            "sessions": diagnostic.get("sessions"),
            "symbols": diagnostic.get("symbols"),
            "net_return": diagnostic.get("net_return"),
            "max_drawdown": diagnostic.get("max_drawdown"),
            "sharpe_zero_rf_252": diagnostic.get("sharpe_zero_rf_252"),
            "fees": diagnostic.get("fees"),
            "stale_valuation_days": diagnostic.get("stale_valuation_days"),
            "selection_is_point_in_time": False,
            "fresh_holdout_claimed": False,
            "strategy_validated": False,
            "execution_admitted": False,
            "live_ready": False,
            "comparison_eligible": False,
            "research_comparison_eligible": _research_comparison_eligible(diagnostic),
            "independent_sample": diagnostic.get("independent_sample"),
            "independent_sample_basis": diagnostic.get("independent_sample_basis"),
            "prior_forward_overlap": diagnostic.get("prior_forward_overlap"),
            "alternate_provider": diagnostic.get("alternate_provider"),
            "provider_complete_paired_rows": diagnostic.get("provider_complete_paired_rows"),
            "target_basket_size": diagnostic.get("target_basket_size"),
            "target_universe_count": diagnostic.get("target_universe_count"),
            "window_count": diagnostic.get("window_count"),
            "total_sessions": diagnostic.get("total_sessions"),
            "non_overlapping": diagnostic.get("non_overlapping"),
            "calendar_continuity_verified": diagnostic.get("calendar_continuity_verified"),
            "positive_windows": diagnostic.get("positive_windows"),
            "stability_status": diagnostic.get("stability_status"),
            "mean_net_return": diagnostic.get("mean_net_return"),
            "median_net_return": diagnostic.get("median_net_return"),
            "worst_net_return": diagnostic.get("worst_net_return"),
            "best_net_return": diagnostic.get("best_net_return"),
            "worst_max_drawdown": diagnostic.get("worst_max_drawdown"),
            "paired_rows_all_windows": diagnostic.get("paired_rows_all_windows"),
             "order_count": diagnostic.get("order_count"),
             "fill_count": diagnostic.get("fill_count"),
             "symbols_with_orders": diagnostic.get("symbols_with_orders"),
             "symbols_without_orders": diagnostic.get("symbols_without_orders"),
             "no_trade_reason_counts": diagnostic.get("no_trade_reason_counts") or {},
             "snapshot": diagnostic.get("snapshot"),
             "snapshot_meta": diagnostic.get("snapshot_meta") or {},
            "blockers": list(diagnostic.get("blockers") or []) + ["仅诊断，未纳入可比候选选择"],
            "limitations": diagnostic.get("limitations") or [],
            "report_path": diagnostic.get("report_path"),
            **({key: diagnostic.get(key) for key in ("source_summary", "source_refs", "reused_sessions",
                "newly_completed_sessions", "returns_reportable")} if family == "native_lowvol_shared_ledger_diagnostic" else {}),
        })
    rows = sorted(grouped.values(), key=lambda row: (row["end"], row["start"], row["family"], row["signal"]), reverse=True)
    value = {
        "rows": rows[:limit],
        "total_groups": len(rows),
        "truncated": len(rows) > limit,
        "execution_admitted": False,
        "selection_enabled": False,
        "research_comparison_eligible_count": sum(
            row.get("research_comparison_eligible") is True for row in rows
        ),
    }
    _CANDIDATE_BOARD_CACHE = (now + 30.0, source_revision, {**value, "rows": rows})
    return value
