"""Read-only application view for saved paper execution evidence.

This view projects frozen TCA, risk, child-order and factor diagnostics for
the workbench. It never contacts a broker/THS client or mutates a ledger.
"""

from __future__ import annotations

from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project saved paper execution evidence for the desk consumer."""

    HTTPException = namespace["HTTPException"]
    Counter = namespace["Counter"]
    csv = namespace["csv"]
    json = namespace["json"]
    operator_package = namespace["operator_package"]
    _package_artifact = namespace["_package_artifact"]
    _ths_journal_reconciliation_view = namespace["_ths_journal_reconciliation_view"]
    _same_ledger_bundle_reconciliation_view = namespace["_same_ledger_bundle_reconciliation_view"]
    _same_ledger_compat_fields = namespace["_same_ledger_compat_fields"]
    _broker_observation_alerts = namespace["_broker_observation_alerts"]
    _observed_fill_tca_summary = namespace["_observed_fill_tca_summary"]

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


