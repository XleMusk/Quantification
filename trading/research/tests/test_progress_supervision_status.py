"""The monitor reports dated evidence without acquiring or executing anything."""
from datetime import datetime

import pytest

from scripts.progress_supervision_status import (
    close_receipt_complete, observed_workers, progress_status,
    readonly_runtime_ok, scheduling_clock, settlement_summary,
)


CALENDAR = {
    "schema": "cn_a_share_exchange_calendar_v1", "calendar_id": "test_sse_2026",
    "year": 2026, "closed_ranges": ["2026-01-01/2026-01-03", "2026-10-01/2026-10-07"],
    "open_sessions": [],
}


def clock(stamp="2026-10-21T13:20:00+08:00"):
    return scheduling_clock(datetime.fromisoformat(stamp), CALENDAR)


def complete(day="2026-10-20"):
    return {
        "day": day, "status": "completed_bulk_close_cycle_research_only",
        "source_status": "complete_full_dated_close_inventory_not_admitted",
        "full_dated_roster_inventory_complete": True,
        "full_close_package": "saved/full_close",
    }


def system(day="2026-10-20"):
    return {"profile": {"last_source_day": day}}


@pytest.mark.parametrize("stamp,closed,due,phase", [
    ("2026-10-21T10:00:00+08:00", "2026-10-20", "2026-10-20", "preclose"),
    ("2026-10-21T15:00:00+08:00", "2026-10-21", "2026-10-20", "closed_waiting_supplier"),
    ("2026-10-21T18:29:59+08:00", "2026-10-21", "2026-10-20", "closed_waiting_supplier"),
    ("2026-10-21T18:30:00+08:00", "2026-10-21", "2026-10-21", "close_due"),
    ("2026-10-17T20:00:00+08:00", "2026-10-16", "2026-10-16", "non_trading_day"),
    ("2026-10-18T10:00:00+08:00", "2026-10-16", "2026-10-16", "non_trading_day"),
    ("2026-10-06T20:00:00+08:00", "2026-09-30", "2026-09-30", "non_trading_day"),
    ("2026-10-21T10:29:59+00:00", "2026-10-21", "2026-10-20", "closed_waiting_supplier"),
])
def test_exchange_clock_distinguishes_close_and_supplier_due(stamp, closed, due, phase):
    actual = clock(stamp)
    assert actual["calendar_status"] == "ready"
    assert (actual["latest_closed_day"], actual["latest_due_day"], actual["market_phase"]) == (closed, due, phase)


@pytest.mark.parametrize("stamp,error", [
    ("2027-01-05T19:00:00+08:00", "observation_outside_calendar_year"),
    ("2026-01-05T16:00:00+08:00", "previous_year_schedule_required"),
])
def test_missing_cross_year_calendar_is_unknown(stamp, error):
    actual = clock(stamp)
    assert actual["calendar_status"] == "unknown"
    assert actual["calendar_error"] == error
    assert actual["latest_due_day"] is None
    status = progress_status(actual, complete(), complete(), system(), {})
    assert status["data_status"] == "calendar_unknown"
    assert status["system_lagging_due_day"] is None


def test_naive_clock_and_invalid_calendar_do_not_guess():
    with pytest.raises(ValueError, match="aware"):
        scheduling_clock(datetime(2026, 10, 21), CALENDAR)
    for calendar in (None, {}, {**CALENDAR, "closed_ranges": ["bad"]}):
        actual = scheduling_clock(datetime.fromisoformat("2026-10-21T13:00:00+08:00"), calendar)
        assert actual["calendar_status"] == "unknown"
        assert actual["latest_due_day"] is None


def test_saved_old_complete_is_stale_and_exact_due_source_is_missing():
    actual = progress_status(clock(), complete("2026-10-19"), None, system("2026-10-19"),
        {"day": "2026-10-19", "status": "completed_saved_research_refresh"})
    assert actual["latest_receipt_stale"] is True
    assert actual["data_status"] == "due_close_missing"
    assert actual["paper_status"] == "due_source_unavailable"


@pytest.mark.parametrize("receipt,error,status", [
    ({"day": "2026-10-20", "status": "failed"}, None, "failed"),
    ({"day": "2026-10-20", "status": "running"}, None, "incomplete_or_unknown"),
    (complete("2026-10-19"), None, "wrong_day_or_invalid"),
    (None, "JSONDecodeError", "unreadable_or_unknown"),
])
def test_exact_due_failure_and_unknown_are_retained(receipt, error, status):
    actual = progress_status(clock(), receipt, receipt, system(), {}, due_receipt_error=error)
    assert actual["due_source_status"] == status
    assert actual["paper_status"] == "due_source_unavailable"


def test_source_ready_old_system_is_lagging_even_when_old_operation_completed():
    actual = progress_status(clock(), complete(), complete(), system("2026-10-19"),
        {"day": "2026-10-19", "status": "completed_saved_research_refresh"})
    assert actual["data_status"] == "due_close_complete_research_snapshot"
    assert actual["paper_status"] == "source_ready_system_lagging"
    assert actual["system_source_day"] == "2026-10-19"
    assert actual["system_lagging_due_day"] is True


@pytest.mark.parametrize("saved_status,expected", [
    ("completed_saved_research_refresh", "daily_routes_completed"),
    ("partial_research_routes", "daily_routes_partial"),
])
def test_current_route_completion_and_partial_are_not_conflated(saved_status, expected):
    actual = progress_status(clock(), complete(), complete(), system(),
        {"day": "2026-10-20", "status": saved_status})
    assert actual["paper_status"] == expected
    assert actual["system_lagging_due_day"] is False


def test_complete_source_requires_exact_dated_inventory_not_status_substring():
    assert close_receipt_complete(complete(), "2026-10-20")
    assert not close_receipt_complete(complete(), "2026-10-21")
    assert not close_receipt_complete({**complete(), "full_dated_roster_inventory_complete": None})
    assert not close_receipt_complete({**complete(), "status": "completed_something_else"})


def test_settled_accounts_do_not_advance_signal_cursor_or_prove_full_system():
    saved = {
        "schema": "system_daily_native_settlement_result_v1",
        "day": "2026-10-20", "source_day": "2026-10-19", "status": "completed_native_settlement",
        "policies": {"v1": {"status": "completed"}, "qualified": {"status": "completed"}},
        "signals_published": False, "system_source_cursor_advanced": False, "whole_system_daily_complete": False,
    }
    actual = settlement_summary(saved)
    assert actual["reported_account_execution_day"] == "2026-10-20"
    assert actual["counts_as_system_publication"] is False
    assert actual["whole_system_daily_complete"] is False
    progress = progress_status(clock(), complete(), complete(), system("2026-10-19"),
        {"day": "2026-10-19", "status": "partial_research_routes"})
    assert progress["paper_status"] == "source_ready_system_lagging"
    saved["policies"]["qualified"]["status"] = "failed"
    assert settlement_summary(saved)["reported_account_execution_day"] is None


@pytest.mark.parametrize("value,observed,ready", [(False, False, True), (True, True, False), (None, None, False), ("false", None, False)])
def test_workers_unknown_is_not_false_or_healthy(value, observed, ready):
    health = {"status": "ok", "mode": "research_only", "live_trading": False, "workers_started": value}
    assert observed_workers(health) is observed
    assert readonly_runtime_ok(health, True) is ready
    del health["workers_started"]
    assert observed_workers(health) is None
    assert readonly_runtime_ok(health, True) is False
