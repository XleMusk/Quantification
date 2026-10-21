"""Same-watch saved changes select response windows before member pairing."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trading.research import market_roster_observation_change as c
from trading.research import market_roster_quote_view as view
from trading.research import prior_dated_market_quote_snapshot as collector
from trading.research.tests.test_market_roster_watch import WatchClock
from trading.research.tests.test_prior_dated_market_quote_snapshot import fixture, transport, dump, read, execute


@pytest.fixture
def saved(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    clock = WatchClock()
    get, _ = transport(clock, kind="members")
    folder, job = collector.watch(package=value["package"], calendar=value["calendar"], data_root=value["data"],
                                   max_observations=3, _clock=clock.now, _sleep=clock.sleep, _get=get)
    paths = [Path(path) for path in job["attempt_receipts"]]
    value.update(job_folder=folder, job=job, paths=paths, baseline=read(paths[0]), latest=read(paths[-1]))
    return value


def change(value, now="2026-10-15T13:11:30+08:00", **kwargs):
    return c.market_roster_observation_change(value["data"], now=now, **kwargs)


def test_response_360_second_baseline_preserves_full_n_groups_old_labels_and_empty_groups(saved):
    result = change(saved)
    assert result["status"] == "current_same_watch_roster_change"
    assert result["baseline_receipt"] == str(saved["paths"][0])
    assert result["window_response_span_seconds"] == 360
    assert result["summary"]["member_count"] == 205 and result["summary"]["paired_count"] == 193
    assert result["summary"]["unknown_count"] == 12 and result["summary"]["flat_count"] == 193
    assert result["summary"]["breadth_bounds"] == pytest.approx([-12 / 205, 12 / 205])
    assert result["summary"]["full_cohort_breadth"] is None and result["summary"]["conditional_paired_breadth"] == 0
    assert len(result["groups"]) == 6 and len(result["archetype_groups"]) == 8
    assert [group["id"] for group in result["archetype_groups"]] == [id_ for id_, _ in c.ARCHETYPES]
    empty = [group for group in result["archetype_groups"] if group["member_count"] == 0]
    assert len(empty) == 7 and all(group["status"] == "unknown_no_prior_label_members" for group in empty)
    assert all(group["summary"]["conditional_median_window_return_percent"] is None for group in empty)
    assert all(group["summary"]["breadth_bounds"] is None for group in empty)
    assert "quotes" not in result and all(result[key] is expected for key, expected in collector.FLAGS.items())


def test_response_eligible_but_member_source_span_under_300_stays_unknown(saved):
    saved["latest"]["quotes"][4]["source_quote_time"] = "2026-10-15T13:09:30+08:00"
    dump(saved["paths"][-1], saved["latest"])
    result = change(saved)
    assert result["window_response_span_seconds"] == 360
    assert result["summary"]["paired_count"] == 192 and result["summary"]["unknown_count"] == 13
    assert result["unknown_reason_counts"]["member_source_span_outside_300_to_420_seconds"] == 1


def test_previous_close_revision_is_member_unknown_while_decimal_equivalence_stays_known(saved):
    saved["latest"]["quotes"][4]["previous_close_price_rmb"] = "9.800001"
    saved["latest"]["quotes"][5]["previous_close_price_rmb"] = "9.80000000"
    dump(saved["paths"][-1], saved["latest"])
    result = change(saved)
    assert result["summary"]["paired_count"] == 192 and result["summary"]["unknown_count"] == 13
    assert result["unknown_reason_counts"]["previous_close_reference_changed"] == 1


def test_clock_closest_baseline_wins_even_with_zero_pairs_not_older_high_coverage(saved):
    original = saved["paths"][0]
    folder = saved["out"].parent / "same_watch_closer_unknown_baseline"
    source, manifest = deepcopy(saved["baseline"]), read(original.parent / "targets.json")
    folder.mkdir()
    (folder / "calendar.json").write_bytes((original.parent / "calendar.json").read_bytes())
    for field in ("started_at", "response_received_at", "finished_at"):
        source[field] = (datetime.fromisoformat(source[field]) + timedelta(seconds=50)).isoformat()
    manifest["created_at"] = source["started_at"]
    manifest["calendar_file"] = source["calendar_file"] = str(folder / "calendar.json")
    dump(folder / "targets.json", manifest)
    source.update(observation_id=folder.name, targets_file=str(folder / "targets.json"), targets_sha256=c.digest(folder / "targets.json"))
    for quote in source["quotes"]:
        quote.update(source_status=None, source_quote_time=None, received_at=None)
    dump(folder / "receipt.json", source)
    saved["job"]["attempt_receipts"].append(str(folder / "receipt.json"))
    dump(saved["job_folder"] / "job_receipt.json", saved["job"])
    result = change(saved)
    assert result["baseline_receipt"] == str(folder / "receipt.json")
    assert result["window_response_span_seconds"] == 310
    assert result["status"] == "unknown_no_compatible_member_pairs"
    assert result["summary"]["paired_count"] == 0 and result["summary"]["unknown_count"] == 205
    assert result["current_hint"] is None


@pytest.mark.parametrize("status", ["started", "unavailable_single_attempt_stopped"])
def test_latest_started_or_failed_never_rolls_back_to_old_success(saved, status):
    source = deepcopy(saved["latest"])
    source["status"] = status
    dump(saved["paths"][-1], source)
    result = change(saved)
    assert result["status"] == "unavailable_latest_attempt" and result["latest_attempt_status"] == status
    assert result["baseline_receipt"] is result["summary"] is result["current_hint"] is None


def test_one_shot_has_no_same_watch_window(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch)
    execute(value)
    result = c.market_roster_observation_change(value["data"], now="2026-10-15T13:05:30+08:00")
    assert result["status"] == "no_same_watch_window" and result["summary"] is None


@pytest.mark.parametrize("problem", ["prepared_hash", "cross_slot", "target_context"])
def test_cross_job_prepared_or_slot_mismatch_never_creates_a_window(saved, problem):
    source = saved["baseline"]
    if problem == "prepared_hash":
        source["prepared_source_sha256"] = "0" * 64
    elif problem == "cross_slot":
        source["session_name"] = "morning"
    else:
        source["quotes"][4]["background_isST"] = "1"
    dump(saved["paths"][0], source)
    result = change(saved)
    assert result["status"] == "unavailable_invalid_saved_market_roster_change"
    assert result["summary"] is result["current_hint"] is None


def test_read_expiration_retains_only_historical_pairs_not_current(saved):
    result = change(saved, "2026-10-15T13:14:00+08:00")
    assert result["status"] == "historical_same_watch_roster_change_not_current"
    assert result["summary"]["paired_count"] == 193 and result["current_hint"] is None
    assert all(group["current_hint"] is None for group in result["groups"] + result["archetype_groups"])


def test_only_the_nearest_180_second_round_is_not_interpolated_to_five_minutes(saved):
    saved["job"]["attempt_receipts"] = saved["job"]["attempt_receipts"][1:]
    dump(saved["job_folder"] / "job_receipt.json", saved["job"])
    result = change(saved)
    assert result["status"] == "unknown_no_common_300_to_420_second_baseline"
    assert result["summary"] is result["current_hint"] is result["baseline_receipt"] is None


def test_endpoint_before_availability_has_roster_reason_and_never_flat(saved):
    saved["baseline"]["quotes"][4]["source_quote_time"] = "2026-10-15T09:49:00+08:00"
    dump(saved["paths"][0], saved["baseline"])
    result = change(saved)
    assert result["summary"]["unknown_count"] == 13
    assert result["unknown_reason_counts"]["baseline_source_time_precedes_roster_availability"] == 1
    assert "registration" not in str(result["unknown_reason_counts"])


def test_view_hook_keeps_final_pointer_guard_after_change_io(saved, monkeypatch):
    monkeypatch.setattr(view, "process_identity", lambda *a: {"status": "not_running"})
    monkeypatch.setattr(view, "industry_context", lambda **k: {})
    def move_pointer(*args, **kwargs):
        pointer_path = saved["out"].parent / "latest.json"
        pointer = read(pointer_path)
        pointer["updated_at"] = "moved during change IO"
        dump(pointer_path, pointer)
        return {"status": "unknown_no_common_300_to_420_second_baseline"}
    monkeypatch.setattr(c, "market_roster_observation_change", move_pointer)
    result = view.market_roster_quote_view(saved["data"], now="2026-10-15T13:11:30+08:00")
    assert result["status"] == "unavailable_locator_changed_during_read"
    assert result["current_summary"] is result["observation_summary"] is None
