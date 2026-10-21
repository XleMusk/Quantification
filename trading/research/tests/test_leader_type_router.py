"""Small real-Engine router transitions; no saved market inputs or returns."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_historical_execution_policy import HistoricalDecisionDayFeed, HistoricalNativeRiskValidation
from trading.research.leader_type_router import LeaderTypeRouter, POLICY_ID, STATE_KEY, TYPE_IDS
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config
from trading.research.recent_alpha158_panel_replay import _raw_issues, _reference


A, B = "600001.SSE", "600002.SSE"
CAL = pd.bdate_range("2026-10-01", periods=23).strftime("%Y-%m-%d").tolist()
DAYS = CAL[6:]
CONFIG = _config(100000, 5)


def fixture(*, guard="unguarded", end_index=10, historical=False):
    bars = pd.DataFrame([dict(date=day, symbol=symbol, open=10., high=10.2, low=9.8,
        close=10., preclose_for_limits=10., volume=10_000_000., amount=100_000_000., is_st=False)
        for day in CAL for symbol in (A, B)])
    base = ShortlinePanelFeed(bars, {}, [A, B])
    base.context_anchor = {"day": CAL[5], "closes": {A: 10., B: 10.}}
    qualification = IpoQualification({symbol: {"ipo_date": "2000-01-01", "available_at": "2000-01-01T00:00:00+08:00",
        "source_basis": "synthetic_listing", "board": "main"} for symbol in (A, B)}, tuple(CAL), {})
    panels, paths, env = {}, {identity: {"uncapped": {}} for identity in TYPE_IDS}, {}
    for day in DAYS:
        candidates = [dict(symbol=A, score=1., date=day, source_day=day, board="main", evidence={})]
        types = [dict(id=identity, status="matched", candidates=deepcopy(candidates), unresolved_symbols=[])
                 for identity in TYPE_IDS]
        feature = {symbol: dict(date=day, symbol=symbol, listing_feature_policy_verified=True,
            ipo_feature_support=True, qualified_limit_up=True, qualified_stage=2,
            sma20=9.5, sma60=9., amount_rank_pct=.995, board="main") for symbol in (A, B)}
        panels[day] = dict(types=types, risk_features=feature, risk_feature_scope_complete=True)
        for identity in TYPE_IDS:
            paths[identity]["uncapped"][day] = dict(candidates=deepcopy(candidates))
        env[day] = {"per_type": {identity: {"mode": "ALLOW_FIXED_CANDIDATES"} for identity in TYPE_IDS}}
    context = RunContext("synthetic_router:" + guard, TradingMode.REPLAY)
    execution = None
    if historical:
        previous = {date: CAL[index - 1] if index else None for index, date in enumerate(CAL)}
        execution = SimpleNamespace(raw_issues=lambda day, required: _raw_issues(base, day, required),
            references=lambda day, required: [_reference(base, day, symbol, previous) for symbol in sorted(required)],
            reference_ok=lambda row: row["status"] == "consistent_proxy")
        execution.decision_feed = lambda batches, state: HistoricalDecisionDayFeed(base, batches, state, execution)
        execution.wrap_risk_policy = lambda policy: HistoricalNativeRiskValidation(policy, execution)
    router = LeaderTypeRouter(base, CONFIG, context, panels=panels, paths=paths,
        environments=env, sessions=CAL, source_days=DAYS, members={A, B}, qualification=qualification,
        execution=execution, start=DAYS[0], end=DAYS[end_index], guard=guard, capacity="uncapped")
    return router, new_state(100000), panels, paths, env


def take(router, state, index, **kwargs):
    original = deepcopy(state)
    result = router.step(state, DAYS[index], index, **kwargs)
    assert state == original, "Only a successful returned trial may become committed state"
    return result


def test_calendar_then_allowed_type_before_contents_unknown_and_empty_never_fallback():
    router, state, panels, paths, env = fixture()
    panels[DAYS[0]]["types"][0]["status"] = "unknown"
    paths[TYPE_IDS[1]]["uncapped"][DAYS[0]]["candidates"] = [dict(symbol=B, score=99.)]
    state, _, audit = take(router, state, 0)
    assert audit["selected_type"] == TYPE_IDS[0] and audit["status"] == "NO_CHANGE"
    assert state["pending_targets"] is None and not state["orders"]
    paths[TYPE_IDS[1]]["uncapped"][DAYS[1]]["candidates"] = []
    state, _, audit = take(router, state, 1)
    assert audit["selected_type"] == TYPE_IDS[1] and audit["status"] == "NO_CHANGE"
    guarded, empty, panels, _, env = fixture(guard="two_close_envguard")
    for identity in TYPE_IDS[:2]:
        env[DAYS[0]]["per_type"][identity]["mode"] = "NO_CHANGE"
    panels[DAYS[0]]["types"][2]["status"] = "unknown"
    _, _, audit = take(guarded, empty, 0)
    assert audit["selected_type"] == TYPE_IDS[2] and audit["status"] == "NO_CHANGE"
    with pytest.raises(ValueError, match="calendar offset"):
        router.step(state, DAYS[2], 1)


def test_old_pending_fill_preserves_shares_then_environment_exit_flat_releases_next_day_only():
    router, state, _, _, env = fixture(guard="two_close_envguard")
    state, _, _ = take(router, state, 0)
    assert state["pending_targets"] == {A: .16}
    state, row, audit = take(router, state, 1)
    assert audit["close_rule"] == "old_pending_actual_fill_preserve_exact_shares"
    assert state["pending_targets"] is None and _quantity(state, A) > 0
    assert {f["available_day"] for f in state["fills"].values()} == {DAYS[2]}
    env[DAYS[2]]["per_type"][TYPE_IDS[0]]["mode"] = "EXIT_ALL"
    state, _, _ = take(router, state, 2)
    assert state["pending_targets"] == {} and state[STATE_KEY]["exit_sticky"] is True
    state, row, audit = take(router, state, 3)
    assert _quantity(state, A) == 0 and audit["selected_type"] == TYPE_IDS[0]
    assert audit["close_rule"] == "actual_flat_cancel_old_pending_then_release_owner"
    assert state[STATE_KEY]["active_type"] is None and state["pending_targets"] == {}
    assert not [o for o in state["orders"].values() if o["day"] == DAYS[3] and o["side"] == "buy"]
    state, _, audit = take(router, state, 4)
    assert audit["selected_type"] == TYPE_IDS[4]
    assert state["replay_risk_exit_policy"]["policy_id"] == POLICY_ID


def _quantity(state, symbol):
    return sum(lot["qty"] for lot in state["positions"].get(symbol, []))


def test_unfilled_old_positive_pending_is_cancelled_and_does_not_rotate_same_close():
    router, state, _, _, _ = fixture()
    state, _, _ = take(router, state, 0)
    router.base.rows[DAYS[1]][A].update(open=11., high=11., low=11., close=11.)
    for day in DAYS[2:]:
        router.base.rows[day][A].update(open=11., high=11.2, low=10.8, close=11., preclose=11.)
    state, row, audit = take(router, state, 1)
    assert not state["fills"] and state["pending_targets"] == {}
    assert audit["selected_type"] == TYPE_IDS[0] and state[STATE_KEY]["active_type"] is None
    assert any(e["kind"] == "order_blocked" for e in row["events"])
    state, _, audit = take(router, state, 2)
    assert audit["selected_type"] == TYPE_IDS[2] and state["pending_targets"] == {A: .16}


def test_confirmed_structure_sticky_basket_exit_retries_blocked_sale_no_new_type_or_FIFO_wash():
    router, state, panels, paths, _ = fixture()
    for index in (0, 1):
        state, _, _ = take(router, state, index)
    anchor_fill = deepcopy(next(iter(state["fills"].values())))
    panels[DAYS[2]]["risk_features"][A]["qualified_limit_up"] = False
    paths[TYPE_IDS[0]]["uncapped"][DAYS[2]]["candidates"] = [dict(symbol=B, score=10.)]
    state, row, audit = take(router, state, 2)
    assert state["pending_targets"] == {} and state[STATE_KEY]["exit_sticky"]
    assert len([e for e in row["events"] if e["kind"] == "leader_type_risk_assessed"]) == 1
    router.base.rows[DAYS[3]][A].update(open=9., high=9., low=9., close=9., preclose=10.)
    for day in DAYS[4:]:
        router.base.rows[day][A].update(open=9., high=9.2, low=8.8, close=9., preclose=9.)
    state, row, audit = take(router, state, 3)
    assert _quantity(state, A) > 0 and state[STATE_KEY]["active_type"] == TYPE_IDS[0]
    assert state["pending_targets"] == {} and not _quantity(state, B)
    assert any(e["kind"] == "order_blocked" for e in row["events"])
    assert next(iter(state["fills"].values())) == anchor_fill
    state, _, _ = take(router, state, 4)
    assert _quantity(state, A) == 0 and state[STATE_KEY]["active_type"] is None


def test_unknown_risk_does_not_start_exit_original_five_day_expiry_and_tail_still_reduce():
    router, state, panels, _, _ = fixture(end_index=6)
    for day in DAYS:
        panels[day]["risk_features"][A]["qualified_limit_up"] = None
    for index in range(6):
        state, row, audit = take(router, state, index)
        if index < 5:
            assert not state[STATE_KEY]["exit_sticky"]
    assert state[STATE_KEY]["exit_sticky"] is True and state["pending_targets"] == {}
    assert len([e for e in row["events"] if e["kind"] == "risk_exit_scheduled"]) == 1
    state, _, _ = take(router, state, 6)
    assert not _quantity(state, A)
    for index in (7, 8):
        state, row, audit = take(router, state, index)
        assert audit["selected_type"] is None and audit["status"] == "NO_CHANGE"
    state, _, audit = take(router, state, 9, tail_mode="EXIT_ALL")
    assert state["pending_targets"] == {} and audit["selected_type"] is None


def test_D_known_expiry_empty_FULL_does_not_depend_on_E_unknown_execution_quote():
    router, state, panels, _, _ = fixture(historical=True)
    for day in DAYS:
        panels[day]["risk_features"][A]["qualified_limit_up"] = None
    for index in range(5):
        state, _, _ = take(router, state, index)
    router.base.rows[DAYS[6]][A].update(open=float("nan"), volume=float("nan"), amount=float("nan"))
    state, row, audit = take(router, state, 5)
    assert state["pending_targets"] == {} and state[STATE_KEY]["exit_sticky"] is True
    assert state[STATE_KEY]["exit_reason"] == "original_D_five_session_basket_exit"
    assert state.get("pending_risk_exits") is None
    assert any(event["kind"] == "risk_exit_deferred" for event in row["events"])
    assert audit["close_rule"] == "sticky_owner_basket_empty_FULL"
