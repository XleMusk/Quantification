"""Read-only application view for paper release controls and alerts."""
from __future__ import annotations

from typing import Any, Mapping, Sequence


SCHEMA = "offline_release_controls_view_v1"


def build_view(
    *,
    alerts: Sequence[Mapping[str, Any]],
    controls: Sequence[Mapping[str, Any]],
    source_files: Mapping[str, str],
    journal_reconciliation: Mapping[str, Any],
    same_ledger_bundle_reconciliation: Mapping[str, Any],
    compatibility_fields: Mapping[str, Any],
) -> dict[str, Any]:
    """Join saved paper controls with read-only THS reconciliation state."""

    alert_rows = [dict(row) for row in alerts]
    control_rows = [dict(row) for row in controls]
    return {
        "schema": SCHEMA,
        "status": "paper_controls_only",
        "execution_admitted": False,
        "live_alerts": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **compatibility_fields,
        "ledger_mutated": bool(journal_reconciliation.get("ledger_mutated")),
        "alerts": {
            "row_count": len(alert_rows),
            "open_count": sum(row.get("state") == "open" for row in alert_rows),
            "severity_counts": _counts(alert_rows, "severity"),
            "rows": alert_rows,
        },
        "controls": {"row_count": len(control_rows), "rows": control_rows},
        "source_files": dict(source_files),
    }


def _counts(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key, "unknown"))
        counts[value] = counts.get(value, 0) + 1
    return counts


__all__ = ["SCHEMA", "build_view"]
