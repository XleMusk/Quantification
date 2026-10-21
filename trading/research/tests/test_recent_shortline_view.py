"""Synthetic saved-receipt checks; no training, strategy or market requests."""
from datetime import datetime
from functools import partial
import json

import pytest

from trading.research import recent_shortline_view as m


NOW = datetime.fromisoformat("2026-10-15T23:00:00+08:00")


def save(root, name="20261015_recent_source_increment_v1", created="2026-10-15T20:00:00+08:00"):
    folder = root / "outputs/shortline_research" / name
    folder.mkdir(parents=True)
    batches = {"schema": m.BATCH_SCHEMA, "future_label_fields_included": False,
        "candidate_panel_only": True, **{key: False for key in m.FALSE_FLAGS}, "batches": [
            {"day": "2026-10-15", "stage": stage, "status": "incomplete",
             "potential_unknown_stage_members": ["UNALLOCATED"], "unknown_fixed_rule_fields": [],
             "known_matching_rows": [{"symbol": "NOT_A_SIGNAL"}] if stage == 1 else [],
             "selection_permitted": False, "unknown_is_empty_target": False} for stage in (1, 2, 3)]}
    record = {"schema": m.SCHEMA, "method_id": m.METHOD, "status": m.COMPLETED,
        "created_at": created, "source_days": ["2026-10-15"], "base_history_cutoff": "2026-10-09",
        "candidate_panel_only": True, **{key: False for key in m.FALSE_FLAGS + m.EXTRA_FALSE_FLAGS},
        "return_metrics": None, "label_completion_requires_exact_nonlimit_witness": True,
        "history_policy": m.HISTORY_POLICY, "market_scope": m.MARKET_SCOPE,
        "event_rows": 6, "by_stage": [{"stage": stage, "known_stage_events": 2,
            "complete_labels": 1, "unresolved_labels": 1, "hit4_complete_known_stage": 0,
            "hit5_complete_known_stage": 0, "scope": m.STAGE_SCOPE} for stage in (1, 2, 3)],
        "fixed_rule_pass_rows": 1, "unresolved_membership_rows": 1,
        "fixed_rule": "amount_rel20_pre<=1 AND market_breadth>=0",
        "candidate_batches": str(folder / "candidate_batches.json"), "incomplete_stage_day_batches": 3}
    (folder / "result.json").write_text(json.dumps(record), encoding="utf-8")
    (folder / "candidate_batches.json").write_text(json.dumps(batches), encoding="utf-8")
    return folder, record, batches


def test_missing_does_not_substitute_legacy_or_create_work(tmp_path):
    old = tmp_path / "outputs/shortline_research/old13/result.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"schema":"a_share_shortline_stage_selection_diagnostic_v1"}', encoding="utf-8")
    before = list(tmp_path.rglob("*"))
    view = m.recent_shortline_view(tmp_path, now=NOW)
    assert view["status"] == "unavailable" and view["event_rows"] is None
    assert view["by_stage"] == [] and view["return_metrics"] is None
    assert view["fallback_to_old_batch"] is view["strategy_run_started"] is view["account_accessed"] is False
    assert list(tmp_path.rglob("*")) == before


def test_latest_uses_actual_aware_clock_counts_unknowns_and_no_security_or_return_payload(tmp_path):
    save(tmp_path, "z_old", "2026-10-15T20:00:00+09:00")
    folder, record, _ = save(tmp_path, "a_new", "2026-10-15T19:30:00+08:00")
    before = (folder / "result.json").read_bytes(), (folder / "candidate_batches.json").read_bytes()
    view = m.recent_shortline_view(tmp_path, now=NOW)
    assert view["source_file"] == str((folder / "result.json").resolve())
    assert view["event_rows"] == 6 and view["complete_labels"] == view["unresolved_labels"] == 3
    assert view["unresolved_membership_rows"] == 1 and view["known_rule_match_rows"] == 1
    assert view["incomplete_stage_day_batches"] == 3 and len(view["stage_day_batches"]) == 3
    assert view["source_days"] == record["source_days"] and [row["stage"] for row in view["by_stage"]] == [1, 2, 3]
    assert "NOT_A_SIGNAL" not in json.dumps(view) and "UNALLOCATED" not in json.dumps(view)
    assert view["rankings_exposed"] is False and view["return_metrics"] is None
    assert all(view[key] is False for key in m.FALSE_FLAGS)
    assert before == ((folder / "result.json").read_bytes(), (folder / "candidate_batches.json").read_bytes())


@pytest.mark.parametrize("field,value", [("execution_admitted", True), ("status", "failed"),
    ("created_at", "2026-10-16T00:00:00+08:00"), ("event_rows", 7)])
def test_bad_newest_does_not_fall_back_to_old_completed(tmp_path, field, value):
    save(tmp_path, "old", "2026-10-15T19:00:00+08:00")
    folder, record, _ = save(tmp_path)
    record[field] = value
    (folder / "result.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError):
        m.recent_shortline_view(tmp_path, now=NOW)


def test_unknown_batch_is_not_empty_or_overcounted_and_api_is_read_only(tmp_path, monkeypatch):
    from trading import api
    from fastapi import HTTPException
    folder, _, batches = save(tmp_path)
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    monkeypatch.setattr(m, "recent_shortline_view", partial(m.recent_shortline_view, now=NOW))
    assert api.latest_recent_shortline()["unresolved_membership_rows"] == 1
    batches["batches"][0]["unknown_is_empty_target"] = True
    (folder / "candidate_batches.json").write_text(json.dumps(batches), encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        api.latest_recent_shortline()
    assert error.value.status_code == 409
