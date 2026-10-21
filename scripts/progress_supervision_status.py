"""Pure scheduling and saved-evidence interpretation for the read-only monitor."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any


SHANGHAI_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
_COMPLETE_SOURCE_STATUS = "complete_full_dated_close_inventory_not_admitted"


def _day(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


def scheduling_clock(now: datetime, calendar: Any) -> dict[str, Any]:
    """Use only the pinned year's schedule; never extrapolate across years."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("An aware observation time is required")
    local = now.astimezone(SHANGHAI_TZ)
    result = {
        "checked_at_shanghai": local.isoformat(),
        "calendar_status": "unknown",
        "calendar_error": None,
        "calendar_id": None,
        "calendar_year": None,
        "is_trading_day": None,
        "latest_closed_day": None,
        "latest_due_day": None,
        "market_close_time": "15:00:00+08:00",
        "supplier_due_time": "18:30:00+08:00",
        "market_phase": "unknown",
    }
    if not isinstance(calendar, dict):
        result["calendar_error"] = "calendar_missing_or_unreadable"
        return result
    result["calendar_id"] = calendar.get("calendar_id")
    result["calendar_year"] = calendar.get("year")
    try:
        year = calendar["year"]
        if (calendar.get("schema") != "cn_a_share_exchange_calendar_v1"
                or not isinstance(year, int) or isinstance(year, bool)
                or not isinstance(calendar.get("closed_ranges"), list)
                or not isinstance(calendar.get("open_sessions"), list)):
            raise ValueError("unsupported_or_invalid_calendar")
        if local.year != year:
            result["calendar_error"] = "observation_outside_calendar_year"
            return result
        closed: set[date] = set()
        for item in calendar["closed_ranges"]:
            start_text, end_text = item.split("/")
            start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
            if start.year != year or end.year != year or end < start:
                raise ValueError("invalid_closed_range")
            for offset in range((end - start).days + 1):
                closed.add(start + timedelta(days=offset))
        opened = {date.fromisoformat(item) for item in calendar["open_sessions"]}
        if any(item.year != year for item in opened):
            raise ValueError("open_session_outside_calendar_year")
    except (KeyError, TypeError, ValueError, AttributeError):
        result["calendar_error"] = "invalid_calendar"
        return result

    def is_session(day: date) -> bool:
        return day in opened or (day.weekday() < 5 and day not in closed)

    today = local.date()
    result["is_trading_day"] = is_session(today)
    wall_time = local.time().replace(tzinfo=None)
    result["market_phase"] = (
        "non_trading_day" if not result["is_trading_day"]
        else "preclose" if wall_time < time(15)
        else "closed_waiting_supplier" if wall_time < time(18, 30)
        else "close_due"
    )
    for key, cutoff in (("latest_closed_day", time(15)), ("latest_due_day", time(18, 30))):
        candidate = today if wall_time >= cutoff else today - timedelta(days=1)
        while candidate.year == year:
            if is_session(candidate):
                result[key] = candidate.isoformat()
                break
            candidate -= timedelta(days=1)
    if result["latest_closed_day"] is None or result["latest_due_day"] is None:
        result["calendar_error"] = "previous_year_schedule_required"
    else:
        result["calendar_status"] = "ready"
    return result


def close_receipt_complete(receipt: Any, day: str | None = None) -> bool:
    return bool(
        isinstance(receipt, dict)
        and _day(receipt.get("day"))
        and (day is None or receipt["day"] == day)
        and receipt.get("status") == "completed_bulk_close_cycle_research_only"
        and receipt.get("source_status") == _COMPLETE_SOURCE_STATUS
        and receipt.get("full_dated_roster_inventory_complete") is True
        and isinstance(receipt.get("full_close_package"), str)
        and bool(receipt["full_close_package"])
    )


