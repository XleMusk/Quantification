"""Independent trend holding-clock comparison, reusing the saved native inputs."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from .baostock_daily_slice import digest
from . import leader_eight_type_case_replay as original_durable
from .leader_eight_type_historical_native import load_inputs
from .leader_eight_type_panel_replay import (TYPE_IDS, GUARDS, CAPS, PROTOCOL, _rank_path,
    _environment, _validate_panel_data, _QualifiedFeed)
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .shortline_decision_v2 import BATCH_SCHEMA, fixed_exit_policy
from .leader_type_risk_exit import LeaderTypeRiskExitPolicy
from .shortline_panel_replay import _config
from .stockdb_action_accounting import load_action_accounting
from . import leader_trend_holding_policy as policy


METHOD_ID = 'native_trend_holding_clock_matched_current_producer_2024_2025_v2'
SCHEMA = METHOD_ID + '_result'
MAP_SCHEMA = 'leader_trend_holding_comparison_baselines_v1'
REGISTRATION_SCHEMA = METHOD_ID + '_registration'
TREND = 'trend_leader'
if TREND not in TYPE_IDS:
    raise ValueError('The exact original trend type is required')
CASE_IDS = tuple(f'{TREND}__{guard}__{cap}' for guard in GUARDS for cap, _ in CAPS)
ARMS = ('matched_control','structure_only')
FLAGS = dict(research_only=True, candidate_panel_only=True, execution_admitted=False,
    selection_enabled=False, strategy_validated=False, returns_reportable=False,
    model_refit=False, future_labels_used_for_score=False, network_accessed=False,
    user_account_mutated=False, historical_signal_publication_verified=False,
    pit_verified=False, corporate_actions_verified=False)
SPEC = dict(method_id=METHOD_ID, years=[2024,2025], case_ids=list(CASE_IDS),
    new_cases=16, arms=list(ARMS), baseline='matched_current_producer_same_source_and_terminal_clock',
    historical_saved_cases='eight_old_cases_are_historical_evidence_not_paired_controls_old_code_unavailable',
    type=TREND, policy=deepcopy(policy.RULE_SPEC), original_protocol=deepcopy(PROTOCOL),
    config=_config(100000,5).to_dict(),
    target_expiry={'matched_control':True,'structure_only':False},
    risk_expiry={'matched_control':True,'structure_only':False},
    unchanged=['saved_raw_context_panels_scope_top5_capacity_guard_structure_costs_Engine_T1'],
    terminal_clock='six_original_tail_sessions;tail1_executes_end_target;tail5_close_FULL_EMPTY;tail6_open_exit;tail6_close_NO_CHANGE',
    tail_risk='original_structure_only_daily;original_environment_guard_only_inside_decision_window',
    comparison_limit='same_new_terminal_clock_in_both_new_arms;old_saved_returns_not_causally_compared_without_old_producer_bytes',
    unknown='preserve_original_NC_and_held_source_failure_residual_and_null_no_notice_repair')


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _pin(path):
    path=Path(path).resolve()
    return {'path':str(path),'sha256':digest(path)}


def _checked(ref):
    path=Path(ref['path']).resolve()
    if digest(path)!=ref['sha256']:
        raise ValueError('Registered source or baseline bytes changed: '+str(path))
    return path


def _save(path,value):
    with Path(path).open('x',encoding='utf-8') as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2,allow_nan=False)


def _separate(out,refs):
    out=Path(out).resolve()
    if out.exists(): raise FileExistsError('Use an explicitly new comparison output')
    for ref in refs:
        parent=Path(ref['path']).resolve().parent
        if out==parent or parent in out.parents or out in parent.parents:
            raise ValueError('New output must be outside original source and account directories')
    return out


def _source_study(study):
    year=study['year']
    if year not in (2024,2025): raise ValueError('Only the two fixed years are admitted')
    header=_read(_checked(study['inputs']))
    event=_read(_checked(study['action_event_view']))
    reference=_read(_checked(study['action_reference_view']))
    metadata=_read(_checked(study['metadata']))
    if (not header['start'].startswith(str(year)) or not header['through'].startswith(str(year))
            or header['fixed_protocol']!=PROTOCOL
            or event.get('status')!='research_action_events_projected'
            or reference.get('status')!='research_reference_presence_diagnostic'
            or Path(reference['event_view']).resolve()!=Path(study['action_event_view']['path']).parent):
        raise ValueError('Exact same-year native input and action pair required')
    if study['metadata_kind']=='registration':
        record=metadata
        if (record.get('schema')!=original_durable.METHOD+'_registration'
                or record.get('method_id')!=original_durable.METHOD or record.get('protocol')!=PROTOCOL
                or record.get('case_ids')!=list(original_durable.CASE_IDS)
                or record.get('config')!=SPEC['config']
                or any(record.get(k) is not v for k,v in original_durable.FLAGS.items())
                or any(record[k]!=study[k] for k in ('inputs','action_event_view','action_reference_view'))):
            raise ValueError('Original registration and chosen baseline sources differ')
    elif study['metadata_kind']=='historical_method':
        if (metadata.get('schema')!='leader_eight_type_historical_native_result_v1'
                or metadata.get('original_32_protocol_unchanged') is not True):
            raise ValueError('Original historical method receipt required, not a fabricated registration')
        accounting=metadata['action_accounting']
        if any(Path(accounting[k+'_view']).resolve()!=Path(study['action_'+k+'_view']['path']).parent for k in ('event','reference')):
            raise ValueError('Historical original pair differs from the requested baseline')
    else:
        raise ValueError('Explicit original registration or posthoc historical-method provenance required')
    tails=[d for d in header['source_days'] if d>header['end']]
    if len(tails)!=6: raise ValueError('Retain exactly the original six tail source sessions')
    if [r['case_id'] for r in study['cases']]!=list(CASE_IDS):
        raise ValueError('All four original guard/cap cases must be pinned in fixed order')
    summaries=[]
    for item in study['cases']:
        case=_read(_checked(item['case_file']))
        if (case.get('case_id')!=item['case_id'] or case.get('type')!=TREND
                or case.get('config')!=SPEC['config']):
            raise ValueError('Exact original trend case identity and costs required')
        own=case['historical_execution_policy']['accounting']
        if (Path(own['event_view']).resolve()!=Path(study['action_event_view']['path']).parent
                or Path(own['reference_view']).resolve()!=Path(study['action_reference_view']['path']).parent):
            raise ValueError('Reused-case original pair must not be replaced by its supplement container pair')
        summaries.append({k:deepcopy(case.get(k)) for k in ('case_id','type','guard','capacity',
            'status','complete_PnL_available','metrics','engine_sessions_committed','blocking_evidence',
            'residual_positions','source_origin','source_origin_history')})
    return header,tails,summaries


def register(baselines,out):
    baselines=Path(baselines).resolve()
    mapping=_read(baselines)
    if mapping.get('schema')!=MAP_SCHEMA or [s['year'] for s in mapping['studies']]!=[2024,2025]:
        raise ValueError('Explicit ordered 2024/2025 baseline map required')
    references=[_pin(baselines)]
    studies=[]
    for study in mapping['studies']:
        header,tails,cases=_source_study(study)
        references.extend([study[k] for k in ('inputs','metadata','action_event_view','action_reference_view')])
        references.extend(item['case_file'] for item in study['cases'])
        studies.append({**deepcopy(study),'start':header['start'],'end':header['end'],
            'through':header['through'],'source_days':header['source_days'],'tail_days':tails,
            'baseline_cases':cases,'source_payloads':{k:header[k] for k in
                ('raw_file','raw_sha256','context_raw_file','context_raw_sha256','panels_file','panels_sha256','calendar','calendar_sha256')}})
        if study['metadata_kind']=='registration':
            old={r['path']:r['sha256'] for r in _read(study['metadata']['path'])['code']}
            current={r['path']:r['sha256'] for r in original_durable._runtime_pins()}
            studies[-1]['historical_current_code_differences']=[dict(path=p,old_sha256=old.get(p),current_sha256=current.get(p))
                for p in sorted(set(old)|set(current)) if old.get(p)!=current.get(p)]
        else:
            studies[-1]['historical_current_code_differences']=None
    out=_separate(out,references)
    runtime=original_durable._runtime_pins()+[_pin(__file__),_pin(policy.__file__)]
    record=dict(schema=REGISTRATION_SCHEMA,method_id=METHOD_ID,spec=SPEC,registered_at=_now(),
        baselines=_pin(baselines),studies=studies,dependencies=references,
        implementation=_pin(__file__),policy_implementation=_pin(policy.__file__),runtime_code=runtime,**FLAGS)
    out.mkdir(parents=True,exist_ok=False)
    snapshots=out/'code_snapshot';snapshots.mkdir()
    copies=[]
    for index,ref in enumerate(runtime):
        source=_checked(ref);destination=snapshots/(f'{index:02}_'+source.name)
        with destination.open('xb') as handle: handle.write(source.read_bytes())
        copied=_pin(destination)
        if copied['sha256']!=ref['sha256']: raise ValueError('Source changed during its byte snapshot')
        copies.append({'source':ref,'copy':copied})
    record['code_snapshots']=copies
    _save(out/'registration.json',record)
    return record


def _new_case(raw,days,sessions,panels,paths,environments,members,qualification,anchor,
              start,end,identity,cap,guard,account_id,factory,context_raw,*,arm='structure_only'):
    """Original case consumer flow; only expiry factories and study-end clock differ.

    Kept separate because old durable registrations pin their original producer.
    No global override, old account restoration, or default-policy fallback occurs.
    """
    config,state=_config(100000,5),new_state(100000)
    from .recent_alpha158_panel_replay import ExactSourceRawFeed
    base=ExactSourceRawFeed(pd.concat([context_raw,raw],ignore_index=True),
        sorted(set(context_raw.date)|set(days)),sorted(members),context_anchor=anchor)
    execution=factory(base,days)
    context=RunContext(account_id,TradingMode.REPLAY)
    if arm not in ARMS: raise ValueError('Explicit registered matched arm required')
    risk_factory=LeaderTypeRiskExitPolicy if arm=='matched_control' else policy.risk_policy
    target_factory=fixed_exit_policy if arm=='matched_control' else policy.target_policy
    risk=risk_factory(identity,
        candidate_by_day={d:next(t for t in p['types'] if t['id']==identity)['candidates'] for d,p in panels.items()},
        feature_by_day={d:p.get('risk_features',{}) for d,p in panels.items()},
        complete_feature_days=[d for d,p in panels.items() if p.get('risk_feature_scope_complete') is True])
    risk=execution.wrap_risk_policy(risk)
    batches={d:{'schema':BATCH_SCHEMA,'day':d,'source':'research:fixed_eight_type_panel',
        'status':'incomplete','candidates':[]} for d in days}
    tail=[d for d in days if d>end]
    if len(tail)!=6: raise ValueError('Fixed six-session tail is required')
    terminal=tail[4]
    daily,decisions,blocked,main_snapshot=[],[],None,None
    for index,day in enumerate(days):
        held={s for s,lots in state['positions'].items() if sum(l['qty'] for l in lots)>0}
        pending=set(state.get('pending_targets') or {})
        owned_issues=execution.raw_issues(day,held|pending)
        owned_refs=execution.references(day,held|pending)
        owned_ipo=[qualification.assess(s,day,decision_as_of=day+'T09:30:00+08:00') for s in sorted(held|pending)]
        if (owned_issues or any(not execution.reference_ok(r) for r in owned_refs)
                or any(r['engine_block_reason'] is not None for r in owned_ipo)):
            blocked=dict(day=day,reason='held_or_pending_raw_or_action_reference_unknown',
                raw_issues=owned_issues,reference_evidence=owned_refs,IPO_evidence=owned_ipo,session_not_committed=True)
            break
        panel=panels.get(day)
        item=next((r for r in panel['types'] if r['id']==identity),None) if panel else None
        path=paths.get(day,{'candidates':[],'original_top5':[],'cap_dropped_rows':0})
        mode=environments[day]['per_type'][identity]['mode'] if guard!='unguarded' and day in environments else 'ALLOW_FIXED_CANDIDATES'
        reasons,candidate_issues,refs,ipo=[],[],[],[]
        selected=path['candidates']
        following=sessions[sessions.index(day)+1] if sessions.index(day)+1<len(sessions) else None
        in_window=start<=day<=end
        if day==terminal:
            selected=[]  # Independent fixed study-end liquidation after the last entry's tail1 open.
        elif not in_window:
            reasons.append('execution_tail_no_new_entry')
        elif following not in days:
            reasons.append('withheld_missing_exact_successor_full_source')
        elif mode=='EXIT_ALL':
            selected=[]
        elif mode=='NO_CHANGE':
            reasons.append('environment_unknown_or_unconfirmed')
        else:
            if item is None or item.get('status') not in {'matched','not_matched'}:
                reasons.append('type_observation_unknown')
            if item is not None and set(item.get('unresolved_symbols',[])).intersection(members):
                reasons.append('type_membership_unknown_in_fixed_panel')
            if not selected: reasons.append('type_empty_or_capacity_filtered_all_no_signal')
            required={r['symbol'] for r in selected}
            candidate_issues=execution.raw_issues(day,required)
            refs=execution.references(day,required)
            ipo=[qualification.assess(s,day,decision_as_of=day+'T15:05:00+08:00') for s in sorted(required)]
            if candidate_issues: reasons.append('selected_current_raw_unknown')
            if any(not execution.reference_ok(r) for r in refs): reasons.append('selected_current_or_execution_reference_unknown')
            if any(r['engine_block_reason'] is not None for r in ipo): reasons.append('selected_IPO_qualification_unknown_or_unsupported')
        batches[day].update(status='complete' if not reasons else 'incomplete',
            candidates=[{'symbol':r['symbol'],'score':r['score']} for r in selected])
        trial=deepcopy(state)
        feed=_QualifiedFeed(execution.decision_feed(batches,trial),qualification)
        try:
            row=advance(trial,feed,day,config,index,context=context,
                target_policy=target_factory(feed,trial,config),replay_risk_exit_policy=risk)
        except (ValueError,KeyError) as error:
            blocked=dict(day=day,reason='shared_engine_session_blocked',error=f'{type(error).__name__}: {error}',session_not_committed=True)
            break
        state=trial
        if day==end: main_snapshot=deepcopy(state)
        daily.append(row)
        no_decision=next((e['reason'] for e in row['events'] if e['kind']=='no_decision'),None)
        decisions.append(dict(day=day,execution_day=following,in_decision_window=in_window,
            environment_mode=mode,status='NO_CHANGE' if no_decision else 'STUDY_END_FULL_EMPTY' if day==terminal
                else 'EXPLICIT_EXIT_ALL' if mode=='EXIT_ALL' else 'FULL_TARGET',
            original_top5=path['original_top5'],cap_candidates=path['candidates'],cap_dropped_rows=path['cap_dropped_rows'],
            reasons=reasons,no_change_reason=no_decision,raw_issues=candidate_issues,reference_evidence=refs,IPO_evidence=ipo,
            source_available_at=panel['available_at'] if panel else None,
            clock_semantics='late_derived_historical_EOD_mechanical_assumption_not_actual_publication',
            successor_used_for_ranking_or_refill=False,candidate_clearance_scope='decision_day_only_execution_guard_on_exact_E',
            successor_quotes_used_for_candidate_clearance=False))
    residual=sorted(s for s,lots in state['positions'].items() if sum(l['qty'] for l in lots)>0)
    complete=blocked is None and len(daily)==len(days) and not residual and bool(state['fills'])
    return dict(case_id=f'{identity}__{guard}__{cap}',type=identity,guard=guard,capacity=cap,
        status='completed_conditional_mechanical_replay' if complete else 'no_complete_PnL',config=config.to_dict(),
        decisions=decisions,daily=daily,orders=list(state['orders'].values()),fills=list(state['fills'].values()),account=state,
        blocking_evidence=blocked,observed_tail_sessions=len(tail),required_tail_sessions=6,residual_positions=residual,
        metrics=metrics(daily,config.initial_cash) if complete else None,complete_PnL_available=complete,
        engine_sessions_committed=len(daily),historical_execution_policy=execution.provenance,
        action_reference_checks=execution.accounting.checks,candidate_reference_checks=execution.checks,
        main_window_snapshot=main_snapshot,terminal_liquidation_decision=terminal,terminal_liquidation_execution=tail[5],
        arm=arm,risk_policy_id=(state.get('replay_risk_exit_policy') or {}).get('policy_id'),
        target_expiry_enabled=arm=='matched_control',risk_expiry_enabled=arm=='matched_control',
        engine_advanced=bool(daily),paper_orders_generated=bool(state['orders']),**FLAGS)


def _run_year(study,record,out,registration_pin):
    loaded,manifest=load_inputs(study['inputs']['path'])
    if any(manifest[k]!=v for k,v in study['source_payloads'].items()):
        raise ValueError('Original per-year source payload declarations changed')
    members,sessions,previous,as_of=_validate_panel_data(loaded['panel_by_day'],loaded['raw'],loaded['source_days'],
        loaded['calendar_payload'],loaded['scope_members'],loaded['qualification'])
    paths,_=_rank_path(loaded['panel_by_day'],members,study['start'],study['end'])
    environments=_environment(loaded['panel_by_day'],sessions,as_of)
    context_raw=pd.read_parquet(manifest['context_raw_file'])
    if sorted(context_raw.date.unique())!=[loaded['context_anchor']['day']]:
        raise ValueError('Original exact preceding context day required')
    accounting=load_action_accounting(Path(study['action_event_view']['path']).parent,
        Path(study['action_reference_view']['path']).parent,symbols=loaded['scope_members'],
        raw_days=sorted(set(context_raw.date)|set(loaded['source_days'])),replay_days=loaded['source_days'],
        price_source='independent_baostock_raw')
    factory=HistoricalExecutionFactory(accounting)
    results=[]
    for index,case_id in enumerate(CASE_IDS):
        identity,guard,cap=case_id.split('__')
        for arm in ARMS:
            folder=out/str(study['year'])/case_id/arm
            folder.mkdir(parents=True,exist_ok=False)
            try:
                case=_new_case(loaded['raw'],loaded['source_days'],sessions,loaded['panel_by_day'],paths[identity][cap],
                    environments,members,loaded['qualification'],loaded['context_anchor'],study['start'],study['end'],identity,cap,guard,
                    f'{METHOD_ID}:{registration_pin["sha256"]}:{study["year"]}:{case_id}:{arm}',factory,context_raw,arm=arm)
                for key in ('account','daily','decisions','orders','fills'):
                    _save(folder/(key+'.json'),case.pop(key))
                _save(folder/'main_window_snapshot.json',case.pop('main_window_snapshot'))
                case.update(year=study['year'],historical_saved_case=study['baseline_cases'][index],
                    historical_saved_file=study['cases'][index]['case_file'],source_inputs=study['inputs'],
                    measured_at=_now(),paired_terminal_clock_identical=True,
                    historical_saved_returns_are_paired_controls=False)
                _save(folder/'result.json',case)
            except Exception as error:
                case=dict(schema=SCHEMA,year=study['year'],case_id=case_id,arm=arm,status='case_failed',
                    error=f'{type(error).__name__}: {error}',execution_progress_unknown=True,
                    complete_PnL_available=False,metrics=None,historical_saved_case=study['baseline_cases'][index],**FLAGS)
                _save(folder/'failure.json',case)
            results.append(case)
            print(json.dumps(dict(event='new_case_saved',year=study['year'],case_id=case_id,arm=arm,
                status=case['status'],engine_sessions_committed=case.get('engine_sessions_committed')),ensure_ascii=False),flush=True)
    return results


def run(registration,out):
    registration=Path(registration).resolve()
    record=_read(registration)
    if record.get('schema')!=REGISTRATION_SCHEMA or record.get('spec')!=SPEC:
        raise ValueError('Exact independently registered holding-clock comparison required')
    refs=record['dependencies']+record['runtime_code']+[r['copy'] for r in record['code_snapshots']]
    for ref in refs: _checked(ref)
    out=_separate(out,refs+[{'path':str(registration),'sha256':digest(registration)}])
    out.mkdir(parents=True,exist_ok=False)
    _save(out/'started.json',dict(started_at=_now(),registration=_pin(registration),expected_new_cases=16,**FLAGS))
    cases=[]
    for study in record['studies']:
        try:
            cases.extend(_run_year(study,record,out,_pin(registration)))
        except Exception as error:
            failure=dict(year=study['year'],status='year_source_failed',error=f'{type(error).__name__}: {error}',
                execution_progress_unknown=True,expected_case_ids=list(CASE_IDS),**FLAGS)
            _save(out/(str(study['year'])+'_failure.json'),failure)
            cases.extend(dict(**failure,case_id=c,arm=a,complete_PnL_available=False,metrics=None) for c in CASE_IDS for a in ARMS)
    for ref in refs: _checked(ref)
    result=dict(schema=SCHEMA,method_id=METHOD_ID,registration=_pin(registration),completed_at=_now(),
        status='all_sixteen_matched_new_case_outcomes_saved',case_count=len(cases),cases=cases,
        historical_saved_baseline_replayed=False,matched_current_controls_run=True,
        old_accounts_modified=False,parameters_selected_by_return=False,
        paired_terminal_clock_identical=True,historical_saved_returns_are_paired_controls=False,**FLAGS)
    _save(out/'result.json',result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    reg=commands.add_parser('register');reg.add_argument('--baselines',type=Path,required=True);reg.add_argument('--out',type=Path,required=True)
    execute=commands.add_parser('run');execute.add_argument('--registration',type=Path,required=True);execute.add_argument('--out',type=Path,required=True)
    args=vars(parser.parse_args(argv));command=args.pop('command')
    result={'register':register,'run':run}[command](**args)
    print(json.dumps({k:result[k] for k in ('schema','execution_admitted')},ensure_ascii=False))
    return 0
