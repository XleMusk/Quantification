"""Application boundary for the paper-only human promotion review journey."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "offline_promotion_review_view_v1"
RECEIPT_SCHEMA = "human_promotion_review_receipt_v1"


def _load_json(path: Path, http_exception: Any, message: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise http_exception(409, f"{message}：{error}") from error
    if not isinstance(value, dict):
        raise http_exception(409, f"{message}不是对象")
    return value


def _template(namespace: Mapping[str, Any], package: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    http_exception = namespace["HTTPException"]
    path = namespace["_package_artifact"](package, "strategy_promotion_review")
    if not path:
        raise http_exception(404, "当前离线包缺少策略晋级审查模板")
    review = _load_json(path, http_exception, "策略晋级审查模板尚不可读取")
    if review.get("schema") != "strategy_promotion_review_record_v1":
        raise http_exception(409, "策略晋级审查模板版本不受支持")
    return Path(path), review


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Read the frozen promotion template and the latest human receipt."""

    package = namespace["operator_package"]()
    review_path, review = _template(namespace, package)
    receipt_path = namespace["_promotion_review_receipt_path"](package)
    receipt = None
    if receipt_path.is_file():
        receipt = _load_json(receipt_path, namespace["HTTPException"], "人工晋级审查回执尚不可读取")
    return {
        "schema": SCHEMA,
        "status": "human_review_recorded" if receipt else review.get("status"),
        "source_template": str(review_path),
        "receipt": receipt,
        "candidate_count": review.get("candidate_count", 0),
        "eligible_candidate_count": review.get("eligible_candidate_count", 0),
        "required_evidence": review.get("required_evidence") or [],
        "automatic_promotion": False,
        "execution_admitted": False,
        "next_action": (
            "保留人工审查记录；策略仍需独立证据、账户/行情观察和后续人工确认"
            if receipt else
            "提交明确的人审决定前，策略保持待审，不会自动晋级"
        ),
    }


def submit_review(namespace: Mapping[str, Any], body: Any) -> dict[str, Any]:
    """Persist an explicit paper-only human decision after confirmation."""

    http_exception = namespace["HTTPException"]
    package = namespace["operator_package"]()
    if package.get("package_freshness") == "stale_execution_day":
        raise http_exception(409, package.get("package_warning", "离线运营包已过期，不能记录新审查"))
    review_path, review = _template(namespace, package)
    candidate = next(
        (row for row in review.get("rows") or []
         if str(row.get("strategy_id") or "") == body.strategy_id),
        None,
    )
    if candidate is None:
        raise http_exception(400, "策略不在当前冻结晋级审查模板中")
    if body.confirm_review is not True:
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "confirmation_required",
            "strategy_id": body.strategy_id,
            "decision": body.decision,
            "automatic_promotion": False,
            "execution_admitted": False,
            "next_action": "确认该纸面审查决定后，再次提交 confirm_review=true；不会自动下单。",
        }
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "human_review_recorded",
        "execution_day": package.get("execution_day"),
        "strategy_id": body.strategy_id,
        "decision": body.decision,
        "reviewer": body.reviewer,
        "reviewed_at": body.reviewed_at,
        "notes": body.notes,
        "candidate_snapshot": candidate,
        "source_template": str(review_path),
        "automatic_promotion": False,
        "execution_admitted": False,
        "submission_enabled": False,
    }
    target = namespace["_promotion_review_receipt_path"](package)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return receipt


__all__ = ["RECEIPT_SCHEMA", "SCHEMA", "build_view", "submit_review"]
