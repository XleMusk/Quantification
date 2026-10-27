"""Read-only application views for paper portfolio controls and risk.

The API supplies its helper namespace so these views can be tested without
granting them broker, THS, account, or ledger access.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


def _read_csv(path: Path, message: str, http_exception: Any) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as error:
        raise http_exception(409, message) from error


def build_controls_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project frozen what-if and allocation evidence for the desk UI."""

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
    scenario_path = _package_artifact(package, "scenario_analysis")
    allocation_path = _package_artifact(package, "allocation_settlement_projection")
    if not scenario_path or not allocation_path:
        raise HTTPException(404, "当前离线包缺少纸面情景或分配结算投影")
    scenario_rows = _read_csv(scenario_path, "纸面组合控制尚不可读取", HTTPException)
    allocation_all_rows = _read_csv(allocation_path, "纸面组合控制尚不可读取", HTTPException)
    allocation_order_rows = [row for row in allocation_all_rows if row.get("row_type", "order") == "order"]
    allocation_total_rows = [row for row in allocation_all_rows if row.get("row_type") != "order"]
    return {
        "schema": "offline_portfolio_controls_view_v1",
        "status": "paper_projection_only",
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(same_ledger_bundle_reconciliation, journal_reconciliation),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "scenario": {
            "row_count": len(scenario_rows),
            "rows": scenario_rows,
            "basis": "frozen research reference prices; paper what-if only; no quote or fill observed",
        },
        "allocation": {
            "order_count": len(allocation_order_rows),
            "total_rows": allocation_total_rows,
            "state_counts": dict(Counter(row.get("settlement_state", "unknown") for row in allocation_order_rows)),
            "t1_dependency_counts": dict(Counter(row.get("t1_dependency", "unknown") for row in allocation_order_rows)),
            "rows": allocation_order_rows,
            "basis": "frozen research reference price; no fill or account observation",
        },
        "source_files": {"scenario": str(scenario_path), "allocation": str(allocation_path)},
    }


def build_risk_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project the saved candidate portfolio risk book without mutation."""

    HTTPException = namespace["HTTPException"]
    _package_artifact = namespace["_package_artifact"]
    operator_package = namespace["operator_package"]
    package = operator_package()
    risk_path = _package_artifact(package, "offline_portfolio_risk_summary")
    if not risk_path:
        raise HTTPException(404, "当前离线包缺少候选组合风险书")
    try:
        payload = json.loads(Path(risk_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(409, f"候选组合风险书尚不可读取：{error}") from error
    if payload.get("schema") != "offline_portfolio_risk_summary_v1":
        raise HTTPException(409, "候选组合风险书版本不受支持")
    return {
        "schema": "offline_portfolio_risk_view_v1",
        "status": payload.get("status", "offline_reference_risk_comparison"),
        "execution_day": payload.get("execution_day"),
        "candidate_count": payload.get("candidate_count", 0),
        "sort_basis": payload.get("sort_basis"),
        "shared_account_scenario": payload.get("shared_account_scenario") or {},
        "rows": payload.get("rows") or [],
        "account_observed": False,
        "quotes_observed": False,
        "execution_admitted": False,
        "source_file": str(risk_path),
        "basis": "冻结参考价格和配置现金的纸面风险比较；候选策略互斥，不合并到统一账户账本",
        "not_attempted": payload.get("not_attempted") or [],
    }


__all__ = ["build_controls_view", "build_risk_view"]
