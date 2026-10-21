"""Saved JSON only: no source retrieval, strategy import or account read."""
from datetime import datetime
from functools import partial
from hashlib import sha256
import json
from pathlib import Path

import pytest

from trading.research import leader_exit_hook_view as m


NOW = datetime.fromisoformat("2026-10-15T23:00:00+08:00")
FALSE = {key: False for key in m.PERMISSION_FLAGS}


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def save(root, name="new", created="2026-10-15T21:00:00+08:00"):
    folder = root / "outputs/leader_exit_research" / name
    folder.mkdir(parents=True)
    def summary(family):
        return {"family": family, "pool": "common_pre_window_symbol20",
            "exit_policy": "complete_empty_and_five_session_exit", "status": m.CASE_COMPLETED,
            "candidate_panel_only": True, **FALSE, "metrics": {"net_return": -.1},
            "tail_included_diagnostic_metrics": {"net_return": -.2},
            "decision_coverage": {"over_five_session_held_days": 17}, "open_positions": {"000017.SZSE": 1400},
            "orders": 3, "fills": 4, "blocked_exit_count": 2, "error": None,
            "last_successful_session": "2026-10-13"}
    protocol = {"schema": m.SCHEMA, "method_id": m.METHOD, "recorded_at": "2026-10-15T18:00:00+08:00",
        "candidate_panel_only": True, **FALSE, "families": list(m.FAMILIES),
        "pool": "common_pre_window_symbol20", "start": "2026-01-05", "end": "2026-10-09",
        "execution_tail_sessions": ["2026-10-12", "2026-10-13"],
        "frozen_symbols": [f"{i:06}.SZSE" for i in range(1, 21)]}
    record = {"schema": m.SCHEMA, "method_id": m.METHOD, "created_at": created,
        "status": m.COMPLETED, "candidate_panel_only": True, **FALSE,
        "protocol": str(folder / "protocol.json"), "new_engine_cases": 5,
        "comparisons": [{"family": family, "original": summary(family), "independent_hook": summary(family)}
                        for family in m.FAMILIES],
        "original_unavailable_cases_retained_without_new_replay": [
            {"family": family, "pool": pool, "exit_policy": policy,
             "status": "unavailable_verified_full_market_breadth" if policy == "verified_full_market_breadth_defense"
                 else "unavailable_complete_cohort_decision_batches", "candidate_panel_only": True, **FALSE}
            for family in m.FAMILIES for pool in ("common_pre_window_symbol20", "common_pre_window_uncapped")
            for policy in ("complete_empty_and_five_session_exit", "verified_full_market_breadth_defense")
            if (pool, policy) != ("common_pre_window_symbol20", "complete_empty_and_five_session_exit")]}
    write(folder / "protocol.json", protocol)
    write(folder / "result.json", record)
    return folder, record


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_latest_actual_clock_not_name_retains_all_five_nulls_and_never_writes_or_runs(tmp_path, monkeypatch):
    save(tmp_path, "z_old", "2026-10-15T21:00:00+09:00")
    folder, record = save(tmp_path, "a_new", "2026-10-15T20:30:00+08:00")
    rows = record["comparisons"]
    rows[0]["original"]["fills"] = rows[0]["independent_hook"]["fills"] = 0
    rows[1]["independent_hook"]["metrics"] = None
    rows[2]["original"]["error"] = "saved original failure"
    rows[2]["original"]["status"] = "blocked_engine_replay"
    rows[3]["independent_hook"]["last_successful_session"] = None
    rows[3]["independent_hook"]["decision_coverage"] = None
    rows[3]["independent_hook"]["open_positions"] = None
    write(folder / "result.json", record)
    result_bytes = (folder / "result.json").read_bytes()
    write(folder / "recovery_evidence.json", {"schema": "leader_exit_hook_saved_economic_and_recovery_evidence_v1",
        "created_at": "2026-10-15T21:30:00+08:00", **FALSE,
        "engine_advanced": False, "returns_recomputed": False,
        "source_result": {"file": str(folder / "result.json"), "sha256": sha256(result_bytes).hexdigest()}})
    # A stray large-account locator must not cause an account file read.
    (folder / "account.json").write_text("not JSON: this file must never be opened", encoding="utf-8")
    original_read = Path.read_bytes
    def reject_account(path):
        if path.name == "account.json":
            pytest.fail("Read-only summary opened an account")
        return original_read(path)
    before = snapshot(tmp_path)
    monkeypatch.setattr(Path, "read_bytes", reject_account)
    view = m.leader_exit_hook_view(tmp_path, now=NOW)
    assert view["source_file"] == str((folder / "result.json").resolve())
    assert view["status"] == "completed" and [r["family"] for r in view["rows"]] == list(m.FAMILIES)
    assert (view["new_engine_cases"], view["retained_unavailable_original_cases"], view["frozen_cohort_size"]) == (5, 15, 20)
    first, second, third, fourth, last = view["rows"]
    assert first["old_fills"] == first["new_fills"] == 0
    assert all(first[key] is None for key in ("old_main_return", "new_main_return", "old_tail_return", "new_tail_return"))
    assert second["new_main_return"] is None and second["new_tail_return"] == -.2
    assert third["old_main_return"] is third["old_tail_return"] is None
    assert fourth["new_last_session"] is fourth["new_open_positions"] is fourth["new_overdue_days"] is None
    assert last["new_main_return"] == -.1 and last["new_tail_return"] == -.2
    assert last["new_open_positions"] == 1 and last["new_overdue_days"] == 17
    assert last["old_last_session"] == "2026-10-13"
    assert view["recovery_evidence_file"] == str((folder / "recovery_evidence.json").resolve())
    assert all(view[key] is False for key in m.PERMISSION_FLAGS)
    assert view["strategy_run_started"] is view["engine_run_started"] is view["provider_contacted"] is False
    monkeypatch.setattr(Path, "read_bytes", original_read)
    assert snapshot(tmp_path) == before


