"""Completed continuous-account display; no market replay or new returns."""
from copy import deepcopy
import csv

import pytest

from trading.research import native_lowvol_replay_summary as m
from trading.research.tests.test_native_lowvol_replay_summary import fixture_saved, write


def multiyear_saved(tmp_path):
    source, output, result = fixture_saved(tmp_path)
    registration = deepcopy(result["registration"])
    days = ["2025-12-30", "2025-12-31", "2026-01-02"]
    refs = []
    for year in (2025, 2026):
        calendar = tmp_path / "calendars" / f"{year}.json"
        write(calendar, {"year": year, "closed_ranges": [f"{year}-01-01/{year}-01-01"]})
        refs.append({"path": str(calendar), "sha256": m._sha(calendar), "year": year})
    registration.update(method_id=m.MULTIYEAR_SOURCE_SCHEMA, start=days[0], end=days[-1],
        account_year_boundary_reset=False, calendar_method={
            "method_id": "continuous_explicit_year_exchange_calendars_v1", "files": refs,
            "years": [2025, 2026], "missing_years_inferred": False})
    result.update(schema=m.MULTIYEAR_SOURCE_SCHEMA, registration=registration)
    state = m._json(source / "account.json")
    state["day"] = days[-1]
    for name, value in (("registration.json", registration), ("result.json", result), ("account.json", state)):
        write(source / name, value)
    with (source / "daily.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["date", "cash", "equity"])
        writer.writeheader()
        writer.writerows({"date": day, "cash": value, "equity": value}
            for day, value in zip(days, (990, 980, 970)))
    return source, output, result


def test_completed_multiyear_negative_account_is_readable_without_replay_or_large_json_on_read(tmp_path, monkeypatch):
    source, output, result = multiyear_saved(tmp_path)
    before = {path: path.read_bytes() for path in source.iterdir()}
    summary = m.build_summary(source, output)
    assert summary["source_schema"] == m.MULTIYEAR_SOURCE_SCHEMA
    assert (summary["start"], summary["end"], summary["sessions"]) == ("2025-12-30", "2026-01-02", 3)
    assert summary["metrics"] == result["metrics"] and summary["metrics"]["net_return"] < 0
    assert summary["calendar_method"] == result["registration"]["calendar_method"]
    assert summary["account_year_boundary_reset"] is False
    assert summary["new_cash_account_created"] is False
    assert (summary["reused_sessions"], summary["newly_completed_sessions"]) == (2, 1)
    assert all(path.read_bytes() == data for path, data in before.items())
    original, reads = m._json, []

    def small_only(path):
        assert path == output, "The saved reader must not parse the large replay JSON again"
        reads.append(path)
        return original(path)

    monkeypatch.setattr(m, "_json", small_only)
    assert m.load_summary(output) == summary
    assert reads == [output]


@pytest.mark.parametrize("defect", ["incomplete", "year_reset", "inferred_year"])
def test_unfinished_or_recreated_multiyear_account_cannot_be_displayed_as_completed(tmp_path, defect):
    source, output, result = multiyear_saved(tmp_path)
    if defect == "incomplete":
        result.update(status="incomplete", completed_sessions=2,
            error={"day": "2026-01-02", "message": "unknown saved reference"}, metrics=None)
    elif defect == "year_reset":
        result["registration"]["account_year_boundary_reset"] = True
    else:
        result["registration"]["calendar_method"]["missing_years_inferred"] = True
    write(source / "registration.json", result["registration"])
    write(source / "result.json", result)
    with pytest.raises(ValueError, match="completed unadmitted|continuous calendar"):
        m.build_summary(source, output)
    assert not output.exists()
