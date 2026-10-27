"""Read-only application view for the paper session handoff desk."""

from __future__ import annotations

import builtins
import csv as csv_module
import json as json_module
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from fastapi import HTTPException


def _read_csv(path: Path, message: str, *, csv: Any, http_exception: Any) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as error:
        raise http_exception(409, f"{message}尚不可读取：{error}") from error


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project persisted recovery and next-session artifacts without side effects."""

    operator_package = namespace["operator_package"]
    package = operator_package()
    research_evidence_summary = namespace["_research_evidence_consumer_summary"](
        package.get("strategy_id")
    )
    structured_package = namespace["_latest_structured_observation_package"]()
    ledger_import_preview = package.get("ths_ledger_import_preview_rehearsal") or {}
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    journal_reconciliation = namespace["_ths_journal_reconciliation_view"]()
    same_ledger_bundle_reconciliation = namespace["_same_ledger_bundle_reconciliation_view"]()
    release_readiness = namespace["paper_preview_readiness"]()
    package_artifact = namespace["_package_artifact"]
    recovery_path = package_artifact(package, "recovery_plan")
    recovery_handoff_index_path = package_artifact(package, "paper_recovery_handoff_index")
    carry_path = package_artifact(package, "paper_next_session_carry")
    reconciliation_path = package_artifact(package, "paper_reconciliation")
    target_path = package_artifact(package, "target_order_reconciliation")
    if not all((recovery_path, carry_path, reconciliation_path, target_path)):
        raise HTTPException(404, "当前离线包缺少会话交接或对账产物")

    csv = namespace.get("csv", csv_module)
    json = namespace.get("json", json_module)
    path_type = namespace.get("Path", Path)
    http_exception = namespace.get("HTTPException", HTTPException)
    try:
        carry = json.loads(carry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise http_exception(409, f"纸面下一会话结转尚不可读取：{error}") from error
    recovery_rows = _read_csv(recovery_path, "纸面恢复计划", csv=csv, http_exception=http_exception)
    reconciliation_rows = _read_csv(reconciliation_path, "纸面对账", csv=csv, http_exception=http_exception)
    target_rows = _read_csv(target_path, "目标订单对账", csv=csv, http_exception=http_exception)
    recovery_reviews = namespace["_recovery_review_receipts"](package)
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
                handoff_path = path_type(index_row.get("handoff_json", ""))
                if not handoff_path.is_file():
                    continue
                handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
                template = handoff.get("reconciliation_template") or {}
                checks = template.get("checks") or []
                pending_evidence = sorted({
                    evidence for check in checks for evidence in (check.get("pending_evidence") or [])
                })
                recovery_reconciliation["rows"].append({
                    "strategy_id": handoff.get("strategy_id"),
                    "scenario_count": handoff.get("scenario_count", 0),
                    "unresolved_scenario_count": handoff.get("unresolved_scenario_count", 0),
                    "status": template.get("status", "unobserved_paper_reconciliation_template"),
                    "pending_check_count": sum(
                        check.get("status") == "pending_observation" for check in checks
                    ),
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
        **namespace["_same_ledger_compat_fields"](
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "release_readiness": release_readiness,
        "release_readiness_status": release_readiness.get("status"),
        "release_ready": bool(release_readiness.get("release_ready")),
        "research_evidence_summary": research_evidence_summary,
        "basis": "paper recovery and reconciliation artifacts only; observed account, broker orders, fills and next-session release remain absent",
        "source_files": {
            "recovery": str(recovery_path),
            "next_session": str(carry_path),
            "paper_reconciliation": str(reconciliation_path),
            "target_reconciliation": str(target_path),
            "paper_recovery_handoff_index": str(recovery_handoff_index_path)
            if recovery_handoff_index_path else None,
        },
    }


__all__ = ["build_view"]
