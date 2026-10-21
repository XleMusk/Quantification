"""Only new entry-policy paths: actual shared Engine fills, no historical runs."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine
from trading.config import StrategyConfig
from trading.contracts import IntentKind, PortfolioIntent, RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.research.leader_entry_confirmation_policy import (
    ARM_IDS, POLICY_IDS, PublishedEntryConfirmationReduction,
)
from trading.research.leader_published_forward_run import PublishedFiveSessionExpiryPolicy


DAYS = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08",
        "2025-01-09", "2025-01-10", "2025-01-13", "2025-01-14"]
D, E = DAYS[5:7]
A, B = "600000.SSE", "000001.SZSE"
CONTEXT = RunContext("synthetic_entry_confirmation", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, gross_limit=.4,
                        single_limit=.2, rebalance_sessions=1, slippage_bps=0)


@pytest.fixture(autouse=True)
def published_clock(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(DAYS[-1] + "T20:00:00+08:00"))


def feed():
    row = dict(open=10., high=10.5, low=9.5, close=10., preclose=10.,
               volume=1_000_000., amount=10_000_000., tradestatus=1, isST=0, adjustflag="3")
    def forbidden_scores(*args):
        raise AssertionError("An explicit published intent cannot fall back to old scores")
    return SimpleNamespace(calendar=DAYS, rows={d:{s:deepcopy(row) for s in (A,B)} for d in DAYS},
        volumes=pd.DataFrame(1_000_000., index=DAYS, columns=[A,B]),
        instruments={s:{"ipoDate":"2000-01-01", "outDate":""} for s in (A,B)},
        actions=[], next_day=lambda d:DAYS[DAYS.index(d)+1], scores=forbidden_scores,
        market_defense=False)


def intent(weights=None, *, day=D, no_change=False):
    value = PortfolioIntent.full_target({A:.2,B:.2} if weights is None else weights,
        context=CONTEXT, decided_at=day+"T18:45:00+08:00",
        execution_not_before=DAYS[DAYS.index(day)+1], source="saved:synthetic_original_publication")
    return replace(value, kind=IntentKind.NO_CHANGE, weights=(), reason="original_incomplete_batch") if no_change else value


def seeded(day=DAYS[4]):
    state = new_state(CONFIG.initial_cash)
    book = Ledger(state, CONFIG, CONTEXT)
    book.submit("seed", day, A, "buy", 200, 1000)
    book.fill("seed:fill", "seed", 200, 1000, day, DAYS[DAYS.index(day)+1])
    book.mark_price(A,1000,day)
    return state


def step(state, base, day, publication, reducer, risk=None):
    return engine.advance(state, base, day, CONFIG, DAYS.index(day), context=CONTEXT,
        published_close_intent=publication, published_close_target_policy=reducer,
        replay_risk_exit_policy=risk)


def confirmation(a="known_false", b="known_true"):
    return {A:{"status":a,"reason":"synthetic_A"},B:{"status":b,"reason":"synthetic_B"}}


def test_actual_opening_partial_buy_is_held_and_T1_locked_not_a_new_entry():
    state, base = new_state(CONFIG.initial_cash), feed()
    state["pending_targets"] = {A:.2}
    base.volumes.at[DAYS[4],A] = 10_000.  # Only 100 of the desired 200 shares can fill.
    reducer = PublishedEntryConfirmationReduction(base,state,CONFIG,
        arm_id="entry_only_confirmation",confirmation=confirmation())
    result = step(state,base,D,intent(),reducer)
    book = Ledger(state,CONFIG,CONTEXT)
    assert book.quantity(A) == 100 and book.available(A,D) == 0
    assert sum(o["filled"] for o in state["orders"].values() if o["day"]==D) == 100
    assert state["pending_targets"] == {A:.2,B:.2}
    audit = reducer.observation["evaluations"][0]
    assert audit["post_opening_quantity"]==100 and audit["confirmation_status"]=="known_false"
    assert not audit["confirmation_applied"] and audit["retained_in_final_target"] is True
    assert result["published_close_reduction"]["policy_id"] == POLICY_IDS["entry_only_confirmation"]
    assert audit["position_role"]=="held_or_addition_to_existing"


@pytest.mark.parametrize("arm,expected",[
    ("entry_only_confirmation",{A:.2,B:.2}), ("daily_confirmation",{B:.2})])
def test_existing_position_or_addition_bypasses_only_entry_arm_without_refill(arm,expected):
    state, base = seeded(), feed()
    # A's target can require an addition as equity rises; positive current qty still defines holding.
    state["cash"] += 500_000
    reducer = PublishedEntryConfirmationReduction(base,state,CONFIG,arm_id=arm,confirmation=confirmation())
    step(state,base,D,intent(),reducer)
    assert Ledger(state,CONFIG,CONTEXT).quantity(A)==200
    assert state["pending_targets"]==expected
    assert dict(reducer.observation["final_intent"]["weights"]) == expected
    assert sum(expected.values())<=.4 and reducer.observation["weights_renormalized"] is False
    assert reducer.observation["evaluations"][0]["confirmation_applied"] is (arm=="daily_confirmation")


def test_unknown_new_entry_keeps_NO_CHANGE_but_independent_expiry_queues_and_sells():
    state, base = seeded(DAYS[0]), feed()
    original = intent()
    reducer = PublishedEntryConfirmationReduction(base,state,CONFIG,
        arm_id="entry_only_confirmation",confirmation=confirmation(b="unknown"))
    risk = PublishedFiveSessionExpiryPolicy()
    row = step(state,base,D,original,reducer,risk)
    reduced = row["published_close_reduction"]["reduced_intent"]
    assert reduced["kind"]=="NO_CHANGE" and state["pending_targets"] is None
    assert state["pending_risk_exits"]["symbols"]==[A]
    assert state["pending_risk_exits"]["execution_on"]==E
    for key in ("context","decided_at","execution_not_before","source"):
        assert reduced[key] == original.to_dict()[key]
    assert all(r["retained_in_final_target"] is None for r in reducer.observation["evaluations"])
    following = PublishedEntryConfirmationReduction(base,state,CONFIG,
        arm_id="entry_only_confirmation",confirmation={})
    step(state,base,E,intent(day=E,no_change=True),following,risk)
    assert Ledger(state,CONFIG,CONTEXT).quantity(A)==0
    assert following.observation is None  # Original NO_CHANGE bypasses the hook.
    assert any(f["side"]=="sell" and f["day"]==E for f in state["fills"].values())


def test_complete_empty_target_remains_exit_and_executes_next_open():
    state, base = seeded(), feed()
    reducer = PublishedEntryConfirmationReduction(base,state,CONFIG,
        arm_id="entry_only_confirmation",confirmation=confirmation(a="unknown",b="unknown"))
    step(state,base,D,intent({}),reducer)
    assert state["pending_targets"]=={} and Ledger(state,CONFIG,CONTEXT).quantity(A)==200
    assert reducer.observation["application"]=="original_complete_empty_exit_preserved"
    following = PublishedEntryConfirmationReduction(base,state,CONFIG,
        arm_id="entry_only_confirmation",confirmation={})
    step(state,base,E,intent(day=E,no_change=True),following)
    assert Ledger(state,CONFIG,CONTEXT).quantity(A)==0


def test_original_incomplete_NO_CHANGE_does_not_call_hook_or_clear_held():
    state, base = seeded(), feed()
    class Forbidden(PublishedEntryConfirmationReduction):
        def __call__(self,value):
            raise AssertionError("Original incomplete must not be filtered into cash")
    reducer = Forbidden(base,state,CONFIG,arm_id="daily_confirmation",confirmation={})
    row = step(state,base,D,intent(no_change=True),reducer)
    assert reducer.observation is None and state["pending_targets"] is None
    assert Ledger(state,CONFIG,CONTEXT).quantity(A)==200
    assert "published_close_reduction" not in row


def test_pure_baseline_ignores_confirmation_and_policy_itself_never_mutates_state():
    state, base = new_state(CONFIG.initial_cash), feed()
    before = deepcopy(state)
    reducer = PublishedEntryConfirmationReduction(base,state,CONFIG,
        arm_id="baseline",confirmation=confirmation(a="unknown",b="known_false"))
    original = intent()
    assert reducer(original)==original and state==before
    assert not any(r["confirmation_applied"] for r in reducer.observation["evaluations"])
    assert len(ARM_IDS)==3 and len(set(POLICY_IDS.values()))==3
    # Direct typed NO_CHANGE is also preserved if a non-Engine caller inspects it.
    assert reducer(intent(no_change=True))==intent(no_change=True) and state==before
