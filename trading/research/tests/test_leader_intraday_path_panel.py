"""Real-write-time semantics using injected local clocks and fake saved rounds."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trading.research import leader_intraday_path_panel as p
from trading.research import prior_dated_market_quote_snapshot as source
from trading.research.tests.test_prior_dated_market_quote_snapshot import fixture, transport, dump, read
from trading.research.tests.test_market_roster_watch import WatchClock


@pytest.fixture
def saved(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch, count=4)
    value["registry"]["members"] = value["registry"]["members"][:2]
    dump(value["registry_path"], value["registry"])
    monkeypatch.setattr(source, "_registry", lambda path, now: (deepcopy(value["registry"]), source.digest(path), [], {}))
    clock = WatchClock()
    get, _ = transport(clock)
    folder, job = source.watch(package=value["package"], calendar=value["calendar"], data_root=value["data"],
                                max_observations=3, _clock=clock.now, _sleep=clock.sleep, _get=get)
    value.update(paths=[Path(path) for path in job["attempt_receipts"]], job_file=folder / "job_receipt.json", job=job,
                 panel=tmp_path / "panel", clock=clock)
    return value


def show(value, count, when):
    value["clock"].value = datetime.fromisoformat(when)
    job = deepcopy(value["job"])
    job.update(attempt_receipts=[str(path) for path in value["paths"][:count]], status="running", finished_at=None,
               attempts=count, successes=count)
    dump(value["job_file"], job)
    path = value["paths"][count - 1]
    record = read(path)
    pointer = read(value["out"].parent / "latest.json")
    pointer.update(observation_id=path.parent.name, receipt_file=str(path), targets_file=record["targets_file"],
                   targets_sha256=record["targets_sha256"], snapshot_status=record["status"])
    dump(value["out"].parent / "latest.json", pointer)


def start(value):
    show(value, 1, "2026-10-15T13:05:10+08:00")
    result = p.register(value["job_file"], value["panel"], data_root=value["data"], _clock=value["clock"].now)
    assert result["anchor_count"] == 0
    return p.advance(value["panel"], _clock=value["clock"].now)


def test_actual_anchor_then_first_future_outcome_preserves_full_groups_and_old_labels(saved):
    first = start(saved)
    assert first["anchor_count"] == first["pending_count"] == 1 and first["outcome_count"] == 0
    show(saved, 3, "2026-10-15T13:11:10+08:00")
    result = p.advance(saved["panel"], _clock=saved["clock"].now)
    outcome = result["outcomes"][0]
    assert outcome["status"] == "observed" and outcome["prospective_claim"] is True
    assert outcome["response_span_seconds"] == 360 and outcome["summary"]["member_count"] == outcome["summary"]["paired_count"] == 4
    assert len(outcome["groups"]) == 6 and len(outcome["archetype_groups"]) == 8
    trend = next(group for group in outcome["archetype_groups"] if group["id"] == "trend_leader")
    assert trend["member_count"] == 2 and trend["conditional_median_difference_vs_rest_pct"] == 0
    assert outcome["archetype_groups"][0]["summary"]["conditional_median_window_return_percent"] is None
    assert result["anchor_count"] == 2 and result["pending_count"] == 1
    assert result["overlapping_windows_not_independent"] is result["group_labels_overlap"] is True


def test_registration_does_not_backfill_past_rounds_and_anchor_is_once_only(saved):
    show(saved, 3, "2026-10-15T13:11:10+08:00")
    p.register(saved["job_file"], saved["panel"], data_root=saved["data"], _clock=saved["clock"].now)
    first = p.advance(saved["panel"], _clock=saved["clock"].now)
    second = p.advance(saved["panel"], _clock=saved["clock"].now)
    assert first["anchor_count"] == second["anchor_count"] == 1 and second["outcome_count"] == 0
    assert first["anchors"][0]["anchor_id"] == saved["paths"][-1].parent.name


def test_failed_or_started_latest_does_not_freeze_old_success(saved):
    start(saved)
    show(saved, 2, "2026-10-15T13:08:10+08:00")
    latest = read(saved["paths"][1]); latest["status"] = "started"
    dump(saved["paths"][1], latest)
    result = p.advance(saved["panel"], _clock=saved["clock"].now)
    assert result["anchor_count"] == 1 and result["latest_freeze_status"] == "latest_started_or_failed_no_fallback"


def clone_successor(value, *, seconds, empty=False):
    original = value["paths"][-1]
    folder = value["out"].parent / ("successor_" + str(seconds))
    folder.mkdir()
    record, manifest = read(original), read(original.parent / "targets.json")
    delta = timedelta(seconds=seconds - 360)
    for key in ("started_at", "response_received_at", "finished_at"):
        record[key] = (datetime.fromisoformat(record[key]) + delta).isoformat()
    for quote in record["quotes"]:
        for key in ("source_quote_time", "received_at"):
            quote[key] = (datetime.fromisoformat(quote[key]) + delta).isoformat()
        if empty:
            quote["source_status"] = None
    manifest.update(created_at=record["started_at"], calendar_file=str(folder / "calendar.json"))
    record.update(observation_id=folder.name, calendar_file=manifest["calendar_file"], targets_file=str(folder / "targets.json"))
    (folder / "calendar.json").write_bytes((original.parent / "calendar.json").read_bytes())
    dump(folder / "targets.json", manifest)
    record["targets_sha256"] = source.digest(folder / "targets.json")
    dump(folder / "receipt.json", record)
    return folder / "receipt.json"


def test_earliest_response_chosen_before_price_coverage_and_never_replaced(saved):
    start(saved)
    show(saved, 3, "2026-10-15T13:11:10+08:00")
    poor = clone_successor(saved, seconds=330, empty=True)
    job = read(saved["job_file"]); job["attempt_receipts"].append(str(poor)); dump(saved["job_file"], job)
    outcome = p.advance(saved["panel"], _clock=saved["clock"].now)["outcomes"][0]
    assert outcome["selected_successor_receipt"] == str(poor) and outcome["status"] == "observed_unknown"
    assert outcome["summary"]["unknown_count"] == 4 and outcome["summary"]["paired_count"] == 0


def test_420_is_inclusive_and_missing_endpoint_terminal_only_after_deadline(saved):
    start(saved)
    saved["clock"].value = datetime.fromisoformat("2026-10-15T13:12:00+08:00")
    assert p.advance(saved["panel"], _clock=saved["clock"].now)["pending_count"] == 1
    endpoint = clone_successor(saved, seconds=420)
    job = read(saved["job_file"]); job["attempt_receipts"].append(str(endpoint)); dump(saved["job_file"], job)
    saved["clock"].value += timedelta(seconds=1)
    outcome = p.advance(saved["panel"], _clock=saved["clock"].now)["outcomes"][0]
    assert outcome["response_span_seconds"] == 420 and outcome["summary"]["paired_count"] == 4


def test_stale_actual_anchor_mask_cannot_be_promoted_by_good_future_prices(saved):
    show(saved, 1, "2026-10-15T13:07:10+08:00")
    p.register(saved["job_file"], saved["panel"], data_root=saved["data"], _clock=saved["clock"].now)
    anchor = p.advance(saved["panel"], _clock=saved["clock"].now)["anchors"][0]
    assert anchor["features"]["summary"]["known_count"] == 0
    show(saved, 3, "2026-10-15T13:11:10+08:00")
    outcome = p.advance(saved["panel"], _clock=saved["clock"].now)["outcomes"][0]
    assert outcome["status"] == "observed_unknown" and outcome["prospective_claim"] is False
    assert outcome["unknown_reason_counts"]["anchor_price_not_current_at_actual_creation"] == 4


def test_close_finalizes_unknown_without_creating_a_post_close_anchor(saved):
    start(saved)
    saved["clock"].value = datetime.fromisoformat("2026-10-15T15:00:00+08:00")
    result = p.advance(saved["panel"], _clock=saved["clock"].now)
    assert result["anchor_count"] == 1 and result["pending_count"] == 0
    assert result["outcomes"][0]["status"] == "terminal_unknown_no_future_counterpart"


def test_locator_change_during_freeze_never_creates_old_anchor(saved, monkeypatch):
    show(saved, 1, "2026-10-15T13:05:10+08:00")
    p.register(saved["job_file"], saved["panel"], data_root=saved["data"], _clock=saved["clock"].now)
    original = p._adapt_row
    def changing(*args, **kwargs):
        pointer_file = saved["out"].parent / "latest.json"
        pointer = read(pointer_file); pointer["snapshot_status"] = "started"; dump(pointer_file, pointer)
        return original(*args, **kwargs)
    monkeypatch.setattr(p, "_adapt_row", changing)
    result = p.advance(saved["panel"], _clock=saved["clock"].now)
    assert result["anchor_count"] == 0 and result["latest_freeze_status"] == "latest_changed_during_freeze_no_anchor_written"


def test_watch_reuses_actual_anchors_is_bounded_and_has_separate_local_receipt(saved):
    start(saved)
    saved["clock"].sleeps.clear()
    result = p.watch(saved["panel"], max_advances=2, _clock=saved["clock"].now, _sleep=saved["clock"].sleep)
    assert result["status"] == "completed_max_advances" and result["advances"] == 2
    assert result["anchor_count"] == 1 and sum(saved["clock"].sleeps) == 30
    assert read(saved["panel"] / "watch_receipt.json")["provider_contacted"] is False


def test_watch_at_15_finalizes_pending_without_new_anchor(saved):
    start(saved)
    saved["clock"].value = datetime.fromisoformat("2026-10-15T14:59:50+08:00")
    result = p.watch(saved["panel"], max_advances=4, _clock=saved["clock"].now, _sleep=saved["clock"].sleep)
    assert result["status"] == "completed_session_boundary" and result["advances"] == 2
    assert result["anchor_count"] == 1 and result["pending_count"] == 0


def test_watch_stops_first_local_failure_without_retry(saved, monkeypatch):
    start(saved)
    saved["clock"].sleeps.clear()
    calls = []
    def broken(*args, **kwargs):
        calls.append(1)
        raise ValueError("test local evidence failure")
    monkeypatch.setattr(p, "advance", broken)
    result = p.watch(saved["panel"], _clock=saved["clock"].now, _sleep=saved["clock"].sleep)
    assert result["status"] == "failed_stopped_no_retry" and calls == [1] and not saved["clock"].sleeps


def test_watch_read_crossing_close_runs_post_boundary_finalization(saved, monkeypatch):
    start(saved)
    saved["clock"].value = datetime.fromisoformat("2026-10-15T14:59:59+08:00")
    original, calls = p.advance, []
    def crossing(*args, **kwargs):
        calls.append(saved["clock"].now())
        result = original(*args, **kwargs)
        if len(calls) == 1:
            saved["clock"].value += timedelta(seconds=2)
        return result
    monkeypatch.setattr(p, "advance", crossing)
    result = p.watch(saved["panel"], max_advances=4, _clock=saved["clock"].now, _sleep=saved["clock"].sleep)
    assert result["status"] == "completed_session_boundary" and result["advances"] == 2
    assert calls[1].hour == 15 and result["pending_count"] == 0
