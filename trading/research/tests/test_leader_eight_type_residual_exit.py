"""New opt-in lifecycle paths through the real Engine, not market returns."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading import engine
from trading.contracts import PortfolioIntent, RunContext, TradingMode
from trading.engine import ReplayRiskExitIntent, advance
from trading.ledger import Ledger, new_state
from trading.research import leader_eight_type_residual_exit as m
from trading.research.leader_type_risk_exit import TYPE_IDS
from trading.research.shortline_decision_v2 import ShortlineDecisionFeedV2
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config


A, B = "600001.SSE", "600002.SSE"
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]
CONFIG = _config(100000, 5)


def setup(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat("2026-10-31T20:00:00+08:00"))
    bars = pd.DataFrame([{"date": day, "symbol": symbol, "open": 10., "high": 10.2, "low": 9.8,
        "close": 10., "preclose_for_limits": 10., "volume": 10_000_000., "amount": 100_000_000., "is_st": False}
        for day in DAYS for symbol in (A, B)])
    base = ShortlinePanelFeed(bars, {}, [A, B])
    for symbol in (A, B):
        base.instruments[symbol]["ipoDate"] = "1999-01-01"
    feed = ShortlineDecisionFeedV2(base, {})
    context = RunContext("synthetic_eight_type_residual", TradingMode.REPLAY)
    return feed, context, new_state(CONFIG.initial_cash)


def step(feed, context, state, index, identity, targets=None, *, broken=False):
    day = DAYS[index]
    metadata = {"context": context, "decided_at": day + "T18:00:00+08:00",
        "execution_not_before": DAYS[index + 1], "source": "synthetic_only"}
    intent = (PortfolioIntent.no_change("synthetic_unknown", **metadata) if targets is None
        else PortfolioIntent.full_target(targets, **metadata))
    features = {day: {A: {"date": day, "symbol": A, "board": "main",
        "listing_feature_policy_verified": True, "ipo_feature_support": True,
        "qualified_limit_up": False, "qualified_stage": 0,
        "sma20": 10., "sma60": 9., "amount_rank_pct": .98}}} if broken else {}
    risk = m.risk_policy(identity, state=state,
        candidate_by_day={DAYS[0]: [{"symbol": A, "source_day": DAYS[0], "setup_low": 9.8,
            "evidence": {"structure_close": 9.8, "previous_open": 9.8}}]},
        feature_by_day=features, complete_feature_days=features)
    return advance(state, feed, day, CONFIG, index, context=context, published_close_intent=intent,
        published_close_target_policy=m.target_policy(feed, state, CONFIG), replay_risk_exit_policy=risk)


def book(state, context):
    return Ledger(state, CONFIG, context)


@pytest.mark.parametrize("identity", TYPE_IDS)
def test_each_type_keeps_blocked_structure_exit_after_unknown_or_recovery_and_new_target(monkeypatch, identity):
    feed, context, state = setup(monkeypatch)
    step(feed, context, state, 0, identity, {A: .2})
    feed.rows[DAYS[1]][A].update(low=9.6, close=9.7)
    step(feed, context, state, 1, identity, broken=True)
    original = book(state, context).quantity(A)
    assert original > 0 and state["pending_risk_exits"]["execution_on"] == DAYS[2]
    assert [e for e in state["events"] if e["kind"] == "leader_type_risk_assessed"][-1]["status"] == "invalid"
    feed.rows[DAYS[2]][A].update(tradestatus=0, volume=0., amount=0.)
    feed.volumes.at[DAYS[2], A] = 0.
    step(feed, context, state, 2, identity, {A: .2, B: .16})
    assert book(state, context).quantity(A) == original
    # Removed A's allocation stays cash; the other allocation is unchanged.
    assert state["pending_targets"] == {B: .16}
    assert state["pending_risk_exits"]["symbols"] == [A]
    assert state["pending_risk_exits"]["execution_on"] == DAYS[3]
    assessment = [e for e in state["events"] if e["kind"] == "leader_type_risk_assessed"][-1]
    feature_types = {"space_height_leader", "relay_continuation_leader", "trend_leader", "liquidity_capacity_proxy"}
    assert assessment["status"] == ("unknown" if identity in feature_types else "not_invalid")
    assert any(e["kind"] == "order_blocked" and e.get("reason") == "suspended" for e in state["events"])
    state = json.loads(json.dumps(state))
    step(feed, context, state, 3, identity)
    if book(state, context).quantity(A):
        assert state["pending_risk_exits"]["execution_on"] == DAYS[4]
    step(feed, context, state, 4, identity)
    assert book(state, context).quantity(A) == 0
    assert state["pending_risk_exits"] is None
    assert len([f for f in state["fills"].values() if f["side"] == "buy" and f["symbol"] == A]) == 2
    # A subsequent close may enter anew only after actual liquidation.
    step(feed, context, state, 5, identity, {A: .2})
    assert state["pending_targets"] == {A: .2}


def test_partial_full_exit_restores_remaining_and_cannot_be_rebought(monkeypatch):
    feed, context, state = setup(monkeypatch)
    identity = "cross_regime_candidate"
    step(feed, context, state, 0, identity, {A: .2})
    step(feed, context, state, 1, identity, {})
    original = book(state, context).quantity(A)
    feed.volumes.at[DAYS[1], A] = 10_000.
    step(feed, context, state, 2, identity, {A: .2, B: .16})
    assert 0 < book(state, context).quantity(A) < original
    assert state["pending_targets"] == {B: .16}
    assert state["pending_risk_exits"]["symbols"] == [A]
    assert state["pending_risk_exits"]["execution_on"] == DAYS[3]
    state = json.loads(json.dumps(state))
    step(feed, context, state, 3, identity)
    assert book(state, context).quantity(A) == 0
    assert sum(f["qty"] for f in state["fills"].values() if f["symbol"] == A and f["side"] == "buy") == original
    assert sum(f["qty"] for f in state["fills"].values() if f["symbol"] == A and f["side"] == "sell") == original


def test_partial_reweight_is_not_full_exit_and_blocked_buy_is_not_retried(monkeypatch):
    feed, context, state = setup(monkeypatch)
    identity = "trend_leader"
    step(feed, context, state, 0, identity, {A: .2})
    step(feed, context, state, 1, identity, {A: .1})
    step(feed, context, state, 2, identity)
    assert book(state, context).quantity(A) > 0 and state.get("pending_risk_exits") is None
    assert not any(e["kind"] == "eight_type_full_exit_residual_requeued" for e in state["events"])
    feed, context, state = setup(monkeypatch)
    step(feed, context, state, 0, identity, {A: .2})
    feed.rows[DAYS[1]][A]["tradestatus"] = 0
    step(feed, context, state, 1, identity)
    step(feed, context, state, 2, identity)
    assert book(state, context).quantity(A) == 0 and not state["fills"]
    assert state["pending_targets"] is None and state.get("pending_risk_exits") is None


def test_missing_current_quote_preserves_residual_before_mutation(monkeypatch):
    feed, context, state = setup(monkeypatch)
    identity = "turnover_leader"
    step(feed, context, state, 0, identity, {A: .2})
    step(feed, context, state, 1, identity)
    state["pending_risk_exits"] = ReplayRiskExitIntent(context, DAYS[1], DAYS[2], (A,),
        "synthetic_only", "synthetic_confirmed_exit").to_dict()
    del feed.rows[DAYS[2]][A]
    before = deepcopy(state)
    with pytest.raises(ValueError, match="Unpriceable"):
        step(feed, context, state, 2, identity)
    assert state == before
