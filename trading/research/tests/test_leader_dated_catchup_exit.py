"""Synthetic paths through the real Engine/Ledger, never market performance."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime

import pandas as pd
import pytest

from trading import engine
from trading.contracts import PortfolioIntent, RunContext, TradingMode
from trading.engine import ReplayRiskExitIntent, advance
from trading.ledger import Ledger, new_state
from trading.research import leader_dated_catchup_exit as m
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config
from trading.research.shortline_decision_v2 import ShortlineDecisionFeedV2

A='600001.SSE'
DAYS=['2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19','2026-10-20']
CONFIG=_config(100000,5)


def setup(monkeypatch):
    monkeypatch.setattr(engine,'_published_now',lambda:datetime.fromisoformat('2026-10-31T20:00:00+08:00'))
    bars=pd.DataFrame([{'date':d,'symbol':A,'open':10.,'high':10.2,'low':9.8,
        'close':10.,'preclose_for_limits':10.,'volume':10_000_000.,'amount':100_000_000.,'is_st':False} for d in DAYS])
    base=ShortlinePanelFeed(bars,{},[A]); base.instruments[A]['ipoDate']='1999-01-01'
    feed=ShortlineDecisionFeedV2(base,{})
    context=RunContext('synthetic_dated_catchup',TradingMode.REPLAY)
    state=new_state(CONFIG.initial_cash)
    return feed,context,state


def step(feed,context,state,index,targets=None):
    day=DAYS[index]
    metadata={'context':context,'decided_at':day+'T18:00:00+08:00',
        'execution_not_before':DAYS[index+1],'source':'synthetic_only'}
    intent=PortfolioIntent.no_change('synthetic_unknown',**metadata) if targets is None else PortfolioIntent.full_target(targets,**metadata)
    risk=m.risk_policy('sector_catchup_leader',state=state,
        candidate_by_day={DAYS[0]:[{'symbol':A,'source_day':DAYS[0],'setup_low':9.8}]},feature_by_day={})
    return advance(state,feed,day,CONFIG,index,context=context,published_close_intent=intent,
        published_close_target_policy=m.target_policy(feed,state,CONFIG),replay_risk_exit_policy=risk)


def book(state,context): return Ledger(state,CONFIG,context)


def test_structural_exit_halt_recovery_new_target_cannot_cancel_residual_then_real_fill(monkeypatch):
    feed,context,state=setup(monkeypatch)
    step(feed,context,state,0,{A:.2})
    feed.rows[DAYS[1]][A].update(low=9.6,close=9.7)
    step(feed,context,state,1)
    original=book(state,context).quantity(A)
    assert original>0 and book(state,context).available(A,DAYS[1])==0
    assert state['pending_risk_exits']['execution_on']==DAYS[2]
    feed.rows[DAYS[2]][A].update(tradestatus=0,volume=0.,amount=0.)
    feed.volumes.at[DAYS[2],A]=0.
    step(feed,context,state,2,{A:.2})
    assert book(state,context).quantity(A)==original
    assert state['pending_targets']=={} and state['pending_risk_exits']['execution_on']==DAYS[3]
    assert any(e['kind']=='order_blocked' and e.get('reason')=='suspended' for e in state['events'])
    assert any(e['kind']=='dated_catchup_full_exit_residual_requeued' for e in state['events'])
    # Restore through JSON-equivalent state and fresh policies; prior-day zero
    # volume can cause another blocked/partial execution and must not lose exit.
    import json
    state=json.loads(json.dumps(state))
    step(feed,context,state,3)
    if book(state,context).quantity(A):
        assert state['pending_risk_exits']['execution_on']==DAYS[4]
    step(feed,context,state,4)
    assert book(state,context).quantity(A)==0
    buys=[f for f in state['fills'].values() if f['side']=='buy']
    sells=[f for f in state['fills'].values() if f['side']=='sell']
    assert sum(f['qty'] for f in buys)==sum(f['qty'] for f in sells)
    assert all(f['day']>DAYS[1] for f in sells)
    assert state['pending_risk_exits'] is None
    # Once actually flat, a later close may make a new entry; this is not a
    # permanent symbol blacklist or a replay of an old buy order.
    step(feed,context,state,5,{A:.2})
    assert state['pending_targets']=={A:.2}


def test_full_target_partial_exit_survives_unknown_current_structure(monkeypatch):
    feed,context,state=setup(monkeypatch)
    step(feed,context,state,0,{A:.2})
    step(feed,context,state,1,{})
    original=book(state,context).quantity(A)
    feed.volumes.at[DAYS[1],A]=10_000.
    step(feed,context,state,2)
    assert 0<book(state,context).quantity(A)<original
    assert state['pending_risk_exits']['symbols']==[A]
    assert state['pending_risk_exits']['execution_on']==DAYS[3]
    step(feed,context,state,3)
    assert book(state,context).quantity(A)==0


def test_partial_reweight_does_not_become_full_exit_and_old_buy_is_not_retried(monkeypatch):
    feed,context,state=setup(monkeypatch)
    step(feed,context,state,0,{A:.2}); step(feed,context,state,1,{A:.1})
    step(feed,context,state,2)
    assert book(state,context).quantity(A)>0 and state.get('pending_risk_exits') is None
    assert not any(e['kind']=='dated_catchup_full_exit_residual_requeued' for e in state['events'])
    feed2,context2,state2=setup(monkeypatch)
    step(feed2,context2,state2,0,{A:.2})
    feed2.rows[DAYS[1]][A]['tradestatus']=0
    step(feed2,context2,state2,1); step(feed2,context2,state2,2)
    assert book(state2,context2).quantity(A)==0 and not state2['fills']
    assert state2['pending_targets'] is None and state2.get('pending_risk_exits') is None


def test_missing_current_quote_raises_before_mutation_not_clear_exit(monkeypatch):
    feed,context,state=setup(monkeypatch)
    step(feed,context,state,0,{A:.2}); step(feed,context,state,1)
    state['pending_risk_exits']=ReplayRiskExitIntent(context,DAYS[1],DAYS[2],(A,),
        'synthetic_only','synthetic_confirmed_exit').to_dict()
    del feed.rows[DAYS[2]][A]
    before=deepcopy(state)
    with pytest.raises(ValueError,match='Unpriceable'):
        step(feed,context,state,2)
    assert state==before
