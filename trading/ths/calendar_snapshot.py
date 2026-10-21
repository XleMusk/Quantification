"""Small, source-declared exchange-calendar snapshot for THS account binding.

This snapshot deliberately contains dates only. It is never a quote, universe, or
price source and must not be used to manufacture a current trading signal.
"""
from datetime import date, timedelta
import csv
import json
from pathlib import Path

from .. import settings


SOURCE_REFS = [
    "https://www.sse.com.cn/disclosure/announcement/general/c/c_20260915_10832273.shtml",
    "https://investor.szse.cn/disclosure/notice/general/t20260917_622911.html",
]
SNAPSHOT_NAME = "ths_exchange_calendar_2026_0924_1231_v1"
START = date(2026, 9, 24)
END = date(2026, 12, 31)
HOLIDAYS = {date(2026, 9, 25), *{date(2026, 10, day) for day in range(1, 8)}}


def _calendar_days():
    days = []
    cursor = START
    while cursor <= END:
        if cursor.weekday() < 5 and cursor not in HOLIDAYS:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def _write_snapshot(root):
    root.mkdir(parents=True, exist_ok=True)
    calendar_path = root / "calendar.csv"
    manifest_path = root / "manifest.json"
    days = _calendar_days()
    if not calendar_path.exists():
        with calendar_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["date"])
            writer.writerows([[day] for day in days])
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps({
            "status": "ready",
            "dataset_type": "exchange_calendar_only",
            "market": "A-share SSE/SZSE",
            "start": days[0],
            "end": days[-1],
            "source_refs": SOURCE_REFS,
            "holiday_scope": "2026-09-25 and 2026-10-01 through 2026-10-07; weekends excluded",
            "quote_data": False,
            "price_data": False,
            "universe_data": False,
        }, ensure_ascii=True, indent=2), encoding="utf-8")
    return root


def dataset_for_day(day):
    """Return a date-only snapshot when the primary dataset lacks day+next day."""
    target = date.fromisoformat(day)
    root = settings.DATA_ROOT / "datasets" / SNAPSHOT_NAME
    days = _calendar_days()
    if target.isoformat() not in days or days.index(target.isoformat()) == len(days) - 1:
        raise ValueError(f"No source-declared exchange calendar covers {day} and its next session")
    return _write_snapshot(root)
