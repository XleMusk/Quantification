from trading.research.ledger_recovery_scenarios import (
    build_receipt, classify_external_result, run_late_fill_cancel_scenario,
)


def test_late_fill_is_kept_after_cancel_request_and_idempotent_replay():
    result = run_late_fill_cancel_scenario()
    assert result["pending_status_after_late_fill"] == "cancel_pending"
    assert result["final_order_status"] == "cancelled"
    assert result["filled_quantity"] == 100
    assert result["remaining_quantity"] == 100
    assert result["order_count"] == 1 and result["fill_count"] == 1
    assert result["persistent_account_mutated"] is False


def test_unknown_result_requires_reconciliation_and_never_retry():
    result = classify_external_result("submission_unknown")
    assert result["state"] == "UNKNOWN_RECONCILIATION_REQUIRED"
    assert result["reconciliation_required"] is True
    assert result["retry_allowed"] is False
    assert result["duplicate_action_allowed"] is False


def test_receipt_acceptance_is_complete():
    receipt = build_receipt()
    assert receipt["status"] == "VERIFIED_LOCAL_LEDGER_RECOVERY"
    assert all(receipt["acceptance"].values())
    assert receipt["execution_admitted"] is False