def test_latest_saved_failure_remains_visible_instead_of_older_success(tmp_path):
    save(tmp_path, "old", "2026-10-15T19:00:00+08:00")
    folder, record = save(tmp_path)
    record["status"] = m.FAILED
    record["comparisons"][4]["independent_hook"].update(status="blocked_engine_replay", error="owned raw unavailable")
    write(folder / "result.json", record)
    view = m.leader_exit_hook_view(tmp_path, now=NOW)
    assert view["status"] == "diagnostic_failed" and view["source_file"] == str((folder / "result.json").resolve())
    assert view["rows"][4]["new_error"] == "owned raw unavailable"
    assert view["rows"][4]["new_main_return"] is view["rows"][4]["new_tail_return"] is None


@pytest.mark.parametrize("field,value", [("execution_admitted", True), ("created_at", "2026-10-16T00:00:00+08:00"),
    ("method_id", "wrong_method"), ("new_engine_cases", 20), ("status", "running")])
def test_bad_newest_cannot_fall_back_to_an_older_valid_run(tmp_path, field, value):
    save(tmp_path, "old", "2026-10-15T19:00:00+08:00")
    folder, record = save(tmp_path)
    record[field] = value
    write(folder / "result.json", record)
    with pytest.raises(ValueError):
        m.leader_exit_hook_view(tmp_path, now=NOW)


def test_missing_other_schema_and_api_get_only_rejects_bad_contract_without_mutation(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from trading import api
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    monkeypatch.setattr(m, "leader_exit_hook_view", partial(m.leader_exit_hook_view, now=NOW))
    assert api.latest_leader_exit_hook()["status"] == "unavailable"
    assert not list(tmp_path.iterdir())
    legacy = tmp_path / "outputs/leader_exit_research/legacy/result.json"
    legacy.parent.mkdir(parents=True)
    write(legacy, {"schema": "unrelated", "status": "completed"})
    assert api.latest_leader_exit_hook()["rows"] == []
    folder, record = save(tmp_path)
    record["comparisons"][0]["independent_hook"]["selection_enabled"] = True
    write(folder / "result.json", record)
    before = snapshot(tmp_path)
    with pytest.raises(HTTPException) as error:
        api.latest_leader_exit_hook()
    assert error.value.status_code == 409
    assert snapshot(tmp_path) == before
    assert any(route.path == "/api/trading/research/leader-exit-hook" and route.methods == {"GET"}
               for route in api.router.routes)
