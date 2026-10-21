import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.research import shortline_native_evaluation as subject
from trading.research.baostock_daily_slice import digest


def write(path, value):
    Path(path).write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def saved(tmp_path):
    source, model = tmp_path / 'source', tmp_path / 'model'
    source.mkdir()
    model.mkdir()
    days = [subject.START, '2026-01-06', subject.END]
    rows = []
    for index in range(20):
        symbol = f'{600000 + index:06d}.SSE'
        rows.append(dict(event_id=f'{symbol}|{days[0]}|s1', symbol=symbol,
            date=days[0], stage=1, stage_reference_known=True, decision_eligible=True,
            **{name: .1 for name in subject.FEATURES}))
    features = pd.DataFrame(rows)
    features.to_csv(source / 'event_features.csv', index=False)
    labels = features.loc[:, ['event_id', 'date', 'symbol', 'stage']].copy()
    labels['label_status'] = 'complete'
    labels['label_witness_day'] = '2026-01-06'
    labels['hit_5'] = [i % 2 for i in range(20)]
    labels['hit_4'] = labels.hit_5
    labels.loc[0, 'label_witness_day'] = '2026-10-08'
    labels.to_parquet(source / 'event_panel.parquet', index=False)
    pd.DataFrame([dict(date=days[1], symbol='601000.SSE', stage=None,
        reason='limit_proxy_observed_but_exact_prior_stage_or_reference_unknown',
        not_a_negative_example=True)]).to_csv(source / 'unresolved_membership.csv', index=False)
    pd.DataFrame([dict(date=day, unknown_stage_count=int(day == days[1]),
        stage_membership_complete=day != days[1], full_market_stage_membership_complete=False,
        stage123_event_count=20 if day == days[0] else 0, source_rows=30)
        for day in days]).to_csv(source / 'daily_market_context.csv', index=False)
    semantics = {'scope': 'synthetic_valid_observed_original_code'}
    entries = []
    for kind in subject.MODEL_KINDS:
        for stage in subject.STAGES:
            row = dict(model_kind=kind, stage=stage)
            for key in ('model', 'preprocessing'):
                file = model / f'{kind}_{stage}_{key}.json'
                write(file, {'fixture': True})
                row[key + '_file'], row[key + '_sha256'] = file.name, digest(file)
            entries.append(row)
    prior = [dict(stage=stage, rows=100, positive_labels=stage) for stage in subject.STAGES]
    write(model / 'manifest.json', dict(feature_contract_id=subject.CONTRACT,
        feature_semantics=semantics, training_proof=dict(train_end='2025-12-31', training_stage_counts=prior),
        models=entries, model_available_at='2026-10-16T02:55:29+08:00'))
    files = {file.name: digest(file) for file in source.iterdir()}
    panel = dict(feature_contract_id=subject.CONTRACT, feature_semantics=semantics,
        features=list(subject.FEATURES), source_days=days, available_at='2026-10-16T02:52:40+08:00',
        execution_admitted=False, strategy_validated=False, future_labels_in_feature_files=False,
        files=files, events_sha256=files['event_panel.parquet'],
        events_path=str(source / 'event_panel.parquet'), event_features_path=str(source / 'event_features.csv'),
        unknown_membership_path=str(source / 'unresolved_membership.csv'),
        daily_context_path=str(source / 'daily_market_context.csv'))
    write(source / 'result.json', panel)
    return dict(source=source, model=model, days=days, tmp=tmp_path)


