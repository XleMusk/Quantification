"""Read-only application view for paper-preview release readiness.

The HTTP adapter gathers the latest saved artifacts.  This module owns the
decision contract that joins those artifacts without creating tasks, mutating
the ledger, or authorizing a broker/THS action.
"""
from __future__ import annotations

from typing import Any, Mapping


SCHEMA = "paper_preview_release_readiness_v1"


def build_view(
    *,
    preview: Mapping[str, Any],
    observation: Mapping[str, Any],
    structured: Mapping[str, Any],
    structured_package: Mapping[str, Any],
    structured_reconciliation: Mapping[str, Any],
    journal_reconciliation: Mapping[str, Any],
    same_ledger_bundle_reconciliation: Mapping[str, Any],
    quotes: Mapping[str, Any],
    quote_input: Mapping[str, Any],
    compatibility_fields: Mapping[str, Any],
) -> dict[str, Any]:
    """Join saved paper and read-only observations into one decision view."""

    blockers = list(preview.get("release_blockers") or [])
    blockers.extend(observation.get("missing_evidence") or [])

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
    if (
        quotes.get("status") == "complete"
        and quotes.get("coverage_status") == "complete"
        and quotes.get("trading_date") == execution_day
    ):
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

    same_ledger_status = same_ledger_bundle_reconciliation.get("status")
    if same_ledger_status == "reconciliation_required":
        blockers.append("same_ledger_bundle_reconciliation_required")
    elif same_ledger_status == "isolated_rehearsal_only":
        blockers.append("persistent_ths_export_required_for_same_ledger_reconciliation")
    elif same_ledger_status == "structured_package_not_ready":
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
        "schema": SCHEMA,
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
        "structured_account_summary_observed_at": (
            structured_package.get("account_summary", {}).get("observed_at")
            if isinstance(structured_package.get("account_summary"), dict)
            else None
        ),
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
        **compatibility_fields,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "order_count": structured_reconciliation.get("order_count"),
        "trade_count": structured_reconciliation.get("trade_count"),
        "cashflow_count": structured_reconciliation.get("cashflow_count"),
        "quote_input": quote_input,
        "quote_observation": quotes,
        "paper_preview": preview,
        "ths_observation": observation,
        "next_action": "先补齐账户页、结构化持仓、现金冻结、委托和成交回读，再由人工复核；当前仅允许纸面预览。",
    }


__all__ = ["SCHEMA", "build_view"]
