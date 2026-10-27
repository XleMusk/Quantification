"""Pure read-only view for the local research job lifecycle.

The HTTP adapter supplies the persisted job, available result files, recovery
evidence and release binding.  This module owns the stage contract and never
starts workers, retries jobs, mutates accounts or infers broker state.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


SCHEMA = "quantification_workbench_job_lifecycle_v1"
ALLOWED_RESULT_FILES = (
    "summary.csv",
    "daily_ic.csv",
    "groups.csv",
    "factors.parquet",
    "REPORT.md",
    "result.json",
)


def available_result_files(result_dir: Any) -> list[str]:
    """Return the allow-listed files currently persisted for one job."""

    return [name for name in ALLOWED_RESULT_FILES if (result_dir / name).is_file()]


def build_view(
    *,
    identifier: str,
    item: Mapping[str, Any],
    available_files: Sequence[str],
    recovery_evidence: Mapping[str, Any],
    release: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one lifecycle projection from already-read persisted evidence."""

    status = str(item["status"])
    result_files = list(available_files)
    result_ready = status == "completed" and "result.json" in result_files
    if status in ("queued", "running"):
        compute_status = "active"
    elif status == "completed":
        compute_status = "completed"
    else:
        compute_status = status
    recovery_ready = status in ("failed", "interrupted", "cancelled")
    return {
        "schema": SCHEMA,
        "job_id": identifier,
        "execution_mode": "research_only",
        "execution_admitted": False,
        "live_trading": False,
        "stages": [
            {"job_id": identifier, "id": "submitted", "status": "completed", "detail": "任务已登记并绑定数据集与参数"},
            {"job_id": identifier, "id": "compute", "status": compute_status, "detail": f"{status} · {item['progress']}%"},
            {"job_id": identifier, "id": "result", "status": "available" if result_ready else "waiting", "detail": "结果 JSON 已保存" if result_ready else "任务完成前不可读取"},
            {"job_id": identifier, "id": "download", "status": "available" if result_files else "waiting", "detail": f"可下载 {len(result_files)} 个文件" if result_files else "暂无结果文件"},
        ],
        "available_files": result_files,
        "recovery": {
            "status": "ready_for_resubmit" if recovery_ready else "not_required",
            "automatic": False,
            "next_action": "确认失败原因后重新提交一个新任务" if recovery_ready else "保持当前任务状态并继续刷新",
        },
        "recovery_evidence": recovery_evidence,
        "release": release,
        "source": {"dataset_id": item["dataset_id"], "updated_at": item["updated_at"]},
    }


__all__ = ["ALLOWED_RESULT_FILES", "SCHEMA", "available_result_files", "build_view"]
