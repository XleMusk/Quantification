"""No-network tests of the sealed-cohort collector and its source-time boundary."""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

from trading.research import leader_cohort_observation as r


class Clock:
    def __init__(self, value="2026-10-15T13:01:00+08:00"):
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


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def fixture(tmp_path, monkeypatch):
    data = tmp_path / "data"
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                                    "closed_ranges": [], "open_sessions": []}), encoding="utf8")
    members = [{"symbol": f"{i:06d}.SZSE", "name": f"股票{i}", "labels": [{"id": "trend_leader"}]}
               for i in range(1, 591)]
    registry = {"registered_at": "2026-10-15T11:54:13+08:00", "members": members,
                "binding": {"calendar_sha256": sha256(calendar.read_bytes()).hexdigest()}}
    registry_file = data / r.REGISTRY_RELATIVE
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text(json.dumps(registry), encoding="utf8")
    seal = sha256(registry_file.read_bytes()).hexdigest()
    def sealed(path, actual):
        assert path == registry_file and actual >= datetime.fromisoformat(registry["registered_at"])
        assert sha256(path.read_bytes()).hexdigest() == seal
        return registry, seal, [], {}
    monkeypatch.setattr(r, "_registry", sealed)
    contract = data / r.CONTRACT_RELATIVE
    contract.parent.mkdir(parents=True)
    contract.write_text(json.dumps({"schema": "sina_ordinary_SH_SZ_A_price_time_contract_assessment_v1",
                                   "created_at": "2026-10-15T12:41:39+08:00"}), encoding="utf8")
    return data, calendar, registry_file


def payload(provider_symbol, when, *, source_status="00", name="当前名字", empty=False, zero=False):
    if empty:
        return f'var hq_str_{provider_symbol}="";'
    fields = ["0"] * 34
    for index, value in {0: name, 1: "0.000" if zero else "10.001100", 2: "9.800100", 3: "10.123400",
                         4: "0.000" if zero else "10.234500", 5: "0.000" if zero else "9.981100",
                         8: "000100", 9: "001000.123000", 30: when.strftime("%Y-%m-%d"),
                         31: when.strftime("%H:%M:%S"), 32: source_status, 33: ""}.items():
        fields[index] = value
    return f'var hq_str_{provider_symbol}="' + ",".join(fields) + '";'


def transport(clock, *, kind=None, failure_call=None, advance=0):
    calls = []
    def get(endpoint, **kwargs):
        codes = endpoint.removeprefix(r.ENDPOINT).split(",")
        calls.append((clock.now(), codes, kwargs))
        assert kwargs["allow_redirects"] is False and len(codes) <= 100
        if failure_call == len(calls) and kind == "transport":
            raise OSError("transport failure")
        when = clock.now() - timedelta(seconds=1)
        lines = []
        for i, code in enumerate(codes):
            if kind == "member_unknowns" and i == 0:
                continue
            lines.append(payload(code, when - timedelta(seconds=125) if kind == "member_unknowns" and i == 3 else when,
                                 empty=kind == "member_unknowns" and i == 1,
                                 source_status="03" if kind == "member_unknowns" and i == 2 else "00",
                                 zero=kind == "member_unknowns" and i == 2))
        status = 200
        if failure_call == len(calls):
            if kind == "duplicate":
                lines.append(lines[0])
            elif kind == "unknown_symbol":
                lines.append(payload("sh600000", when))
            elif kind == "empty":
                lines = []
            elif kind == "non200":
                status = 302
        clock.value += timedelta(seconds=advance)
        return Response("\n".join(lines).encode("gb18030"), status)
    return get, calls


def run(tmp_path, monkeypatch, *, clock=None, **kwargs):
    data, calendar, registry = fixture(tmp_path, monkeypatch)
    clock = clock or Clock()
    get, calls = transport(clock, **{key: kwargs.pop(key) for key in tuple(kwargs) if key in {"kind", "failure_call", "advance"}})
    before = registry.read_bytes()
    folder, job = r.run(data_root=data, calendar=calendar, _clock=clock.now, _sleep=clock.sleep, _get=get, **kwargs)
    assert registry.read_bytes() == before
    return folder, job, calls, clock


def test_lunch_gate_is_before_registry_lock_mkdir_or_http(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                                    "closed_ranges": [], "open_sessions": []}), encoding="utf8")
    def forbidden(*args, **kwargs):
        pytest.fail("Lunch gate must precede registry/lock/network")
    monkeypatch.setattr(r, "_registry", forbidden)
    monkeypatch.setattr(r, "_collector_lock", forbidden)
    folder, job = r.run(calendar=calendar, data_root=tmp_path / "none", _clock=Clock("2026-10-15T12:55:00+08:00").now, _get=forbidden)
    assert folder is None and job["quote_request_count"] == 0 and job["status"].startswith("waiting_")
    assert not (tmp_path / "none").exists()


