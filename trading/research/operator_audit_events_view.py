"""Read-only application view for the saved paper event trail.

The route supplies its helper namespace so the projection can be tested
without opening a broker/THS client or mutating the unified Ledger.  The
event log is an ordered paper artifact, not an observation of broker state.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project the persisted paper audit event log for the workbench."""

    HTTPException = namespace["HTTPException"]
    Counter = namespace["Counter"]
    _package_artifact = namespace["_package_artifact"]
    _same_ledger_bundle_reconciliation_view = namespace["_same_ledger_bundle_reconciliation_view"]
    _same_ledger_compat_fields = namespace["_same_ledger_compat_fields"]
    _ths_journal_reconciliation_view = namespace["_ths_journal_reconciliation_view"]
    operator_package = namespace["operator_package"]

    package = operator_package()
    event_path = _package_artifact(package, "paper_audit_event_log")
    jsonl_path = _package_artifact(package, "paper_audit_event_log_jsonl")
    if not event_path and not jsonl_path:
        raise HTTPException(404, "当前离线包缺少纸面审计事件日志")

    rows: list[dict[str, Any]] = []
    if event_path:
        try:
            with Path(event_path).open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error) as error:
            raise HTTPException(409, f"纸面审计事件日志尚不可读取：{error}") from error
    elif jsonl_path:
        try:
            for line in Path(jsonl_path).read_text(encoding="utf-8-sig").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("审计 JSONL 行必须是对象")
                rows.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise HTTPException(409, f"纸面审计 JSONL 尚不可读取：{error}") from error

    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    return {
        "schema": "offline_audit_event_log_view_v1",
        "status": "paper_event_log_only",
        "execution_admitted": False,
        "live_alerts": False,
        "broker_state_observed": False,
        "client_interacted": False,
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "event_count": len(rows),
        "state_counts": dict(Counter(row.get("state") or "unknown" for row in rows)),
        "event_type_counts": dict(Counter(row.get("event_type") or "unknown" for row in rows)),
        "events": rows,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "source_files": {
            "csv": str(event_path) if event_path else None,
            "jsonl": str(jsonl_path) if jsonl_path else None,
        },
        "next_action": "纸面事件仅用于审计和人工交接；真实订单、成交、账户和资金流水仍需持久同花顺观察包。",
    }


__all__ = ["build_view"]
