"""Native three-phase saved evidence projection; synthetic small JSON only."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from trading import research_evidence_catalog as m
from trading.research.tests.test_research_evidence_catalog import fixture,write


def native_fixture(tmp_path):
    b=fixture(tmp_path)
    project,data=b['project'],b['data']
    flags={k:v for k,v in m.FLAGS.items() if k in
        ('candidate_panel_only','execution_admitted','selection_enabled','strategy_validated','returns_reportable')}
    qdir=data/'native/quality'
    rows=[]
    for kind in ('lr','lgbm'):
        for stage in (1,2,3):
            known=4 if stage==3 else 3
            rows.append(dict(model_kind=kind,stage=stage,event_rows=4,evaluable_rows=known,
                unknown_rows=4-known,positive_rows=1,base_rate=1/known,metrics={
                    'AP':.25 if stage!=3 else None,'ROC_AUC':.7,'Brier':.1,
                    'training_prior_Brier':.100002,'Brier_minus_training_prior_Brier':-.000002}))
    qr=write(qdir/'quality.json',rows)
    summary=dict(unique_event_rows=12,evaluated_unique_events=10,registered_days=181)
    quality=dict(schema='native_frozen_shortline_evaluation_result_v1',
        status='completed_retrospective_frozen_stagewise_quality',engine_advanced=False,model_refit=False,
        completed_at='2026-10-18T23:44:16+08:00',model_available_at='2026-10-16T02:55:29+08:00',
        event_start='2026-01-05',event_end='2026-09-30',label_witness_cutoff='2026-09-30',
        summary=summary,files={'quality.json':qr['sha256']},**flags)
    qref=write(qdir/'result.json',quality)
    qread=project/'outputs/native/quality_readback.json'
    write(qread,dict(result=qref['path'],result_sha256=qref['sha256'],summary=summary,unique_symbols=3))
    edir=data/'native/engine'
    cases=[];checks=[]
    for kind,count,last,stop in [('lr',103,'2026-06-10','2026-06-11'),('lgbm',95,'2026-05-29','2026-06-01')]:
        cases.append(dict(kind=kind,engine_sessions_committed=count,last_committed_day=last,
            parent_orders=223 if kind=='lr' else 190,fill_callbacks=446 if kind=='lr' else 380,
            full_tail={'complete_PnL_available':False,'metrics':None},blocking_evidence={'day':stop},
            residual_positions={'600001.SSE':100} if kind=='lr' else None,
            pending_targets={} if kind=='lr' else None,
            main_window={'source_prefix_completed':False,'marked_NAV':None,'closed_total_return':None},
            source_issues=[{'day':stop,'reason':'saved_native_execution_source_unresolved','references':[
                {'symbol':'600001.SSE','status':'unresolved_blank_terms'},
                {'symbol':'600002.SSE','status':'no_reported_event_reference_continuous'}]}]))
        fees=2043.44 if kind=='lr' else 0
        checks.append(dict(model_kind=kind,engine_sessions_committed=count,last_committed_day=last,
            parent_orders=cases[-1]['parent_orders'],fill_callbacks=cases[-1]['fill_callbacks'],
            fees_rmb={key:fees for key in ('sum_parent_order_fees','sum_fill_callback_fees','sum_saved_daily_fees')}))
    engine=dict(schema='native_frozen_saved_selection_fixed16pct_shared_engine_replay_v1_result',
        case_count=2,cases=cases,user_account_mutated=False,engine_advanced=True,paper_orders_generated=True,
        actual_completed_at='2026-10-19T00:05:00+08:00',**flags)
    eref=write(edir/'result.json',engine)
    eread=project/'outputs/native/engine_readback.json'
    write(eread,dict(result=eref['path'],result_sha256=eref['sha256'],execution_checks={'cases':checks}))
    categories=[]
    for state,size,known,hit in [('buy_order_filled',1,1,1),('buy_blocked',1,1,0),
            ('execution_session_unavailable',1,0,0),('no_buy_attempt_recorded',0,0,0)]:
        categories.append(dict(execution_status=state,selected_slots=size,known_label_slots=known,
            unknown_label_slots=size-known,hit5_slots=hit,secondary_hit4_slots=hit))
    acases=[dict(model_kind=kind,selected_slots=3,categories=deepcopy(categories),
        execution_status_counts={r['execution_status']:r['selected_slots'] for r in categories if r['selected_slots']})
        for kind in ('lr','lgbm')]
    attribution=dict(schema='native_saved_selection_execution_attribution_v1',
        completed_at='2026-10-19T00:17:10+08:00',cases=acases,returns_calculated=False,
        models_rerun=False,accounts_advanced=False,**flags)
    apath=data/'native/attribution/result.json';write(apath,attribution)
    candidate=dict(strategy_id=m.NATIVE_ID,display_name='原生冻结短线模型中文标题',
        source=[],result=dict(evidence=qref['path'],actual_readback=str(qread),
            saved_selection_engine_replay=dict(evidence=eref['path'],actual_readback=str(eread),
                execution_attribution={'result':str(apath)})),**flags)
    b['registry']['candidates'].append(candidate)
    write(b['registry_path'],b['registry'])
    return {**b,'qdir':qdir,'edir':edir,'qread':qread,'eread':eread,'apath':apath,'native':candidate}


def native_result(b):
    result=m.read_catalog(b['project'],b['data'],now=datetime.fromisoformat('2026-10-19T12:00:00+08:00'))
    return result,next(c for c in result['candidates'] if c['id']==m.NATIVE_ID)


def test_native_three_phases_exact_saved_units_clocks_nulls_and_old_52_behavior(tmp_path):
    b=native_fixture(tmp_path)
    result,candidate=native_result(b)
    assert result['candidate_count']==20 and candidate['title']=='原生冻结短线模型中文标题'
    phases=candidate['native_evidence']
    assert {r['status'] for r in phases.values()}=={'saved'}
    q=phases['quality']
    assert q['coverage']==dict(registered_days=181,unique_events=12,mature_events=10,unknown_events=2,unique_symbols=3)
    assert len(q['rows'])==6 and q['rows'][2]['metrics']['AP'] is None
    assert q['rows'][0]['metrics']['Brier_minus_training_prior_Brier']==-.000002
    assert q['model_available_at']!=q['saved_at']!=result['served_at']
    assert q['saved_engine_advanced'] is False
    assert phases['accounts']['saved_engine_advanced'] is True and phases['accounts']['saved_paper_orders_generated'] is True
    assert phases['attribution']['saved_accounts_advanced'] is False
    accounts=phases['accounts']['rows']
    assert accounts[0]['committed_sessions']==103 and accounts[0]['fees']==2043.44
    assert accounts[1]['fees']==0 and accounts[1]['residual_positions'] is None
    assert accounts[0]['pending_targets']=={} and accounts[1]['pending_targets'] is None
    assert accounts[0]['source_issues'][-1]['symbol']=='600001.SSE'
    assert all(row['metrics'] is None and row['complete_PnL_available'] is False for row in accounts)
    a=phases['attribution']['rows'][0]
    assert (a['selected_slots'],a['filled_slots'],a['blocked_slots'],a['unavailable_slots'],a['no_attempt_slots'])==(3,1,1,1,0)
    assert a['known_label_slots']==2 and a['known_hit5_slots']==1 and a['hit5_filled']==1
    paired=result['candidates'][0]
    assert paired['ranking_diagnostic']['summaries'][0]['mean_rank_IC']<0
    assert paired['account_comparison']['cases'][0]['metrics'] is None
    assert result['engine_advanced'] is False and result['producer_called'] is False


def test_one_corrupt_phase_does_not_hide_the_other_saved_phases(tmp_path):
    b=native_fixture(tmp_path)
    quality=b['qdir']/'quality.json'
    original=quality.read_bytes();quality.write_text('{}',encoding='utf-8')
    _,candidate=native_result(b)
    e=candidate['native_evidence']
    assert e['quality']['status']=='unknown' and e['quality']['rows'] is None
    assert e['accounts']['status']==e['attribution']['status']=='saved'
    quality.write_bytes(original)
    b['eread'].unlink()
    _,candidate=native_result(b)
    e=candidate['native_evidence']
    assert e['accounts']['status']=='unknown' and e['accounts']['rows'] is None
    assert e['quality']['status']==e['attribution']['status']=='saved'
    b['apath'].write_text('not JSON',encoding='utf-8')
    _,candidate=native_result(b)
    assert candidate['native_evidence']['quality']['status']=='saved'
    assert candidate['native_evidence']['attribution']['status']=='unknown'


def test_native_outside_path_and_readback_conflict_rejected_without_writes_or_large_reads(tmp_path,monkeypatch):
    b=native_fixture(tmp_path)
    outside=tmp_path/'outside.json';write(outside,{'untrusted':True})
    b['native']['result']['saved_selection_engine_replay']['execution_attribution']['result']=str(outside)
    write(b['registry_path'],b['registry'])
    readback=json.loads(b['qread'].read_text(encoding='utf-8'));readback['summary']['unique_event_rows']=13
    write(b['qread'],readback)
    original=Path.read_bytes
    def bounded(path):
        assert path!=outside and path.suffix=='.json'
        assert path.stat().st_size<=4*1024*1024
        return original(path)
    def no_write(*args,**kwargs): raise AssertionError('GET wrote or ran a producer')
    monkeypatch.setattr(Path,'read_bytes',bounded)
    for name in ('write_bytes','write_text','mkdir','unlink','replace'):
        monkeypatch.setattr(Path,name,no_write)
    result,candidate=native_result(b)
    e=candidate['native_evidence']
    assert e['quality']['status']=='unknown' and e['quality']['coverage'] is None
    assert 'coverage_conflict' in e['quality']['reason']
    assert e['accounts']['status']=='saved'
    assert e['attribution']['status']=='unknown' and e['attribution']['reason']=='evidence_path_outside_allowed_roots'
    assert result['files_written'] is False and result['training_started'] is False
