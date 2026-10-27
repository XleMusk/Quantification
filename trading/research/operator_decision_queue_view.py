"""Read-only application view for the saved operator decision queue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping


class UnsupportedDecisionQueue(ValueError):
    """The saved queue is not the supported operator contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"operator decision queue is not readable: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"operator decision queue must be an object: {path}")
    return value


def build_view(
    package: dict[str, Any],
    artifact_resolver: Callable[[dict[str, Any], str], Path | None],
    journal_reconciliation: Mapping[str, Any],
    same_ledger_bundle_reconciliation: Mapping[str, Any],
    compatibility_fields: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Read a saved queue and attach current read-only reconciliation views."""

    queue_path = artifact_resolver(package, "offline_operator_decision_queue_json")
    if not queue_path:
        raise FileNotFoundError("operator decision queue artifact is missing")
    payload = _read_json(queue_path)
    if payload.get("schema") != "offline_operator_decision_queue_v1":
        raise UnsupportedDecisionQueue("operator decision queue version is unsupported")

    payload["account_observed"] = False
    payload["quotes_observed"] = False
    payload["execution_admitted"] = False
    payload["ths_journal_reconciliation"] = journal_reconciliation
    payload["same_ledger_bundle_reconciliation"] = same_ledger_bundle_reconciliation
    payload.update(compatibility_fields(same_ledger_bundle_reconciliation, journal_reconciliation))
    payload["ledger_mutated"] = bool(journal_reconciliation.get("ledger_mutated"))

    handoff_rows = []
    handoff_index_path = Path(str(payload.get("recovery_handoff_index") or ""))
    if handoff_index_path.is_file():
        handoff_index = _read_json(handoff_index_path)
        for index_row in handoff_index.get("rows") or []:
            handoff_path = Path(str(index_row.get("handoff_json") or ""))
            if not handoff_path.is_file():
                continue
            handoff = _read_json(handoff_path)
            pending = sorted({
                str(item)
                for check in ((handoff.get("reconciliation_template") or {}).get("checks") or [])
                for item in (check.get("pending_evidence") or [])
                if str(item)
            })
            handoff_rows.append({
                "strategy_id": handoff.get("strategy_id") or index_row.get("strategy_id"),
                "scenario_count": handoff.get("scenario_count", 0),
                "unresolved_scenario_count": handoff.get("unresolved_scenario_count", 0),
                "retry_permitted": False,
                "rows": handoff.get("rows") or [],
                "pending_evidence_count": len(pending),
                "pending_evidence": pending,
                "not_attempted": handoff.get("not_attempted") or [],
            })
    payload["recovery_handoffs"] = handoff_rows
    for row in payload.get("rows") or []:
        row["execution_admitted"] = "false"
    return payload


__all__ = ["UnsupportedDecisionQueue", "build_view"]
