"""Bounded collector lifecycle without provider, account or broker access."""
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from trading.research import index_observation as runner


class Clock:
    def __init__(self, value="2026-10-15T10:00:00+08:00"):
        self.value = datetime.fromisoformat(value)
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class Response:
    def __init__(self, raw, status=200):
        self.raw, self.status_code = raw, status
        self.headers = {"Content-Type": "application/javascript; charset=GB18030"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield self.raw


def calendar(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}), encoding="utf-8")
    return path


def raw_at(observed, *, kind=None):
    when = observed - timedelta(seconds=1)
    if kind == "stale":
        when -= timedelta(days=1)
    elif kind == "old_time":
        when -= timedelta(seconds=121)
    elif kind == "future":
        when += timedelta(seconds=10)
    elif kind == "near_future":
        when += timedelta(seconds=2)
    lines = []
    for index, (symbol, name) in enumerate(runner.TARGETS.items()):
        if kind == "missing" and index == 2:
            continue
        fields = ["0"] * 33
        for field, value in {0: name, 1: "10.0011", 2: "9.8001", 3: "10.1234", 4: "10.2345",
                             5: "9.9811", 8: "100", 9: "1000.123", 30: when.strftime("%Y-%m-%d"),
                             31: when.strftime("%H:%M:%S"), 32: "02" if kind == "halted" else "00"}.items():
            fields[field] = value
        lines.append(f'var hq_str_{symbol}="' + ",".join(fields) + '";')
    return ("\n".join(lines) + ("alert(1);" if kind == "code" else "")).encode("gb18030")


def fake_get(clock, *, fail_second=False, kind=None):
    calls = []
    def get(endpoint, **kwargs):
        calls.append((clock.now(), endpoint, kwargs))
        assert endpoint == runner.ENDPOINT and kwargs["allow_redirects"] is False
        return Response(b"" if fail_second and len(calls) == 2 else raw_at(clock.now(), kind=kind),
                        302 if fail_second and len(calls) == 2 else 200)
    return get, calls


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_successful_watch_is_minute_spaced_capped_and_keeps_distinct_source_records(tmp_path):
    clock = Clock()
    get, calls = fake_get(clock)
    folder, job = runner.run(calendar=calendar(tmp_path), data_root=tmp_path / "data", watch=True,
                             max_observations=2, _clock=clock.now, _sleep=clock.sleep, _get=get)
    assert len(calls) == 2 and (calls[1][0] - calls[0][0]).total_seconds() == 60
    assert job["status"] == "completed_max_observations" and job["attempts"] == job["successes"] == 2
    receipts = sorted((folder / "observations").glob("*/receipt.json"))
    assert len(receipts) == 2 and all((p.parent / "quote_response.raw").is_file() for p in receipts)
    first = read(receipts[0])
    assert first["quotes"][0]["last_index_points"] == "10.1234"
    assert first["quotes"][0]["source_quote_time"] != first["response_received_at"]
    assert first["execution_admitted"] is first["selection_enabled"] is False
    latest = read(folder.parent / "latest.json")
    assert latest["attempt_receipt"] == str(receipts[1]) and latest["attempt_status"] == runner.SUCCESS
    assert job["process_identity_status"] in {"observed_os_process_start", "identity_unverified"}


def test_failed_second_attempt_replaces_success_pointer_and_stops_without_retry(tmp_path):
    clock = Clock()
    get, calls = fake_get(clock, fail_second=True)
    folder, job = runner.run(calendar=calendar(tmp_path), data_root=tmp_path / "data", watch=True,
                             max_observations=90, _clock=clock.now, _sleep=clock.sleep, _get=get)
    assert len(calls) == 2 and job["status"] == "failed_stopped_no_retry" and job["successes"] == 1
    latest = read(folder.parent / "latest.json")
    assert latest["attempt_receipt"] == job["latest_attempt_receipt"] != job["last_success_receipt"]
    assert read(Path(latest["attempt_receipt"]))["http_status"] == 302
    assert latest["attempt_status"] == "unavailable_single_attempt_stopped"


@pytest.mark.parametrize("kind", ["missing", "stale", "old_time", "future", "near_future", "halted", "code"])
def test_incomplete_stale_nonregular_or_invalid_quote_stops_at_first_request(tmp_path, kind):
    clock = Clock()
    get, calls = fake_get(clock, kind=kind)
    _, job = runner.run(calendar=calendar(tmp_path), data_root=tmp_path / "data", watch=True,
                        max_observations=90, _clock=clock.now, _sleep=clock.sleep, _get=get)
    assert len(calls) == 1 and job["status"] == "failed_stopped_no_retry" and job["successes"] == 0
    assert clock.sleeps == []


@pytest.mark.parametrize("value", ["2026-10-15T09:29:59+08:00", "2026-10-15T11:30:00+08:00",
    "2026-10-15T12:59:59+08:00", "2026-10-15T15:00:00+08:00", "2026-10-17T10:00:00+08:00",
    "2026-10-01T10:00:00+08:00", "2027-10-15T10:00:00+08:00"])
def test_outside_current_pinned_session_performs_no_network_and_points_to_blocked_attempt(tmp_path, value):
    clock = Clock(value)
    get, calls = fake_get(clock)
    folder, job = runner.run(calendar=calendar(tmp_path), data_root=tmp_path / "data", watch=True,
                             max_observations=90, _clock=clock.now, _sleep=clock.sleep, _get=get)
    assert calls == [] and clock.sleeps == [] and job["status"] == "blocked_outside_trading_session"
    pointer = read(folder.parent / "latest.json")
    assert pointer["attempt_status"] == "blocked_outside_trading_session"
    assert read(Path(pointer["attempt_receipt"]))["quote_request_count"] == 0


def test_watch_stops_at_morning_boundary_and_never_waits_for_afternoon(tmp_path):
    clock = Clock("2026-10-15T11:29:40+08:00")
    get, calls = fake_get(clock)
    _, job = runner.run(calendar=calendar(tmp_path), data_root=tmp_path / "data", watch=True,
                        max_observations=90, _clock=clock.now, _sleep=clock.sleep, _get=get)
    assert len(calls) == 1 and job["status"] == "completed_session_boundary"
    assert clock.now().isoformat() == "2026-10-15T11:30:00+08:00" and sum(clock.sleeps) == 20


@pytest.mark.parametrize("kwargs", [{"interval_seconds": 59}, {"max_observations": 121},
                                   {"max_observations": 2}])
def test_bad_bounds_are_rejected_before_creating_job_or_network(tmp_path, kwargs):
    with pytest.raises(ValueError):
        runner.run(data_root=tmp_path / "data", **kwargs)
    assert not (tmp_path / "data").exists()


def test_busy_observation_root_rejects_before_job_request_or_latest_update(tmp_path):
    root = tmp_path / "data" / "research" / "index_observation"
    root.mkdir(parents=True)
    pointer = root / "latest.json"
    original = b'{"existing":"latest attempt must stay byte-identical"}'
    pointer.write_bytes(original)
    clock = Clock()
    get, calls = fake_get(clock)
    with runner._collector_lock(root):
        with pytest.raises(RuntimeError, match="collector busy"):
            runner.run(calendar=calendar(tmp_path), data_root=tmp_path / "data", watch=True,
                       max_observations=90, _clock=clock.now, _sleep=clock.sleep, _get=get)
    assert calls == [] and clock.sleeps == [] and pointer.read_bytes() == original
    assert sorted(path.name for path in root.iterdir()) == [".collector.lock", "latest.json"]
