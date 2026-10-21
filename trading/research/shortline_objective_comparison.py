"""Registered label-objective comparison; shared native features and execution."""
import argparse
import json
from pathlib import Path

import pandas as pd

from .baostock_daily_slice import digest
from . import shortline_objective_models as models
from . import shortline_objective_labels as labels
from . import shortline_native_evaluation as original
from . import shortline_native_selection_replay as execution
from . import shortline_remaining_edge_stats as statistics
from .leader_eight_type_historical_native import load_posthoc_listing

METHOD = 'native_matched_binary_vs_next_open_h5_return_objective_v1'
FLAGS = dict(candidate_panel_only=True, strategy_validated=False, selection_enabled=False,
    execution_admitted=False, returns_reportable=False, historical_publication_verified=False,
    independent_holdout_claimed=False, network_accessed=False, old_models_modified=False)
SPEC = dict(id=METHOD, arms=list(models.ARMS), features=list(models.FEATURES), stages=[1,2,3],
    train_start='2024-01-02', train_end='2025-12-31', score_start='2026-01-05', score_end='2026-09-30',
    binary_target='hit5', return_target='adjusted_open_Dplus6_divided_by_Dplus1_minus1',
    training_sample='identical_joint_eligible_eventIDs_for_both_arms_not_old_binary_sample',
    purge='max(hit5_witness_day,return_exit_day)_le2025_12_31',
    training_clearance_filter=False, unknown_training_targets='exclude_from_both_arms_never_zero',
    preprocessing='one_stagewise_training_only_median_imputer_with_indicator_shared_by_both_arms',
    complexity='same_original_LGBM200trees_leaf15_lr0.03_child50_seed42_no_search',
    changing_model_field='objective_binary_vs_L2_regression_only',
    selection='original_stagewise_first_rank_top10percent_then_global_score_desc_symbol_stage_top5',
    empty_or_unknown='original_whole_day_NO_CHANGE_not_empty_FULL',
    predicted_negative_returns='remain_finite_scores_no_new_profit_threshold',
    evaluation='existing_saved_same_event_next_open_h1_h5_prices_joined_only_after_score_and_selection_seal',
    account='original_saved_selection_execution_helper_same5slots0.16_each_T1_fee_raw_sources_and6tail_days',
    differences='no_old_frozen_weight_refit_no_new_factors_or_hyperparameter_search',
    interpretation='retrospective_mechanism_test_in_previously_inspected2026_not_untouched_validation')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    models.write(path, value)


def pin(path):
    return dict(path=str(Path(path).resolve()),sha256=digest(path))


def check(ref):
    if digest(ref['path']) != ref['sha256']:
        raise ValueError('Registered objective-comparison dependency changed: '+ref['path'])


def register(panel, scope_registration, edge_registration, edge_result, out):
    out = Path(out).resolve()
    refs = {k:pin(v) for k,v in dict(panel=panel,scope_registration=scope_registration,
        edge_registration=edge_registration,edge_result=edge_result).items()}
    scope, edge, source_panel, prior_price = (read(refs[k]['path']) for k in
        ('scope_registration','edge_registration','panel','edge_result'))
    if (scope.get('spec') != original.SPEC or len(scope.get('days',[])) != 181
            or source_panel.get('feature_contract_id') != original.CONTRACT
            or edge.get('calendar') is None
            or prior_price.get('status') != 'completed_source_only_remaining_price_edge_diagnostic'
            or source_panel['event_features_path'] != scope['refs']['features']['path']
            or prior_price.get('registration') != pin(edge_registration)):
        raise ValueError('Require original matching native contract/scope and saved remaining-price result')
    refs['features'] = pin(source_panel['event_features_path'])
    refs['labels'] = pin(source_panel['events_path'])
    refs['eval_prices'] = pin(Path(edge_result).resolve().parent/'event_observations.parquet')
    refs['raw_inputs'] = edge['refs']['raw_inputs']
    implementations = [pin(p) for p in (__file__,models.__file__,labels.__file__,original.__file__,
        execution.__file__,statistics.__file__)]
    for ref in [*refs.values(),*edge['refs'].values(),*scope['refs'].values(),*implementations]:
        check(ref)
        p = Path(ref['path']).resolve()
        if out==p or p in out.parents or out in p.parents:
            raise ValueError('New study must not overwrite existing source/registration directories')
    out.mkdir(parents=True,exist_ok=False)
    value = dict(schema=METHOD+'_registration',spec=SPEC,registered_at=models.clock(),
        refs=refs,implementations=implementations,source=read(refs['raw_inputs']['path']),
        expected_days=scope['days'],prior_price_files=prior_price['files'],
        new_fit_explicitly_authorized_by_research_scope=True, **FLAGS)
    save(out/'registration.json',value)
    return value


