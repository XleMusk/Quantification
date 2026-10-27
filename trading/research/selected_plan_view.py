"""Read-only selected-plan application view.

The API route supplies its module namespace so this view can be tested and
changed independently without granting the view broker or account access.
"""

import csv
import json
from collections import Counter
from pathlib import Path
import builtins

from fastapi import HTTPException


def build_selected_plan_view(namespace):
    """Build the selected paper strategy desk view from API read-only helpers."""
    Counter = namespace['Counter']
    HTTPException = namespace['HTTPException']
    OSError = namespace.get('OSError', builtins.OSError)
    Path = namespace['Path']
    UnicodeError = namespace.get('UnicodeError', builtins.UnicodeError)
    _broker_capability_snapshot = namespace['_broker_capability_snapshot']
    _latest_alpha158_lightgbm_diagnostic = namespace['_latest_alpha158_lightgbm_diagnostic']
    _latest_alpha158_linear_diagnostic = namespace['_latest_alpha158_linear_diagnostic']
    _latest_complete_structured_observation_package = namespace['_latest_complete_structured_observation_package']
    _latest_fixed_idea_frontier = namespace['_latest_fixed_idea_frontier']
    _latest_fixed_profitability_diagnostic = namespace['_latest_fixed_profitability_diagnostic']
    _latest_saved_comparison_diagnostic = namespace['_latest_saved_comparison_diagnostic']
    _latest_stockdb_forward_compare = namespace['_latest_stockdb_forward_compare']
    _latest_structured_observation_package = namespace['_latest_structured_observation_package']
    _normalize_blockers = namespace['_normalize_blockers']
    _operator_account_risk_inputs = namespace['_operator_account_risk_inputs']
    _operator_package_session_root = namespace['_operator_package_session_root']
    _promotion_review_receipt_path = namespace['_promotion_review_receipt_path']
    operator_package = namespace['operator_package']
    paper_preview_readiness = namespace['paper_preview_readiness']
    ths_reference_payload = namespace['ths_reference_payload']

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
    from ..ths.account_risk import build_account_risk_view, evaluate_order_risk

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
