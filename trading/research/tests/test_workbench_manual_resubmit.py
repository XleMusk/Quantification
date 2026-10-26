from trading.research.workbench_manual_resubmit import build_receipt


def _jobs():
    parent = {
        "id": "p1",
        "dataset_id": "d1",
        "status": "interrupted",
        "config": {"start": "2025-01-01", "end": "2025-03-31", "min_pairs": 5},
    }
    child = {
        "id": "c1",
        "parent_job_id": "p1",
        "dataset_id": "d1",
        "status": "queued",
        "recovery_attempt": 1,
        "config": parent["config"].copy(),
    }
    return parent, child


def test_manual_resubmit_receipt_binds_parent_and_keeps_execution_closed():
    parent, child = _jobs()
    receipt = build_receipt(parent, child, request_key="recovery-key", captured_at="2026-10-26T00:00:00+00:00")
    assert receipt["status"] == "MANUAL_RESUBMIT_LINKED"
    assert receipt["parent_job_id"] == "p1"
    assert receipt["child_job_id"] == "c1"
    assert receipt["config_equal"] is True
    assert receipt["automatic_retry"] is False
    assert receipt["execution_admitted"] is False
    assert receipt["broker_or_ths_interacted"] is False


def test_manual_resubmit_receipt_rejects_unlinked_or_completed_parent():
    parent, child = _jobs()
    parent["status"] = "completed"
    try:
        build_receipt(parent, child)
    except ValueError as error:
        assert "requires" in str(error)
    else:
        raise AssertionError("completed jobs must not produce a resubmit receipt")
