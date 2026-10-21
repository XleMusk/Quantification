"""Saved raw quote parsing and the real daily PAPER feed, using synthetic files."""
from datetime import datetime
from types import SimpleNamespace
import json

import pytest

from trading import engine, intraday_session
from trading.contracts import PortfolioIntent, PreopenPortfolioIntent, RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.native_intraday_feed import import_quote, load_quote, NativeInputs, PRICE_METHOD
from trading.research.shortline_panel_replay import _config
from trading.research.shortline_native_preopen import QUALIFIED_POLICY_ID

D,E='2026-10-15','2026-10-16'
A='002103.SZSE'


def stamp(value): return datetime.fromisoformat(value)


def snapshot(tmp_path, phase, at, *, price='10.00', preclose='10.00', quote_at=None):
    csv=tmp_path/(phase+'.csv')
    csv.write_text('symbol,market,price_rmb,preclose_rmb,is_st,tradable\n'
        +'002103,SZSE,'+price+','+preclose+',0,1\n',encoding='utf-8')
    out=tmp_path/(phase+'.json')
    record=import_quote(csv,source='synthetic-provider-export',source_kind='public_export',
        quote_asof=quote_at or at,trading_day=E,phase=phase,out=out,now=at)
    return out,csv,record


def test_quote_raw_bytes_and_clock_are_checked_without_making_it_a_broker_quote(tmp_path):
    at=E+'T09:31:00+08:00'
    path,csv,record=snapshot(tmp_path,'opening',at)
    loaded,_=load_quote(path,'opening',E,stamp(E+'T09:31:05+08:00'))
    assert loaded['execution_price_method']==PRICE_METHOD
    assert loaded['broker_quote'] is False and loaded['provider_authenticated'] is False
    with pytest.raises(ValueError,match='stale'):
        load_quote(path,'opening',E,stamp(E+'T09:33:00+08:00'))
    changed=dict(record);changed['prices']={A:{**record['prices'][A],'price_rmb':'10.01'}}
    path.write_text(json.dumps(changed),encoding='utf-8')
    with pytest.raises(ValueError,match='normalized'):
        load_quote(path,'opening',E,stamp(at))
    path.write_text(json.dumps(record),encoding='utf-8')
    csv.write_text(csv.read_text(encoding='utf-8').replace('10.00','10.02'),encoding='utf-8')
    with pytest.raises(ValueError,match='CSV changed'):
        load_quote(path,'opening',E,stamp(at))


def test_real_feed_projects_only_opening_fields_then_closes_without_a_second_fill(tmp_path,monkeypatch):
    at=E+'T09:31:00+08:00';context=RunContext('synthetic-daily:lr',TradingMode.PAPER)
    clock={'now':stamp(at)}
    monkeypatch.setattr(engine,'_published_now',lambda:clock['now'])
    open_path,_,_=snapshot(tmp_path,'opening',at)
    def assess(symbol,day,*,decision_as_of):
        assert decision_as_of==E+'T09:30:00+08:00'
        return {'engine_block_reason':None}
    qualification=SimpleNamespace(records={A:{'ipo_date':'2000-01-01'}},assess=assess)
    raw=dict(open='10.00',close='10.00',high='10.10',low='9.90',preclose='10.00',
        volume='1000000',amount='10000000',tradestatus=1,isST=0,adjustflag='3')
    loaded=dict(source_day=D,execution_day=E,record={'policy_id':QUALIFIED_POLICY_ID},
        decisions={'lr':{'weights':{A:.16}}})
    config=_config(100000,5)
    prepared=NativeInputs(loaded,config,[D,E,'2026-10-19'],{},[],
        {'original_rows':{A:raw},'canonical_rows':{A:raw}},qualification,stamp(at))
    feed=prepared.feed(open_path,'opening',context)
    assert set(feed.rows[E][A])=={'open','preclose','isST','tradestatus','adjustflag'}
    assert list(feed.volumes.index)==[D] and not hasattr(feed,'research_listing_limit_reason')
    state=new_state(config.initial_cash)
    envelope=PreopenPortfolioIntent(D,E,E+'T08:00:00+08:00',
        PortfolioIntent.full_target({A:.16},context=context,source='synthetic-native-plan'))
    intraday_session.opening(state,feed,E,config,envelope,context=context,opened_at=at,
        source_ref=feed.intraday_opening_source_ref)
    fills=dict(state['fills']); cash=state['cash']
    assert fills and Ledger(state,config,context).available(A,E)==0
    # A close observation may be imported hours after its last price update;
    # that is a valuation input, never permission to trade an old opening quote.
    late=E+'T18:30:00+08:00';clock['now']=stamp(late)
    close_path,_,_=snapshot(tmp_path,'closing',late,price='10.20',quote_at=E+'T15:00:00+08:00')
    prepared.now=stamp(late)
    close_feed=prepared.feed(close_path,'closing',context)
    daily=intraday_session.closing(state,close_feed,E,config,context=context,closed_at=late,
        source_ref=close_feed.intraday_closing_source_ref)
    assert state['fills']==fills and state['cash']==cash and state['day']==E
    assert daily['holdings']==1 and state['prices'][A]==1020
