from __future__ import annotations

from trading.research.workbench_recovery_evidence import build_recovery_evidence


def test_interrupted_job_requires_manual_resubmit_and_stays_paper_only():
    result = build_recovery_evidence({
        "id": "job-1",
        "status": "interrupted",
        "error": "计算进程中断；未标记完成，可重新提交",
    })

    assert result["decision"] == "MANUAL_RESUBMIT_REQUIRED"
    assert result["recovery_status"] == "ready_for_resubmit"
    assert result["automatic_retry"] is False
    assert result["execution_admitted"] is False
    assert result["broker_or_ths_interacted"] is False


def test_active_and_completed_jobs_are_not_duplicated():
    for status in ("queued", "running", "completed"):
        result = build_recovery_evidence({"id": status, "status": status})
        assert result["decision"] == "NO_RECOVERY_ACTION"
        assert result["automatic_retry"] is False
