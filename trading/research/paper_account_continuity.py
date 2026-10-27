"""Read-only projection of the latest saved paper-account continuity receipt.

The application route owns HTTP concerns. This module owns the paper-account
view contract and receives its root explicitly, so it never reads process
environment or the clock while calculating the projection.
"""
from __future__ import annotations

import json
from pathlib import Path


SCHEMA = "paper_account_continuity_view_v1"


def latest_receipt_path(root: str | Path) -> Path | None:
    """Return the newest saved continuity receipt below an explicit root."""

    receipt_root = Path(root) / "outputs" / "paper_plans"
    candidates = [
        path for path in receipt_root.glob("**/*continuity_receipt.json")
        if path.is_file()
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def build_view(root: str | Path) -> dict:
    """Read one saved receipt without advancing the paper account or ledger."""

    path = latest_receipt_path(root)
    base = {
        "schema": SCHEMA,
        "execution_admitted": False,
        "broker_connected": False,
        "client_interacted": False,
        "ledger_mutated": False,
        "account_observed": False,
    }
    if path is None:
        return {
            **base,
            "status": "not_observed",
            "next_action": "先生成纸面账户连续性回执。",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {
            **base,
            "status": "invalid_receipt",
            "source_file": str(path),
            "error": str(error),
        }
    return {
        **base,
        "status": payload.get("status", "paper_continuity_saved"),
        "source_file": str(path),
        "run_dir": payload.get("run_dir"),
        "last_completed_day": payload.get("last_completed_day"),
        "generation": payload.get("generation"),
        "daily_rows": payload.get("daily_rows", 0),
        "replay_completed_sessions": payload.get("replay_completed_sessions", 0),
        "order_count": payload.get("order_count", 0),
        "fill_count": payload.get("fill_count", 0),
        "pending_targets": bool(payload.get("pending_targets", False)),
        "cash_cny": payload.get("cash_cny"),
        "position_count": payload.get("position_count", 0),
        "refresh_gate": payload.get("refresh_gate"),
        "limitations": payload.get("limitations") or [],
        "next_action": "新交易日收市后沿同一纸面账户推进；当前不生成重复日切，不预填、不提交。",
    }
