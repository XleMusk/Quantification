"""Read-only application view for the last explicit paper strategy selection.

The HTTP route supplies its helper namespace so the selection read path can be
tested independently.  The POST selection action remains in the API because
it invokes the offline paper-selection command; this module only reads the
saved receipt and frozen candidate risk row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project the saved paper selection without rerunning or mutating state."""

    HTTPException = namespace["HTTPException"]
    _package_artifact = namespace["_package_artifact"]
    _operator_package_session_root = namespace["_operator_package_session_root"]
    operator_package = namespace["operator_package"]

    package = operator_package()
    receipt_path = _operator_package_session_root(package) / "paper_strategy_application_receipt_v1.json"
    if not receipt_path.is_file():
        return {
            "schema": "offline_paper_selection_view_v1",
            "status": "no_paper_strategy_selected",
            "strategy_id": None,
            "receipt": None,
            "capacity": None,
        }
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"单策略纸面回执尚不可读取：{error}") from error
    if receipt.get("schema") != "paper_strategy_application_receipt_v1":
        raise HTTPException(409, "单策略纸面回执版本不受支持")
    if str(receipt.get("execution_day")) != str(package.get("execution_day")):
        return {
            "schema": "offline_paper_selection_view_v1",
            "status": "stale_paper_strategy_selection",
            "strategy_id": None,
            "receipt": None,
            "capacity": None,
        }
    capacity = None
    risk_path = _package_artifact(package, "offline_portfolio_risk_summary")
    if risk_path:
        try:
            risk_payload = json.loads(risk_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"候选组合风险书尚不可读取：{error}") from error
        capacity = next(
            (row for row in risk_payload.get("rows") or []
             if str(row.get("strategy_id")) == str(receipt.get("strategy_id"))),
            None,
        )
        if capacity is not None:
            capacity = {"row": capacity, "source_file": str(risk_path), "execution_admitted": False}
    return {
        "schema": "offline_paper_selection_view_v1",
        "status": receipt.get("status", "offline_paper_strategy_applied"),
        "strategy_id": receipt.get("strategy_id"),
        "receipt": receipt,
        "capacity": capacity,
    }


__all__ = ["build_view"]
