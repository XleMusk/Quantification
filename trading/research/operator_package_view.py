"""Read-only application view for the complete offline operator package.

The API adapter supplies the helper namespace so this projection can be
tested without opening a broker/THS client.  It enriches a saved paper package
with reconciliation references while keeping account and execution state
explicitly outside the view.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Load and enrich the newest saved offline operator package."""

    HTTPException = namespace["HTTPException"]
    _latest_operator_package_path = namespace["_latest_operator_package_path"]
    _latest_structured_observation_package = namespace["_latest_structured_observation_package"]
    _latest_complete_structured_observation_package = namespace["_latest_complete_structured_observation_package"]
    _ths_journal_reconciliation_view = namespace["_ths_journal_reconciliation_view"]
    _same_ledger_bundle_reconciliation_view = namespace["_same_ledger_bundle_reconciliation_view"]
    _same_ledger_compat_fields = namespace["_same_ledger_compat_fields"]

    path = _latest_operator_package_path()
    if path is None:
        raise HTTPException(404, "尚无离线运营包")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"离线运营包尚不可读取：{error}") from error
    if payload.get("schema") != "offline_operator_package_v1":
        raise HTTPException(409, "离线运营包版本不受支持")

    payload["package_source_file"] = str(Path(path).resolve())
    execution_day = str(payload.get("execution_day") or "")
    today = date.today().isoformat()
    if execution_day and execution_day < today:
        payload["package_freshness"] = "stale_execution_day"
        payload["package_warning"] = (
            f"execution_day={execution_day} is before server_date={today}; "
            "do not reuse this package for a new paper selection or THS handoff"
        )
        payload["next_action"] = (
            "build a future-dated opening pack from a current source cutoff before selection"
        )
    else:
        payload["package_freshness"] = "current_or_future_execution_day"

    structured_package = _latest_structured_observation_package()
    complete_structured_package = _latest_complete_structured_observation_package()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    payload["structured_observation_package"] = structured_package
    payload["structured_observation_package_complete"] = complete_structured_package
    payload["structured_package_ready_for_ledger_reconciliation"] = bool(
        structured_package.get("ready_for_ledger_reconciliation")
    )
    payload["complete_structured_package_ready_for_ledger_reconciliation"] = bool(
        complete_structured_package.get("ready_for_ledger_reconciliation")
    )
    payload["order_reconciliation"] = structured_reconciliation
    payload["order_reconciliation_status"] = structured_reconciliation.get("status")
    payload["order_count"] = structured_reconciliation.get("order_count")
    payload["trade_count"] = structured_reconciliation.get("trade_count")
    payload["cashflow_count"] = structured_reconciliation.get("cashflow_count")
    payload["ths_journal_reconciliation"] = journal_reconciliation
    payload["same_ledger_bundle_reconciliation"] = same_ledger_bundle_reconciliation
    payload.update(_same_ledger_compat_fields(
        same_ledger_bundle_reconciliation, journal_reconciliation
    ))
    payload["execution_admitted"] = False
    payload["ledger_mutated"] = journal_reconciliation["ledger_mutated"]
    payload["client_interacted"] = False
    return payload


__all__ = ["build_view"]
