"""Read-only application view for the offline paper lifecycle desk.

The API route supplies its helper namespace so the lifecycle projection can be
tested independently.  This view only reads saved paper/research artifacts;
it never accesses a broker, submits orders, or mutates a ledger.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Build the normalized paper lifecycle, carry and recovery projection."""

    Counter = namespace["Counter"]
    Path = namespace["Path"]
    _latest_complete_structured_observation_package = namespace[
        "_latest_complete_structured_observation_package"
    ]
    _latest_structured_observation_package = namespace["_latest_structured_observation_package"]
    _observed_fill_tca_summary = namespace["_observed_fill_tca_summary"]
    _package_artifact = namespace["_package_artifact"]
    _research_evidence_consumer_summary = namespace["_research_evidence_consumer_summary"]
    _same_ledger_bundle_reconciliation_view = namespace["_same_ledger_bundle_reconciliation_view"]
    _same_ledger_compat_fields = namespace["_same_ledger_compat_fields"]
    _ths_journal_reconciliation_view = namespace["_ths_journal_reconciliation_view"]
    json_module = namespace["json"]
    operator_package = namespace["operator_package"]
    paper_preview_readiness = namespace["paper_preview_readiness"]

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

    def read_csv(path: Path | None) -> list[dict[str, str]]:
        if not path:
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    lifecycle_rows = read_csv(lifecycle_path)
    release_rows = [row for row in read_csv(release_path) if row.get("row_type", "order") == "order"]
    reconciliation_rows = read_csv(reconciliation_path)
    recovery_rows = read_csv(recovery_path)
    target_reconciliation_rows = [
        row for row in read_csv(target_reconciliation_path) if row.get("row_type", "order") == "order"
    ]
    settlement_projection_rows = [
        row for row in read_csv(settlement_projection_path) if row.get("row_type", "order") == "order"
    ]
    carry = json_module.loads(carry_path.read_text(encoding="utf-8")) if carry_path else {}
    ledger = json_module.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path else {}
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
        count = (
            release_decision_counts if source == "paper_release_decision" else reconciliation_status_counts
        ).get(value, 0)
        if count:
            exception_queue.append(
                {
                    "priority": priority,
                    "exception": value,
                    "count": count,
                    "action": action,
                    "source": source,
                    "execution_admitted": False,
                }
            )
    return {
        "schema": "offline_paper_lifecycle_view_v1",
        "status": "paper_lifecycle_view",
        "execution_day": package.get("execution_day"),
        "strategy_id": package.get("strategy_id"),
        "lifecycle": {
            "order_count": len({row.get("sequence") for row in lifecycle_rows if row.get("sequence")}),
            "event_count": len(lifecycle_rows),
            "state_counts": dict(Counter(row.get("state", "unknown") for row in lifecycle_rows)),
            "real_broker_state_count": sum(
                row.get("real_broker_state", "false").lower() == "true" for row in lifecycle_rows
            ),
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
            "active_target_count": sum(
                row.get("active_target", "false").lower() == "true" for row in target_reconciliation_rows
            ),
            "account_holdings_observed_count": sum(
                row.get("account_holdings_observed", "false").lower() == "true"
                for row in target_reconciliation_rows
            ),
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
        **_same_ledger_compat_fields(same_ledger_bundle_reconciliation, journal_reconciliation),
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


__all__ = ["build_view"]
