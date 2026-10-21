"""Synthetic saved-panel necessities; no Engine, raw reader or provider call."""
from copy import deepcopy

import pytest

from trading.research import leader_panel_readiness as readiness
from trading.research.tests.test_leader_eight_type_panel_replay import A, B, CALENDAR, SESSIONS, panels


def test_saved_entry_counts_keep_unknown_null_guard_and_exact_successor_causal(monkeypatch):
    from trading.research import leader_eight_type_panel_replay as replay

    def forbidden(*args, **kwargs):
        raise AssertionError("Readiness must not load raw sources or run an account")

    monkeypatch.setattr(replay, "_case", forbidden)
    monkeypatch.setattr(replay, "advance", forbidden)
    monkeypatch.setattr(readiness, "load_inputs", forbidden)
    days = SESSIONS[SESSIONS.index("2026-09-14"):SESSIONS.index("2026-09-14") + 8]
    saved = panels(days)
    for day in days:
        saved[day]["available_at"] = day + "T17:00:00+08:00"
    for item in saved[days[1]]["types"]:
        item["unresolved_symbols"] = [B]
    for day in days[3:5]:
        saved[day]["environment"].update(breadth=-.5, median_return_pct=-1.)
    for item in saved[days[4]]["types"]:
        item.update(status="not_matched", candidates=[])
    original = deepcopy(saved)
    arguments = dict(panel_by_day=saved, source_days=days, calendar_payload=CALENDAR,
        scope_members=[A, B], start=days[0], end=days[-1], as_of="2026-10-01T17:00:00+08:00")
    result = readiness.summarize_panels(**arguments, environment_exit_ablation=True)
    assert saved == original and result["case_count"] == 48
    identity, cap = "relay_continuation_leader", "causal_first_seen_cap20"
    cases = {row["guard"]: row for row in result["cases"] if row["type"] == identity and row["capacity"] == cap}
    unguarded, guarded, control = (cases[key] for key in ("unguarded", "two_close_envguard", readiness.CONTROL_GUARD))
    assert unguarded["decision_day_count"] == 8 and unguarded["nonempty_candidate_days"] == 7
    assert unguarded["membership_unknown_days"] == 1
    assert unguarded["potential_entry_dates"] == [days[i] for i in (0, 2, 3, 5, 6)]
    assert guarded["potential_entry_dates"] == control["potential_entry_dates"] == [days[2], days[6]]
    assert guarded["environment_mode_counts"]["EXIT_ALL"] == 2
    assert "EXIT_ALL" not in control["environment_mode_counts"] and control["environment_exit_suppressed_days"] == 2
    assert unguarded["failure_reason_counts"] == {
        "type_membership_unknown_in_fixed_panel": 1,
        "type_empty_or_capacity_filtered_all_no_signal": 1,
        "withheld_missing_exact_successor_full_source": 1}
    assert unguarded["common_unresolved_symbols"] == [{"symbol": B, "decision_days": 1}]
    assert all(row["metrics"] is None and row["execution_feasibility_verified"] is False for row in result["cases"])
    assert result["engine_called"] is False and result["execution_admitted"] is False

    # A future candidate can alter neither an earlier rank nor an earlier entry date.
    for item in saved[days[-1]]["types"]:
        item["candidates"] = [{"symbol": B, "score": 999999999.}]
    later = readiness.summarize_panels(**arguments)
    assert later["case_count"] == 32
    assert next(row for row in later["cases"] if row["case_id"] == unguarded["case_id"])["potential_entry_dates"] == unguarded["potential_entry_dates"]

    # Explicit unknown/known-empty stay non-entry with unknown PnL, never zero return.
    for panel in saved.values():
        for item in panel["types"]:
            item.update(status="unknown", unresolved_symbols=[B], candidates=[])
    absent = readiness.summarize_panels(**arguments)
    assert all(row["status"] == "no_possible_entry_from_saved_panel" and row["potential_entry_days"] == 0
        and row["first_potential_entry_day"] is None and row["metrics"] is None for row in absent["cases"])
    saved[days[0]]["types"][0]["candidates"] = [{"symbol": A, "score": 1., "source_day": days[1]}]
    with pytest.raises(ValueError, match="Future or differently dated"):
        readiness.summarize_panels(**arguments)
