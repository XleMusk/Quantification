"""Record the bounded result of a manual workbench resubmission.

The web endpoint creates the child in the local SQLite queue.  This module
turns the parent/child observations into a small receipt and report that can
be archived with a run.  It never retries a worker and never contacts a
broker or Tonghuashun account.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_workbench_manual_resubmit_v1"
RECOVERABLE = frozenset({"failed", "interrupted", "cancelled"})


def _key_fingerprint(request_key: str | None) -> str | None:
    if request_key is None:
        return None
    return hashlib.sha256(request_key.encode("utf-8")).hexdigest()


def build_receipt(
    parent: Mapping[str, Any],
    child: Mapping[str, Any],
    *,
    request_key: str | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Validate and describe one explicitly requested child task."""

    parent_id = parent.get("id")
    child_id = child.get("id")
    if not parent_id or not child_id or child.get("parent_job_id") != parent_id:
        raise ValueError("child must link to the observed parent job")
    if parent.get("status") not in RECOVERABLE:
        raise ValueError("manual resubmit requires a failed, interrupted, or cancelled parent")
    if child.get("config") != parent.get("config") or child.get("dataset_id") != parent.get("dataset_id"):
        raise ValueError("child must preserve the parent dataset and configuration")
    attempt = int(child.get("recovery_attempt") or 0)
    if attempt < 1:
        raise ValueError("child recovery_attempt must be positive")
    return {
        "schema": SCHEMA,
        "status": "MANUAL_RESUBMIT_LINKED",
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "parent_job_id": parent_id,
        "parent_status": parent.get("status"),
        "child_job_id": child_id,
        "child_status": child.get("status"),
        "recovery_attempt": attempt,
        "dataset_id": parent.get("dataset_id"),
        "config_equal": child.get("config") == parent.get("config"),
        "request_key_sha256": _key_fingerprint(request_key),
        "automatic_retry": False,
        "execution_admitted": False,
        "live_trading": False,
        "broker_or_ths_interacted": False,
        "ledger_mutated": False,
        "limitations": [
            "The child is queued for a separate local research attempt; the parent remains immutable.",
            "This receipt does not prove current market, broker, account, or Tonghuashun state.",
        ],
    }


def render_report(receipt: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# Workbench manual resubmit",
            "",
            f"Parent `{receipt['parent_job_id']}` remains `{receipt['parent_status']}`.",
            f"Child `{receipt['child_job_id']}` is `{receipt['child_status']}` (attempt {receipt['recovery_attempt']}).",
            "",
            "The child preserves the parent dataset/configuration and is linked by an explicit idempotency request.",
            "Automatic retry, broker/Tonghuashun interaction, Ledger mutation and live execution remain disabled.",
            "",
        ]
    )


def write_outputs(
    parent_path: str | Path,
    child_path: str | Path,
    output_path: str | Path,
    report_path: str | Path,
    *,
    request_key: str | None = None,
    captured_at: str | None = None,
) -> tuple[Path, Path]:
    parent = json.loads(Path(parent_path).expanduser().resolve().read_text(encoding="utf-8"))
    child = json.loads(Path(child_path).expanduser().resolve().read_text(encoding="utf-8"))
    receipt = build_receipt(parent, child, request_key=request_key, captured_at=captured_at)
    output = Path(output_path).expanduser().resolve()
    report = Path(report_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(render_report(receipt), encoding="utf-8")
    return output, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--child", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--request-key")
    args = parser.parse_args(argv)
    output, report = write_outputs(
        args.parent, args.child, args.output, args.report,
        request_key=args.request_key,
    )
    print(json.dumps({"output": str(output), "report": str(report), "status": "MANUAL_RESUBMIT_LINKED"}, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "build_receipt", "main", "render_report", "write_outputs"]


if __name__ == "__main__":
    raise SystemExit(main())