def progress_status(
    clock: dict[str, Any], latest_close: Any, due_close: Any, system: Any,
    operations: Any, *, due_receipt_error: str | None = None,
) -> dict[str, Any]:
    """Describe source, publication cursor and route result independently."""
    due = clock.get("latest_due_day")
    latest_day = _day(latest_close.get("day")) if isinstance(latest_close, dict) else None
    profile = system.get("profile") if isinstance(system, dict) else None
    cursor = _day(profile.get("last_source_day")) if isinstance(profile, dict) else None
    operation_day = _day(operations.get("day")) if isinstance(operations, dict) else None
    operation_status = str(operations.get("status") or operations.get("state") or "") if isinstance(operations, dict) else None
    result = {
        "latest_receipt_day": latest_day,
        "latest_receipt_stale": None,
        "due_source_status": "calendar_unknown",
        "data_status": "calendar_unknown",
        "system_source_day": cursor,
        "system_lagging_due_day": None,
        "operations_day": operation_day,
        "operations_status": operation_status,
        "paper_status": "calendar_unknown",
        "source_receipt_basis": "exact_due_day_cycle_receipt_only",
        "publication_basis": "system_head_profile_last_source_day_not_account_settlement",
    }
    if clock.get("calendar_status") != "ready" or not due:
        return result
    result["latest_receipt_stale"] = latest_day < due if latest_day else None
    result["system_lagging_due_day"] = cursor < due if cursor else None
    if due_receipt_error:
        source_status = "unreadable_or_unknown"
    elif due_close is None:
        source_status = "missing"
    elif not isinstance(due_close, dict) or due_close.get("day") != due:
        source_status = "wrong_day_or_invalid"
    elif close_receipt_complete(due_close, due):
        source_status = "complete_research_only"
    else:
        saved_status = str(due_close.get("status") or "").lower()
        source_status = "failed" if any(word in saved_status for word in ("fail", "error", "interrupt", "abort")) else "incomplete_or_unknown"
    result["due_source_status"] = source_status
    result["data_status"] = {
        "complete_research_only": "due_close_complete_research_snapshot",
        "missing": "due_close_missing",
        "failed": "due_close_failed",
    }.get(source_status, "due_close_incomplete_or_unknown")
    if source_status != "complete_research_only":
        result["paper_status"] = "due_source_unavailable"
    elif cursor is None:
        result["paper_status"] = "system_source_cursor_unknown"
    elif cursor < due:
        result["paper_status"] = "source_ready_system_lagging"
    elif operation_day is None or operation_day < due:
        result["paper_status"] = "daily_receipt_missing_or_stale"
    elif operation_status in {"completed", "complete", "success", "completed_saved_research_refresh"}:
        result["paper_status"] = "daily_routes_completed"
    elif operation_status == "partial_research_routes":
        result["paper_status"] = "daily_routes_partial"
    else:
        result["paper_status"] = "daily_routes_status_unknown"
    return result


def settlement_summary(receipt: Any) -> dict[str, Any] | None:
    """A saved execution receipt is never a system publication or full-system result."""
    if not isinstance(receipt, dict):
        return None
    policies = receipt.get("policies")
    policy_statuses = {
        key: value.get("status") if isinstance(value, dict) else None
        for key, value in policies.items()
    } if isinstance(policies, dict) else {}
    completed = (
        receipt.get("schema") == "system_daily_native_settlement_result_v1"
        and receipt.get("status") == "completed_native_settlement"
        and bool(policy_statuses)
        and all(value == "completed" for value in policy_statuses.values())
    )
    return {
        "day": receipt.get("day"),
        "source_day": receipt.get("source_day"),
        "status": receipt.get("status"),
        "policy_statuses": policy_statuses,
        "reported_account_execution_day": _day(receipt.get("day")) if completed else None,
        "basis": "saved_settlement_receipt_not_a_fresh_account_readback",
        "signals_published": receipt.get("signals_published"),
        "system_source_cursor_advanced": receipt.get("system_source_cursor_advanced"),
        "whole_system_daily_complete": receipt.get("whole_system_daily_complete"),
        "counts_as_system_publication": False,
    }


def observed_workers(health: Any) -> bool | None:
    value = health.get("workers_started") if isinstance(health, dict) else None
    return value if isinstance(value, bool) else None


def readonly_runtime_ok(health: Any, ui_ok: bool) -> bool:
    return bool(
        isinstance(health, dict) and health.get("status") == "ok"
        and health.get("mode") == "research_only"
        and health.get("live_trading") is False
        and observed_workers(health) is False and ui_ok
    )
