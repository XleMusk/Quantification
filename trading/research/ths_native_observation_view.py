"""Read-only application view for the saved THS native observation candidate.

The API adapter supplies its helper namespace.  This projection keeps native,
indexed and rehearsal evidence separate and never opens a client, imports an
account, or mutates a ledger.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project saved THS observation evidence without external interaction."""

    ths_native_summary = namespace["ths_native_summary"]
    ths_native_grids = namespace["ths_native_grids"]
    ths_readonly_evidence = namespace["ths_readonly_evidence"]
    _latest_structured_observation_package = namespace["_latest_structured_observation_package"]
    _ths_client_session_status = namespace["_ths_client_session_status"]

    summary = ths_native_summary()
    grids = ths_native_grids()
    readonly_evidence = ths_readonly_evidence()
    structured_package = _latest_structured_observation_package()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    holdings = (grids.get("grids") or {}).get("holdings") or {}
    candidate = holdings.get("ocr_candidate") or {}

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


__all__ = ["build_view"]
