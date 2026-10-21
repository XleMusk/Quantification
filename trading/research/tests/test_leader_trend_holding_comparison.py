"""New runner pairing and exact shared Engine clocks; synthetic sources only."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import leader_trend_holding_comparison as m
from trading.research.tests.test_leader_eight_type_historical_native import (
    A, OBSERVED, SESSIONS, qualification, bars, accounting)


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    m._save(path,value)
    return m._pin(path)


def source_map(tmp_path,monkeypatch):
    studies=[];originals={}
    for year in (2024,2025):
        folder=tmp_path/'sources'/str(year)
        event=save(folder/'events/result.json',{'status':'research_action_events_projected'})
        reference=save(folder/'refs/result.json',{'status':'research_reference_presence_diagnostic','event_view':str(folder/'events')})
        days=[f'{year}-12-{day}' for day in ('19','20','23','24','25','26','27')]
        header={'start':days[0],'end':days[0],'through':days[-1],'source_days':days,'fixed_protocol':m.PROTOCOL,
            'raw_file':str(folder/'unread_raw.parquet'),'raw_sha256':'a'*64,
            'context_raw_file':str(folder/'unread_context.parquet'),'context_raw_sha256':'b'*64,
            'panels_file':str(folder/'unread_panels.json'),'panels_sha256':'c'*64,
            'calendar':str(folder/'unread_calendar.json'),'calendar_sha256':'d'*64}
        inputs=save(folder/'manifest.json',header)
        pair={'event_view':str(folder/'events'),'reference_view':str(folder/'refs')}
        if year==2024:
            meta=save(folder/'registration.json',{'inputs':inputs,'action_event_view':event,'action_reference_view':reference,
                'schema':m.original_durable.METHOD+'_registration','method_id':m.original_durable.METHOD,
                'protocol':m.PROTOCOL,'case_ids':list(m.original_durable.CASE_IDS),'config':m.SPEC['config'],
                'code':[{'path':str(Path(m.__file__).resolve()),'sha256':'0'*64}],**m.original_durable.FLAGS})
            originals[meta['path']]=dict(inputs=inputs,action_event_view=event,action_reference_view=reference)
        else:
            meta=save(folder/'historical_method.json',{'schema':'leader_eight_type_historical_native_result_v1',
                'original_32_protocol_unchanged':True,'action_accounting':pair})
        cases=[]
        for index,case_id in enumerate(m.CASE_IDS):
            complete=index==0
            row=dict(case_id=case_id,type=m.TREND,guard=case_id.split('__')[1],capacity=case_id.split('__')[2],
                config=m.SPEC['config'],historical_execution_policy={'accounting':pair},
                complete_PnL_available=complete,metrics={'net_return':-.12} if complete else None,
                status='saved',source_origin={'mode':'reused'} if year==2025 else None)
            ref=save(folder/case_id/'attempt-0001/case.json',row)
            cases.append({'case_id':case_id,'case_file':ref})
        studies.append(dict(year=year,inputs=inputs,metadata_kind='registration' if year==2024 else 'historical_method',
            metadata=meta,action_event_view=event,action_reference_view=reference,cases=cases))
    monkeypatch.setattr(m.original_durable,'_runtime_pins',lambda:[m._pin(m.__file__)])
    mapping=tmp_path/'maps/baselines.json';save(mapping,{'schema':m.MAP_SCHEMA,'studies':studies})
    return mapping,studies


def test_registration_exact_eight_baselines_keeps_negative_null_and_reused_original_pair(tmp_path,monkeypatch):
    mapping,studies=source_map(tmp_path,monkeypatch)
    monkeypatch.setattr(m.pd,'read_parquet',lambda *a,**k:(_ for _ in ()).throw(AssertionError('registration read prices')))
    monkeypatch.setattr(m,'advance',lambda *a,**k:(_ for _ in ()).throw(AssertionError('registration ran Engine')))
    record=m.register(mapping,tmp_path/'registered')
    assert len(record['studies'])==2 and sum(len(s['baseline_cases']) for s in record['studies'])==8
    assert record['studies'][1]['metadata_kind']=='historical_method'
    assert record['studies'][0]['baseline_cases'][0]['metrics']['net_return']==-.12
    assert record['studies'][0]['baseline_cases'][1]['metrics'] is None
    assert record['studies'][1]['baseline_cases'][0]['source_origin']=={'mode':'reused'}
    assert record['spec']['target_expiry']=={'matched_control':True,'structure_only':False}
    assert record['spec']['risk_expiry']==record['spec']['target_expiry'] and record['spec']['new_cases']==16
    assert len(record['studies'][0]['historical_current_code_differences'])==1
    for pair in record['code_snapshots']:
        assert Path(pair['copy']['path']).read_bytes()==Path(pair['source']['path']).read_bytes()
        assert pair['copy']['sha256']==pair['source']['sha256']
    altered=deepcopy(studies[1]);case=m._read(altered['cases'][0]['case_file']['path'])
    case['historical_execution_policy']['accounting']['event_view']=str(tmp_path/'wrong_new_container_pair')
    wrong=tmp_path/'wrong/case.json';altered['cases'][0]['case_file']=save(wrong,case)
    with pytest.raises(ValueError,match='original pair'):
        m._source_study(altered)
    with pytest.raises(ValueError,match='outside original'):
        m._separate(Path(studies[0]['inputs']['path']).parent/'new_account',[studies[0]['inputs']])


def case_fixture():
    days=[d for d in SESSIONS if d>='2026-01-12'][:14]
    before='2026-01-09';end=days[7]
    raw=bars([before]+days,[A])
    candidate=lambda day:dict(symbol=A,score=1.,date=day,source_day=day,board='main',setup_low=9.8,
        structure_high=10.1,evidence={})
    panels={day:dict(day=day,available_at=OBSERVED,types=[dict(id=m.TREND,status='matched',
        candidates=[candidate(day)],unresolved_symbols=[])],risk_features={A:dict(date=day,symbol=A,sma20=9.5,sma60=9.)},
        risk_feature_scope_complete=True) for day in days}
    paths={day:dict(candidates=[candidate(day)],original_top5=[candidate(day)],cap_dropped_rows=0) for day in days}
    environments={day:{'per_type':{m.TREND:{'mode':'ALLOW_FIXED_CANDIDATES'}}} for day in days}
    source=accounting([A],[before]+days,days)
    args=(raw.loc[raw.date.isin(days)],days,SESSIONS,panels,paths,environments,{A},qualification([A]),
        {'day':before,'closes':{A:10.}},days[0],end,m.TREND,'uncapped','unguarded','new-trend-case',
        m.HistoricalExecutionFactory(source),raw.loc[raw.date.eq(before)])
    return args


def test_real_shared_Engine_holds_past_five_and_clears_only_tail5_to_tail6():
    args=case_fixture()
    result=m._new_case(*args)
    assert result['blocking_evidence'] is None
    assert result['engine_sessions_committed']==14
    assert result['target_expiry_enabled'] is False and result['risk_expiry_enabled'] is False
    assert result['complete_PnL_available'] is True
    events=result['account']['events']
    assessed=[e for e in events if e['kind']=='leader_type_risk_assessed']
    assert assessed and all(e['expiry_enabled'] is False for e in assessed)
    assert not [e for e in events if 'five_session' in str(e.get('reason',''))]
    tail=args[1][8:]
    exit_decisions=[d for d in result['decisions'] if d['status']=='STUDY_END_FULL_EMPTY']
    assert [d['day'] for d in exit_decisions]==[tail[4]]
    assert all(d['status']=='NO_CHANGE' for d in result['decisions'] if d['day'] in tail[:4]+tail[5:])
    assert not result['residual_positions']
    assert any(sum(l['qty'] for l in lots)>0 for lots in result['main_window_snapshot']['positions'].values())
    assert result['account']['replay_risk_exit_policy']['context']['account_id']=='new-trend-case'
    control_args=list(args);control_args[14]='matched-current-control'
    control=m._new_case(*control_args,arm='matched_control')
    assert control['target_expiry_enabled'] is True and control['risk_expiry_enabled'] is True
    assert control['terminal_liquidation_decision']==result['terminal_liquidation_decision']
    assert control['terminal_liquidation_execution']==result['terminal_liquidation_execution']
    assert control['account']['replay_risk_exit_policy']['context']['account_id']=='matched-current-control'
    assert any('five_session' in str(e.get('reason','')) for e in control['account']['events'])


def test_future_E_gap_keeps_original_D_target_and_failed_session_uncommitted():
    args=list(case_fixture())
    day,E=args[1][:2]
    args[0]=args[0].loc[~args[0].date.eq(E)].copy()
    result=m._new_case(*args)
    assert result['engine_sessions_committed']==1 and result['account']['day']==day
    assert result['decisions'][0]['status']=='FULL_TARGET'
    assert result['decisions'][0]['successor_quotes_used_for_candidate_clearance'] is False
    assert result['account']['pending_targets']=={A:.16}
    assert result['blocking_evidence']['day']==E and result['blocking_evidence']['session_not_committed'] is True
    assert result['metrics'] is None and not result['fills']
