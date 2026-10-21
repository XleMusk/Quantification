"""Synthetic reader-contract checks; these are not model or market evidence."""
from datetime import datetime
from hashlib import sha256
import json

import pytest

from trading.research import recent_alpha158_view as m


NOW = datetime.fromisoformat("2026-10-15T13:00:00+08:00")


def save(tmp_path, status=m.COMPLETED):
    provider_path = tmp_path / "research/recent_source_qlib_provider" / m.RUN / "result.json"
    provider_path.parent.mkdir(parents=True)
    provider = {"schema": m.PROVIDER_SCHEMA, "provider_method_id": m.PROVIDER_METHOD,
        "status": "research_provider_completed", "created_at": "2026-10-15T12:00:00+08:00",
        "recent_source_days": ["2026-10-12", "2026-10-13", "2026-10-14"], "symbols": 10,
        "base_history_end": "2026-10-09", "normalization_matches_original_qfq_factor_training": False,
        "prior_scores_modified": False, **{key: False for key in m.FALSE_FLAGS}}
    provider_path.write_text(json.dumps(provider), encoding="utf-8")
    days = provider["recent_source_days"]
    record = {"schema": m.SCHEMA, "method_id": m.METHOD_ID, "status": status,
        **{key: False for key in m.FALSE_FLAGS}, "candidate_panel_only": True,
        "cross_lineage_diagnostic": True, "feature_count": 158, "score_scope": m.SCORE_SCOPE,
        "native_grid_scope": m.GRID_SCOPE, "score_aggregation": m.RANK_SCOPE,
        "started_at": "2026-10-15T12:01:00+08:00", "source_available_at": provider["created_at"],
        "provider_view": str(provider_path.parent), "provider_result_sha256": sha256(provider_path.read_bytes()).hexdigest(),
        "provider_method_id": m.PROVIDER_METHOD, "requested_feature_dates": days,
        "partitions": [{"symbols": [str(i) for i in range(10)], "rows": 30}],
        "native_symbols_completed": 10, "native_feature_rows": 30, "native_symbols": 10,
        "provider_calendar_start": "2024-01-02", "provider_calendar_end": days[-1], "provider_dates": 675,
        "model_run": "synthetic-frozen-model.json", "coverage": [
            {"day": day, "full_dated_roster_rows": 9, "active_non_ST_valid_observations": 7,
             "not_scored_count": 2, "suspended_rows_retained_unscored": 1, "ST_rows_retained_unscored": 1,
             "native_grid_rows": 10, "unobserved_or_out_of_scope_grid_rows": 3,
             "missing_feature_cells": 5, "all_missing_active_feature_rows": 0} for day in days]}
    if status != m.BUILDING:
        record["finished_at"] = "2026-10-15T12:02:00+08:00"
    if status == m.COMPLETED:
        record.update(actual_computed_at=record["finished_at"], score_dates=days, score_rows=21)
    if status == m.FAILED:
        record["error"] = "ValueError: synthetic native extraction failure"
    path = tmp_path / "research/recent_source_alpha158" / m.RUN / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path, record, provider_path


def read(tmp_path):
    return m.recent_alpha158_view(tmp_path, now=NOW)


def test_missing_is_explicit_and_does_not_discover_old_native_or_start_work(tmp_path):
    old = tmp_path / "research/alpha158/old/result.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"status":"completed"}', encoding="utf8")
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    view = read(tmp_path)
    assert view["status"] == "unavailable" and view["score_rows"] is None
    assert view["coverage"] == view["score_dates"] == []
    assert view["execution_admitted"] is view["account_accessed"] is view["score_run_started"] is False
    assert view["fallback_to_old_native"] is False
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("status", [m.BUILDING, m.COMPLETED, m.FAILED])
def test_read_real_saved_progress_and_only_expose_terminal_scope_not_rankings(tmp_path, status):
    path, _, provider_path = save(tmp_path, status)
    before = path.read_bytes(), provider_path.read_bytes()
    view = read(tmp_path)
    assert view["status"] == status and view["partitions_completed"] == 1
    assert view["native_symbols_completed"] == view["native_symbols_total"] == 10
    assert view["native_feature_rows"] == view["native_grid_expected_rows"] == 30
    assert view["base_history_end"] == "2026-10-09" and view["cross_lineage_diagnostic"] is True
    assert view["coverage"][0]["full_dated_roster_rows"] == 9 and view["coverage"][0]["native_grid_rows"] == 10
    assert view["old_scores_preserved"] is True and view["rankings_exposed"] is False
    assert view["score_rows"] == (21 if status == m.COMPLETED else None)
    assert view["score_dates"] == (["2026-10-12", "2026-10-13", "2026-10-14"] if status == m.COMPLETED else [])
    assert "partitions" not in view and "seed_models" not in view and "scores" not in view
    assert (path.read_bytes(), provider_path.read_bytes()) == before


def test_failed_batch_never_exposes_leftover_scores_as_success(tmp_path):
    path, record, _ = save(tmp_path, m.FAILED)
    record.update(score_rows=21, score_dates=record["requested_feature_dates"], actual_computed_at=record["finished_at"])
    path.write_text(json.dumps(record), encoding="utf8")
    (path.parent / "dated_scores.parquet").write_bytes(b"leftover-not-success")
    view = read(tmp_path)
    assert view["status"] == m.FAILED and view["score_rows"] is None and view["score_dates"] == []
    assert view["actual_computed_at"] is None and view["error"].startswith("ValueError")


@pytest.mark.parametrize("change", ["old_method", "flag", "missing_flag", "future_clock", "naive_clock", "rank_scope", "coverage", "native_grid"])
def test_unknown_method_unsafe_boundary_or_misleading_scope_and_clocks_are_rejected(tmp_path, change):
    path, record, _ = save(tmp_path)
    if change == "old_method":
        record["method_id"] = "old_native_batch"
    elif change == "flag":
        record["execution_admitted"] = True
    elif change == "missing_flag":
        del record["model_refit"]
    elif change == "future_clock":
        record["finished_at"] = record["actual_computed_at"] = "2026-10-15T14:00:00+08:00"
    elif change == "naive_clock":
        record["started_at"] = "2026-10-15T12:01:00"
    elif change == "rank_scope":
        record["score_aggregation"] = "cross_date_rank"
    elif change == "coverage":
        record["coverage"][0]["active_non_ST_valid_observations"] = 8
    else:
        record["coverage"][0]["native_grid_rows"] = 7
    path.write_text(json.dumps(record), encoding="utf8")
    with pytest.raises(ValueError):
        read(tmp_path)


def test_changed_pinned_provider_is_not_read_as_unchanged_input(tmp_path):
    _, _, provider_path = save(tmp_path)
    provider_path.write_text("{}", encoding="utf8")
    with pytest.raises(ValueError, match="Pinned provider result changed"):
        read(tmp_path)
