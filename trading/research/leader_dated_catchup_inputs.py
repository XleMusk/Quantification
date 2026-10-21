"""Independent exact-prior-close identity inputs for broad-industry catch-up.

The original eight-type producer and its 32 accounts retain their frozen
identity rule. This method changes only which already-known prior leaders are
looked up. It neither names a theme leader nor publishes an account intent.
"""
from copy import deepcopy
from datetime import datetime
import argparse
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_daily_signals import _calendar_view
from .leader_eight_type_panel_replay import load_inputs
from .leader_eight_type_paper import _source_bindings
from .leader_eight_type_inputs import _industry
from .leader_missing_type_candidates import (CATCHUP_LEADER_TYPES, FLAGS, MIN_AMOUNT,
    RULE_SPEC as ORIGINAL_RULES, _bool, _number, _time, _views, _eligibility,
    _reference, _sector_median)
from .leader_listing_features import METHOD_ID as FEATURE_METHOD

METHOD_ID = 'dated_prior_close_broad_industry_catchup_proxy_v1'
REG_SCHEMA = 'leader_dated_catchup_method_registration_v1'
INPUT_SCHEMA = 'leader_dated_catchup_candidate_input_v1'
RULES = {'id':METHOD_ID, 'base_rule':ORIGINAL_RULES['sector_catchup_leader'],
    'minimum_amount_cny':MIN_AMOUNT,'reference_tolerance':.01,
    'leader_types':sorted(CATCHUP_LEADER_TYPES),
    'changed_component':'exact_D_minus_1_daily_proxy_labels_inside_original_frozen_registry_members',
    'industry':ORIGINAL_RULES['industry'],'candidate_order':ORIGINAL_RULES['candidate_order'],
    'prior_identity_availability':'actual_persisted_catalog_before_decision_close_and_decision_as_of',
    'unknown':'retain_all_fixed_candidate_scope_unknown_not_empty_target',
    'parameters_selected_by_PnL':False,'original_32_method_changed':False}


def _now():
    return datetime.now(MARKET_TZ)


def _sessions(loaded):
    p=loaded['calendar_payload']
    sessions, previous, status=_calendar_view(p,f"{p['year']}-01-01",f"{p['year']}-12-31",[])
    if status!='pinned_static_exchange_calendar':
        raise ValueError('A fixed complete exchange calendar is required')
    return sessions,previous


def _base_binding(loaded):
    p=loaded['provenance']; record=p['input_record']
    return {'calendar_sha256':record['calendar_sha256'],'scope_members':list(loaded['scope_members']),
        'scope_frozen_at':record['scope_frozen_at'],'context_anchor':deepcopy(p['context_anchor']),
        'identity_registry':deepcopy({k:record['identity_report'][k] for k in ('registry','registry_sha256')}),
        'industry_source':deepcopy(record['industry_source']),
        'IPO_qualification':deepcopy(record['IPO_qualification']),
        'base_protocol':deepcopy(p['external_fixed_protocol'])}


def _catalog_binding(catalog,loaded,observed,anchor_ref=None):
    from .leader_dated_identity_catalog import load_catalog
    current=load_catalog(catalog,observed_at=observed.isoformat())
    expected=loaded['provenance']['input_record']['identity_report']
    anchor=current['anchor']
    if (Path(anchor['registry_file']).resolve()!=Path(expected['registry']).resolve()
        or anchor['registry_sha256']!=expected['registry_sha256']
        or anchor['calendar']!=loaded['provenance']['input_record']['calendar']
        or anchor['binding']['calendar_sha256']!=loaded['provenance']['input_record']['calendar_sha256']):
        raise ValueError('Dated identity must retain the original fixed registry and calendar')
    if anchor_ref is not None:
        cursor=current
        while True:
            if Path(cursor['catalog_file']).resolve()==Path(anchor_ref['file']).resolve():
                if digest(cursor['catalog_file'])!=anchor_ref['sha256']:
                    raise ValueError('Original catalog anchor changed')
                break
            if cursor.get('parent') is None:
                raise ValueError('Catalog must append the method registration catalog prefix')
            cursor=json.loads(Path(cursor['parent']['file']).read_text(encoding='utf-8'))
    return current


def _base_prefix(loaded,reference):
    """Keep the registered source/panel prefix, even if later files are valid."""
    path=Path(reference['file']).resolve()
    if digest(path)!=reference['sha256']:
        raise ValueError('Registered base input bytes changed')
    base=json.loads(path.read_text(encoding='utf-8'))
    current=loaded['provenance']['input_record']
    for key in ('source_days','source_proofs','panels','preview_sources'):
        prefix=base[key]
        if current[key][:len(prefix)]!=prefix:
            raise ValueError('Dated catchup must retain the exact registered input prefix')
    if _time(current['created_at'])<_time(base['created_at']):
        raise ValueError('Current input precedes its registered base')


