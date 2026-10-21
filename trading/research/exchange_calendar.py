"""Pinned exchange-calendar helpers for offline paper workflows."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..settings import DATA_ROOT


DEFAULT_CALENDAR_PATH = (
    DATA_ROOT / "research" / "calendar" / "cn_a_share_2026_sse_v1.json"
)


def _parse_day(value: object) -> date:
    return date.fromisoformat(str(value))


def load_calendar(path: Path | None = None) -> dict[str, Any] | None:
    """Load a versioned local calendar without contacting a data provider."""
    calendar_path = path or DEFAULT_CALENDAR_PATH
    if not calendar_path.is_file():
        return None
    try:
        payload = json.loads(calendar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema") != "cn_a_share_exchange_calendar_v1":
        return None
    return payload


def _closed_days(payload: dict[str, Any]) -> set[date]:
    closed: set[date] = set()
    for interval in payload.get("closed_ranges", []):
        start_text, end_text = str(interval).split("/", 1)
        current = _parse_day(start_text)
        end = _parse_day(end_text)
        while current <= end:
            closed.add(current)
            current += timedelta(days=1)
    return closed


def next_exchange_session(
    execution_day: str,
    calendar_path: Path | None = None,
) -> tuple[str | None, dict[str, str]]:
    """Return the next pinned session and evidence metadata.

    Unsupported or missing calendars stay unresolved; there is no weekday
    fallback because a paper T+1 handoff must not invent a session.
    """
    try:
        day = _parse_day(execution_day)
    except (TypeError, ValueError):
        return None, {"status": "invalid_execution_day"}

    payload = load_calendar(calendar_path)
    if payload is None:
        return None, {"status": "calendar_artifact_unavailable"}
    if int(payload.get("year", -1)) != day.year:
        return None, {
            "status": "calendar_year_not_supported",
            "calendar_id": str(payload.get("calendar_id", "")),
        }

    closed = _closed_days(payload)
    open_overrides = {_parse_day(value) for value in payload.get("open_sessions", [])}
    current = day + timedelta(days=1)
    for _ in range(370):
        if current in open_overrides or (
            current.weekday() < 5 and current not in closed
        ):
            return current.isoformat(), {
                "status": "resolved_authoritative_static_calendar",
                "calendar_id": str(payload.get("calendar_id", "")),
                "calendar_source_url": str(payload.get("source_url", "")),
                "calendar_source_published": str(
                    payload.get("source_published", "")
                ),
            }
        current += timedelta(days=1)
    return None, {
        "status": "calendar_search_exhausted",
        "calendar_id": str(payload.get("calendar_id", "")),
    }
