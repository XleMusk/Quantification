"""Wide quote reads preserve all dated targets and recompute source freshness."""
from copy import deepcopy
from datetime import datetime

import pytest

from trading.research import market_roster_quote_view as v
from trading.research.tests.test_prior_dated_market_quote_snapshot import fixture, execute, dump, read


@pytest.fixture
def saved(tmp_path, monkeypatch):
    value = fixture(tmp_path, monkeypatch, count=205)
    folder, source, calls = execute(value, kind="members")
    monkeypatch.setattr(v, "process_identity", lambda *a: {"status": "not_running"})
    monkeypatch.setattr(v, "industry_context", lambda **kwargs: {"status": "unavailable_industry_context", "groups": []})
    value.update(folder=folder, source=source, calls=calls)
    return value


def view(value, now="2026-10-15T13:05:30+08:00"):
    return v.market_roster_quote_view(value["data"], now=now)


def test_summary_keeps_all_targets_unknowns_frozen_backgrounds_and_clock_ranges(saved):
    result = view(saved)
    assert result["status"] == "current_prior_dated_roster_snapshot"
    assert "quotes" not in result and result["target_count"] == 205
    assert result["current_summary"]["member_count"] == 205
    assert result["current_summary"]["known_count"] == 193 and result["current_summary"]["unknown_count"] == 12
    assert result["current_summary"]["full_cohort_breadth"] is None
    assert result["observation_summary"]["known_count"] == 193
    assert len(result["groups"]) == 6 and result["groups"][0]["name"].startswith("2026-10-14")
    assert sum(group["member_count"] for group in result["groups"] if group["id"] in ("fixed_590", "rest")) == 205
    assert sum(group["member_count"] for group in result["groups"] if group["id"] in ("prior_day_nonST", "prior_day_ST")) == 205
    st = next(group for group in result["groups"] if group["id"] == "prior_day_ST")
    assert st["member_count"] == 21
    assert result["source_quote_clocks"]["all_parseable_including_stale_or_nonregular"]["span_seconds"] == 125
    assert result["source_quote_clocks"]["regular_current_at_read"]["count"] == 193
    assert result["source_available_at"] == "2026-10-15T09:50:42+08:00"
    assert result["today_ST_eligibility_verified"] is result["all_A_breadth"] is result["current_universe_verified"] is False
    assert result["industry_context"]["status"] == "unavailable_industry_context"


def test_read_time_expiry_retains_explicit_historical_summary_and_no_current(saved):
    result = view(saved, "2026-10-15T13:08:00+08:00")
    assert result["status"] == "saved_prior_dated_roster_snapshot_not_current"
    assert result["current_summary"] is None and result["observation_summary"]["known_count"] == 193
    assert all(group["current_summary"] is None for group in result["groups"])


def test_next_day_cannot_relabel_the_old_snapshot_as_current(saved):
    result = view(saved, "2026-10-16T13:05:10+08:00")
    assert result["status"] == "saved_prior_dated_roster_snapshot_not_current"
    assert result["observation_day"] == "2026-10-15" and result["current_summary"] is None


def test_failed_latest_retains_no_successful_history_and_no_fallback(saved):
    source = deepcopy(saved["source"])
    source.update(status="unavailable_single_attempt_stopped", all_batches_completed=False, error="batch failure")
    dump(saved["folder"] / "receipt.json", source)
    pointer = read(saved["folder"].parent / "latest.json")
    pointer["snapshot_status"] = source["status"]
    dump(saved["folder"].parent / "latest.json", pointer)
    result = view(saved)
    assert result["status"] == "unavailable_latest_market_roster_snapshot"
    assert result["current_summary"] is result["observation_summary"] is None and result["groups"] == []
    assert result["unavailable_summary"]["unknown_count"] == 205


def test_started_snapshot_is_graceful_not_promoted_to_complete(saved):
    source = deepcopy(saved["source"])
    source.update(status="started", finished_at=None, all_batches_completed=False)
    dump(saved["folder"] / "receipt.json", source)
    result = view(saved)
    assert result["status"] == "collecting_snapshot"
    assert result["current_summary"] is result["observation_summary"] is None


@pytest.mark.parametrize("mutation", ["path", "target_hash", "row_order", "source_clock", "prior_session", "false_boundary"])
def test_invalid_locator_hash_scope_and_actual_source_clocks_cannot_be_admitted(saved, mutation):
    pointer_path = saved["folder"].parent / "latest.json"
    source_path, targets_path = saved["folder"] / "receipt.json", saved["folder"] / "targets.json"
    pointer, source, targets = read(pointer_path), read(source_path), read(targets_path)
    if mutation == "path":
        pointer["receipt_file"] = str(saved["folder"] / "nested/receipt.json")
    elif mutation == "target_hash":
        pointer["targets_sha256"] = "0" * 64
    elif mutation == "row_order":
        source["quotes"] = source["quotes"][::-1]
    elif mutation == "source_clock":
        targets["source_available_at"] = source["source_available_at"] = "2026-10-15T09:07:47+08:00"
    elif mutation == "prior_session":
        targets["source_day"] = source["source_day"] = "2026-10-13"
    elif mutation == "false_boundary":
        source["current_universe_verified"] = True
    dump(targets_path, targets)
    if mutation in ("source_clock", "prior_session"):
        pointer["targets_sha256"] = source["targets_sha256"] = v.digest(targets_path)
    dump(pointer_path, pointer); dump(source_path, source)
    result = view(saved)
    assert result["status"] == "unavailable_invalid_saved_market_roster_snapshot"
    assert result["current_summary"] is result["observation_summary"] is None


def test_quote_before_roster_availability_stays_unknown_with_neutral_vocabulary(saved):
    source = deepcopy(saved["source"])
    source["quotes"][4].update(source_quote_time="2026-10-15T09:49:00+08:00", source_status="00")
    dump(saved["folder"] / "receipt.json", source)
    result = view(saved)
    assert result["current_summary"]["known_count"] == 192
    assert result["read_status_counts"]["source_time_precedes_roster_availability"] == 1
    assert "registration" not in str(result["read_status_counts"])


def test_source_prev_and_current_name_do_not_create_today_st_eligibility(saved):
    source = deepcopy(saved["source"])
    source["quotes"][4]["source_name"] = "*ST_更名"
    source["quotes"][4]["previous_close_price_rmb"] = "9.7"
    dump(saved["folder"] / "receipt.json", source)
    result = view(saved)
    assert result["status"] == "current_prior_dated_roster_snapshot"
    assert result["current_summary"]["known_count"] == 193
    assert result["today_ST_eligibility_verified"] is False


def test_final_pointer_guard_occurs_after_industry_io(saved, monkeypatch):
    def industry(**kwargs):
        pointer_path = saved["folder"].parent / "latest.json"
        pointer = read(pointer_path)
        pointer["updated_at"] = "changed while industry mapping was read"
        dump(pointer_path, pointer)
        return {"status": "observed_lagged_industry_price_context_only"}
    monkeypatch.setattr(v, "industry_context", industry)
    result = view(saved)
    assert result["status"] == "unavailable_locator_changed_during_read"
    assert result["current_summary"] is result["observation_summary"] is None