def _identity_source(identity,loaded,prior_day):
    """Dated labels and the prior-stage/price facts must use the same source."""
    if identity.get('status')!='available':
        return None
    source=identity['source']
    if identity['required_prior_day']!=prior_day or source['day']!=prior_day:
        raise ValueError('Dated identity is not the exact previous source')
    raw=loaded['panel_by_day'][prior_day]['source_proof']['raw_daily_sha256']
    references=[]
    for item in loaded['provenance']['input_record']['preview_sources']:
        if digest(item['preview'])!=item['preview_sha256']:
            raise ValueError('Saved preview bytes changed')
        preview=json.loads(Path(item['preview']).read_text(encoding='utf-8'))
        if preview['day']==prior_day:
            references.append(item)
    if len(references)!=1:
        raise ValueError('Exactly one same-source prior preview is required')
    item=references[0]
    if (source['raw_daily_sha256']!=raw or item['raw_daily_sha256']!=raw
        or source['feature_sha256']!=item['feature_sha256']
        or source['preview_sha256']!=item['preview_sha256']
        or Path(source['feature_file']).resolve()!=Path(item['feature_file']).resolve()
        or Path(source['preview_file']).resolve()!=Path(item['preview']).resolve()
        or digest(item['feature_file'])!=item['feature_sha256']):
        raise ValueError('Prior identity labels and price/feature inputs use different sources')
    return deepcopy(source)


def register(base_panel, catalog, ipo_evidence, research_master_run, out, *, _clock=None):
    out=Path(out).resolve()
    if out.exists(): raise FileExistsError('A new method registration directory is required')
    loaded=load_inputs(base_panel,ipo_evidence,research_master_run)
    actual=_clock() if _clock else _now()
    actual=_time(actual)
    _source_bindings(loaded,actual)
    catalogue=_catalog_binding(catalog,loaded,actual)
    sessions,_=_sessions(loaded)
    for entry in catalogue['entries']:
        if entry['source_day'] in loaded['source_days']:
            _identity_source({'status':'available','source':entry['source'],
                'required_prior_day':entry['source_day']},loaded,entry['source_day'])
    finished=_time(_clock() if _clock else _now())
    if finished<actual: raise ValueError('Registration clock moved backwards')
    actual=finished
    future=[d for d in sessions if _time(d+'T15:00:00+08:00')>actual]
    if not future: raise ValueError('The pinned calendar has no future decision close')
    if (sessions.index(future[0])+1>=len(sessions)
        or catalogue['previous'][future[0]] not in {e['source_day'] for e in catalogue['entries']}):
        raise ValueError('The first decision requires its exact prior identity and next execution session')
    record={'schema':REG_SCHEMA,'method_id':METHOD_ID,'registered_at':actual.isoformat(),
        'first_decision_day':future[0],
        'first_execution_day':sessions[sessions.index(future[0])+1],
        'rules':deepcopy(RULES),'base_binding':_base_binding(loaded),
        'base_panel':{'file':str(Path(base_panel).resolve()),'sha256':digest(base_panel)},
        'catalog_anchor':{'file':str(Path(catalog).resolve()),'sha256':digest(catalog)},
        'candidate_only':True,'paper_publication_implemented':False,'forward_consumer_implemented':False,
        'target_generation_allowed':False,**FLAGS}
    out.mkdir(parents=True,exist_ok=False)
    write_json(out/'registration.json',record)
    return record


def _registration(path,loaded,observed):
    path=Path(path).resolve(); r=json.loads(path.read_text(encoding='utf-8'))
    sessions,_=_sessions(loaded)
    first=r.get('first_decision_day')
    following=sessions[sessions.index(first)+1] if first in sessions[:-1] else None
    if (r.get('schema')!=REG_SCHEMA or r.get('method_id')!=METHOD_ID or r.get('rules')!=RULES
        or r.get('base_binding')!=_base_binding(loaded)
        or r.get('candidate_only') is not True or r.get('target_generation_allowed') is not False
        or r.get('paper_publication_implemented') is not False or r.get('forward_consumer_implemented') is not False
        or any(r.get(k) is not v for k,v in FLAGS.items())
        or _time(r['registered_at'])>observed or following is None
        or _time(r['registered_at'])>=_time(r['first_decision_day']+'T15:00:00+08:00')
        or r['first_execution_day']!=following):
        raise ValueError('Independent fixed dated-catchup registration changed')
    _base_prefix(loaded,r['base_panel'])
    return r,{'file':str(path),'sha256':digest(path)}