def evaluate_saved_prices(selection, days, path, out):
    price = pd.read_parquet(path)
    keys = ['event_id','date','symbol','stage']
    price = price.drop(columns=['model_kind','candidate_selected']).drop_duplicates()
    if price.event_id.duplicated().any():
        raise ValueError('Old model variants disagree about identical price outcomes')
    events = selection.merge(price,on=keys,how='left',validate='many_to_one',indicator=True)
    if not events['_merge'].eq('both').all():
        raise ValueError('All new score events need the original exact saved price observation')
    events = events.drop(columns='_merge')
    summary = statistics.analyze(events,days)
    events.to_parquet(out/'evaluated_events.parquet',index=False)
    for name in ('detail','summary','coverage','nonoverlap_origins'):
        summary[name].to_csv(out/(name+'.csv'),index=False)
    save(out/'price_metadata.json',summary['metadata'])
    return summary


def replay_new_selection(record, plans, days, out):
    source = record['source']
    symbols = {arm:sorted({s for p in plans[arm].values() for s in p['symbols']}) for arm in models.ARMS}
    sessions = days+execution.TAIL
    action = read(source['action_event']['path'])
    action_end = min(action['end'],source['action_source_end']) if 'action_source_end' in source else action['end']
    view = dict(source=source,expected_sessions=sessions,action_source_end=action_end,symbols=symbols)
    raw = execution._raw(source,sessions,sorted(set(symbols['binary'])|set(symbols['return'])))
    qualification = load_posthoc_listing(source['master_run']['path'],source['calendar']['path'],
        observed_at=record['registered_at'],prior_calendars=[r['path'] for r in source.get('prior_calendars',[])])
    cases = []
    for arm in models.ARMS:
        try:
            case = execution._case(arm,plans[arm],raw,view,out/arm,qualification)
        except (ValueError,KeyError,TypeError,PermissionError) as error:
            case = dict(kind=arm,status='execution_setup_failed',error=type(error).__name__+': '+str(error),
                account_state_unknown=True,full_tail=dict(complete_PnL_available=False,metrics=None))
        cases.append(case)
        print(json.dumps(dict(stage='new_objective_account_terminal',arm=arm,status=case['status'],
            engine_sessions=case.get('engine_sessions_committed'),fills=case.get('fill_callbacks'))),flush=True)
    return dict(execution_helper_method=execution.METHOD_ID,
        helper_schema_is_execution_protocol_not_old_model_acceptance=True,
        cases=cases,full_paired_account_return_available=all(c['full_tail']['complete_PnL_available'] for c in cases))


