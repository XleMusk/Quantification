"""Application boundary for paper-only manual recovery review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "offline_recovery_review_view_v1"


def _read(path: Path, http_exception: Any, message: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise http_exception(409, f"{message}：{error}") from error
    if not isinstance(value, dict):
        raise http_exception(409, f"{message}不是对象")
    return value


def _candidates(namespace: Mapping[str, Any], package: Mapping[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    http_exception = namespace["HTTPException"]
    index_path = namespace["_package_artifact"](package, "paper_recovery_handoff_index")
    if not index_path:
        raise http_exception(404, "当前离线包缺少恢复交接索引")
    index = _read(Path(index_path), http_exception, "恢复交接索引尚不可读取")
    candidates: list[dict[str, Any]] = []
    for index_row in index.get("rows") or []:
        handoff_path = Path(str(index_row.get("handoff_json") or ""))
        if not handoff_path.is_file():
            continue
        handoff = _read(handoff_path, http_exception, "恢复交接模板尚不可读取")
        for row in handoff.get("rows") or []:
            candidates.append({
                "strategy_id": handoff.get("strategy_id") or index_row.get("strategy_id"),
                "scenario_id": row.get("scenario_id"),
                "lifecycle_status": row.get("lifecycle_status"),
                "recovery_action": row.get("recovery_action"),
                "required_observation": row.get("required_observation"),
                "retry_permitted": False,
                "execution_admitted": False,
                "source_file": str(handoff_path),
            })
    return Path(index_path), candidates


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    package = namespace["operator_package"]()
    index_path, candidates = _candidates(namespace, package)
    root = namespace["_recovery_review_receipt_path"](package)
    receipts = []
    if root.is_dir():
        for path in sorted(root.glob("human_recovery_review_*_v1.json")):
            receipts.append(_read(path, namespace["HTTPException"], "人工恢复审查回执尚不可读取"))
    return {
        "schema": SCHEMA,
        "status": "human_recovery_review_recorded" if receipts else "manual_recovery_review_required",
        "source_template": str(index_path),
        "receipts": receipts,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "retry_permitted": False,
        "automatic_recovery": False,
        "execution_admitted": False,
        "next_action": (
            "保留人工接管决定，继续等待账户、委托和成交观察；不会自动重试或撤单"
            if receipts else
            "提交人工恢复审查决定前，系统保持停止和对账优先，不会自动重试"
        ),
    }


def submit_review(namespace: Mapping[str, Any], body: Any) -> dict[str, Any]:
    http_exception = namespace["HTTPException"]
    package = namespace["operator_package"]()
    if package.get("package_freshness") == "stale_execution_day":
        raise http_exception(409, package.get("package_warning", "离线包已过期，不能记录新恢复审查"))
    index_path, candidates = _candidates(namespace, package)
    candidate = next((row for row in candidates
                      if str(row.get("strategy_id") or "") == body.strategy_id
                      and str(row.get("scenario_id") or "") == body.scenario_id), None)
    if candidate is None:
        raise http_exception(400, "恢复场景不在当前冻结恢复交接模板中")
    if body.confirm_review is not True:
        return {
            "schema": "human_recovery_review_receipt_v1",
            "status": "confirmation_required",
            "strategy_id": body.strategy_id,
            "scenario_id": body.scenario_id,
            "decision": body.decision,
            "retry_permitted": False,
            "automatic_recovery": False,
            "execution_admitted": False,
            "next_action": "确认该人工处置决定后，再次提交 confirm_review=true；仍不会自动重试、撤单或提交。",
        }
    receipt = {
        "schema": "human_recovery_review_receipt_v1",
        "status": "human_recovery_review_recorded",
        "execution_day": package.get("execution_day"),
        "strategy_id": body.strategy_id,
        "scenario_id": body.scenario_id,
        "decision": body.decision,
        "reviewer": body.reviewer,
        "reviewed_at": body.reviewed_at,
        "notes": body.notes,
        "candidate_snapshot": candidate,
        "source_template": str(index_path),
        "retry_permitted": False,
        "automatic_recovery": False,
        "execution_admitted": False,
        "submission_enabled": False,
    }
    target = namespace["_recovery_review_receipt_file"](
        package, body.strategy_id, body.scenario_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return receipt


__all__ = ["SCHEMA", "build_view", "submit_review"]