def evaluate_member(symbol,row,day,observed,views,previous,identity,membership):
    """Original first-board/relative-return rules, with an exact dated identity."""
    limit,stage=_bool(row.get('limit_up')),_number(row.get('stage'))
    if limit is False or stage is not None and stage!=1:
        return 'not_matched','current_not_qualified_first_board',{}
    if limit is not True or stage is None:
        return 'unknown','current_first_board_qualification_unknown',{}
    prior_day=previous.get(day); prior=views.get(prior_day)
    before=(prior or {}).get('features',{}).get(symbol)
    if before is None or _reference(row,before) is not True:
        return 'unknown','catchup_exact_previous_reference_unknown_or_incompatible',{}
    if before.get('listing_qualification_status')!='supported':
        return 'unknown','catchup_previous_listing_qualification_unknown',{}
    before_limit=_bool(before.get('limit_up'))
    if before_limit is True:
        return 'not_matched','candidate_already_limit_up_on_previous_close',{}
    if before_limit is None:
        return 'unknown','previous_candidate_limit_state_unknown',{}
    if identity.get('status')!='available' or identity.get('required_prior_day')!=prior_day:
        return 'unknown',identity.get('reason') or 'exact_prior_dated_identity_unavailable',{}
    if (not identity.get('available_at') or _time(identity['available_at'])>observed
        or _time(identity['available_at'])>=_time(day+'T15:00:00+08:00')):
        return 'unknown','prior_identity_not_known_before_decision_close',{}
    sector=membership.get(symbol)
    if not sector: return 'unknown','candidate_industry_unknown',{}
    members={r['symbol']:r for r in identity['members']}
    unknown={s for s in identity.get('unknown_leader_symbols',[]) if membership.get(s)==sector}
    leaders=[s for s,r in members.items() if CATCHUP_LEADER_TYPES & set(r['labels'])
             and membership.get(s)==sector and s not in unknown]
    if symbol in leaders:
        return 'not_matched','candidate_already_prior_close_leader_proxy',{}
    if not leaders:
        return ('unknown','prior_sector_leader_membership_unknown',{}) if unknown else (
            'not_matched','no_prior_close_leader_proxy_in_this_broad_industry',{})
    first,reason,n=_sector_median(views,membership,sector,prior_day,prior_day)
    current,reason2,_=_sector_median(views,membership,sector,prior_day,day)
    pct,prior_pct=_number(row.get('pct_chg')),_number(before.get('pct_chg'))
    if first is None or current is None or pct is None or prior_pct is None:
        return 'unknown',reason or reason2 or 'catchup_relative_return_unknown',{}
    if prior_pct>=first or not pct>current>0:
        return 'not_matched','prior_lag_or_current_positive_sector_excess_not_matched',{}
    supported=[]; uncertain=[]
    for leader in leaders:
        value=prior['features'].get(leader,{})
        ls,lp,ll=_number(value.get('stage')),_number(value.get('pct_chg')),_bool(value.get('limit_up'))
        if value.get('listing_qualification_status')=='supported' and ls is not None and ls>=2 and ll is True and lp is not None and lp>first:
            supported.append(leader)
        elif value.get('listing_qualification_status')!='supported' or ls is None or lp is None or ll is None:
            uncertain.append(leader)
    if not supported:
        return ('unknown' if uncertain or unknown else 'not_matched',
            'prior_leader_qualified_high_board_unknown' if uncertain or unknown else 'prior_leader_high_board_or_sector_excess_not_matched',{})
    return 'matched',None,{'broad_industry':sector,'dated_prior_leaders':supported,
        'identity_source_day':prior_day,'identity_available_at':identity['available_at'],
        'catalog_reference':identity['catalog_reference'],'prior_sector_median_pct':first,
        'current_sector_median_pct':current,'sector_denominator':n,
        'candidate_prior_pct':prior_pct,'candidate_current_pct':pct,'not_verified_theme_catchup_leader':True}


