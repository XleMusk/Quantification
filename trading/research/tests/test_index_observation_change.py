"""Saved proxy-window calculations and clock boundaries; no market requests."""
from datetime import datetime
import json

import pytest

from trading.research.index_observation_change import FALSE_FLAGS, TARGETS, index_observation_change


def stamp(clock):
    return "2026-10-15T" + clock + "+08:00"


def flags():
    return {**{key: False for key in FALSE_FLAGS}, "no_account_access": True}


def put(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def fixture(tmp_path, *, slot="morning"):
    root = tmp_path / "research/index_observation"
    job = root / "job1"
    job_path = job / "job_receipt.json"
    put(job_path, {"schema": "index_observation_session_job_v1", "job_id": "job1", **flags(),
        "session_day": "2026-10-15", "session_name": slot,
        "session_start": stamp("09:30:00" if slot == "morning" else "13:00:00"),
        "stop_at": stamp("11:30:00" if slot == "morning" else "15:00:00")})
    return root, job, job_path


def observation(job, clock, *, received=None, last=(100, 100, 100), name=None):
    path = job / "observations" / (name or clock.replace(":", "")) / "receipt.json"
    record = {"schema": "sina_fixed_three_index_quote_observation_v1", "job_id": "job1", **flags(),
        "status": "complete_current_fixed_index_proxy_observation", "source_scope": "three_fixed_index_proxies",
        "started_at": stamp(received or clock), "response_received_at": stamp(received or clock),
        "http_status": 200, "complete_target_coverage": True, "returned_target_count": 3,
        "current_quote_count": 3, "returned_targets": list(TARGETS), "missing_targets": [],
        "quotes": [{"symbol": symbol, "source_name": label, "source_status": "00",
            "status": "same_day_provider_time_within_120_seconds", "fresh_at_observation": True,
            "source_quote_time": stamp(clock), "last_index_points": str(value),
            "previous_close_index_points": "100"}
            for (symbol, label), value in zip(TARGETS.items(), last)]}
    put(path, record)
    return path, record


def locate(root, job_path, latest):
    put(root / "latest.json", {"schema": "index_observation_latest_v1", "job_id": "job1", **flags(),
        "job_receipt": str(job_path), "attempt_receipt": str(latest)})


def calculate(tmp_path, clock="10:10:05"):
    return index_observation_change(tmp_path, now=datetime.fromisoformat(stamp(clock)))


def test_common_window_precise_units_closest_baseline_and_read_only(tmp_path):
    root, job, job_path = fixture(tmp_path)
    observation(job, "10:03:00", last=(98, 98, 98))
    observation(job, "10:04:00", last=(99, 99, 99))
    baseline, _ = observation(job, "10:05:00")
    # Too recent is not used, even though it is a valid complete observation.
    observation(job, "10:06:00", last=(101, 101, 101))
    latest, _ = observation(job, "10:10:00", last=(101, 99, 100))
    locate(root, job_path, latest)
    before = {path: path.read_bytes() for path in root.rglob("*.json")}
    view = calculate(tmp_path)
    assert view["status"] == "current_same_slot_index_change"
    assert view["baseline_source_file"] == str(baseline.resolve())
    assert [row["window_span_seconds"] for row in view["indices"]] == [300, 300, 300]
    assert [row["window_return_bp"] for row in view["indices"]] == [100, -100, 0]
    assert [row["day_change_delta_pp"] for row in view["indices"]] == [1, -1, 0]
    assert view["summary"]["index_proxy_positive_count"] == view["summary"]["index_proxy_negative_count"] == 1
    assert view["summary"]["tech_minus_sh_window_return_bp"] == {"sz399006": -200, "sh000688": -100}
    assert view["current_hint"] == view["summary"]
    assert view["execution_admitted"] is view["provider_contacted"] is False
    assert before == {path: path.read_bytes() for path in root.rglob("*.json")}


def test_missing_baseline_does_not_interpolate_or_compare_seven_plus_minutes(tmp_path):
    root, job, job_path = fixture(tmp_path)
    observation(job, "10:02:59")
    observation(job, "10:06:00")
    latest, _ = observation(job, "10:10:00")
    locate(root, job_path, latest)
    view = calculate(tmp_path)
    assert view["status"] == "unknown_no_common_300_to_420_second_baseline"
    assert view["indices"] == [] and view["current_hint"] is None


def test_all_three_source_spans_must_fit_not_only_mean(tmp_path):
    root, job, job_path = fixture(tmp_path)
    baseline, record = observation(job, "10:05:00", received="10:05:01")
    record["quotes"][1]["source_quote_time"] = stamp("10:05:01")
    put(baseline, record)
    latest, _ = observation(job, "10:10:00")
    locate(root, job_path, latest)
    assert calculate(tmp_path)["status"] == "unknown_no_common_300_to_420_second_baseline"


def test_same_distance_tie_uses_latest_received_batch(tmp_path):
    root, job, job_path = fixture(tmp_path)
    observation(job, "10:05:00", received="10:05:01", name="first")
    newer, _ = observation(job, "10:05:00", received="10:05:02", name="second")
    latest, _ = observation(job, "10:10:00")
    locate(root, job_path, latest)
    assert calculate(tmp_path)["baseline_source_file"] == str(newer.resolve())


def test_latest_failed_never_falls_back_to_saved_success(tmp_path):
    root, job, job_path = fixture(tmp_path)
    observation(job, "10:05:00")
    observation(job, "10:10:00")
    latest, record = observation(job, "10:11:00")
    record.update(status="unavailable_single_attempt_stopped", error="HTTP502")
    put(latest, record)
    locate(root, job_path, latest)
    view = calculate(tmp_path, "10:11:05")
    assert view["status"] == "unavailable_latest_attempt" and view["error"] == "HTTP502"
    assert view["indices"] == [] and view["current_hint"] is None


@pytest.mark.parametrize("clock", ["10:12:01", "13:05:00", "11:30:00"])
def test_stale_or_different_read_slot_is_historical_only(tmp_path, clock):
    root, job, job_path = fixture(tmp_path)
    observation(job, "10:05:00")
    latest, _ = observation(job, "10:10:00")
    locate(root, job_path, latest)
    view = calculate(tmp_path, clock)
    assert view["status"] == "historical_same_slot_index_change_not_current"
    assert view["indices"] and view["current_hint"] is None
    assert not any(row["current_at_read"] for row in view["indices"])


def test_baseline_cannot_cross_lunch_slot(tmp_path):
    root, job, job_path = fixture(tmp_path, slot="afternoon")
    observation(job, "11:29:00")
    latest, _ = observation(job, "13:05:00")
    locate(root, job_path, latest)
    view = calculate(tmp_path, "13:05:05")
    assert view["status"] == "unavailable_invalid_saved_observation_change"
    assert view["indices"] == [] and view["current_hint"] is None


@pytest.mark.parametrize("change", ["future_response", "future_source", "wrong_job", "unsafe_flag", "nonregular", "previous_close_changed"])
def test_invalid_saved_clock_identity_or_boundary_is_rejected(tmp_path, change):
    root, job, job_path = fixture(tmp_path)
    baseline, old = observation(job, "10:05:00")
    latest, record = observation(job, "10:10:00")
    if change == "future_response":
        record["response_received_at"] = stamp("10:10:06")
    elif change == "future_source":
        record["quotes"][0]["source_quote_time"] = stamp("10:10:01")
    elif change == "wrong_job":
        record["job_id"] = "job2"
    elif change == "unsafe_flag":
        old["selection_enabled"] = True
        put(baseline, old)
    elif change == "nonregular":
        record["quotes"][0]["source_status"] = "01"
    else:
        record["quotes"][0]["previous_close_index_points"] = "101"
    put(latest, record)
    locate(root, job_path, latest)
    view = calculate(tmp_path)
    assert view["status"] == "unavailable_invalid_saved_observation_change"
    assert view["indices"] == [] and view["current_hint"] is None


def test_locator_path_escape_is_rejected_before_reading_outside_job(tmp_path):
    root, job, job_path = fixture(tmp_path)
    outside = tmp_path / "outside/receipt.json"
    locate(root, job_path, outside)
    view = calculate(tmp_path)
    assert view["status"] == "unavailable_invalid_saved_observation_change"
    assert "escapes" in view["error"]


def test_missing_locator_is_explicit_without_creating_files(tmp_path):
    view = calculate(tmp_path)
    assert view["status"] == "missing" and list(tmp_path.iterdir()) == []


def test_expected_latest_guard_keeps_primary_table_and_change_endpoint_same_batch(tmp_path):
    root, job, job_path = fixture(tmp_path)
    older, _ = observation(job, "10:05:00")
    latest, _ = observation(job, "10:10:00")
    locate(root, job_path, latest)
    view = index_observation_change(tmp_path, now=datetime.fromisoformat(stamp("10:10:05")),
                                    expected_latest_receipt=older)
    assert view["status"] == "unavailable_locator_changed_during_read"
    assert view["indices"] == [] and view["current_hint"] is None
    matched = index_observation_change(tmp_path, now=datetime.fromisoformat(stamp("10:10:05")),
                                      expected_latest_receipt=latest)
    assert matched["status"] == "current_same_slot_index_change"