def test_score_input_never_contains_labels_and_selection_is_closed_before_label_read(saved, monkeypatch):
    registration = saved['tmp'] / 'registration'
    subject.register(saved['model'], saved['source'] / 'result.json', registration)
    output = saved['tmp'] / 'evaluation'
    loads, calls = [], []
    monkeypatch.setattr(subject, 'load_generation', lambda path: loads.append(path) or object())

    def score(generation, frame, *, day, model_kind, feature_provenance):
        assert not {'hit_4', 'hit_5', 'label_status', 'label_witness_day'}.intersection(frame.columns)
        assert day == subject.START
        assert feature_provenance['unknown_stage_symbols'] == []
        calls.append(model_kind)
        own = frame.loc[:, ['event_id', 'date', 'symbol', 'stage']].copy()
        own['model_kind'] = model_kind
        own['model_hit5_probability'] = np.linspace(.99, .1, len(own))
        return dict(scores=own, provenance={'day': day, 'model_kind': model_kind, 'model_refit': False})

    monkeypatch.setattr(subject, 'score_day', score)
    original = pd.read_parquet

    def read(path, **kwargs):
        assert (output / 'prelabel_seal.json').is_file()
        assert (output / 'selection.csv').is_file()
        assert len(calls) == 2
        assert kwargs['columns'] == ['event_id', 'date', 'symbol', 'stage', 'label_status',
            'label_witness_day', 'hit_4', 'hit_5']
        return original(path, **kwargs)

    monkeypatch.setattr(subject.pd, 'read_parquet', read)
    result = subject.run(registration / 'registration.json', output)
    assert len(loads) == 1 and calls == ['lr', 'lgbm']
    assert result['summary']['model_selected_rows'] == 4
    assert result['summary']['model_selected_unknown_rows'] == 2
    evaluated = original(output / 'event_evaluation.parquet')
    first = evaluated.loc[evaluated.symbol.eq('600000.SSE')]
    assert first.candidate_selected.all() and not first.label_evaluable.any()
    assert first.hit_5_evaluated.isna().all()
    coverage = pd.read_csv(output / 'daily_stage_coverage.csv')
    assert len(coverage) == 18
    assert coverage.loc[coverage.stage.eq(3), 'pool_rows'].eq(0).all()
    decisions = pd.read_csv(output / 'daily_decisions.csv')
    assert decisions.loc[decisions.date.eq('2026-01-06'), 'reason'].eq(
        'stage_membership_incomplete_whole_day_NO_CHANGE').all()
    assert result['execution_admitted'] is False and result['engine_advanced'] is False


def test_fixed_small_stage_and_ties_do_not_force_one_or_refill():
    rows = [dict(event_id=f'{stage}:{i}', date=subject.START, symbol=f'{600000 + stage * 100 + i:06d}.SSE',
        stage=stage, model_hit5_probability=.8) for stage, count in [(1, 9), (2, 10)] for i in range(count)]
    frame = pd.DataFrame(rows[::-1])
    selected, decision = subject._select(frame, {'stage_membership_complete': True})
    assert selected.loc[selected.stage.eq(1), 'candidate_selected'].sum() == 0
    assert selected.loc[selected.candidate_selected, 'symbol'].tolist() == ['600200.SSE']
    assert decision['selected_count'] == 1
    unknown, decision = subject._select(frame, {'stage_membership_complete': False})
    assert not unknown.candidate_selected.any() and decision['decision'] == 'NO_CHANGE'


def test_scope_conflict_and_wrong_dates_rejected_without_label_read(saved):
    reg = saved['tmp'] / 'reg'
    record = subject.register(saved['model'], saved['source'] / 'result.json', reg)
    daily_file = saved['source'] / 'daily_market_context.csv'
    daily = pd.read_csv(daily_file)
    daily.loc[daily.date.eq('2026-01-06'), 'unknown_stage_count'] = 0
    daily.to_csv(daily_file, index=False)
    with pytest.raises(ValueError, match='scope and unresolved'):
        subject._scope_inputs(record)
    with pytest.raises(ValueError, match='bytes changed'):
        subject.run(reg / 'registration.json', saved['tmp'] / 'bad')
    assert not (saved['tmp'] / 'bad').exists()
    with pytest.raises(ValueError, match='Exact ascending'):
        subject._dates([subject.END, subject.START])


def test_class_deficiency_null_metrics_and_fixed_bins_keep_censored_rows():
    scores = pd.DataFrame([dict(event_id=str(i), date=subject.START, symbol=f'{600000+i:06d}.SSE',
        stage=1, model_kind='lr', model_hit5_probability=p, candidate_selected=True)
        for i, p in enumerate([0., .1, 1.])])
    labels = scores.loc[:, ['event_id', 'date', 'symbol', 'stage']].copy()
    labels['label_status'] = ['complete', 'complete', 'right_censored_snapshot_end']
    labels['label_witness_day'] = ['2026-01-06', '2026-01-06', None]
    labels['hit_5'], labels['hit_4'] = [0, 0, None], [0, 1, None]
    evaluated = subject._evaluate_labels(scores, labels)
    quality, bins, daily = subject._summaries(evaluated, {1: .1, 2: .2, 3: .3}, [subject.START])
    first = quality[0]
    assert first['sufficient_classes'] is False and all(v is None for v in first['metrics'].values())
    assert first['evaluable_rows'] == 2 and first['unknown_rows'] == 1
    own = [r for r in bins if r['model_kind'] == 'lr' and r['stage'] == 1]
    assert own[0]['pool_rows'] == own[1]['pool_rows'] == own[9]['pool_rows'] == 1
    assert own[9]['evaluable_rows'] == 0 and own[9]['unknown_rows'] == 1
    assert daily[0]['selected_rows'] == 3 and daily[0]['selected_unknown_rows'] == 1
