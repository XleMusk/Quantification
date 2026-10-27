from pathlib import Path

from webapp.backend.app.job_lifecycle_view import available_result_files, build_view


def _item(status="queued"):
    return {
        "status": status,
        "progress": 50,
        "dataset_id": "dataset-1",
        "updated_at": "2026-10-27T12:00:00+08:00",
    }


def test_available_files_are_allow_listed(tmp_path: Path):
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    (tmp_path / "unexpected.bin").write_bytes(b"ignored")

    assert available_result_files(tmp_path) == ["result.json"]


def test_lifecycle_view_keeps_running_job_active_and_read_only():
    result = build_view(
        identifier="job-1",
        item=_item("running"),
        available_files=[],
        recovery_evidence={"decision": "NO_RECOVERY_REQUIRED", "automatic_retry": False},
        release={"version": "v1", "execution_admitted": False},
    )

    assert result["schema"] == "quantification_workbench_job_lifecycle_v1"
    assert result["stages"][1]["status"] == "active"
    assert result["stages"][2]["status"] == "waiting"
    assert result["recovery"]["status"] == "not_required"
    assert result["execution_admitted"] is False
    assert result["live_trading"] is False


def test_completed_lifecycle_exposes_result_and_manual_recovery_only():
    result = build_view(
        identifier="job-2",
        item=_item("completed"),
        available_files=["result.json", "REPORT.md"],
        recovery_evidence={"decision": "NO_RECOVERY_REQUIRED", "automatic_retry": False},
        release={"version": "v1", "execution_admitted": False},
    )

    assert result["stages"][1]["status"] == "completed"
    assert result["stages"][2]["status"] == "available"
    assert result["stages"][3]["detail"] == "可下载 2 个文件"
    assert result["recovery"]["automatic"] is False


def test_failed_lifecycle_requires_explicit_new_submission():
    result = build_view(
        identifier="job-3",
        item=_item("interrupted"),
        available_files=[],
        recovery_evidence={"decision": "MANUAL_RESUBMIT_REQUIRED", "automatic_retry": False},
        release={"version": "v1", "execution_admitted": False},
    )

    assert result["recovery"] == {
        "status": "ready_for_resubmit",
        "automatic": False,
        "next_action": "确认失败原因后重新提交一个新任务",
    }
