"""Read-only application view for the frozen paper order blotter.

The API route supplies its helper namespace so the blotter projection can be
tested independently.  The view reads a saved paper artifact only; it never
contacts a broker/THS client, acknowledges an order, or mutates a ledger.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project the saved paper blotter for the workbench consumer."""

    HTTPException = namespace["HTTPException"]
    Counter = namespace["Counter"]
    _package_artifact = namespace["_package_artifact"]
    _same_ledger_bundle_reconciliation_view = namespace["_same_ledger_bundle_reconciliation_view"]
    _same_ledger_compat_fields = namespace["_same_ledger_compat_fields"]
    _ths_journal_reconciliation_view = namespace["_ths_journal_reconciliation_view"]
    operator_package = namespace["operator_package"]

    package = operator_package()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    blotter_path = _package_artifact(package, "blotter")
    if not blotter_path:
        raise HTTPException(404, "当前离线包缺少纸面中央订单台")

    try:
        with Path(blotter_path).open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as error:
        raise HTTPException(409, f"纸面中央订单台尚不可读取：{error}") from error

    return {
        "schema": "offline_blotter_view_v1",
        "status": "paper_blotter_only",
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "blotter": {
            "row_count": len(rows),
            "preflight_status_counts": dict(Counter(row.get("preflight_status") or "unknown" for row in rows)),
            "route_status_counts": dict(Counter(row.get("route_status") or "unknown" for row in rows)),
            "lifecycle_counts": dict(Counter(row.get("lifecycle") or "unknown" for row in rows)),
            "side_counts": dict(Counter(row.get("side") or "unknown" for row in rows)),
            "route_available_count": sum(row.get("route_status") == "route_available" for row in rows),
            "rows": rows,
            "basis": "frozen paper order intent and reference prices; no broker acknowledgement, fill, account or quote observation",
        },
        "source_file": str(blotter_path),
    }


__all__ = ["build_view"]
