"""Retrospective frozen-model quality, separate from accounts and returns."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from .baostock_daily_slice import digest
from .shortline_frozen_model import CN, FEATURES, MODEL_KINDS, STAGES, load_generation, score_day
from . import shortline_frozen_model, shortline_native_preopen


METHOD_ID = 'native_frozen_stagewise_hit5_retrospective_quality_2026_v1'
REGISTRATION_SCHEMA = 'native_frozen_shortline_evaluation_registration_v1'
RESULT_SCHEMA = 'native_frozen_shortline_evaluation_result_v1'
START, END = '2026-01-05', '2026-09-30'
CONTRACT = 'native_active_known_nonST_observed_nine_features_v1'
FLAGS = dict(research_only=True, candidate_panel_only=True, execution_admitted=False,
    selection_enabled=False, strategy_validated=False, model_refit=False,
    future_labels_used_for_score=False, engine_advanced=False, account_backtest=False,
    orders_generated=False, returns_reportable=False, network_accessed=False)
SPEC = dict(method_id=METHOD_ID, event_start=START, event_end=END, label_witness_cutoff=END,
    primary_label='hit_5', secondary_label='hit_4_descriptive_not_four_board_probability',
    training_end='2025-12-31', selection_policy=shortline_native_preopen.POLICY_ID,
    stage_rank='stable_symbol_ascending_then_probability_desc_rank_first_pct_le_0.10',
    daily_top='probability_desc_symbol_asc_stage_asc_head5', minimum_one_per_stage=False,
    unknown_membership='original_valid_observed_scope_whole_day_NO_CHANGE_no_IPO_repair',
    label_join='only_after_scores_and_selection_closed_exact_event_id',
    metrics=['AP', 'AP_minus_base_rate', 'ROC_AUC', 'Brier', 'training_prior_Brier',
             'Brier_minus_training_prior_Brier'], calibration_bins=10,
    calibration_edges='[0,.1),[.1,.2),...,[.9,1]', insufficient_classes='metrics_null',
    selection_comparator='same_day_same_stage_known_label_pool_prevalence_not_fitted',
    retrospective=True, untouched_holdout_claimed=False, event_independence_claimed=False,
    historical_publication_claimed=False, returns=None)


def _clock():
    return datetime.now(CN).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _pin(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': digest(path)}


def _verify(ref):
    path = Path(ref['path']).resolve()
    if digest(path) != ref['sha256']:
        raise ValueError('Registered source bytes changed: ' + str(path))
    return path


def _bool(value):
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if value in ('True', 'False'):
        return value == 'True'
    raise ValueError('A saved explicit boolean is required')


def _dates(values):
    dates = list(values)
    if (not dates or dates != sorted(set(dates))
            or any(not isinstance(d, str) or pd.Timestamp(d).strftime('%Y-%m-%d') != d for d in dates)):
        raise ValueError('Exact ascending source dates are required')
    return dates


def register(model, panel_result, out):
    """Pin existing files and specification only; never load labels or infer."""
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError('Use a new registration directory')
    model = Path(model).resolve()
    manifest_file = model / 'manifest.json' if model.is_dir() else model
    manifest, panel_file = _read(manifest_file), Path(panel_result).resolve()
    panel = _read(panel_file)
    if (panel.get('feature_contract_id') != CONTRACT
            or panel.get('feature_contract_id') != manifest.get('feature_contract_id')
            or panel.get('feature_semantics') != manifest.get('feature_semantics')
            or panel.get('features') != list(FEATURES)
            or panel.get('execution_admitted') is not False
            or panel.get('strategy_validated') is not False
            or panel.get('future_labels_in_feature_files') is not False
            or manifest.get('training_proof', {}).get('train_end') != SPEC['training_end']):
        raise ValueError('Saved native feature and frozen training contracts must match')
    source_days = _dates(panel['source_days'])
    days = [d for d in source_days if START <= d <= END]
    if not days or days[0] != START or days[-1] != END:
        raise ValueError('The fixed historical evaluation interval is not covered')
    files = panel['files']
    paths = {'features': panel['event_features_path'], 'labels': panel['events_path'],
        'unresolved': panel['unknown_membership_path'], 'daily': panel['daily_context_path']}
    refs = {'panel_result': _pin(panel_file), 'manifest': _pin(manifest_file)}
    for name, raw in paths.items():
        path = Path(raw).resolve()
        if path.parent != panel_file.parent or path.name not in files:
            raise ValueError('Native files must be the original declared sibling payloads')
        refs[name] = _pin(path)
        if refs[name]['sha256'] != files[path.name]:
            raise ValueError('Native payload differs from its original declaration')
    if refs['labels']['sha256'] != panel['events_sha256']:
        raise ValueError('Native label source declaration differs')
    model_refs = []
    for entry in manifest['models']:
        own = []
        for key in ('model', 'preprocessing'):
            path = manifest_file.parent / entry[key + '_file']
            if path.resolve().parent != manifest_file.parent or path.name != entry[key + '_file']:
                raise ValueError('Model payload must stay within its generation')
            ref = _pin(path)
            if ref['sha256'] != entry[key + '_sha256']:
                raise ValueError('Frozen generation payload declaration differs')
            own.append(ref)
        model_refs.extend(own)
    if len(model_refs) != 12 or len(manifest['models']) != 6:
        raise ValueError('Exactly six frozen models and preprocessing states are required')
    prior = manifest['training_proof']['training_stage_counts']
    _priors(prior)
    code = [_pin(__file__), _pin(shortline_frozen_model.__file__), _pin(shortline_native_preopen.__file__)]
    protected = [Path(ref['path']) for ref in refs.values()] + [Path(r['path']) for r in model_refs]
    if any(out == p or out in p.parents or p.parent == out or p.parent in out.parents for p in protected):
        raise ValueError('Registration output must be outside all source directories')
    record = dict(schema=REGISTRATION_SCHEMA, method_id=METHOD_ID, spec=SPEC,
        registered_at=_clock(), refs=refs, model_payloads=model_refs, implementation=code,
        days=days, source_days=source_days, training_stage_counts=prior,
        model_available_at=manifest['model_available_at'], feature_available_at=panel['available_at'],
        feature_contract_id=CONTRACT, feature_semantics=panel['feature_semantics'], **FLAGS)
    out.mkdir(parents=True, exist_ok=False)
    _write(out / 'registration.json', record)
    return record


def _priors(rows):
    result = {}
    for stage in STAGES:
        matches = [r for r in rows if r['stage'] == stage]
        if len(matches) != 1:
            raise ValueError('Every fixed stage needs its original training count')
        row = matches[0]
        if row['rows'] <= 0 or not 0 <= row['positive_labels'] <= row['rows']:
            raise ValueError('Invalid fixed training prior')
        result[stage] = row['positive_labels'] / row['rows']
    return result


def _scope_inputs(record):
    """Read feature/scope files only; this function cannot open labels."""
    refs = record['refs']
    required = ['event_id', 'date', 'symbol', 'stage', 'stage_reference_known',
        'decision_eligible', *FEATURES]
    features = pd.read_csv(refs['features']['path'], usecols=required,
        dtype={'event_id': str, 'date': str, 'symbol': str})
    if features.event_id.isna().any() or features.event_id.duplicated().any():
        raise ValueError('Feature event identities must be unique and explicit')
    features['stage_reference_known'] = features.stage_reference_known.map(_bool)
    features['decision_eligible'] = features.decision_eligible.map(_bool)
    if (not features.date.isin(record['source_days']).all()
            or not features.stage.isin(STAGES).all()
            or not features.stage_reference_known.all()):
        raise ValueError('Feature identities or stage dates differ from their source contract')
    expected = features.symbol + '|' + features.date + '|s' + features.stage.astype(int).astype(str)
    if not features.event_id.eq(expected).all():
        raise ValueError('Feature event identities must match their exact D/symbol/stage')
    unresolved = pd.read_csv(refs['unresolved']['path'], dtype={'date': str, 'symbol': str})
    if (not {'date', 'symbol', 'reason', 'stage', 'not_a_negative_example'} <= set(unresolved)
            or unresolved.duplicated(['date', 'symbol']).any()
            or not unresolved.date.isin(record['source_days']).all()
            or not unresolved.stage.isna().all()
            or not unresolved.not_a_negative_example.map(_bool).all()
            or not unresolved.reason.eq('limit_proxy_observed_but_exact_prior_stage_or_reference_unknown').all()):
        raise ValueError('Original unresolved-membership records must remain explicit unknowns')
    daily = pd.read_csv(refs['daily']['path'], dtype={'date': str})
    if daily.date.duplicated().any() or daily.date.tolist() != record['source_days']:
        raise ValueError('Every original source D needs its exact saved daily scope')
    scopes = {}
    for row in daily.to_dict('records'):
        day = row['date']
        unknown = sorted(unresolved.loc[unresolved.date.eq(day), 'symbol'].tolist())
        complete = _bool(row['stage_membership_complete'])
        if (int(row['unknown_stage_count']) != len(unknown) or complete != (not unknown)
                or int(row['stage123_event_count']) != int(features.date.eq(day).sum())
                or _bool(row['full_market_stage_membership_complete']) is not False):
            raise ValueError('Daily saved scope and unresolved membership disagree')
        scopes[day] = {'stage_membership_complete': complete, 'unknown_stage_symbols': unknown,
            'source_scope': {k: (None if pd.isna(v) else v) for k, v in row.items()}}
    own = features.loc[features.date.isin(record['days']) & features.decision_eligible].copy()
    return own, scopes


def _select(scores, scope):
    """Exact original preopen ordering; selection never inspects a label."""
    frame = scores.sort_values('symbol', kind='stable').copy()
    frame['stage_rank_pct'] = frame.groupby('stage').model_hit5_probability.rank(
        ascending=False, method='first', pct=True)
    frame['rank_pass'] = frame.stage_rank_pct.le(.10) & scope['stage_membership_complete']
    frame = frame.sort_values(['model_hit5_probability', 'symbol', 'stage'],
        ascending=[False, True, True], kind='stable').reset_index(drop=True)
    selected = frame.loc[frame.rank_pass].head(5)
    frame['candidate_selected'] = frame.event_id.isin(selected.event_id)
    reason = ('stage_membership_incomplete_whole_day_NO_CHANGE' if not scope['stage_membership_complete']
        else 'no_candidates_after_fixed_stage_rank_NO_CHANGE' if selected.empty
        else 'fixed_native_stagewise_top10pct_daily_top5')
    return frame, {'decision': 'FULL_TARGET' if not selected.empty else 'NO_CHANGE',
        'reason': reason, 'selected_count': len(selected)}


def _evaluate_labels(scores, labels, *, cutoff=END):
    keys = ['event_id', 'date', 'symbol', 'stage']
    if labels.event_id.duplicated().any() or labels.event_id.isna().any():
        raise ValueError('Labels need one exact original event_id per event')
    missing = set(scores.event_id) - set(labels.event_id)
    if missing:
        raise ValueError('A saved feature event is absent from its original label panel')
    frame = scores.merge(labels, on=keys, how='left', validate='many_to_one', indicator=True)
    if not frame['_merge'].eq('both').all():
        raise ValueError('Label D/symbol/stage differs from its feature identity')
    frame = frame.drop(columns='_merge')
    witness = pd.to_datetime(frame.label_witness_day, errors='coerce')
    known = frame.label_status.eq('complete') & witness.notna() & witness.le(pd.Timestamp(cutoff))
    if (known & witness.le(pd.to_datetime(frame.date))).any():
        raise ValueError('A label witness must be strictly after its event D')
    for field in ('hit_5', 'hit_4'):
        value = pd.to_numeric(frame[field], errors='raise')
        if (known & ~value.isin([0, 1])).any():
            raise ValueError('Complete witnessed labels require binary outcomes')
        frame[field + '_evaluated'] = value.where(known)
    frame['label_evaluable'] = known
    frame['evaluation_label_reason'] = np.where(known, 'complete_witness_by_fixed_cutoff',
        np.where(frame.label_status.eq('complete'), 'witness_missing_or_after_fixed_cutoff',
            'original_' + frame.label_status.fillna('unknown')))
    return frame


def _metrics(frame, prior):
    own = frame.loc[frame.label_evaluable]
    y, p = own.hit_5_evaluated.to_numpy(float), own.model_hit5_probability.to_numpy(float)
    both = len(y) > 0 and len(np.unique(y)) == 2
    base = float(y.mean()) if len(y) else None
    metrics = {key: None for key in SPEC['metrics']}
    if both:
        metrics = {'AP': float(average_precision_score(y, p)),
            'AP_minus_base_rate': float(average_precision_score(y, p) - base),
            'ROC_AUC': float(roc_auc_score(y, p)), 'Brier': float(brier_score_loss(y, p)),
            'training_prior_Brier': float(np.mean((y - prior) ** 2)),
            'Brier_minus_training_prior_Brier': float(np.mean((y - p) ** 2) - np.mean((y - prior) ** 2))}
    return dict(event_rows=len(frame), evaluable_rows=len(own), unknown_rows=int((~frame.label_evaluable).sum()),
        positive_rows=int(y.sum()), negative_rows=int(len(y) - y.sum()),
        complete_label_episode_proxy_count=len(own[['symbol', 'label_witness_day']].drop_duplicates()),
        base_rate=base, training_prior=prior,
        sufficient_classes=both, metrics=metrics,
        secondary_hit4_rate=float(own.hit_4_evaluated.mean()) if len(own) else None)


def _summaries(frame, priors, days):
    quality, calibration, daily = [], [], []
    for kind in MODEL_KINDS:
        for stage in STAGES:
            own = frame.loc[frame.model_kind.eq(kind) & frame.stage.eq(stage)]
            quality.append({'model_kind': kind, 'stage': stage, **_metrics(own, priors[stage])})
            for index in range(10):
                mask = own.model_hit5_probability.ge(index / 10) & (
                    own.model_hit5_probability.le(1) if index == 9
                    else own.model_hit5_probability.lt((index + 1) / 10))
                bucket = own.loc[mask & own.label_evaluable]
                calibration.append(dict(model_kind=kind, stage=stage, bin=index,
                    lower=index / 10, upper=(index + 1) / 10, pool_rows=int(mask.sum()),
                    evaluable_rows=len(bucket), unknown_rows=int((mask & ~own.label_evaluable).sum()),
                    mean_probability=float(bucket.model_hit5_probability.mean()) if len(bucket) else None,
                    observed_hit5_rate=float(bucket.hit_5_evaluated.mean()) if len(bucket) else None))
    for day, kind, stage in ((d, k, s) for d in days for k in MODEL_KINDS for s in STAGES):
        own = frame.loc[frame.date.eq(day) & frame.model_kind.eq(kind) & frame.stage.eq(stage)]
        pool = own.loc[own.label_evaluable]
        selected = own.loc[own.candidate_selected]
        known = selected.loc[selected.label_evaluable]
        prevalence = float(pool.hit_5_evaluated.mean()) if len(pool) else None
        rate = float(known.hit_5_evaluated.mean()) if len(known) else None
        daily.append(dict(date=day, model_kind=kind, stage=int(stage), pool_rows=len(own),
            evaluable_rows=len(pool), unknown_rows=int((~own.label_evaluable).sum()),
            selected_rows=len(selected), selected_evaluable_rows=len(known),
            selected_unknown_rows=int((~selected.label_evaluable).sum()),
            pool_hit5_prevalence=prevalence, selected_hit5_rate=rate,
            selected_minus_same_day_stage_prevalence=rate - prevalence if rate is not None and prevalence is not None else None,
            selected_secondary_hit4_rate=float(known.hit_4_evaluated.mean()) if len(known) else None))
    return quality, calibration, daily


def run(registration, out):
    registration, out = Path(registration).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError('Use a new exclusive evaluation output directory')
    record = _read(registration)
    if record.get('schema') != REGISTRATION_SCHEMA or record.get('spec') != SPEC:
        raise ValueError('Use the exact independently registered evaluation specification')
    all_refs = list(record['refs'].values()) + record['model_payloads'] + record['implementation']
    for ref in all_refs:
        _verify(ref)
        parent = Path(ref['path']).resolve().parent
        if out == parent or parent in out.parents or out in parent.parents:
            raise ValueError('Evaluation output must be outside all registered source directories')
    if out == registration.parent or registration.parent in out.parents:
        raise ValueError('Evaluation cannot overwrite its independent registration')
    if _dates(record['days']) != [d for d in record['source_days'] if START <= d <= END]:
        raise ValueError('Registered evaluation dates differ from their exact source calendar')
    priors, started = _priors(record['training_stage_counts']), _clock()
    features, scopes = _scope_inputs(record)
    generation = load_generation(record['refs']['manifest']['path'])
    out.mkdir(parents=True, exist_ok=False)
    batches, decisions, provenance = [], [], []
    for day in record['days']:
        own, scope = features.loc[features.date.eq(day)].copy(), scopes[day]
        proof = dict(day=day, feature_names=list(FEATURES),
            source_sha256=record['refs']['features']['sha256'],
            feature_contract_id=record['feature_contract_id'], feature_semantics=record['feature_semantics'],
            available_at=record['feature_available_at'], known_stage_partial_research_only=not scope['stage_membership_complete'],
            full_market_stage_membership_complete=False, **scope)
        for kind in MODEL_KINDS:
            if own.empty:
                decisions.append(dict(date=day, model_kind=kind, input_rows=0,
                    decision='NO_CHANGE', reason='stage_membership_incomplete_whole_day_NO_CHANGE'
                        if not scope['stage_membership_complete'] else 'no_stage123_events_NO_CHANGE',
                    selected_count=0, unknown_stage_count=len(scope['unknown_stage_symbols'])))
                continue
            scored = score_day(generation, own, day=day, model_kind=kind, feature_provenance=proof)
            selected, decision = _select(scored['scores'], scope)
            batches.append(selected)
            provenance.append(scored['provenance'])
            decisions.append(dict(date=day, model_kind=kind, input_rows=len(own), **decision,
                unknown_stage_count=len(scope['unknown_stage_symbols'])))
    if not batches:
        raise ValueError('No known-stage events exist in the registered evaluation interval')
    scores = pd.concat(batches, ignore_index=True)
    scores.to_csv(out / 'scores.csv', index=False)
    scores.loc[:, ['event_id', 'date', 'symbol', 'stage', 'model_kind',
        'stage_rank_pct', 'rank_pass', 'candidate_selected']].to_csv(out / 'selection.csv', index=False)
    pd.DataFrame(decisions).to_csv(out / 'daily_decisions.csv', index=False)
    _write(out / 'score_provenance.json', provenance)
    frozen = dict(scores=_pin(out / 'scores.csv'), selection=_pin(out / 'selection.csv'),
        daily_decisions=_pin(out / 'daily_decisions.csv'), closed_before_label_read_at=_clock())
    _write(out / 'prelabel_seal.json', frozen)
    # The first semantic label read is deliberately after all scoring and selection are closed.
    labels = pd.read_parquet(record['refs']['labels']['path'], columns=[
        'event_id', 'date', 'symbol', 'stage', 'label_status', 'label_witness_day', 'hit_4', 'hit_5'])
    evaluated = _evaluate_labels(scores, labels)
    quality, calibration, daily = _summaries(evaluated, priors, record['days'])
    evaluated.to_parquet(out / 'event_evaluation.parquet', index=False)
    pd.DataFrame(daily).to_csv(out / 'daily_stage_coverage.csv', index=False)
    _write(out / 'quality.json', quality)
    _write(out / 'calibration.json', calibration)
    summary = dict(unique_event_rows=int(evaluated.event_id.nunique()),
        model_event_rows=len(evaluated), evaluated_unique_events=int(evaluated.loc[evaluated.label_evaluable, 'event_id'].nunique()),
        model_selected_rows=int(evaluated.candidate_selected.sum()),
        model_selected_unknown_rows=int((evaluated.candidate_selected & ~evaluated.label_evaluable).sum()),
        complete_label_episode_proxy_count=len(evaluated.loc[evaluated.label_evaluable,
            ['symbol', 'label_witness_day']].drop_duplicates()),
        registered_days=len(record['days']), quality=quality,
        notes=['LR/LGB score the same events; stage/run/date observations are associated, not independent trades.',
            'Model and evaluation were actually generated after historical event dates.',
            'Retrospective frozen out-of-training evaluation; not an untouched holdout or contemporaneous publication.',
            'Quality includes known-stage events on incomplete-membership days; selection on those days remains NO_CHANGE.',
            'Distinct symbol/witness-day episode proxies are descriptive and do not establish independence.',
            'hit_4 is a secondary observed outcome; the model probability predicts hit_5 only.',
            'No Engine, orders, fees, execution feasibility or strategy returns are evaluated.'])
    _write(out / 'summary.json', summary)
    for ref in all_refs:
        _verify(ref)
    result = dict(schema=RESULT_SCHEMA, method_id=METHOD_ID,
        status='completed_retrospective_frozen_stagewise_quality', started_at=started,
        completed_at=_clock(), registration=_pin(registration), model_available_at=record['model_available_at'],
        event_start=START, event_end=END, label_witness_cutoff=END, summary=summary,
        files={file.name: digest(file) for file in out.iterdir() if file.is_file()}, **FLAGS)
    _write(out / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    reg = commands.add_parser('register')
    reg.add_argument('--model', type=Path, required=True)
    reg.add_argument('--panel', type=Path, required=True)
    reg.add_argument('--out', type=Path, required=True)
    execute = commands.add_parser('run')
    execute.add_argument('--registration', type=Path, required=True)
    execute.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    result = (register(args.model, args.panel, args.out) if args.command == 'register'
        else run(args.registration, args.out))
    print(json.dumps({key: result[key] for key in ('schema', 'method_id', 'execution_admitted')}, ensure_ascii=False))
    return 0