def test_single_keeps_590_in_six_serial_batches_and_preserves_decimal_source_strings(tmp_path, monkeypatch):
    folder, job, calls, _ = run(tmp_path, monkeypatch)
    record = read(Path(job["latest_attempt_receipt"]))
    assert job["status"] == "completed_single_observation"
    assert len(calls) == 6 and [len(call[1]) for call in calls] == [100] * 5 + [90]
    assert record["status"] == r.SUCCESS and record["all_batches_completed"] is True
    assert record["member_count"] == len(record["quotes"]) == record["current_quote_count"] == 590
    assert record["completed_batches"] == record["planned_batches"] == record["quote_request_count"] == 6
    quote = record["quotes"][0]
    assert quote["last_price_rmb"] == "10.123400" and quote["source_raw_volume"] == "000100"
    assert quote["source_raw_amount"] == "001000.123000" and quote["name_changed_from_registry"] is True
    assert quote["current_quote"] is True and quote["source_quote_time"] != quote["received_at"]
    assert quote["frozen_labels"] == ["trend_leader"]
    assert all(record[key] is value for key, value in r.FLAGS.items())
    assert read(folder.parent / "latest.json")["source_scope"] == r.SOURCE_SCOPE
    for batch in record["batches"]:
        assert sha256(Path(batch["raw_file"]).read_bytes()).hexdigest() == batch["raw_sha256"]


def test_missing_empty_halted_and_stale_are_member_unknowns_not_round_failure(tmp_path, monkeypatch):
    _, job, calls, _ = run(tmp_path, monkeypatch, clock=Clock("2026-10-15T13:05:00+08:00"), kind="member_unknowns")
    record = read(Path(job["latest_attempt_receipt"]))
    assert len(calls) == 6 and record["status"] == r.SUCCESS
    assert len(record["quotes"]) == 590 and record["unknown_quote_count"] == 24
    first = record["quotes"][:4]
    assert [quote["status"] for quote in first] == ["unknown_missing_assignment", "unknown_empty_payload",
        "unknown_nonregular_provider_status", "unknown_source_age_over_120_seconds"]
    assert first[2]["open_price_rmb"] == "0.000" and first[2]["current_quote"] is False


@pytest.mark.parametrize("kind", ["duplicate", "unknown_symbol", "empty", "non200", "transport"])
def test_whole_batch_failure_stops_following_requests_and_retains_unattempted_cohort(tmp_path, monkeypatch, kind):
    folder, job, calls, _ = run(tmp_path, monkeypatch, kind=kind, failure_call=2)
    record = read(Path(job["latest_attempt_receipt"]))
    assert job["status"] == "failed_stopped_no_retry" and len(calls) == 2
    assert record["status"] != r.SUCCESS and record["all_batches_completed"] is False
    assert len(record["quotes"]) == 590 and record["completed_batches"] == 1
    assert all(quote["status"] == "unknown_failed_batch" for quote in record["quotes"][100:200])
    assert all(quote["status"] == "unknown_not_attempted" for quote in record["quotes"][200:])
    assert read(folder.parent / "latest.json")["attempt_status"] == record["status"]
    if kind != "transport":
        assert record["batches"][-1]["raw_sha256"] and record["response_received_at"]


def test_slot_end_during_first_http_stops_remaining_batches(tmp_path, monkeypatch):
    _, job, calls, _ = run(tmp_path, monkeypatch, clock=Clock("2026-10-15T14:59:59+08:00"), advance=2)
    record = read(Path(job["latest_attempt_receipt"]))
    assert len(calls) == 1 and job["status"] == "failed_stopped_no_retry"
    assert record["completed_batches"] == 0 and record["current_quote_count"] == 0
    assert record["batches"][0]["raw_sha256"]


def test_watch_replaces_success_latest_with_failure_and_does_not_retry(tmp_path, monkeypatch):
    folder, job, calls, clock = run(tmp_path, monkeypatch, watch=True, max_observations=3,
                                   kind="transport", failure_call=7)
    assert len(calls) == 7 and (calls[6][0] - calls[0][0]).total_seconds() == 60
    assert job["attempts"] == 2 and job["successes"] == 1 and job["status"] == "failed_stopped_no_retry"
    latest = read(folder.parent / "latest.json")
    assert latest["attempt_receipt"] != job["last_success_receipt"]
    assert latest["attempt_status"] != r.SUCCESS and max(clock.sleeps) <= 30


def test_active_same_root_lock_prevents_second_worker_without_requests(tmp_path, monkeypatch):
    data, calendar, _ = fixture(tmp_path, monkeypatch)
    get, calls = transport(Clock())
    root = data / "research/leader_cohort_observation"
    with r._collector_lock(root):
        with pytest.raises(RuntimeError, match="busy"):
            r.run(data_root=data, calendar=calendar, _clock=Clock().now, _get=get)
    assert calls == [] and not (root / "latest.json").exists()


@pytest.mark.parametrize("stamp,reason", [("2026-10-15T11:29:59+08:00", "source_time_outside_current_slot"),
                                          ("2026-10-15T13:01:01+08:00", "future_source_time"),
                                          ("2026-10-14T13:00:59+08:00", "different_source_date")])
def test_provider_clock_controls_currentness_not_receive_time(stamp, reason):
    actual = Clock().now()
    session = r.trading_session({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                                "closed_ranges": [], "open_sessions": []}, actual)
    member = {"symbol": "000001.SZSE", "name": "注册名", "labels": []}
    raw = payload("sz000001", datetime.fromisoformat(stamp)).encode("gb18030")
    quotes, _ = r.parse_batch(raw, [member], actual, session=session,
                             registered_at=datetime.fromisoformat("2026-10-15T11:54:13+08:00"), batch_id=0)
    assert quotes[0]["unknown_reason"] == reason and quotes[0]["current_quote"] is False
