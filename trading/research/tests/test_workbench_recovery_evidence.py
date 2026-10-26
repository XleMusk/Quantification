from __future__ import annotations

import hashlib

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


def test_recovery_receipt_can_bind_read_only_release_and_source_identity():
    observation = b'{"id":"job-2","status":"failed"}'
    release = {
        "schema": "quantification_workbench_release_status_v1",
        "status": "AVAILABLE",
        "version": "20261027_v49",
        "git_commit": "abc123",
        "execution_admitted": False,
        "live_execution_ready": False,
        "read_only": True,
    }
    result = build_recovery_evidence(
        {"id": "job-2", "status": "failed"},
        release=release,
        source_path="D:/tmp/job.json",
        source_sha256=hashlib.sha256(observation).hexdigest(),
    )
    assert result["release"]["version"] == "20261027_v49"
    assert result["release"]["execution_admitted"] is False
    assert result["source"]["job_observation_sha256"] == hashlib.sha256(observation).hexdigest()
