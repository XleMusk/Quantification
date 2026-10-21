"""Saved fixed-cohort reads recompute availability and retain every member."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from trading.research import leader_cohort_observation as collector
from trading.research import leader_cohort_quote_view as view

READ_NOW = "2026-10-15T10:00:30+08:00"


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def cohort_fixture(tmp_path, monkeypatch):
    root = tmp_path / "data"
    registry_file = root / "research/leader_identity_registry" / view.COHORT_ID / "registry.json"
    members = [{"symbol": "000001.SZSE", "name": "bankA", "labels": [{"id": "trend_leader"}]},
        {"symbol": "600000.SSE", "name": "bankB", "labels": [{"id": "trend_leader"}]},
        {"symbol": "600293.SSE", "name": "material", "labels": [{"id": "liquidity_capacity_proxy"}]}]
    calendar = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026, "closed_ranges": [], "open_sessions": []}
    job_dir = root / "research/leader_cohort_observation/job1"
    calendar_path = job_dir / "calendar.json"
    dump(calendar_path, calendar)
    registry = {"registered_at": "2026-10-15T09:00:00+08:00", "member_count": len(members),
        "source": {"day": "2026-10-14"}, "members": members,
        "binding": {"calendar_sha256": view.digest(calendar_path)}}
    dump(registry_file, registry)
    registry_sha = view.digest(registry_file)
    def registered(path, now):
        assert path == registry_file
        return deepcopy(registry), registry_sha, None, None
    monkeypatch.setattr(view, "_registry", registered)
    monkeypatch.setattr(view, "process_identity", lambda *a: {"status": "not_running"})
    monkeypatch.setattr(collector, "MEMBER_COUNT", len(members))
    common = {"job_id": "job1", "source_scope": collector.SOURCE_SCOPE,
        "registry_file": str(registry_file), "registry_sha256": registry_sha,
        "registry_registered_at": registry["registered_at"], "member_count": len(members),
        "session_day": "2026-10-15", "session_name": "morning",
        "session_start": "2026-10-15T09:30:00+08:00", "stop_at": "2026-10-15T11:30:00+08:00", **collector.FLAGS}
    job = {**common, "schema": collector.JOB_SCHEMA, "started_at": "2026-10-15T10:00:00+08:00",
        "pinned_calendar_path": str(calendar_path), "status": "completed_single_observation",
        "pid": 123, "process_started_at": "2026-10-15T01:00:00+00:00", "attempts": 1, "successes": 1,
        "planned_batches": 1}
    rows = []
    for i, member in enumerate(members):
        row = collector._member(member, 1)
        if i < 2:
            row.update(source_name=member["name"], source_status="00", source_date="2026-10-15", source_time="10:00:00",
                source_quote_time="2026-10-15T10:00:00+08:00", received_at="2026-10-15T10:00:01+08:00",
                open_price_rmb="10", previous_close_price_rmb="10", last_price_rmb="10.5" if i == 0 else "9.8",
                high_price_rmb="11", low_price_rmb="9", status="same_day_provider_time_within_120_seconds",
                current_quote=True, fresh_at_observation=True, age_seconds=1, name_changed_from_registry=False)
        rows.append(row)
    source = {**common, "schema": collector.OBSERVATION_SCHEMA, "status": collector.SUCCESS,
        "started_at": "2026-10-15T10:00:00+08:00", "response_received_at": "2026-10-15T10:00:01+08:00",
        "finished_at": "2026-10-15T10:00:02+08:00", "quotes": rows,
        "planned_batches": 1, "completed_batches": 1, "quote_request_count": 1,
        "current_quote_count": 2, "unknown_quote_count": 1, "all_batches_completed": True, "units": {"prices": "CNY"}}
    source_path = job_dir / "observations/attempt1/receipt.json"
    job_path = job_dir / "job_receipt.json"
    pointer_path = root / "research/leader_cohort_observation/latest.json"
    pointer = {"schema": collector.LATEST_SCHEMA, "source_scope": collector.SOURCE_SCOPE,
        "job_id": "job1", "registry_sha256": registry_sha, "job_receipt": str(job_path),
        "attempt_receipt": str(source_path), **collector.FLAGS}
    dump(job_path, job); dump(source_path, source); dump(pointer_path, pointer)
    return {"root": root, "registry": registry, "registry_sha": registry_sha, "registry_file": registry_file,
        "members": members, "job": job, "job_path": job_path, "source": source, "source_path": source_path,
        "pointer": pointer, "pointer_path": pointer_path}


def read(value, now=READ_NOW):
    return view.leader_cohort_intraday_view(value["root"], now=now)


def test_full_cohort_unknown_bounds_and_explicit_conditional_statistics(cohort_fixture):
    result = read(cohort_fixture)
    summary = result["current_summary"]
    assert summary["member_count"] == 3 and summary["known_count"] == 2 and summary["unknown_count"] == 1
    assert summary["advance_count"] == summary["decline_count"] == 1 and summary["flat_count"] == 0
    assert summary["conditional_median_change_percent"] == pytest.approx(1.5)
    assert summary["full_cohort_breadth"] is None and summary["conditional_known_breadth"] == 0
    assert summary["breadth_bounds"] == pytest.approx([-1 / 3, 1 / 3])
    assert len(result["quotes"]) == result["registry"]["member_count"] == 3
    group = next(row for row in result["groups"] if row["id"] == "liquidity_capacity_proxy")
    assert group["member_count"] == group["current_summary"]["unknown_count"] == 1
    assert group["current_summary"]["conditional_median_change_percent"] is None
    assert group["current_summary"]["breadth_bounds"] == [-1, 1]


def test_saved_current_flags_do_not_survive_read_time_expiration(cohort_fixture):
    result = read(cohort_fixture, "2026-10-15T10:03:00+08:00")
    assert result["status"] == "saved_cohort_observation_not_current" and result["current_summary"] is None
    assert not any(row["current_at_read"] for row in result["quotes"])
    assert [row["read_status"] for row in result["quotes"][:2]] == ["stale", "stale"]
    assert result["observation_summary"]["known_count"] == 2


def test_fresh_at_morning_endpoint_cannot_remain_current_in_lunch_break(cohort_fixture):
    source = cohort_fixture["source"]
    source.update(started_at="2026-10-15T11:29:48+08:00", response_received_at="2026-10-15T11:29:50+08:00",
                  finished_at="2026-10-15T11:29:51+08:00")
    for row in source["quotes"][:2]:
        row.update(source_quote_time="2026-10-15T11:29:45+08:00", received_at="2026-10-15T11:29:50+08:00")
    dump(cohort_fixture["source_path"], source)
    result = read(cohort_fixture, "2026-10-15T11:30:01+08:00")
    assert not any(row["current_at_read"] for row in result["quotes"])
    assert all(row["read_status"] == "outside_current_slot" for row in result["quotes"][:2])
    assert all(row["age_seconds_at_read"] < 120 for row in result["quotes"][:2])
    assert result["observation_summary"]["known_count"] == 2


def test_suspended_03_with_last_equal_previous_is_unknown_not_flat(cohort_fixture):
    source = cohort_fixture["source"]
    source["quotes"][2].update(source_name="material", source_status="03", source_quote_time="2026-10-15T10:00:00+08:00",
        received_at="2026-10-15T10:00:01+08:00", open_price_rmb="0", high_price_rmb="0", low_price_rmb="0",
        last_price_rmb="3.93", previous_close_price_rmb="3.93", current_quote=True)
    dump(cohort_fixture["source_path"], source)
    result = read(cohort_fixture)
    assert result["quotes"][2]["read_status"] == "suspended_or_nonregular"
    assert result["quotes"][2]["day_change_percent"] is None
    assert result["current_summary"]["unknown_count"] == 1 and result["current_summary"]["flat_count"] == 0


def test_failed_latest_retains_remaining_unknowns_without_falling_back(cohort_fixture):
    old = cohort_fixture["source_path"].parent.parent / "older_success/receipt.json"
    dump(old, cohort_fixture["source"])
    source = deepcopy(cohort_fixture["source"])
    source.update(status="failed_stopped_no_retry", error="single batch failed", all_batches_completed=False, completed_batches=0)
    dump(cohort_fixture["source_path"], source)
    result = read(cohort_fixture)
    assert result["status"] == "unavailable_latest_cohort_attempt" and result["source_file"] == str(cohort_fixture["source_path"])
    assert result["current_summary"] is result["observation_summary"] is None and result["groups"] == []
    assert len(result["quotes"]) == 3 and not any(row["current_at_read"] for row in result["quotes"])
    assert result["unavailable_summary"]["known_count"] == 0 and result["unavailable_summary"]["unknown_count"] == 3
    assert result["unavailable_summary"]["breadth_bounds"] == [-1, 1]


@pytest.mark.parametrize("mutation", ["registry", "missing_member"])
def test_mixed_registry_or_missing_member_is_rejected(cohort_fixture, mutation):
    if mutation == "registry":
        pointer = cohort_fixture["pointer"]
        pointer["registry_sha256"] = "different frozen cohort"
        dump(cohort_fixture["pointer_path"], pointer)
    else:
        source = cohort_fixture["source"]
        source["quotes"] = source["quotes"][:2]
        dump(cohort_fixture["source_path"], source)
    with pytest.raises(ValueError):
        read(cohort_fixture)


def test_name_change_is_a_label_without_invented_ST_eligibility(cohort_fixture):
    source = cohort_fixture["source"]
    source["quotes"][0].update(source_name="*ST_changed_name", name_changed_from_registry=True)
    dump(cohort_fixture["source_path"], source)
    result = read(cohort_fixture)
    first = result["quotes"][0]
    assert first["name_changed_from_registry"] is True and first["registered_name"] == "bankA"
    assert first["source_name"] == "*ST_changed_name" and first["frozen_labels"] == ["trend_leader"]
    assert first["current_at_read"] is True and "ST_eligible" not in first
    assert result["selection_enabled"] is result["execution_admitted"] is result["registry_mutated"] is False


def test_recent_quote_from_before_registration_cannot_be_promoted_from_unknown(cohort_fixture, monkeypatch):
    value = cohort_fixture
    registry = deepcopy(value["registry"])
    registry["registered_at"] = "2026-10-15T09:59:50+08:00"
    dump(value["registry_file"], registry)
    registry_sha = view.digest(value["registry_file"])
    monkeypatch.setattr(view, "_registry", lambda *a: (registry, registry_sha, None, None))
    for key in ("job", "source"):
        value[key].update(registry_sha256=registry_sha, registry_registered_at=registry["registered_at"])
    value["pointer"]["registry_sha256"] = registry_sha
    value["source"]["quotes"][0].update(source_quote_time="2026-10-15T09:59:40+08:00", source_time="09:59:40",
        status="unknown_source_time_precedes_registration", unknown_reason="source_time_precedes_registration",
        current_quote=False, fresh_at_observation=False)
    for key in ("job", "source", "pointer"):
        dump(value[key + "_path"], value[key])
    result = read(value)
    first = result["quotes"][0]
    assert first["read_status"] == "source_time_precedes_registration"
    assert first["current_at_read"] is False and first["day_change_percent"] is None
    assert result["current_summary"]["known_count"] == 1 and result["current_summary"]["unknown_count"] == 2
    assert result["observation_summary"]["known_count"] == 1
