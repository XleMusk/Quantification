"""Application boundary for explicit paper strategy selection."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
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


def apply_selection(namespace: Mapping[str, Any], body: Any) -> dict[str, Any]:
    """Run the offline selection script for one frozen candidate only."""

    HTTPException = namespace["HTTPException"]
    settings = namespace["settings"]
    package = namespace["operator_package"]()
    if package.get("package_freshness") == "stale_execution_day":
        raise HTTPException(409, package.get("package_warning", "离线运营包已过期，不能继续选择"))
    candidates = {str(row.get("strategy_id")) for row in package.get("strategy_candidates", [])}
    if body.strategy_id not in candidates:
        raise HTTPException(400, "候选策略不在当前冻结运营包中")
    opening_pack = Path(str(package.get("opening_pack", "")))
    script = Path(__file__).resolve().parents[2] / "scripts" / "apply_offline_strategy_selection.ps1"
    if not opening_pack.is_file() or not script.is_file():
        raise HTTPException(409, "纸面策略选择所需的冻结运营包或脚本不存在")
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-OpeningPack", str(opening_pack), "-StrategyId", body.strategy_id,
        "-OutputRoot", str(settings.DATA_ROOT / "research" / "opening_session_runs"),
    ]
    try:
        result = subprocess.run(command, cwd=str(script.parents[1]), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=180, check=False)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(504, "纸面策略选择超过 180 秒，未尝试任何真实交易动作") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "纸面策略选择失败").strip()[-4000:]
        raise HTTPException(409, detail)
    receipt_path = namespace["_operator_package_session_root"](package) / "paper_strategy_application_receipt_v1.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig")) if receipt_path.is_file() else None
    return {"status": "offline_paper_strategy_applied", "strategy_id": body.strategy_id,
            "receipt": receipt, "stdout_tail": (result.stdout or "").strip()[-2000:],
            "execution_admitted": False, "submission_enabled": False}


__all__ = ["apply_selection", "build_view"]
