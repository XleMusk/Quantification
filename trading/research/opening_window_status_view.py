"""Read-only view of the saved opening-window coordinator receipt.

The application route supplies the data root and current trading date.  This
view never starts a coordinator, probes a client, touches the Ledger or
rewrites a saved receipt.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any


SCHEMA = "opening_window_status_view_v1"


def build_view(data_root: str | Path, *, trading_date: str | None = None) -> dict[str, Any]:
    """Project the saved opening coordinator state for the workbench."""

    day = trading_date or date.today().isoformat()
    path = (Path(data_root) / "research" / "opening_session_runs" / day
            / "opening_window_coordinator.json").resolve()
    base = {
        "schema": SCHEMA,
        "trading_date": day,
        "source_file": str(path),
        "execution_admitted": False,
        "client_interacted": False,
        "network_probe_attempted": False,
        "ledger_mutated": False,
        "submission_attempted": False,
    }
    if not path.is_file():
        return {**base, "status": "not_started", "next_action": "等待开盘协调器首次运行"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {**base, "status": "invalid_receipt", "error": str(error)}
    if payload.get("schema") != "opening_window_coordinator_v1":
        return {**base, "status": "unsupported_receipt", "receipt_schema": payload.get("schema")}
    return {
        **payload,
        "schema": SCHEMA,
        "source_file": str(path),
        "execution_admitted": False,
        "ledger_mutated": False,
        "submission_attempted": False,
    }


__all__ = ["SCHEMA", "build_view"]
