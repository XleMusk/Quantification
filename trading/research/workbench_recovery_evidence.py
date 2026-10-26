"""Classify paper-only workbench recovery without retrying a task.

This module consumes a saved local job/lifecycle observation.  It records when
an interrupted, failed, or cancelled research job needs a human to submit a
new job, while keeping automatic retry and broker/THS interaction disabled.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_workbench_recovery_evidence_v1"
RECOVERABLE = frozenset({"failed", "interrupted", "cancelled"})
ACTIVE = frozenset({"queued", "running"})
TERMINAL_OK = "completed"


def build_recovery_evidence(job: Mapping[str, Any]) -> dict[str, Any]:
    """Return an explicit manual-recovery decision for one local job."""

    if not isinstance(job, Mapping):
        raise TypeError("job observation must be a mapping")
    status = str(job.get("status") or "unknown")
    if status in RECOVERABLE:
        decision = "MANUAL_RESUBMIT_REQUIRED"
        next_action = "确认失败原因后重新提交一个新任务"
        recovery_status = "ready_for_resubmit"
    elif status in ACTIVE:
        decision = "NO_RECOVERY_ACTION"
        next_action = "保持当前任务并继续刷新，不重复提交"
        recovery_status = "not_required"
    elif status == TERMINAL_OK:
        decision = "NO_RECOVERY_ACTION"
        next_action = "读取已保存结果，不重复提交"
        recovery_status = "not_required"
    else:
        decision = "MANUAL_REVIEW_REQUIRED"
        next_action = "先确认未知任务状态，再决定是否重新提交"
        recovery_status = "manual_review_required"
    return {
        "schema": SCHEMA,
        "created_at": datetime.now().astimezone().isoformat(),
        "job_id": job.get("id") or job.get("job_id"),
        "parent_job_id": job.get("parent_job_id"),
        "recovery_attempt": int(job.get("recovery_attempt") or 0),
        "observed_status": status,
        "observed_error": job.get("error"),
        "decision": decision,
        "recovery_status": recovery_status,
        "automatic_retry": False,
        "next_action": next_action,
        "execution_admitted": False,
        "live_trading": False,
        "broker_or_ths_interacted": False,
        "external_order_state": "not_applicable_paper_job",
        "limitations": [
            "This receipt describes a local research job only; it does not resubmit or mutate a job.",
            "A failed paper job has no broker order state to reconcile.",
        ],
    }


def write_receipt(job_path: str | Path, output: str | Path) -> dict[str, Any]:
    source = Path(job_path).expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    receipt = build_recovery_evidence(payload)
    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def render_report(receipt: Mapping[str, Any]) -> str:
    return "\n".join([
        "# Workbench manual recovery evidence",
        "",
        f"Job `{receipt.get('job_id')}` observed as `{receipt.get('observed_status')}`.",
        f"Decision: `{receipt.get('decision')}`; automatic retry: `false`.",
        f"Next action: {receipt.get('next_action')}",
        "",
        "This is a local paper/research boundary. No broker, Tonghuashun, order, cancel, transfer, or account action occurred.",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    receipt = write_receipt(args.job, args.output)
    if args.report:
        report = args.report.expanduser().resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_report(receipt), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "decision": receipt["decision"]}, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "build_recovery_evidence", "main", "render_report", "write_receipt"]


if __name__ == "__main__":
    raise SystemExit(main())
