"""Read-only projection of the newest saved paper preview."""

from __future__ import annotations

import json
from pathlib import Path


SCHEMA = "paper_preview_latest_view_v1"


def latest_receipt_path(root: str | Path) -> Path | None:
    directory = Path(root) / "research" / "paper_rebalance"
    candidates = [path for path in directory.glob("*/result.json") if path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def build_view(root: str | Path) -> dict:
    path = latest_receipt_path(root)
    base = {"schema": SCHEMA, "execution_admitted": False, "account_observed": False,
            "quotes_observed": False, "client_interacted": False, "submitted": False,
            "ledger_mutated": False}
    if path is None:
        return {**base, "status": "not_observed", "next_action": "先生成下一交易日纸面预览。"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {**base, "status": "invalid_receipt", "source_file": str(path), "error": str(error)}
    artifacts = payload.get("artifacts") or {}
    return {**base, "status": payload.get("status", "paper_preview_only"), "source_file": str(path),
        "artifact_root": str(path.parent), "strategy_id": payload.get("strategy_id"),
        "source_run": payload.get("source_run"), "source_cutoff": payload.get("source_cutoff"),
        "execution_day": payload.get("execution_day"), "execution_scope": payload.get("execution_scope"),
        "scope_excluded_position_symbols": payload.get("scope_excluded_position_symbols") or [],
        "execution_scope_summary": payload.get("execution_scope_summary") or {},
        "trade_status": payload.get("trade_status", "NO_TRADE"), "release_state": payload.get("release_state"),
        "release_blockers": payload.get("release_blockers") or [],
        "provisional_rank_count": payload.get("provisional_rank_count", 0),
        "provisional_target_count": payload.get("provisional_target_count", 0),
        "provisional_order_count": payload.get("provisional_order_count", 0),
        "adapter_handoff_order_count": payload.get("adapter_handoff_order_count", 0),
        "position_count": payload.get("position_count", 0), "cash_rmb": payload.get("cash_rmb"),
        "terminal_equity_rmb": payload.get("terminal_equity_rmb"),
        "missing_quote_symbols": payload.get("missing_quote_symbols") or [],
        "why_no_order": payload.get("why_no_order") or [], "source_limitations": payload.get("source_limitations") or [],
        "artifacts": artifacts,
        "next_action": "补齐当前账户、同日人民币行情、可卖数量/T+1状态后，再由人工复核纸面差额；当前不预填、不提交。"}