def prepare_loaded_input(loaded,catalog,registration,*,actual,finished_clock):
    """Read-only candidate preparation, also used to verify saved input facts."""
    from .leader_dated_identity_catalog import resolve_prior_identity
    actual=_time(actual)
    bindings=_source_bindings(loaded,actual)
    reg,reg_ref=_registration(registration,loaded,actual)
    sessions,previous=_sessions(loaded); day=loaded['source_days'][-1]
    identity=resolve_prior_identity(catalog,decision_day=day,decision_as_of=actual.isoformat(),observed_at=actual.isoformat())
    # This derived input is an explicit new method, never an old32 manifest.
    _catalog_binding(catalog,loaded,actual,reg['catalog_anchor'])
    identity_source=_identity_source(identity,loaded,previous[day])
    views=[]
    for d in loaded['source_days']:
        p=loaded['panel_by_day'][d]
        features=[{'symbol':s,**r} for s,r in p['risk_features'].items()]
        views.append({'day':d,'available_at':p['available_at'],'source_proof':p['source_proof'],
            'features':features,'ordinary_rows':loaded['raw'].loc[loaded['raw'].date.eq(d)],
            'feature_method_id':FEATURE_METHOD,'feature_qualification_verified':True})
    indexed=_views(views,day,actual)
    industry=_industry(loaded['provenance']['input_record']['industry_source']['source_file'],actual.isoformat())
    # Keep the original before-reference-close industry availability rule.
    from .leader_missing_type_candidates import _industry as admitted_industry
    membership,industry_issue=admitted_industry(industry,previous[day],actual)
    rows=[]; candidates=[]; unresolved=[]
    prospective=day>=reg['first_decision_day']
    for symbol in loaded['scope_members']:
        status,reason,row=_eligibility(indexed.get(day),symbol)
        evidence={}
        if status is None:
            if not prospective:
                status,reason='unknown','before_first_prospective_decision_no_backfilled_candidate'
            elif industry_issue:
                status,reason='unknown',industry_issue
            else:
                status,reason,evidence=evaluate_member(symbol,row,day,actual,indexed,previous,identity,membership)
        rows.append({'symbol':symbol,'status':status,'reason':reason,'evidence':evidence})
        if status=='unknown': unresolved.append(symbol)
        if status=='matched':
            candidates.append({'symbol':symbol,'source_day':day,
                'amount_cny':float(row['amount']),'score':float(row['amount']),
                'evidence':evidence,'trade_ready':False,'identity_status':'dated_broad_industry_price_proxy_not_theme_leader'})
    candidates.sort(key=lambda r:(-r['score'],r['symbol']))
    finished=_time(finished_clock())
    if finished<actual: raise ValueError('Candidate generation clock moved backwards')
    for item in candidates: item['available_at']=finished.isoformat()
    record={'schema':INPUT_SCHEMA,'method_id':METHOD_ID,'created_at':finished.isoformat(),
        'calculation_started_at':actual.isoformat(),
        'day':day,'execution_day':sessions[sessions.index(day)+1],
        'status':'completed_prospective_candidate_input' if prospective else 'context_before_first_prospective_decision',
        'registration':reg_ref,'source':bindings,'catalog_reference':{'file':str(Path(catalog).resolve()),'sha256':digest(catalog)},
        'prior_identity':{k:v for k,v in identity.items() if k not in {'members','entry'}},
        'prior_identity_price_source_binding':identity_source,
        'scope_members':list(loaded['scope_members']),'scope_complete':not unresolved,
        'candidates':candidates,'candidate_count':len(candidates),'unresolved_symbols':unresolved,
        'evaluations':rows,'original_32_inputs_changed':False,'target_generation_allowed':False,
        'paper_publication_implemented':False,'forward_consumer_implemented':False,
        'rules':deepcopy(RULES),**FLAGS}
    return record


def build(base_panel,catalog,ipo_evidence,research_master_run,registration,out,*,_clock=None):
    out=Path(out).resolve()
    if out.exists(): raise FileExistsError('A new dated-catchup input directory is required')
    loaded=load_inputs(base_panel,ipo_evidence,research_master_run)
    actual=_time(_clock() if _clock else _now())
    record=prepare_loaded_input(loaded,catalog,registration,actual=actual,
        finished_clock=_clock or _now)
    out.mkdir(parents=True,exist_ok=False)
    write_json(out/'input.json',record)
    return record


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['register','build'])
    for name in ('base-panel','catalog','ipo-evidence','research-master-run','out'):
        p.add_argument('--'+name,required=True,type=Path)
    p.add_argument('--registration',type=Path)
    a=p.parse_args(argv)
    if a.operation=='build' and a.registration is None: p.error('build requires --registration')
    args=[a.base_panel,a.catalog,a.ipo_evidence,a.research_master_run]
    r=register(*args,a.out) if a.operation=='register' else build(*args,a.registration,a.out)
    print(json.dumps({k:r.get(k) for k in ('schema','method_id','registered_at','first_decision_day','day','status','candidate_count','target_generation_allowed')},ensure_ascii=False))
    return 0
