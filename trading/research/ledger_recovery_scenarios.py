"""Focused paper-only recovery scenarios for the shared order ledger.

This is an executable contract for the two failure modes that must stay on the
main Data -> Signal -> Target Portfolio -> Orders -> Ledger path:

* a cancel request races with a late partial fill; and
* an external submission result is unknown and therefore cannot be retried
  automatically or turned into a second action.

The scenarios use a fresh in-memory paper ledger.  They never read a broker,
Tonghuashun, or a persistent account and never create a live order.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..ledger import Ledger, new_state


UNKNOWN_EXTERNAL_STATUSES = frozenset({
    "unknown", "submission_unknown", "timeout", "connection_lost", "transport_unknown",
})
KNOWN_EXTERNAL_STATUSES = frozenset({"accepted", "filled", "cancelled", "rejected"})


def classify_external_result(status: str) -> dict[str, Any]:
    """Return the only safe action class for a broker result observation.

    An unknown response is a reconciliation state, not a retryable failure.
    Even known statuses do not authorize a second submission: the existing
    idempotency key and account observation must be reconciled first.
    """

    if not isinstance(status, str) or not status:
        raise ValueError("external result status is required")
    normalized = status.strip().lower()
    if normalized in UNKNOWN_EXTERNAL_STATUSES:
        return {
            "status": normalized,
            "state": "UNKNOWN_RECONCILIATION_REQUIRED",
            "reconciliation_required": True,
            "retry_allowed": False,
            "duplicate_action_allowed": False,
        }
    if normalized in KNOWN_EXTERNAL_STATUSES:
        return {
            "status": normalized,
            "state": "KNOWN_RESULT_RECONCILE_BEFORE_NEXT_ACTION",
            "reconciliation_required": True,
            "retry_allowed": False,
            "duplicate_action_allowed": False,
        }
    raise ValueError(f"unsupported external result status: {status}")


def run_late_fill_cancel_scenario() -> dict[str, Any]:
    """Exercise cancel-pending -> late partial fill -> confirmed cancel."""

    day = "2026-10-23"
    next_day = "2026-10-24"
    order_id = "recovery-scenario:late-fill"
    fill_id = order_id + ":fill-1"
    symbol = "000001.SZSE"
    state = new_state(100000)
    before = deepcopy(state)
    context = RunContext("recovery-scenarios-paper-only", TradingMode.PAPER)
    book = Ledger(state, StrategyConfig(initial_cash=100000), context)
    order = book.submit(order_id, day, symbol, "buy", 200, 1000)
    if order["status"] != "submitted":
        raise AssertionError(f"synthetic order unexpectedly rejected: {order}")
    book.request_cancel(order_id, day)
    book.fill(fill_id, order_id, 100, 1000, day, next_day)
    pending_status = order["status"]
    book.confirm_cancel(order_id, day)
    # Repeating the same idempotency key returns the same terminal order and
    # cannot create a second order or reserve cash again.
    replayed = book.submit(order_id, day, symbol, "buy", 200, 1000)
    expected_fee = book.fee(100000, "buy", symbol, day)
    if pending_status != "cancel_pending":
        raise AssertionError(f"late fill lost cancel-pending state: {pending_status}")
    if order["status"] != "cancelled" or order["filled"] != 100:
        raise AssertionError(f"late fill/cancel accounting mismatch: {order}")
    if replayed is not order or len(state["orders"]) != 1 or len(state["fills"]) != 1:
        raise AssertionError("idempotent replay created a duplicate action")
    if state["cash"] != 10000000 - 100000 - expected_fee:
        raise AssertionError("late fill cash accounting mismatch")
    if book.quantity(symbol) != 100 or book.available(symbol, day) != 0:
        raise AssertionError("late fill position or T+1 availability mismatch")
    return {
        "scenario": "late_fill_after_cancel_request",
        "order_id": order_id,
        "pending_status_after_late_fill": pending_status,
        "final_order_status": order["status"],
        "requested_quantity": 200,
        "filled_quantity": order["filled"],
        "remaining_quantity": order["qty"] - order["filled"],
        "order_count": len(state["orders"]),
        "fill_count": len(state["fills"]),
        "cash_cents": state["cash"],
        "position_quantity": book.quantity(symbol),
        "sellable_quantity_on_order_day": book.available(symbol, day),
        "isolated_ledger_changed": state["orders"] != before["orders"],
        "persistent_account_mutated": False,
    }


def run_unknown_result_scenario() -> dict[str, Any]:
    """Exercise unknown external result classification without submission."""

    observations = [classify_external_result(status) for status in
                    ("submission_unknown", "timeout", "connection_lost")]
    if any(item["retry_allowed"] or item["duplicate_action_allowed"] for item in observations):
        raise AssertionError("unknown external result was incorrectly made retryable")
    return {
        "scenario": "unknown_external_result",
        "observations": observations,
        "external_submit_calls": 0,
        "reconciliation_required": True,
        "retry_allowed": False,
        "duplicate_action_allowed": False,
        "persistent_account_mutated": False,
    }


def build_receipt() -> dict[str, Any]:
    late_fill = run_late_fill_cancel_scenario()
    unknown = run_unknown_result_scenario()
    return {
        "schema": "ledger_recovery_scenarios_v1",
        "status": "VERIFIED_LOCAL_LEDGER_RECOVERY",
        "execution_admitted": False,
        "broker_or_ths_interacted": False,
        "persistent_account_mutated": False,
        "scenarios": [late_fill, unknown],
        "acceptance": {
            "late_fill_survives_cancel_request": late_fill["pending_status_after_late_fill"] == "cancel_pending",
            "confirmation_terminalizes_only_remaining_quantity": (
                late_fill["final_order_status"] == "cancelled" and late_fill["remaining_quantity"] == 100),
            "same_idempotency_key_no_duplicate": (
                late_fill["order_count"] == 1 and late_fill["fill_count"] == 1),
            "unknown_is_not_retryable": unknown["retry_allowed"] is False,
            "unknown_blocks_duplicate_action": unknown["duplicate_action_allowed"] is False,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path("outputs/pipeline_receipts/20261024_ledger_recovery_scenarios_v1.json"))
    parser.add_argument("--report", type=Path,
                        default=Path("outputs/research_reports/20261024_ledger_recovery_scenarios_v1.md"))
    args = parser.parse_args(argv)
    receipt = build_receipt()
    output = args.output.resolve()
    report = args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    acceptance = receipt["acceptance"]
    report.write_text(
        "# Ledger recovery scenarios\n\n"
        "The receipt proves late-fill/cancel ordering and unknown external-result handling "
        "on an isolated paper ledger. No broker or THS call was made.\n\n"
        f"- status: `{receipt['status']}`\n"
        f"- late fill: `{acceptance['late_fill_survives_cancel_request']}`\n"
        f"- confirmation terminalization: `{acceptance['confirmation_terminalizes_only_remaining_quantity']}`\n"
        f"- idempotency: `{acceptance['same_idempotency_key_no_duplicate']}`\n"
        f"- unknown blocks retry/duplicate: `{acceptance['unknown_is_not_retryable']}` / "
        f"`{acceptance['unknown_blocks_duplicate_action']}`\n"
        f"- execution_admitted: `{receipt['execution_admitted']}`\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": receipt["status"], "output": str(output), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["classify_external_result", "run_late_fill_cancel_scenario", "run_unknown_result_scenario",
           "build_receipt", "main"]
