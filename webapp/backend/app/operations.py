"""Read saved daily progress without starting acquisition or account workers."""
from datetime import datetime
import json
import re

from scripts.progress_supervision_status import (
    SHANGHAI_TZ, progress_status, scheduling_clock, settlement_summary,
)
from trading import settings as trading_settings, store as trading_store
from . import store


def _read(path):
    if path is None:
        return None, None
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("进度记录超过读取限制")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("进度记录须为对象")
        return value, None
    except (OSError, ValueError, UnicodeError) as error:
        return None, str(error)


def _latest(root, pattern):
    # The newest saved attempt is authoritative even when it failed or is bad.
    # Do not hide that attempt by falling back to an older successful file.
    paths = sorted(root.glob(pattern), key=lambda path: tuple(int(part) if part.isdigit() else part
                   for part in re.split(r"(\d+)", str(path))), reverse=True)
    return paths[0] if paths else None


def _evidence(path, value, error):
    return {"file": str(path) if path else None, "status": "unreadable" if error else "saved" if value else "missing",
            "error": error, "day": value.get("day") if value else None,
            "saved_status": value.get("status") if value else None}


def workspace(current=None):
    now = current or datetime.now(SHANGHAI_TZ)
    if now.tzinfo is None:
        raise ValueError("运行状态需要带时区的检查时间")
    now = now.astimezone(SHANGHAI_TZ)
    research = trading_settings.DATA_ROOT / "research"
    calendar_path = research / "calendar" / f"cn_a_share_{now.year}_sse_v1.json"
    calendar, calendar_error = _read(calendar_path)
    clock = scheduling_clock(now, calendar)
    clock.update(calendar_file=str(calendar_path), calendar_read_error=calendar_error)
    close_path = _latest(research / "bulk_close_cycles", "*/receipt.json")
    close, close_error = _read(close_path)
    due = clock.get("latest_due_day")
    due_path = None
    if due:
        root = research / "bulk_close_cycles"
        # Only exact due-day receipts can satisfy the current data requirement.
        due_path = _latest(root, due.replace("-", "") + "_v*/receipt.json")
    due_close, due_error = _read(due_path)
    systems = research / "system_daily"
    head_path = _latest(systems, "*/head.json")
    head, head_error = _read(head_path)
    selected_system = head_path.parent if head_path else None
    operation_path = _latest(selected_system, "operations/*/receipt.json") if selected_system else None
    operation, operation_error = _read(operation_path)
    settlement_path = _latest(selected_system, "settlements/*/receipt.json") if selected_system else None
    settlement, settlement_error = _read(settlement_path)
    progress = progress_status(clock, close, due_close, head, operation, due_receipt_error=due_error)
    profiles = []
    if head:
        for key, value in head.items():
            if (key == "profile" or key.endswith("_profile")) and isinstance(value, dict):
                profiles.append({"id": key, "source_day": value.get("last_source_day", value.get("source_day")),
                                 "publication_day": value.get("publication_source_day", value.get("last_publication_day", value.get("publication_day"))),
                                 "missing_publication_day": ", ".join(value["missing_publication_days"]) if isinstance(value.get("missing_publication_days"), list) else value.get("missing_publication_day"),
                                 "publication_status": value.get("publication_status"),
                                 "source_file": str(head_path)})
    steps = []
    raw_steps = (operation or {}).get("steps") or []
    if isinstance(raw_steps, dict):
        raw_steps = [{"id": key, **value} for key, value in raw_steps.items() if isinstance(value, dict)]
    for step in raw_steps[:200]:
        if isinstance(step, dict):
            steps.append({key: step.get(key) for key in ("id", "status", "error", "recovered", "started_at", "finished_at")})
    accounts = []
    for run in trading_store.runs():
        with trading_store.connection() as db:
            row = db.execute("SELECT payload FROM daily WHERE run_id=? ORDER BY date DESC LIMIT 1", (run["id"],)).fetchone()
        saved = json.loads(row[0]) if row else {}
        accounts.append({"id": run["id"], "name": run["name"], "mode": run["mode"], "status": run["status"],
                         "execution_day": run["day"], "sessions": run["cursor"], "error": run["error"],
                         "market_version": saved.get("market_version"),
                         "market_version_status": saved.get("market_version_status", "legacy_unbound" if row and run["mode"] == "paper" else None)})
    recoveries = []
    paths = sorted(selected_system.glob("operations/*/recover*/*.json"), reverse=True)[:30] if selected_system else []
    for path in paths:
        value, error = _read(path)
        recoveries.append(_evidence(path, value, error))
    return {"schema": "daily_operations_workspace_v1", "checked_at": now.isoformat(), "clock": clock,
            "progress": progress, "workers": {"factor": store.worker_status(), "account": trading_store.health()},
            "evidence": {"latest_close": _evidence(close_path, close, close_error),
                         "due_close": _evidence(due_path, due_close, due_error),
                         "system_head": _evidence(head_path, head, head_error),
                         "operation": _evidence(operation_path, operation, operation_error),
                         "settlement": _evidence(settlement_path, settlement, settlement_error)},
            "system_directory": str(selected_system) if selected_system else None,
            "profiles": profiles, "steps": steps, "accounts": accounts,
            "settlement": settlement_summary(settlement), "recoveries": recoveries,
            "acquisition_started": False, "signals_published": False, "ledger_mutated": False,
            "warnings": ["来源日期、信号发表、账户执行与进程心跳分别显示；缺记录保持未知",
                         "恢复记录只展示已保存结果，此页面不会重跑生产、补写历史信号或推进账户"]}
