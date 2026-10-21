"""Bounded wide-roster loops reuse prepared sources; no real HTTP or sleeps."""
from datetime import timedelta
from pathlib import Path

import pytest

from trading.research import prior_dated_market_quote_snapshot as r
from trading.research.tests.test_prior_dated_market_quote_snapshot import Clock, fixture, transport, read


class WatchClock(Clock):
    def __init__(self, value="2026-10-15T13:05:00+08:00"):
        super().__init__(value)
        self.sleeps = []
        self.on_sleep = None

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep()
        self.value += timedelta(seconds=seconds)


def run(value, clock, *, get=None, **kwargs):
    calls = []
    if get is None:
        get, calls = transport(clock)
    folder, job = r.watch(package=value["package"], calendar=value["calendar"], data_root=value["data"],
                           _clock=clock.now, _sleep=clock.sleep, _get=get, **kwargs)
    return folder, job, calls


def test_two_rounds_have_one_strict_source_preparation_and_180_second_start_spacing(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    clock = WatchClock()
    get, calls = transport(clock, advance=1)
    folder, job, _ = run(value, clock, get=get, max_observations=2)
    assert len(value["loads"]) == 1 and len(calls) == 6
    assert (calls[3][0] - calls[0][0]).total_seconds() == 180
    assert job["status"] == "completed_max_observations" and job["attempts"] == job["successes"] == 2
    assert job["strict_source_preparation_count"] == 1 and job["quote_request_count"] == 6
    assert max(clock.sleeps) <= 30 and job["next_scheduled_at"] is None
    frozen = read(Path(job["prepared_source_file"]))
    assert len(frozen["targets"]) == frozen["target_count"] == 205
    assert r.digest(job["prepared_source_file"]) == job["prepared_source_sha256"]
    records = [read(Path(path)) for path in job["attempt_receipts"]]
    assert all(record["status"] == r.SUCCESS and record["watch"] is True for record in records)
    assert all(record["watch_job_file"] == str(folder / "job_receipt.json") for record in records)
    assert all(record["prepared_source_sha256"] == job["prepared_source_sha256"] for record in records)
    assert records[0]["targets_sha256"] != records[1]["targets_sha256"]  # Real round freeze clocks differ.
    root = value["out"].parent
    pointer = read(root / "latest.json")
    assert pointer["receipt_file"] == job["attempt_receipts"][-1] and pointer["schema"] == r.LATEST_SCHEMA
    job_pointer = read(root / "jobs/latest.json")
    assert job_pointer["job_receipt"] == str(folder / "job_receipt.json") and job_pointer["schema"] == r.JOB_LATEST_SCHEMA


@pytest.mark.parametrize("kind", ["transport", "duplicate"])
def test_first_failed_round_stops_watch_and_latest_points_to_failure(tmp_path, monkeypatch, kind):
    value = fixture(tmp_path, monkeypatch)
    clock = WatchClock()
    get, calls = transport(clock, kind=kind, fail_call=4)
    _, job, _ = run(value, clock, get=get, max_observations=5)
    assert len(calls) == 4 and len(value["loads"]) == 1
    assert job["status"] == "failed_stopped_no_retry" and job["attempts"] == 2 and job["successes"] == 1
    pointer = read(value["out"].parent / "latest.json")
    assert pointer["receipt_file"] == job["latest_receipt"] != job["last_success_receipt"]
    failed = read(Path(pointer["receipt_file"]))
    assert failed["status"] != r.SUCCESS and failed["quote_request_count"] == 1
    assert failed["freshness_is_as_of_observation_only"] is True and job["freshness_limit_seconds"] == 120


def test_wait_to_afternoon_boundary_does_not_start_new_round_after_15(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    clock = WatchClock("2026-10-15T14:58:30+08:00")
    _, job, calls = run(value, clock, max_observations=30)
    assert len(calls) == 3 and job["successes"] == 1
    assert job["status"] == "completed_session_boundary" and clock.now().hour == 15 and clock.now().minute == 0
    assert sum(clock.sleeps) == 90 and max(clock.sleeps) <= 30


def test_slot_end_during_a_round_fails_it_without_following_batches(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    clock = WatchClock("2026-10-15T14:59:59+08:00")
    get, calls = transport(clock, advance=2)
    _, job, _ = run(value, clock, get=get, max_observations=30)
    assert len(calls) == 1 and job["status"] == "failed_stopped_no_retry" and not clock.sleeps


def test_watch_keeps_same_root_lock_while_waiting_and_does_not_disturb_one_shot(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    clock = WatchClock()
    checks = []
    def during_wait():
        if checks:
            return
        with pytest.raises(RuntimeError, match="busy"):
            r.run(package=value["package"], calendar=value["calendar"], out=value["out"],
                  data_root=value["data"], _clock=clock.now, _get=lambda *a, **k: pytest.fail("No competing HTTP"))
        checks.append(True)
    clock.on_sleep = during_wait
    _, job, calls = run(value, clock, max_observations=2)
    assert checks == [True] and len(calls) == 6 and job["successes"] == 2
    assert len(value["loads"]) == 1


def test_lunch_guard_precedes_source_lock_job_or_latest(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    clock = WatchClock("2026-10-15T12:59:59+08:00")
    monkeypatch.setattr(r, "_collector_lock", lambda *a: pytest.fail("No lunch lock or mkdir"))
    folder, job, calls = run(value, clock)
    assert folder is None and job["status"].startswith("waiting_") and job["quote_request_count"] == 0
    assert not calls and not value["loads"] and not value["out"].parent.exists()


@pytest.mark.parametrize("kwargs", [{"interval_seconds": 179}, {"interval_seconds": True},
                                     {"max_observations": 31}, {"max_observations": 0}])
def test_fixed_bounded_watch_parameters_are_not_weakened(tmp_path, kwargs):
    with pytest.raises(ValueError):
        r.watch(package=tmp_path / "absent", calendar=tmp_path / "absent_calendar", data_root=tmp_path, **kwargs)