def run(registration,out):
    registration,out = Path(registration).resolve(),Path(out).resolve()
    record = read(registration)
    if record.get('schema') != METHOD+'_registration' or record.get('spec') != SPEC:
        raise ValueError('Use this independently fixed objective comparison registration')
    for ref in [*record['refs'].values(),*record['implementations']]:
        check(ref)
        p = Path(ref['path']).resolve()
        if out==p or p in out.parents or out in p.parents:
            raise ValueError('Output must be independent of immutable source directories')
    out.mkdir(parents=True,exist_ok=False)
    started=models.clock()
    save(out/'started.json',dict(started_at=started,registration=pin(registration),**FLAGS))
    try:
        built = labels.build_training_labels(record['refs']['panel']['path'],
            record['refs']['edge_registration']['path'],out/'training_labels')
        targets = pd.read_parquet(out/'training_labels'/'training_targets.parquet').rename(columns={'hit5':'hit_5'})
        common = targets.loc[targets.joint_training_eligible].copy()
        feature_fields = ['event_id','date','symbol','stage',*models.FEATURES]
        feature_table = pd.read_csv(record['refs']['features']['path'],usecols=feature_fields,
            dtype={'event_id':str,'date':str,'symbol':str})
        train = common.merge(feature_table,on=['event_id','date','symbol','stage'],how='left',validate='one_to_one',indicator=True)
        if not train['_merge'].eq('both').all():
            raise ValueError('Joint label and feature identities differ')
        train=train.drop(columns='_merge')
        manifest=models.fit_generation(train,out/'models',proof=dict(joint_sample_for_both_arms=True,
            new_generation_not_old_weight_recovery=True,labels=pin(out/'training_labels'/'training_targets.parquet'),
            registered_at=record['registered_at'],maximum_label_day=str(train.max_label_day.max()),
            feature_contract_id=original.CONTRACT))
        frozen=models.load_generation(out/'models')
        own,scopes=original._scope_inputs(read(record['refs']['scope_registration']['path']))
        scores=models.score_features(frozen,own.loc[:,feature_fields])
        scores.to_csv(out/'scores.csv',index=False)
        selection,decisions,plans=models.select_scores(scores,scopes,record['expected_days'])
        selection.to_csv(out/'selection.csv',index=False)
        save(out/'decisions.json',decisions)
        seal=dict(actual_sealed_at=models.clock(),generation=pin(out/'models'/'manifest.json'),
            scores=pin(out/'scores.csv'),selection=pin(out/'selection.csv'),decisions=pin(out/'decisions.json'),
            model_refit=False,future_labels_used_for_score=False,evaluation_labels_opened=False,
            historical_publication_claimed=False)
        save(out/'selection_seal.json',seal)
        print(json.dumps(dict(stage='new_scores_and_selection_closed',events_per_arm=len(own),
            new_fit=True,score_refit=False,seal=seal['actual_sealed_at'])),flush=True)
        price_summary=evaluate_saved_prices(selection,record['expected_days'],record['refs']['eval_prices']['path'],out)
        accounts=replay_new_selection(record,plans,record['expected_days'],out/'accounts')
        result=dict(schema=METHOD+'_result',status='completed_matched_objective_comparison',
            registered_at=record['registered_at'],started_at=started,completed_at=models.clock(),
            registration=pin(registration),model_manifest=pin(out/'models'/'manifest.json'),
            training_label_result=built,training_counts=manifest['training_counts'],
            selected_counts=selection.groupby('model_kind').candidate_selected.sum().to_dict(),
            selected_days={arm:sum(bool(p['symbols']) for p in plans[arm].values()) for arm in models.ARMS},
            evaluation_events_per_arm=len(own),new_generation_model_refit=True,score_model_refit=False,
            future_labels_used_for_score=False,selection_seal=pin(out/'selection_seal.json'),
            price_statistics=price_summary['metadata'],accounts=accounts,**FLAGS)
        save(out/'result.json',result)
        return result
    except Exception as error:
        save(out/'failure.json',dict(schema=METHOD+'_failure',started_at=started,failed_at=models.clock(),
            error=type(error).__name__+': '+str(error),partial_outputs_preserved=True,**FLAGS))
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest='operation',required=True)
    p=subs.add_parser('register')
    for name in ('panel','scope-registration','edge-registration','edge-result','out'):
        p.add_argument('--'+name,required=True)
    p=subs.add_parser('run');p.add_argument('--registration',required=True);p.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    value=(register(args.panel,args.scope_registration,args.edge_registration,args.edge_result,args.out)
        if args.operation=='register' else run(args.registration,args.out))
    print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':
    main()
