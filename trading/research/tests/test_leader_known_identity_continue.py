"""New checkpoint behavior on tiny synthetic shared-Engine accounts only."""
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_known_identity_continue as m
from trading.research import leader_known_identity_scope as scope
from trading.research import leader_eight_type_panel_replay as replay
from trading.research.leader_momentum_source_continue import check_unconsumed_action
from trading.research.tests.test_leader_known_identity_scope import fixture
from trading.research.tests.test_leader_eight_type_panel_replay import (
    A, B, SESSIONS, PREVIOUS, CALENDAR, raw, qualification,
)


def setup():
    days, panels, _ = fixture()
    paths, original = replay._rank_path(panels, {A, B}, days[0], days[4])
    environment = replay._environment(panels, SESSIONS, max(p["available_at"] for p in panels.values()))
    broken = raw(days)
    broken.loc[broken.date.eq(days[4]) & broken.code.eq("sz."+A[:6]), "preclose"] = "9"
    return days, panels, paths, original, environment, broken


def invoke(data, bars, *, guard="unguarded", cap="causal_first_seen_cap20", resume=None):
    days, panels, paths, _, env, _ = data
    return replay._case(bars, days, SESSIONS, PREVIOUS, panels, paths[scope.TYPE_ID][cap], env,
        {A, B}, qualification(), {"day": PREVIOUS[days[0]], "closes": {A: 10., B: 10.}},
        days[0], days[4], scope.TYPE_ID, cap, guard, "synthetic-continuation:"+guard+":"+cap,
        entry_membership_policy=scope.entry_membership_policy, resume_case=resume)


def test_resume_commits_only_suffix_keeps_fifo_risk_anchor_and_prefix(monkeypatch):
    data = setup()
    days = data[0]
    parent = invoke(data, data[-1])
    assert parent["engine_sessions_committed"] == 4
    before = deepcopy(parent)
    uninterrupted = invoke(data, raw(days))
    calls = []
    actual = replay.advance
    def observed(state, feed, day, config, index, **kwargs):
        calls.append((day, index))
        return actual(state, feed, day, config, index, **kwargs)
    monkeypatch.setattr(replay, "advance", observed)
    result = invoke(data, raw(days), resume=parent)
    assert parent == before
    assert calls == list(zip(days[4:], range(4, len(days))))
    assert result["reused_sessions"] == 4 and result["newly_completed_sessions"] == len(days)-4
    assert result["complete_PnL_available"] is True
    assert result["account"] == uninterrupted["account"]
    assert result["daily"] == uninterrupted["daily"] and result["decisions"] == uninterrupted["decisions"]
    assert result["daily"][:4] == parent["daily"] and result["decisions"][:4] == parent["decisions"]
    assert result["account"]["events"][:len(parent["account"]["events"])] == parent["account"]["events"]
    assessment = next(e for e in result["account"]["events"] if e["kind"] == "leader_type_risk_assessed" and e["day"] == days[4])
    assert assessment["entry"]["decision_day"] == days[2]
    first_buy = next(f for f in result["fills"] if f["side"] == "buy")
    assert first_buy["day"] == days[3] and first_buy["available_day"] == days[4]
    assert next(f for f in result["fills"] if f["side"] == "sell")["day"] == days[8]
    assert result["prior_blocking_evidence"] == parent["blocking_evidence"]


def test_new_later_block_preserves_prefix_null_and_wrong_context_rejected(monkeypatch):
    data = setup()
    days = data[0]
    parent = invoke(data, data[-1])
    later = raw(days)
    later.loc[later.date.eq(days[6]) & later.code.eq("sz."+A[:6]), "preclose"] = "9"
    result = invoke(data, later, resume=parent)
    assert result["engine_sessions_committed"] == 6 and result["newly_completed_sessions"] == 2
    assert result["metrics"] is None and result["complete_PnL_available"] is False
    assert result["blocking_evidence"]["day"] == days[6]
    assert result["account"]["positions"] and result["daily"][:4] == parent["daily"]
    wrong = deepcopy(parent)
    wrong["account"]["replay_risk_exit_policy"]["context"]["account_id"] = "different-account"
    monkeypatch.setattr(replay, "advance", lambda *a, **k: pytest.fail("bad seed must not reach Engine"))
    with pytest.raises(ValueError, match="original context"):
        invoke(data, raw(days), resume=wrong)


