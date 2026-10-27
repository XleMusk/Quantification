"""Read-only application view for frozen paper execution attribution.

The API route supplies its helper namespace so this view can be tested without
opening an account or importing broker/THS execution code.  Attribution is
based on saved paper artifacts and configured cost stress only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project saved parent/child attribution for the workbench consumer."""

    HTTPException = namespace["HTTPException"]
    _package_artifact = namespace["_package_artifact"]
    _same_ledger_bundle_reconciliation_view = namespace["_same_ledger_bundle_reconciliation_view"]
    _same_ledger_compat_fields = namespace["_same_ledger_compat_fields"]
    _ths_journal_reconciliation_view = namespace["_ths_journal_reconciliation_view"]
    operator_package = namespace["operator_package"]

    package = operator_package()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    attribution_path = _package_artifact(package, "paper_execution_attribution")
    if not attribution_path:
        raise HTTPException(404, "当前离线包缺少纸面执行归因产物")

    try:
        payload = json.loads(Path(attribution_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"纸面执行归因尚不可读取：{error}") from error

    return {
        "schema": "offline_execution_attribution_view_v1",
        "status": payload.get("status", "offline_reference_attribution"),
        "execution_admitted": False,
        "real_broker_state": False,
        "account_observed": False,
        "quotes_observed": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(
            same_ledger_bundle_reconciliation, journal_reconciliation
        ),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "parent_count": payload.get("parent_count", 0),
        "child_count": payload.get("child_count", 0),
        "completed_parent_count": payload.get("completed_parent_count", 0),
        "residual_parent_count": payload.get("residual_parent_count", 0),
        "tca_metrics": payload.get("tca_metrics", {}),
        "side_summary": payload.get("side_summary", []),
        "scenario_summary": payload.get("scenario_summary", []),
        "child_pressure_scenarios": payload.get("child_pressure_scenarios", []),
        "execution_cost_stress": payload.get("execution_cost_stress", []),
        "parents": payload.get("parents", []),
        "basis": payload.get(
            "tca_basis",
            "frozen reference price and configured paper cost; no observed fill benchmark",
        ),
        "not_attempted": payload.get("not_attempted", []),
        "source_file": str(attribution_path),
    }


__all__ = ["build_view"]
