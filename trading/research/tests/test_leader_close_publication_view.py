from copy import deepcopy
from datetime import datetime
from functools import partial
import json
import os

import pytest

from trading.research.leader_close_publication_view import FALSE_BOUNDARIES, leader_close_publication_view as _publication_view


leader_close_publication_view = partial(_publication_view, now=datetime.fromisoformat("2026-10-15T23:59:00+08:00"))


def publication(recorded="2026-10-15T12:00:00+08:00"):
    cases = [{"case_id": f"case-{i}", "family": "trend_leader_20_60", "pool": "common_pre_window_uncapped",
        "exit_policy": "complete_empty_and_five_session_exit", "status": "late_context_only",
        "ledger_status": "no_current_ledger_evidence", "no_change_reason": None,
        "target_weights": {"600000.SSE": 0.16, "000001.SZSE": 0.1}, "target_cash_weight": 0.74,
        "portfolio_intent": {"kind": "FULL_TARGET", "weights": {"600000.SSE": 0.16, "000001.SZSE": 0.1}},
        "formal_target_admitted": False, "execution_admitted": False,
        "holdings_retention_and_expiry_account_bound": False, "order_sizing_binding_pending": True} for i in range(20)]
    cases[0].update(status="unavailable_fixed_rule_target", target_weights={}, target_cash_weight=None,
        portfolio_intent={"kind": "NO_CHANGE", "weights": {}}, no_change_reason="coverage_unknown")
    return {"schema": "leader_close_target_publication_v1", "status": "late_context_only",
        "recorded_at": recorded, "publication_at": recorded, "source_close_day": "2026-10-14",
        "proposed_execution_day": "2026-10-15", "next_open_deadline": "2026-10-15T09:30:00+08:00",
        "timeliness_uses_actual_recording": True, "publication_time_backfilled": False,
        "execution_day_rolled_forward": False, "candidate_panel_only": True,
        "ledger_checkpoint": None, "execution_binding_pending": True,
        "case_count": len(cases), "cases": cases, **{key: False for key in FALSE_BOUNDARIES}}


def save(root, name="publication1", payload=None):
    path = root / "outputs/leader_close_publication" / name / "publication.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or publication()), encoding="utf-8")
    return path


def test_missing_and_complete_saved_weights_are_read_only_without_cash_nochange_confusion(tmp_path):
    assert leader_close_publication_view(tmp_path)["status"] == "missing"
    payload = publication()
    path = save(tmp_path, payload=payload)
    before = path.read_bytes()
    view = leader_close_publication_view(tmp_path)
    assert view["case_count"] == 20 and len(view["cases"]) == 20
    assert [row["case_id"] for row in view["cases"]] == [row["case_id"] for row in payload["cases"]]
    assert view["cases"][1]["target_weights"] == payload["cases"][1]["target_weights"]
    assert view["cases"][0]["intent_kind"] == "NO_CHANGE" and view["cases"][0]["target_cash_weight"] is None
    assert view["ledger_binding"] == "unbound" and view["history_prefix_binding"] == "legacy_input_path"
    assert view["timeliness"] == "late_context_only" and view["proposed_execution_day"] == "2026-10-15"
    assert view["execution_day_rolled_forward"] is False and view["display_is_not_portfolio_selection"] is True
    assert view["provider_contacted"] is view["strategy_run_started"] is view["account_accessed"] is False
    assert path.read_bytes() == before


def test_actual_aware_recording_orders_utc_instants_not_paths_claims_or_mtimes(tmp_path):
    old = save(tmp_path, "z_old", publication("2026-10-15T14:00:00+09:00"))
    payload = publication("2026-10-15T13:30:00+08:00")
    payload["claimed_publication_at"] = "2026-10-15T08:00:00+08:00"
    new = save(tmp_path, "a_new", payload)
    os.utime(old, (2_000_000_000, 2_000_000_000))
    os.utime(new, (1_000_000_000, 1_000_000_000))
    view = leader_close_publication_view(tmp_path)
    assert view["source_file"] == str(new.resolve())
    assert view["selected_by"] == "timezone_aware_actual_recorded_at"
    assert view["recorded_at"] == payload["recorded_at"] and view["timeliness"] == "late_context_only"


