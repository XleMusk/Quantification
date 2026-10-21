from datetime import datetime
import json

from trading.research.index_quote_view import index_quote_observation


def save(root, **changes):
    source = {"schema": "sina_fixed_three_index_quote_observation_v1",
        "status": "complete_current_fixed_index_proxy_observation", "source_scope": "three_fixed_index_proxies",
        "all_A_breadth": False, "is_BrokerQuote": False, "selection_enabled": False, "execution_admitted": False,
        "response_received_at": "2026-10-15T10:07:41+08:00",
        "quotes": [{"symbol": s, "source_name": s, "source_quote_time": "2026-10-15T10:07:38+08:00",
                    "last_index_points": "98", "previous_close_index_points": "100",
                    "current_quote": True, "age_seconds": 3}
                   for s in ("sh000001", "sz399006", "sh000688")]}
    source.update(changes)
    path = root / "outputs/leader_environment_research/day_v1/sina_index_probe_v1/receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(source), encoding="utf-8")
    return path


def test_saved_fresh_flag_expires_and_bytes_stay_unchanged(tmp_path):
    path = save(tmp_path)
    before = path.read_bytes()
    fresh = index_quote_observation(tmp_path, now=datetime.fromisoformat("2026-10-15T10:08:00+08:00"))
    stale = index_quote_observation(tmp_path, now=datetime.fromisoformat("2026-10-15T10:10:00+08:00"))
    assert all(row["current_at_read"] for row in fresh["quotes"])
    assert not any(row["current_at_read"] for row in stale["quotes"])
    assert stale["status"] == "saved_index_observation_not_current"
    assert abs(stale["quotes"][0]["change_percent"] + 2) < 1e-10
    assert stale["provider_contacted"] is stale["execution_admitted"] is False
    assert before == path.read_bytes()


def test_partial_or_unsafe_saved_observation_is_unavailable(tmp_path):
    save(tmp_path, execution_admitted=True)
    result = index_quote_observation(tmp_path)
    assert result["status"] == "unavailable_invalid_saved_observation"
    assert result["quotes"] == []


def external(root, source, **source_changes):
    data = root / "data"
    scope = data / "research/index_observation"
    job = scope / "job1"
    attempt = job / "observations/attempt1/receipt.json"
    attempt.parent.mkdir(parents=True)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload.update(job_id="job1", **source_changes)
    attempt.write_text(json.dumps(payload), encoding="utf-8")
    job_path = job / "job_receipt.json"
    job_path.write_text(json.dumps({"schema": "index_observation_session_job_v1", "status": "running",
        "attempts": 1, "successes": 1, "execution_admitted": False, "selection_enabled": False}), encoding="utf-8")
    pointer = scope / "latest.json"
    pointer.write_text(json.dumps({"schema": "index_observation_latest_v1", "job_id": "job1",
        "job_receipt": str(job_path), "attempt_receipt": str(attempt),
        "execution_admitted": False, "selection_enabled": False}), encoding="utf-8")
    return data, pointer, attempt


def test_external_latest_attempt_precedes_old_probe_but_not_process_proof(tmp_path):
    legacy = save(tmp_path)
    data, _, attempt = external(tmp_path, legacy)
    result = index_quote_observation(tmp_path, data_root=data,
        now=datetime.fromisoformat("2026-10-15T10:08:00+08:00"))
    assert result["source_file"] == str(attempt.resolve())
    assert len(result["quotes"]) == 3
    assert result["collector"]["recorded_status"] == "running"
    assert result["collector"]["process"]["status"] == "identity_unverified"


def test_newest_failed_attempt_cannot_fall_back_to_successful_legacy(tmp_path):
    legacy = save(tmp_path)
    data, _, attempt = external(tmp_path, legacy, status="unavailable_single_attempt_stopped", error="HTTP 502")
    result = index_quote_observation(tmp_path, data_root=data,
        now=datetime.fromisoformat("2026-10-15T10:08:00+08:00"))
    assert result["source_file"] == str(attempt.resolve())
    assert result["status"] == "unavailable_latest_index_observation"
    assert result["observation_attempt_status"] == "unavailable_single_attempt_stopped"
    assert result["error"] == "HTTP 502" and result["quotes"] == []


def test_invalid_locator_does_not_read_a_path_outside_declared_data_scope(tmp_path):
    legacy = save(tmp_path)
    data, pointer, _ = external(tmp_path, legacy)
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    payload["attempt_receipt"] = str(legacy)
    pointer.write_text(json.dumps(payload), encoding="utf-8")
    result = index_quote_observation(tmp_path, data_root=data)
    assert result["status"] == "unavailable_invalid_saved_observation"
    assert result["quotes"] == []
