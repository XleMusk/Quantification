"""Saved-cohort windows choose by clocks and retain missing-member denominators."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

from trading.research import leader_cohort_observation_change as c
from trading.research import leader_cohort_observation as collector


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf8")


def stamp(value):
    return datetime.fromisoformat(value)


@pytest.fixture
def saved(tmp_path, monkeypatch):
    root = tmp_path / "data"
    job_dir = root / "research/leader_cohort_observation/job1"
    calendar_path = job_dir / "calendar.json"
    dump(calendar_path, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                         "closed_ranges": [], "open_sessions": []})
    cal_sha = sha256(calendar_path.read_bytes()).hexdigest()
    members = [{"symbol": f"{index:06d}.SZSE", "name": f"股票{index}",
                "labels": [{"id": "trend_leader" if index < 3 else "relay_continuation_leader"}]}
               for index in range(1, 5)]
    registry_file = root / collector.REGISTRY_RELATIVE
    registry = {"registered_at": "2026-10-15T09:00:00+08:00", "member_count": len(members),
                "members": members, "binding": {"calendar_sha256": cal_sha}}
    dump(registry_file, registry)
    registry_sha = sha256(registry_file.read_bytes()).hexdigest()
    monkeypatch.setattr(c, "_registry", lambda path, now: (deepcopy(registry), registry_sha, [], {}))
    common = {"job_id": "job1", "registry_file": str(registry_file), "registry_sha256": registry_sha,
              "registry_registered_at": registry["registered_at"], "member_count": len(members),
              "session_day": "2026-10-15", "session_name": "morning", "source_scope": collector.SOURCE_SCOPE,
              "session_start": "2026-10-15T09:30:00+08:00", "stop_at": "2026-10-15T11:30:00+08:00",
              "calendar_sha256": cal_sha, **collector.FLAGS}
    job = {**common, "schema": collector.JOB_SCHEMA, "started_at": "2026-10-15T09:50:00+08:00",
           "pinned_calendar_path": str(calendar_path), "planned_batches": 1}
    dump(job_dir / "job_receipt.json", job)

    def observation(name, received, *, prices=None, halted=(), empty=()):
        received = stamp(received)
        rows = []
        for index, member in enumerate(members):
            quote = collector._member(member, 0)
            if index not in empty:
                source = received - timedelta(seconds=1)
                quote.update(source_name=member["name"], source_status="03" if index in halted else "00",
                             source_quote_time=source.isoformat(), received_at=received.isoformat(),
                             open_price_rmb="10", previous_close_price_rmb="10", high_price_rmb="12", low_price_rmb="8",
                             last_price_rmb=str((prices or {}).get(index, 10)), current_quote=True, fresh_at_observation=True)
            rows.append(quote)
        record = {**common, "schema": collector.OBSERVATION_SCHEMA, "status": collector.SUCCESS,
                  "started_at": (received - timedelta(seconds=1)).isoformat(), "response_received_at": received.isoformat(),
                  "finished_at": (received + timedelta(seconds=1)).isoformat(), "all_batches_completed": True,
                  "planned_batches": 1, "completed_batches": 1, "quote_request_count": 1,
                  "units": {"prices": "CNY"}, "quotes": rows}
        path = job_dir / "observations" / name / "receipt.json"
        dump(path, record)
        return path, record

    baseline_path, baseline = observation("baseline", "2026-10-15T10:01:01+08:00")
    latest_path, latest = observation("latest", "2026-10-15T10:06:01+08:00", prices={0: 11, 1: 9}, halted=(3,))
    pointer = {"schema": collector.LATEST_SCHEMA, "source_scope": collector.SOURCE_SCOPE,
               "job_id": "job1", "registry_sha256": registry_sha, "attempt_receipt": str(latest_path),
               "job_receipt": str(job_dir / "job_receipt.json"), **collector.FLAGS}
    pointer_path = job_dir.parent / "latest.json"
    dump(pointer_path, pointer)
    return {"root": root, "job_dir": job_dir, "job": job, "registry": registry, "members": members,
            "registry_file": registry_file, "pointer": pointer, "pointer_path": pointer_path,
            "baseline_path": baseline_path, "baseline": baseline, "latest_path": latest_path, "latest": latest,
            "observation": observation}


def read(saved, now="2026-10-15T10:06:30+08:00", **kwargs):
    return c.leader_cohort_observation_change(saved["root"], now=now, **kwargs)


def test_pairing_preserves_all_members_unknown_bounds_and_all_eight_groups(saved):
    result = read(saved)
    assert result["status"] == "current_same_slot_cohort_change"
    assert result["baseline_receipt"] == str(saved["baseline_path"]) and result["window_response_span_seconds"] == 300
    summary = result["summary"]
    assert summary["member_count"] == len(result["quotes"]) == 4
    assert summary["paired_count"] == 3 and summary["unknown_count"] == 1
    assert summary["up_count"] == summary["down_count"] == summary["flat_count"] == 1
    assert summary["conditional_median_window_return_percent"] == 0
    assert summary["full_cohort_breadth"] is None and summary["breadth_bounds"] == [-.25, .25]
    assert result["quotes"][3]["reason"] == "latest_suspended_or_nonregular"
    assert len(result["groups"]) == 8
    empty = next(group for group in result["groups"] if group["member_count"] == 0)
    assert empty["summary"]["conditional_median_window_return_percent"] is None
    assert all(result[key] is value for key, value in collector.FLAGS.items())


def test_baseline_selected_by_batch_clocks_before_pair_coverage_or_returns(saved):
    better_coverage_path, _ = saved["observation"]("older_more_pairs", "2026-10-15T10:00:01+08:00", prices={0: 8, 1: 12})
    for quote in saved["baseline"]["quotes"]:
        quote.update(source_status=None, source_quote_time=None, received_at=None)
    dump(saved["baseline_path"], saved["baseline"])
    result = read(saved)
    assert result["baseline_receipt"] == str(saved["baseline_path"]) != str(better_coverage_path)
    assert result["status"] == "unknown_no_compatible_member_pairs"
    assert result["summary"]["paired_count"] == 0 and result["summary"]["unknown_count"] == 4
    assert result["summary"]["conditional_median_window_return_percent"] is None


def test_previous_close_change_is_unknown_member_not_new_baseline_or_zero(saved):
    saved["latest"]["quotes"][0]["previous_close_price_rmb"] = "10.000001"
    saved["latest"]["quotes"][1]["previous_close_price_rmb"] = "10.000000"
    dump(saved["latest_path"], saved["latest"])
    result = read(saved)
    assert result["quotes"][0]["reason"] == "previous_close_reference_changed"
    assert result["quotes"][0]["window_return_percent"] is None
    assert result["quotes"][1]["paired"] is True
    assert result["summary"]["paired_count"] == 2 and result["summary"]["unknown_count"] == 2


def test_each_member_needs_own_300_to_420_second_source_span(saved):
    saved["baseline"]["quotes"][0]["source_quote_time"] = "2026-10-15T10:00:30+08:00"
    saved["baseline"]["quotes"][1]["source_quote_time"] = "2026-10-15T10:00:59+08:00"
    saved["latest"]["quotes"][0]["source_quote_time"] = "2026-10-15T10:04:50+08:00"
    dump(saved["baseline_path"], saved["baseline"]); dump(saved["latest_path"], saved["latest"])
    result = read(saved)
    assert result["window_response_span_seconds"] == 300
    assert result["quotes"][0]["reason"] == "member_source_span_outside_300_to_420_seconds"
    assert result["quotes"][1]["window_span_seconds"] == 301
    assert result["summary"]["paired_count"] == 2


def test_expired_latest_retains_historical_window_without_current_hint(saved):
    result = read(saved, "2026-10-15T10:09:00+08:00")
    assert result["status"] == "historical_same_slot_cohort_change_not_current"
    assert result["summary"]["paired_count"] == 3 and result["current_hint"] is None


def test_partial_read_expiration_keeps_full_current_denominator(saved):
    saved["latest"]["quotes"][0]["source_quote_time"] = "2026-10-15T10:05:00+08:00"
    saved["baseline"]["quotes"][0]["source_quote_time"] = "2026-10-15T10:00:00+08:00"
    dump(saved["latest_path"], saved["latest"]); dump(saved["baseline_path"], saved["baseline"])
    result = read(saved, "2026-10-15T10:07:10+08:00")
    assert result["summary"]["paired_count"] == 3
    assert result["current_hint"]["member_count"] == 4 and result["current_hint"]["paired_count"] == 2
    assert result["current_hint"]["unknown_count"] == 2


def test_lunch_read_cannot_be_current_even_if_latest_source_age_is_small(saved):
    for key in ("baseline", "latest"):
        record = saved[key]
        delta = timedelta(hours=1, minutes=23, seconds=50)
        for field in ("started_at", "response_received_at", "finished_at"):
            record[field] = (stamp(record[field]) + delta).isoformat()
        for row in record["quotes"]:
            for field in ("source_quote_time", "received_at"):
                row[field] = (stamp(row[field]) + delta).isoformat()
        dump(saved[key + "_path"], record)
    result = read(saved, "2026-10-15T11:30:01+08:00")
    assert result["status"] == "historical_same_slot_cohort_change_not_current"
    assert result["summary"]["paired_count"] == 3 and result["current_hint"] is None


def test_failed_latest_does_not_roll_back_to_older_success(saved):
    saved["latest"].update(status="unavailable_single_attempt_stopped", all_batches_completed=False)
    dump(saved["latest_path"], saved["latest"])
    result = read(saved)
    assert result["status"] == "unavailable_latest_attempt"
    assert result["latest_receipt"] == str(saved["latest_path"])
    assert result["baseline_receipt"] is result["summary"] is result["current_hint"] is None


@pytest.mark.parametrize("change", ["future", "registration", "cross_slot"])
def test_member_endpoint_future_registration_and_cross_slot_are_unknown(saved, change):
    row = saved["baseline"]["quotes"][0]
    row["source_quote_time"] = {"future": "2026-10-15T10:01:02+08:00", "registration": "2026-10-15T08:59:59+08:00",
                                 "cross_slot": "2026-10-15T13:01:00+08:00"}[change]
    dump(saved["baseline_path"], saved["baseline"])
    result = read(saved)
    assert result["quotes"][0]["paired"] is False and result["quotes"][0]["window_return_percent"] is None
    assert result["summary"]["member_count"] == 4 and result["summary"]["unknown_count"] == 2


def test_baseline_from_other_job_or_morning_lunch_cannot_be_paired(saved):
    saved["baseline"]["session_name"] = "afternoon"
    dump(saved["baseline_path"], saved["baseline"])
    result = read(saved)
    assert result["status"] == "unavailable_invalid_saved_cohort_change" and result["current_hint"] is None


def test_no_exact_window_or_expected_locator_returns_unknown_not_interpolation(saved):
    saved["baseline_path"].unlink()
    saved["observation"]("too_early", "2026-10-15T09:58:59+08:00")
    result = read(saved)
    assert result["status"] == "unknown_no_common_300_to_420_second_baseline"
    assert len(result["quotes"]) == result["summary"]["unknown_count"] == 4
    assert result["current_hint"] is None
    mismatch = read(saved, expected_latest_receipt=Path("other/receipt.json"))
    assert mismatch["status"] == "unavailable_locator_changed_during_read"


def test_incomplete_technical_round_cannot_supply_baseline(saved):
    saved["baseline"]["completed_batches"] = 0
    dump(saved["baseline_path"], saved["baseline"])
    result = read(saved)
    assert result["status"] == "unavailable_invalid_saved_cohort_change"
    assert result["summary"] is result["current_hint"] is None