@pytest.mark.parametrize("location,key", [("top", "execution_admitted"), ("top", "selection_enabled"),
    ("top", "orders_generated"), ("top", "ledger_mutated"), ("case", "execution_admitted"),
    ("coverage", "selection_enabled")])
def test_unsafe_newest_is_rejected_never_falls_back(tmp_path, location, key):
    save(tmp_path, "old", publication("2026-10-15T10:00:00+08:00"))
    payload = publication()
    target = payload if location == "top" else payload["cases"][0]
    if location == "coverage":
        target = target.setdefault("coverage", {})
    target[key] = True
    save(tmp_path, "new", payload)
    with pytest.raises(ValueError):
        leader_close_publication_view(tmp_path)


def test_naive_or_missing_recording_cannot_be_ranked_as_old_success(tmp_path):
    save(tmp_path, "old")
    payload = publication("2026-10-15T13:00:00")
    save(tmp_path, "unknown", payload)
    with pytest.raises(ValueError, match="timezone"):
        leader_close_publication_view(tmp_path)


def test_recorded_prefix_availability_and_binding_are_not_inferred_for_old_publications(tmp_path):
    payload = publication()
    history = {"base_history_end": "2026-10-09", "available_at": "2026-10-15T11:04:33+08:00",
        "view_sessions": ["2026-10-12", "2026-10-13", "2026-10-14"], "shared_view_id": "saved-view",
        "view_file": "saved-view.json"}
    payload.update(recent_source_history=history, history_input_available_at=history["available_at"],
        protocol_binding={"history_source_view": deepcopy(history), "history_source_supplement": "frozen-prefix.json"})
    path = save(tmp_path, payload=payload)
    view = leader_close_publication_view(tmp_path)
    assert view["recent_source_history"] == history and view["history_prefix_binding"] == "recorded_recent_source_prefix"
    payload["history_input_available_at"] = "2026-10-15T13:00:00+08:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="availability"):
        leader_close_publication_view(tmp_path)


def test_target_weights_cannot_drift_from_saved_typed_intent(tmp_path):
    payload = publication()
    payload["cases"][1]["target_weights"]["600000.SSE"] = 0.2
    save(tmp_path, payload=payload)
    with pytest.raises(ValueError, match="weights"):
        leader_close_publication_view(tmp_path)


def test_future_actual_recording_is_rejected_instead_of_presented_as_current(tmp_path):
    save(tmp_path, "old", publication("2026-10-15T10:00:00+08:00"))
    save(tmp_path, "future", publication("2026-10-15T13:00:00+08:00"))
    with pytest.raises(ValueError, match="future"):
        leader_close_publication_view(tmp_path, now=datetime.fromisoformat("2026-10-15T12:00:00+08:00"))


@pytest.mark.parametrize("change", ["specified_future", "specified_binding_mismatch", "ready_future", "ready_before_input"])
def test_saved_input_clocks_cannot_disguise_late_availability(tmp_path, change):
    payload = publication()
    history = {"base_history_end": "2026-10-09", "available_at": "2026-10-15T11:04:33+08:00",
        "view_sessions": ["2026-10-12", "2026-10-13", "2026-10-14"]}
    specified = "2026-10-15T11:05:00+08:00"
    payload.update(recent_source_history=history, history_input_available_at=history["available_at"],
        history_source_specified_at=specified, source_ready_at=specified,
        protocol_binding={"history_source_view": deepcopy(history), "history_source_supplement": "prefix.json",
                          "history_source_specified_at": specified})
    if change == "specified_future":
        payload["history_source_specified_at"] = payload["protocol_binding"]["history_source_specified_at"] = "2026-10-15T13:00:00+08:00"
    elif change == "specified_binding_mismatch":
        payload["history_source_specified_at"] = "2026-10-15T11:06:00+08:00"
    elif change == "ready_future":
        payload["source_ready_at"] = "2026-10-15T13:00:00+08:00"
    else:
        payload["source_ready_at"] = "2026-10-15T11:00:00+08:00"
    save(tmp_path, payload=payload)
    with pytest.raises(ValueError, match="clock|ready_at"):
        leader_close_publication_view(tmp_path)
