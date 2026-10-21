"""Behavior checks for a separate dated catch-up method, without market replay."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import leader_dated_catchup_inputs as rule
from trading.research import leader_dated_identity_catalog as catalog
from trading.research.tests.test_leader_dated_identity_catalog import dated, clock

C,L,O,B='600001.SSE','600002.SSE','600003.SSE','600004.SSE'
PRIOR,DAY='2026-10-16','2026-10-19'
OBS=datetime.fromisoformat('2026-10-19T16:00:00+08:00')


def setup():
    def row(pct,stage=0,limit=False):
        return {'pct_chg':pct,'stage':stage,'limit_up':limit,'close':10,
            'preclose_for_limits':10,'listing_qualification_status':'supported','amount':40_000_000}
    prior={C:row(-1),L:row(10,2,True),O:row(2),B:row(2)}
    current={C:row(10,1,True),L:row(1),O:row(3),B:row(3)}
    raw={s:{'isST':'0','tradestatus':'1'} for s in prior}
    views={PRIOR:{'features':prior,'ordinary':raw},DAY:{'features':current,'ordinary':deepcopy(raw)}}
    identity={'status':'available','required_prior_day':PRIOR,'available_at':'2026-10-17T10:00:00+08:00',
        'members':[{'symbol':L,'labels':['relay_continuation_leader']}],
        'unknown_leader_symbols':[],'catalog_reference':{'file':'catalog.json','sha256':'pinned'}}
    return current[C],views,identity,{s:'industry' for s in prior}


def evaluate(row,views,identity,membership):
    return rule.evaluate_member(C,row,DAY,OBS,views,{DAY:PRIOR},identity,membership)


def test_daily_prior_label_enters_and_disappears_without_changing_price_rules():
    row,views,identity,membership=setup()
    status,_,evidence=evaluate(row,views,identity,membership)
    assert status=='matched' and evidence['dated_prior_leaders']==[L]
    assert evidence['sector_denominator']==4 and evidence['prior_sector_median_pct']==2
    assert evidence['current_sector_median_pct']==3 and evidence['not_verified_theme_catchup_leader']
    identity['members'][0]['labels']=[]
    assert evaluate(row,views,identity,membership)[:2]==('not_matched','no_prior_close_leader_proxy_in_this_broad_industry')


@pytest.mark.parametrize('change', ['missing','neighbor','late','unknown_same_sector','reference','missing_sector_outcome','unknown_firstboard'])
def test_missing_exact_evidence_never_becomes_empty_target(change):
    row,views,identity,membership=setup()
    if change=='missing': identity.update(status='unknown',reason='exact_previous_close_identity_missing')
    if change=='neighbor': identity['required_prior_day']='2026-10-15'
    if change=='late': identity['available_at']=DAY+'T15:00:00+08:00'
    if change=='unknown_same_sector':
        identity['members'][0]['labels']=[]; identity['unknown_leader_symbols']=[L]
    if change=='reference': row['preclose_for_limits']=9
    if change=='missing_sector_outcome': del views[DAY]['features'][B]
    if change=='unknown_firstboard': row['stage']=None
    assert evaluate(row,views,identity,membership)[0]=='unknown'


def test_unknown_other_sector_does_not_invent_a_possible_peer_or_defeat_known_positive():
    row,views,identity,membership=setup()
    identity['unknown_leader_symbols']=[O]
    assert evaluate(row,views,identity,membership)[0]=='matched'
    identity['members'][0]['labels']=[]; membership[O]='other'
    assert evaluate(row,views,identity,membership)[0]=='not_matched'


def test_prior_leader_itself_is_excluded_and_current_nonfirstboard_is_known_negative():
    row,views,identity,membership=setup()
    identity['members'].append({'symbol':C,'labels':['space_height_leader']})
    assert evaluate(row,views,identity,membership)[:2]==('not_matched','candidate_already_prior_close_leader_proxy')
    row['stage']=2; identity['status']='unknown'
    assert evaluate(row,views,identity,membership)[:2]==('not_matched','current_not_qualified_first_board')


def test_catalog_binding_requires_registered_prefix_not_a_sibling(tmp_path):
    make,_,anchor,source=dated(tmp_path,day='2026-10-15')
    catalog.build_catalog(anchor,[source],tmp_path/'parent',_clock=clock())
    parent=tmp_path/'parent/catalog.json'
    catalog.build_catalog(anchor,[make(PRIOR)],tmp_path/'child',parent=parent,_clock=clock('2026-10-17T11:00:00+08:00'))
    catalog.build_catalog(anchor,[source],tmp_path/'sibling',_clock=clock('2026-10-17T11:00:00+08:00'))
    value=catalog.load_catalog(parent,observed_at='2026-10-17T12:00:00+08:00')
    loaded={'provenance':{'input_record':{'identity_report':{'registry':str(anchor),'registry_sha256':catalog.digest(anchor)},
        'calendar':value['anchor']['calendar'],'calendar_sha256':value['anchor']['binding']['calendar_sha256']}}}
    original={'file':str(parent),'sha256':catalog.digest(parent)}
    obs=datetime.fromisoformat('2026-10-17T12:00:00+08:00')
    child=rule._catalog_binding(tmp_path/'child/catalog.json',loaded,obs,original)
    assert len(child['entries'])==2
    with pytest.raises(ValueError,match='registration catalog prefix'):
        rule._catalog_binding(tmp_path/'sibling/catalog.json',loaded,obs,original)


def test_registration_rejects_last_session_instead_of_index_error(tmp_path,monkeypatch):
    monkeypatch.setattr(rule,'_sessions',lambda loaded:([PRIOR,DAY],{DAY:PRIOR}))
    monkeypatch.setattr(rule,'_base_binding',lambda loaded:{'pinned':True})
    p=tmp_path/'registration.json'
    record={'schema':rule.REG_SCHEMA,'method_id':rule.METHOD_ID,'rules':rule.RULES,
        'base_binding':{'pinned':True},'registered_at':'2026-10-17T10:00:00+08:00',
        'first_decision_day':DAY,'first_execution_day':'2026-10-20',
        'candidate_only':True,'target_generation_allowed':False,
        'paper_publication_implemented':False,'forward_consumer_implemented':False,**rule.FLAGS}
    p.write_text(json.dumps(record),encoding='utf-8')
    with pytest.raises(ValueError,match='registration changed'):
        rule._registration(p,{},OBS)


def test_pre_registration_real_source_is_context_unknown_not_backfilled_candidate(tmp_path,monkeypatch):
    row,views,identity,membership=setup()
    loaded={'source_days':[PRIOR], 'panel_by_day':{PRIOR:{'risk_features':views[DAY]['features'],
        'available_at':'2026-10-16T18:00:00+08:00','source_proof':{}}},
        'raw':__import__('pandas').DataFrame([{'date':PRIOR}]),'scope_members':[C,L,O,B],
        'provenance':{'input_record':{'industry_source':{'source_file':'fixture'}}}}
    monkeypatch.setattr(rule,'load_inputs',lambda *args:loaded)
    monkeypatch.setattr(rule,'_source_bindings',lambda *args:{'saved':True})
    monkeypatch.setattr(rule,'_registration',lambda *args:({'first_decision_day':DAY,'catalog_anchor':{}},{'registered':True}))
    monkeypatch.setattr(rule,'_sessions',lambda loaded:([PRIOR,DAY,'2026-10-20'],{PRIOR:'2026-10-15',DAY:PRIOR}))
    monkeypatch.setattr(rule,'_catalog_binding',lambda *args:None)
    monkeypatch.setattr(rule,'_identity_source',lambda *args:None)
    monkeypatch.setattr(catalog,'resolve_prior_identity',lambda *args,**kwargs:identity)
    monkeypatch.setattr(rule,'_views',lambda *args:{PRIOR:views[DAY]})
    monkeypatch.setattr(rule,'_industry',lambda *args:None)
    monkeypatch.setattr(rule,'digest',lambda path:'fixture_sha')
    record=rule.build('fixture','fixture','fixture','fixture','fixture',tmp_path/'out',
        _clock=clock('2026-10-17T12:00:00+08:00'))
    assert record['status']=='context_before_first_prospective_decision'
    assert record['unresolved_symbols']==[C,L,O,B] and record['candidate_count']==0
    assert record['target_generation_allowed'] is False and record['scope_complete'] is False
    assert all(r['reason']=='before_first_prospective_decision_no_backfilled_candidate' for r in record['evaluations'])
    assert json.loads((tmp_path/'out/input.json').read_text(encoding='utf-8'))==record


def test_original_registered_prefix_is_checked_even_when_current_header_matches(tmp_path):
    record={'created_at':'2026-10-16T18:00:00+08:00','source_days':[PRIOR],
        'source_proofs':[{'day':PRIOR,'raw':'original'}], 'panels':[{'day':PRIOR,'feature':'original'}],
        'preview_sources':[{'preview':'original'}]}
    path=tmp_path/'base.json'; path.write_text(json.dumps(record),encoding='utf-8')
    ref={'file':str(path),'sha256':rule.digest(path)}
    current=deepcopy(record); current['source_days'].append(DAY)
    current['source_proofs'].append({'day':DAY}); current['panels'].append({'day':DAY})
    current['preview_sources'].append({'preview':'new'})
    loaded={'provenance':{'input_record':current}}
    rule._base_prefix(loaded,ref)
    current['panels'][0]['feature']='another_valid_source'
    with pytest.raises(ValueError,match='exact registered input prefix'): rule._base_prefix(loaded,ref)
    path.write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='base input bytes changed'): rule._base_prefix(loaded,ref)


@pytest.mark.parametrize('mismatch',['raw_daily_sha256','feature_sha256'])
def test_same_date_identity_cannot_mix_another_source_with_prior_price(tmp_path,mismatch):
    preview=tmp_path/'preview.json'; preview.write_text(json.dumps({'day':PRIOR}),encoding='utf-8')
    feature=tmp_path/'features.csv'; feature.write_text('symbol,stage\n600002.SSE,2\n',encoding='utf-8')
    item={'preview':str(preview),'preview_sha256':rule.digest(preview),'feature_file':str(feature),
        'feature_sha256':rule.digest(feature),'raw_daily_sha256':'exact_raw'}
    source={**item,'preview_file':str(preview),'day':PRIOR}
    identity={'status':'available','required_prior_day':PRIOR,'source':source}
    loaded={'panel_by_day':{PRIOR:{'source_proof':{'raw_daily_sha256':'exact_raw'}}},
        'provenance':{'input_record':{'preview_sources':[item]}}}
    assert rule._identity_source(identity,loaded,PRIOR)['raw_daily_sha256']=='exact_raw'
    source[mismatch]='another_same_date_source'
    with pytest.raises(ValueError,match='different sources'): rule._identity_source(identity,loaded,PRIOR)


def test_registration_finishing_after_close_does_not_claim_preclose_registration(tmp_path,monkeypatch):
    loaded={'source_days':[]}
    monkeypatch.setattr(rule,'load_inputs',lambda *args:loaded)
    monkeypatch.setattr(rule,'_source_bindings',lambda *args:None)
    monkeypatch.setattr(rule,'_catalog_binding',lambda *args:{'entries':[{'source_day':DAY}],
        'previous':{'2026-10-20':DAY,DAY:PRIOR}})
    monkeypatch.setattr(rule,'_sessions',lambda *args:([PRIOR,DAY,'2026-10-20','2026-10-21'],{}))
    monkeypatch.setattr(rule,'_base_binding',lambda *args:{'fixed':True})
    monkeypatch.setattr(rule,'digest',lambda path:'fixture')
    ticks=iter(datetime.fromisoformat(t) for t in [DAY+'T14:59:59+08:00',DAY+'T15:00:01+08:00'])
    result=rule.register('fixture','fixture','fixture','fixture',tmp_path/'out',_clock=lambda:next(ticks))
    assert result['registered_at']==DAY+'T15:00:01+08:00'
    assert result['first_decision_day']=='2026-10-20' and result['first_execution_day']=='2026-10-21'