def test_driver_loads_once_uses_saved_paths_and_reuses_all_unselected(tmp_path, monkeypatch):
    data = setup()
    days, panels, paths, original, environment, broken = data
    cases = [invoke(data, broken if cap == "uncapped" else raw(days), guard=guard, cap=cap)
        for guard in replay.GUARDS for cap, _ in replay.CAPS]
    # Their source is unchanged for this test; _load_registration is separately
    # responsible for admitting the independently checked source amendment.
    saved = {"source_days": days, "start": days[0], "end": days[4], "scope_members": [A, B],
        "cases": cases, "capacity_paths": {scope.TYPE_ID: paths[scope.TYPE_ID]},
        "raw_top5_paths": {scope.TYPE_ID: original[scope.TYPE_ID]}, "environment_decisions": environment}
    context_file = tmp_path / "context.parquet"
    raw([PREVIOUS[days[0]]]).to_parquet(context_file)
    registration = tmp_path / "registration" / "registration.json"
    registration.parent.mkdir()
    registration.write_text("{}")
    metadata = tmp_path / "inputs" / "manifest.json"
    metadata.parent.mkdir()
    metadata.write_text("{}")
    parent = tmp_path / "parent" / "result.json"
    parent.parent.mkdir()
    parent.write_text("{}")
    event, ref = tmp_path / "event", tmp_path / "reference"
    event.mkdir(); ref.mkdir()
    selected = [c["case_id"] for c in cases if c["capacity"] == "uncapped"]
    value = {"binding": {"continued_case_ids": selected},
        "parent_result": m._pin(parent), "origin_registration": m._pin(registration),
        "action_pair": {"event": {"directory": str(event)}, "reference": {"directory": str(ref)}}}
    record = {"inputs": str(metadata)}
    monkeypatch.setattr(m, "_load_registration", lambda p: (value, saved, record, event, ref))
    loaded = {"source_days": days, "raw": raw(days), "panel_by_day": panels, "calendar_payload": CALENDAR,
        "scope_members": [A, B], "qualification": qualification(),
        "context_anchor": {"day": PREVIOUS[days[0]], "closes": {A: 10., B: 10.}}}
    manifest = {"source_days": days, "start": days[0], "end": days[4], "scope_members": [A, B],
        "context_raw_file": str(context_file)}
    loads = []
    monkeypatch.setattr(m, "load_inputs", lambda p: (loads.append(p) or loaded, manifest))
    monkeypatch.setattr(m, "_validate_panel_data", lambda *a: ({A, B}, SESSIONS, PREVIOUS, None))
    monkeypatch.setattr(m, "load_action_accounting", lambda *a, **k: object())
    class Factory:
        provenance = {"synthetic": True}
        def __init__(self, accounting): pass
        def __call__(self, base, source_days): return None
    monkeypatch.setattr(m, "HistoricalExecutionFactory", Factory)
    actual, calls = m._case, []
    def observe(*args, **kwargs):
        old = kwargs["resume_case"]
        assert args[5] is saved["capacity_paths"][scope.TYPE_ID][old["capacity"]]
        assert args[6] is saved["environment_decisions"]
        calls.append(old["case_id"])
        return actual(*args, **kwargs)
    monkeypatch.setattr(m, "_case", observe)
    result = m.run(registration, tmp_path / "continued")
    assert len(loads) == 1 and calls == selected
    assert result["continued_case_count"] == 2 and result["reused_case_count"] == 2
    for old, new in zip(cases, result["cases"]):
        if old["case_id"] not in selected:
            assert new == old
        else:
            assert new["reused_sessions"] == 4 and new["complete_PnL_available"] is True
    assert result["raw_top5_paths"] == saved["raw_top5_paths"]
    assert result["old_committed_sessions_replayed"] is False
    with pytest.raises(FileExistsError):
        m.run(registration, tmp_path / "continued")


def test_source_admission_requires_existing_registration_and_future_economics():
    action = {"symbol": A, "dividOperateDate": "2026-06-30", "dividRegistDate": "2026-06-29", "dividPayDate": "2026-06-30"}
    key = A+":2026-06-30"
    account = {"day": "2026-06-29", "entitlements": {key: 500}, "receivables": {}, "events": []}
    assert check_unconsumed_action(action, account)["registered_quantity"] == 500
    zero = deepcopy(account); zero["entitlements"][key] = 0
    assert check_unconsumed_action(action, zero)["registered_quantity"] == 0
    missing = deepcopy(account); missing["entitlements"] = {}
    with pytest.raises(ValueError, match="existing explicit registered"):
        check_unconsumed_action(action, missing)
    past = deepcopy(account); past["day"] = "2026-06-30"
    with pytest.raises(ValueError, match="nonfuture"):
        check_unconsumed_action(action, past)
    paid = deepcopy(account); paid["receivables"][key] = {"amount": 500}
    with pytest.raises(ValueError, match="already has an economic"):
        check_unconsumed_action(action, paid)
