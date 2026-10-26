from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research.exchange_calendar import adjacent_d0_d1_sessions, next_exchange_session


def _calendar(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "cn_a_share_exchange_calendar_v1",
                "calendar_id": "fixture_2026",
                "year": 2026,
                "closed_ranges": [],
                "open_sessions": [],
            }
        ),
        encoding="utf-8",
    )


def test_year_scoped_calendar_does_not_invent_next_year_session(tmp_path: Path):
    path = tmp_path / "calendar.json"
    _calendar(path)

    session, evidence = next_exchange_session("2026-12-31", path)

    assert session is None
    assert evidence["status"] == "calendar_coverage_exhausted"
    assert evidence["calendar_year"] == "2026"


def test_year_scoped_calendar_resolves_in_year(tmp_path: Path):
    path = tmp_path / "calendar.json"
    _calendar(path)

    session, evidence = next_exchange_session("2026-12-30", path)

    assert session == "2026-12-31"
    assert evidence["status"] == "resolved_authoritative_static_calendar"


def test_adjacent_d0_d1_sessions_resolve_both_days_from_pinned_calendar(tmp_path: Path):
    path = tmp_path / "calendar.json"
    _calendar(path)

    execution, settlement, evidence = adjacent_d0_d1_sessions("2026-10-26", path)

    assert (execution, settlement) == ("2026-10-27", "2026-10-28")
    assert evidence["status"] == "resolved_authoritative_static_calendar"
    assert evidence["execution"]["calendar_id"] == "fixture_2026"


def test_adjacent_d0_d1_sessions_refuses_missing_calendar(tmp_path: Path):
    with pytest.raises(ValueError, match="calendar_artifact_unavailable"):
        adjacent_d0_d1_sessions("2026-10-26", tmp_path / "missing.json")
