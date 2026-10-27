"""Bind local orchestration timing to one accepted non-live paper task.

This module only joins two saved receipts. It never measures provider cost,
replays an account, or sends an external command.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


SCHEMA = "paper_task_timing_binding_v1"
ACCEPTED_STATUS = "VERIFIED_LOCAL_PAPER_TASK_TIMING"


def _read(path: str | Path) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"receipt must be a JSON object: {path}")
    return payload


def _ref(path: str | Path) -> dict[str, str]:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"receipt file is missing: {path}")
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _measurement(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("schema") != "stockdb_post_close_d0_d1_orchestration_v1":
        raise ValueError("paper task is not a post-close orchestration receipt")
    if task.get("status") != "COMPLETED_RESEARCH_ONLY_POST_CLOSE_D0_D1":
        raise ValueError("paper task is not a completed research-only task")
    if any(task.get(key) is not False for key in (
        "execution_admitted", "broker_interacted", "ths_interacted", "persistent_ledger_mutated"
    )):
        raise ValueError("paper task crossed a non-live boundary")
    d0_d1 = task.get("d0_d1")
    if not isinstance(d0_d1, dict):
        raise ValueError("paper task has no D0/D1 result")
    measurement = d0_d1.get("task_measurement")
    if not isinstance(measurement, dict) or measurement.get("schema") != "local_task_measurement_v1":
        raise ValueError("paper task has no local task measurement")
    elapsed = measurement.get("wall_seconds")
    if type(elapsed) not in (int, float) or elapsed < 0:
        raise ValueError("paper task wall_seconds must be a non-negative number")
    if measurement.get("execution_admitted") is not False:
        raise ValueError("paper task timing crossed the execution boundary")
    if measurement.get("external_provider_cost_cny") is not None:
        raise ValueError("provider expense requires an independently authorized receipt")
    return measurement


def bind_timing(timing_contract: str | Path, paper_task: str | Path) -> dict[str, Any]:
    """Join a timing contract and a completed local paper task receipt."""

    timing_path = Path(timing_contract).expanduser().resolve()
    paper_path = Path(paper_task).expanduser().resolve()
    timing = _read(timing_path)
    task = _read(paper_path)
    if timing.get("schema") != "stockdb_post_close_timing_contract_receipt_v1":
        raise ValueError("unsupported timing contract receipt")
    if timing.get("execution_admitted") is not False:
        raise ValueError("timing contract crossed the execution boundary")
    measurement = _measurement(task)
    d0_d1 = task["d0_d1"]
    return {
        "schema": SCHEMA,
        "status": ACCEPTED_STATUS,
        "timing_contract": _ref(timing_path),
        "paper_task": _ref(paper_path),
        "task": {
            "close_date": task.get("close_date"),
            "execution_day": task.get("execution_day"),
            "settlement_day": task.get("settlement_day"),
            "input_fingerprint": task.get("input_fingerprint"),
            "snapshot_coverage": task.get("snapshot_coverage"),
            "fixed_target_count": (d0_d1.get("fixed") or {}).get("target_count"),
            "fixed_paper_order_count": (d0_d1.get("fixed") or {}).get("paper_order_count"),
            "joint_paper_order_count": (d0_d1.get("joint_paper") or {}).get("paper_order_count"),
        },
        "timing": {
            "started_at": measurement.get("started_at"),
            "finished_at": measurement.get("finished_at"),
            "wall_seconds": measurement["wall_seconds"],
            "clock_basis": measurement.get("clock_basis"),
        },
        "expense": {
            "provider_cost_cny": None,
            "status": "UNMEASURED_EXTERNAL_PROVIDER_EXPENSE",
        },
        "scope": "accepted_non_live_paper_task",
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
        "next": "Use an independently authorized provider receipt before adding monetary expense; do not infer it from wall time.",
    }


def write_binding(output: str | Path, timing_contract: str | Path, paper_task: str | Path) -> dict[str, Any]:
    result = bind_timing(timing_contract, paper_task)
    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timing-contract", type=Path, required=True)
    parser.add_argument("--paper-task", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = write_binding(args.output, args.timing_contract, args.paper_task)
    print(json.dumps({
        "status": result["status"],
        "wall_seconds": result["timing"]["wall_seconds"],
        "provider_cost_cny": result["expense"]["provider_cost_cny"],
        "execution_admitted": False,
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
