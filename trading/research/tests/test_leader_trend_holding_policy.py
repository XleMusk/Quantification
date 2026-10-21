"""Synthetic holding-clock mechanisms, not market-performance evidence."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.engine import ReplayRiskExitIntent, advance
from trading.ledger import Ledger, new_state
from trading.research.leader_daily_replay import CommonCohortDecisionFeed
from trading.research.leader_trend_holding_policy import risk_policy, target_policy
from trading.research.leader_type_risk_exit import LeaderTypeRiskExitPolicy
from trading.research.shortline_decision_v2 import BATCH_SCHEMA
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config


A = "600001.SSE"
DAYS = pd.bdate_range("2026-10-12", periods=11).strftime("%Y-%m-%d").tolist()
CONFIG = _config(100000, 5)
CONTEXT = RunContext("synthetic_trend_no_holding_clock", TradingMode.REPLAY)


def raw():
    return dict(open=10., high=10.2, low=9.8, close=10., preclose=10.,
        volume=10_000_000., amount=100_000_000., tradestatus=1, isST=0, adjustflag="3")


def candidate(day):
    return dict(symbol=A, date=day, source_day=day, board="main", setup_low=9.8,
        structure_high=10.2, evidence={})


def features(*, unknown=False):
    return {day: {A: dict(date=day, symbol=A, sma20=None if unknown else 9.5, sma60=9.)}
            for day in DAYS}


def held_fixture(*, unknown=False):
    state = new_state(CONFIG.initial_cash)
    book = Ledger(state, CONFIG, CONTEXT)
    book.submit("buy", DAYS[1], A, "buy", 100, 1000)
    book.fill("fill", "buy", 100, 1000, DAYS[1], DAYS[2])
    feed = SimpleNamespace(calendar=DAYS, rows={day: {A: raw()} for day in DAYS})
    inputs = dict(candidate_by_day={DAYS[0]: [candidate(DAYS[0])]},
        feature_by_day=features(unknown=unknown), complete_feature_days=DAYS)
    return book, feed, inputs


@pytest.mark.parametrize("unknown", [False, True])
def test_healthy_or_unknown_structure_after_five_sessions_has_no_expiry_but_old_policy_does(unknown):
    book, feed, inputs = held_fixture(unknown=unknown)
    old = LeaderTypeRiskExitPolicy("trend_leader", **inputs)
    new = risk_policy("trend_leader", **inputs)
    late = DAYS[7]
    assert isinstance(old(late, book, feed), ReplayRiskExitIntent)
    original_positions = deepcopy(book.state["positions"])
    assert new(late, book, feed) is None
    assert book.state["positions"] == original_positions
    assessment = [e for e in book.state["events"] if e["kind"] == "leader_type_risk_assessed"][-1]
    assert assessment["status"] == ("unknown" if unknown else "not_invalid")
    assert assessment["expiry_enabled"] is False
    if unknown:
        assert book.state["events"][-1]["expiry_enabled"] is False
        assert "independent_expiry_not_suppressed" not in book.state["events"][-1]
    with pytest.raises(ValueError, match="declared exchange session"):
        new.expiry("2026-10-18", book, feed)
    # A native execution wrapper owns held-quote validation. The empty expiry
    # call must not add the legacy strict quote check a second time.
    feed.rows[late][A].update(tradestatus=0, volume=None, amount=None)
    assert new.expiry(late, book, feed) is None


def test_original_SMA_structure_break_still_queues_same_next_session_reduction():
    book, feed, inputs = held_fixture()
    inputs["feature_by_day"][DAYS[3]][A]["sma20"] = 11.
    policy = risk_policy("trend_leader", **inputs)
    result = policy(DAYS[3], book, feed)
    assert isinstance(result, ReplayRiskExitIntent)
    assert result.symbols == (A,) and result.execution_on == DAYS[4]
    assert result.reason == "fixed_trend_structure_invalid_no_calendar_holding_expiry"
    assessment = [e for e in book.state["events"] if e["kind"] == "leader_type_risk_assessed"][-1]
    assert assessment["status"] == "invalid"
    assert assessment["entry"]["fill_id"] == "fill"
    assert not [f for f in book.state["fills"].values() if f["side"] == "sell"]


def engine_fixture(*, environment_exit_day=None):
    bars = pd.DataFrame([dict(date=day, symbol=A, open=10., high=10.2, low=9.8,
        close=10., preclose_for_limits=10., volume=10_000_000., amount=100_000_000., is_st=False)
        for day in DAYS])
    base = ShortlinePanelFeed(bars, {}, [A])
    base.instruments = {A: {"ipoDate": "2000-01-01", "outDate": ""}}
    batches = {day: dict(schema=BATCH_SCHEMA, day=day, status="complete", source="synthetic_saved_trend",
        candidates=[] if day == environment_exit_day else [{"symbol": A, "score": 1.}]) for day in DAYS}
    if environment_exit_day:
        for day in DAYS[DAYS.index(environment_exit_day) + 1:]:
            batches[day].update(status="incomplete", candidates=[])
    state = new_state(CONFIG.initial_cash)
    feed = CommonCohortDecisionFeed(base, batches, state, market_defense=False)
    policy = risk_policy("trend_leader", candidate_by_day={day: [candidate(day)] for day in DAYS},
        feature_by_day=features(), complete_feature_days=DAYS)
    return state, feed, policy


def test_target_factory_does_not_silently_reapply_five_day_exit_to_original_rank_targets():
    state, feed, policy = engine_fixture()
    assert target_policy(feed, state, CONFIG) is None
    for index, day in enumerate(DAYS[:9]):
        advance(state, feed, day, CONFIG, index, context=CONTEXT,
            target_policy=target_policy(feed, state, CONFIG), replay_risk_exit_policy=policy)
    assert state["targets"] == {A: .16}
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) > 0
    assert state.get("pending_risk_exits") is None
    assert not [e for e in state["events"] if e.get("reason") == "shortline_v2_fixed_five_session_holding_exit"]


def test_explicit_environment_empty_full_target_still_sells_through_same_engine_next_open():
    exit_day = DAYS[7]
    state, feed, policy = engine_fixture(environment_exit_day=exit_day)
    for index, day in enumerate(DAYS[:10]):
        advance(state, feed, day, CONFIG, index, context=CONTEXT,
            target_policy=target_policy(feed, state, CONFIG), replay_risk_exit_policy=policy)
    exits = [e for e in state["events"] if e["kind"] == "targets_generated" and e["day"] == exit_day]
    assert exits and exits[-1]["targets"] == {} and exits[-1]["execution_not_before"] == DAYS[8]
    sells = [f for f in state["fills"].values() if f["side"] == "sell" and f["day"] >= exit_day]
    assert sells and {f["day"] for f in sells} == {DAYS[8]}
    assert all(f["available_day"] > f["day"] for f in state["fills"].values() if f["side"] == "buy")
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 0
