"""Persistent thesis memory for A-share trade learning."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_STATE_DIR = Path("committee_state") / "theses"


def register_thesis(
    thesis: dict[str, Any],
    state_dir: str | Path = DEFAULT_STATE_DIR,
    source_path: str | Path | None = None,
) -> dict[str, Any]:
    """Register a thesis as an IDEA object."""
    state_path = Path(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)
    code = str(thesis.get("code") or "unknown").replace(".", "_").lower()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    thesis_id = f"th_{code}_{stamp}"
    item = {
        "schema_version": "a-share-thesis/v1",
        "thesis_id": thesis_id,
        "status": "IDEA",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stock_name": thesis.get("stock_name", ""),
        "code": thesis.get("code", ""),
        "action": thesis.get("action", ""),
        "time_horizon": thesis.get("time_horizon", ""),
        "source_path": str(source_path) if source_path else "",
        "position_report": "",
        "entry": {},
        "exit": {},
        "lessons": [],
        "thesis": thesis,
        "status_history": [
            {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "from": "",
                "to": "IDEA",
                "reason": "registered",
            }
        ],
    }
    _write_json_atomic(_thesis_path(state_path, thesis_id), item)
    _rebuild_index(state_path)
    return item


def list_theses(
    state_dir: str | Path = DEFAULT_STATE_DIR,
    status: str | None = None,
    code: str | None = None,
) -> list[dict[str, Any]]:
    """List stored theses."""
    state_path = Path(state_dir)
    if not state_path.exists():
        return []
    items = []
    for path in sorted(state_path.glob("th_*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if status and item.get("status") != status:
            continue
        if code and item.get("code") != code:
            continue
        items.append(item)
    return items


def transition_thesis(
    thesis_id: str,
    new_status: str,
    reason: str,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    **fields: Any,
) -> dict[str, Any]:
    """Transition a thesis forward and attach optional fields."""
    allowed = {"IDEA", "ENTRY_READY", "ACTIVE", "CLOSED", "INVALIDATED"}
    if new_status not in allowed:
        raise ValueError(f"Unsupported status: {new_status}")

    path = _thesis_path(Path(state_dir), thesis_id)
    item = json.loads(path.read_text(encoding="utf-8"))
    old_status = item.get("status", "")
    item["status"] = new_status
    item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    item["status_history"].append(
        {
            "at": item["updated_at"],
            "from": old_status,
            "to": new_status,
            "reason": reason,
        }
    )
    for key, value in fields.items():
        if value is not None:
            item[key] = value

    _write_json_atomic(path, item)
    _rebuild_index(Path(state_dir))
    return item


def attach_position_report(
    thesis_id: str,
    report_path: str | Path,
    state_dir: str | Path = DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """Attach a position sizing report to a thesis."""
    return transition_thesis(
        thesis_id,
        "ENTRY_READY",
        "position sizing attached",
        state_dir=state_dir,
        position_report=str(report_path),
    )


def close_thesis(
    thesis_id: str,
    exit_price: float,
    reason: str,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    lessons: list[str] | None = None,
) -> dict[str, Any]:
    """Close a thesis and record lessons."""
    exit_data = {
        "exit_price": exit_price,
        "exit_date": datetime.now().strftime("%Y-%m-%d"),
        "exit_reason": reason,
    }
    item = transition_thesis(
        thesis_id,
        "CLOSED",
        reason,
        state_dir=state_dir,
        exit=exit_data,
    )
    if lessons:
        item["lessons"].extend(lessons)
        _write_json_atomic(_thesis_path(Path(state_dir), thesis_id), item)
        _rebuild_index(Path(state_dir))
    return item


def format_thesis_table(items: list[dict[str, Any]]) -> str:
    """Format thesis list as a simple table."""
    if not items:
        return "暂无交易想法。"
    lines = [
        "| thesis_id | status | code | stock_name | updated_at |",
        "|---|---|---|---|---|",
    ]
    for item in items:
        lines.append(
            "| {thesis_id} | {status} | {code} | {stock_name} | {updated_at} |".format(
                thesis_id=item.get("thesis_id", ""),
                status=item.get("status", ""),
                code=item.get("code", ""),
                stock_name=item.get("stock_name", ""),
                updated_at=item.get("updated_at", ""),
            )
        )
    return "\n".join(lines)


def _thesis_path(state_dir: Path, thesis_id: str) -> Path:
    return state_dir / f"{thesis_id}.json"


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _rebuild_index(state_dir: Path) -> None:
    index = []
    for item in list_theses(state_dir):
        index.append(
            {
                "thesis_id": item.get("thesis_id"),
                "status": item.get("status"),
                "code": item.get("code"),
                "stock_name": item.get("stock_name"),
                "updated_at": item.get("updated_at"),
            }
        )
    _write_json_atomic(state_dir / "_index.json", {"items": index})

