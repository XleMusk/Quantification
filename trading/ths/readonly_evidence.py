"""Build a read-only index over static THS evidence without importing it."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Hong_Kong")


def _load(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _receipt(path, *, expected_kind=None):
    value = _load(path)
    if expected_kind and value.get("kind") != expected_kind:
        raise ValueError(f"Receipt kind mismatch for {path}: {value.get('kind')!r}")
    return {
        "path": str(Path(path)),
        "schema": value.get("schema"),
        "status": value.get("status"),
        "kind": value.get("kind"),
        "observed_at": value.get("observed_at"),
        "structured_rows": value.get("structured_rows"),
        "read_only": value.get("read_only"),
        "client_interacted": value.get("client_interacted"),
        "submitted": value.get("submitted"),
        "execution_admitted": value.get("execution_admitted"),
        "ledger_mutated": value.get("ledger_mutated"),
        "row_count": len(value.get("rows") or []) if isinstance(value.get("rows"), list) else None,
    }


def _structured_receipt_eligible(receipt):
    """Accept only explicit read-only receipts with no execution side effects."""
    return bool(
        receipt.get("structured_rows") is True
        and receipt.get("read_only") is True
        and receipt.get("submitted") is not True
        and receipt.get("execution_admitted") is not True
        and receipt.get("ledger_mutated") is not True
    )


def _candidate(path):
    value = _load(path)
    checks = value.get("checks") or {}
    rows = value.get("rows")
    return {
        "path": str(Path(path)),
        "schema": value.get("schema"),
        "status": value.get("status"),
        "kind": value.get("kind"),
        "candidate_state": value.get("candidate_state"),
        "row_count": len(rows) if isinstance(rows, list) else None,
        "symbols": [row.get("symbol") for row in rows if isinstance(row, dict) and row.get("symbol")],
        "summary_cross_checks_pass": checks.get("summary_cross_checks_pass"),
        "structured_rows": value.get("structured_rows"),
        "semantic_verified": value.get("semantic_verified"),
        "execution_admitted": value.get("execution_admitted"),
        "ledger_mutated": value.get("ledger_mutated"),
        "submitted": value.get("submitted"),
    }


def _observation_window(*values):
    stamps = [value.get("observed_at") for value in values if value.get("observed_at")]
    parsed = [datetime.fromisoformat(stamp) for stamp in stamps]
    return {
        "observed_at": stamps,
        "start": min(stamps) if stamps else None,
        "end": max(stamps) if stamps else None,
        "same_local_date": len({value.astimezone(LOCAL_TZ).date().isoformat() for value in parsed}) <= 1,
        "atomic": False,
        "basis": "separate static receipts; not an atomic broker export",
    }


def build_readonly_evidence_index(summary_path, holdings_candidate_path,
                                  holdings_receipt_path, orders_receipt_path,
                                  trades_receipt_path, cashflows_receipt_path=None):
    summary = _load(summary_path)
    if summary.get("schema") not in {
        "ths_native_account_summary_v1", "ths_native_account_observation_v1",
    }:
        raise ValueError("Unexpected THS account receipt schema")
    summary_receipt = {
        "path": str(Path(summary_path)),
        "schema": summary.get("schema"),
        "status": summary.get("status"),
        "kind": summary.get("kind", "account"),
        "observed_at": summary.get("observed_at"),
        "structured_rows": summary.get("structured_rows"),
        "read_only": summary.get("read_only"),
        "client_interacted": summary.get("client_interacted"),
        "submitted": summary.get("submitted"),
        "execution_admitted": summary.get("execution_admitted"),
        "ledger_mutated": summary.get("ledger_mutated"),
        "row_count": len(summary.get("rows") or []) if isinstance(summary.get("rows"), list) else None,
    }
    holdings = _candidate(holdings_candidate_path)
    if holdings.get("kind") not in (None, "holdings"):
        raise ValueError("Holdings candidate has an unexpected kind")
    holdings_receipt = _receipt(holdings_receipt_path, expected_kind="holdings")
    orders_receipt = _receipt(orders_receipt_path, expected_kind="orders")
    trades_receipt = _receipt(trades_receipt_path, expected_kind="trades")
    cashflows_receipt = (
        _receipt(cashflows_receipt_path, expected_kind="cashflows")
        if cashflows_receipt_path else None
    )
    account_structured = bool(
        summary_receipt.get("kind") in (None, "account")
        and _structured_receipt_eligible(summary_receipt)
        and isinstance(summary.get("rows"), list)
        and Path(summary_path).is_file()
    )
    orders_structured = bool(
        _structured_receipt_eligible(orders_receipt)
        and Path(orders_receipt["path"]).is_file()
    )
    trades_structured = bool(
        _structured_receipt_eligible(trades_receipt)
        and Path(trades_receipt["path"]).is_file()
    )
    cashflows_structured = bool(
        cashflows_receipt
        and _structured_receipt_eligible(cashflows_receipt)
        and Path(cashflows_receipt["path"]).is_file()
    )
    structured_observed = account_structured and orders_structured and trades_structured and cashflows_structured
    return {
        "schema": "ths_readonly_evidence_index_v1",
        "status": "complete_readonly_evidence" if structured_observed else "partial_readonly_evidence",
        "source": "explicit_static_ths_receipts",
        "account_alias": summary.get("account_alias"),
        "summary": {
            "path": str(Path(summary_path)),
            "schema": summary.get("schema"),
            "status": summary.get("status"),
            "observed_at": summary.get("observed_at"),
            "metrics_raw": summary.get("metrics_raw"),
            "positions_read": summary.get("positions_read"),
            "orders_read": summary.get("orders_read"),
            "trades_read": summary.get("trades_read"),
            "full_account_observation": summary.get("full_account_observation"),
            "structured_rows": summary_receipt.get("structured_rows"),
            "read_only": summary_receipt.get("read_only"),
            "row_count": summary_receipt.get("row_count"),
        },
        "observation_window": _observation_window(
            summary, holdings_receipt, orders_receipt, trades_receipt,
            *(tuple([cashflows_receipt]) if cashflows_receipt else tuple())
        ),
        "holdings": {
            "candidate": holdings,
            "native_receipt": holdings_receipt,
        },
        "orders": {"native_receipt": orders_receipt},
        "trades": {"native_receipt": trades_receipt},
        "cashflows": {"native_receipt": cashflows_receipt} if cashflows_receipt else {},
        "account_observed": account_structured,
        "structured_account": account_structured,
        "orders_observed": orders_structured,
        "trades_observed": trades_structured,
        "cashflows_observed": cashflows_structured,
        "execution_admitted": False,
        "ledger_mutated": False,
        "submitted": False,
        "unknowns": [
            *([] if account_structured else ["native structured account rows are absent or unverifiable"]),
            *([] if orders_structured else ["native structured order rows are absent; empty grid is not proof of zero orders"]),
            *([] if trades_structured else ["native structured trade rows are absent; empty grid is not proof of zero trades"]),
            *([] if cashflows_structured else ["cashflow export is absent or not a structured receipt"]),
        ],
        "next_action": (
            "Review the complete read-only receipts, then run Ledger preview before import."
            if structured_observed else
            "Import explicit THS account/orders/trades/cashflows exports before sizing, binding or Ledger reconciliation."
        ),
    }
