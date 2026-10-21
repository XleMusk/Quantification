"""A separately named new native generation with fixed training, then frozen scores.

This command fits new models explicitly. It does not recover old weights,
evaluate an already inspected return window, or publish portfolio candidates.
"""
from datetime import datetime
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .baostock_daily_slice import digest
from .shortline_split import purge_training_labels
from .shortline_frozen_model import (
    CN, FEATURES, FLAGS, LR_PARAMS, LGBM_PARAMS, RECIPE_ID,
    save_generation,
)
from .shortline_native_scoring import load_score_input, score_saved_generation

TRAIN_START, TRAIN_END = '2024-01-01', '2025-12-31'


def _new(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _actual():
    return datetime.now(CN).isoformat()


def train_and_score(panel_result, out):
    """Fit the fixed recipe once in a new directory, then reload for last-D scoring."""
    panel_result, out = Path(panel_result).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError('Use an explicitly new native model-generation directory')
    result = json.loads(panel_result.read_text(encoding='utf-8'))
    if (result.get('feature_contract_id') != 'native_active_known_nonST_observed_nine_features_v1'
            or not isinstance(result.get('feature_semantics'), dict)
            or result.get('execution_admitted') is not False
            or result.get('strategy_validated') is not False):
        raise ValueError('A saved independent native feature contract is required')
    panel = Path(result['events_path']).resolve()
    latest_file = Path(result['latest_features_path']).resolve()
    if digest(panel) != result['events_sha256'] or digest(latest_file) != result['latest_features_sha256']:
        raise ValueError('Native feature or label bytes changed after their completed production')
    if result['last_source_day'] <= TRAIN_END:
        raise ValueError('Last source day must be strictly after the fixed training window')
    frame = pd.read_parquet(panel)
    required = {'date', 'stage', 'decision_eligible', 'label_status', 'label_witness_day', 'hit_5', *FEATURES}
    if not required <= set(frame):
        raise ValueError('Native events must separate D eligibility from exact later label witnesses')
    if not frame.decision_eligible.map(lambda value: isinstance(value, (bool, np.bool_))).all():
        raise ValueError('Decision eligibility must be an explicit causal boolean')
    eligible = frame.loc[frame.decision_eligible & frame.label_status.eq('complete')].copy()
    train, purge = purge_training_labels(eligible, TRAIN_START, TRAIN_END,
        label_end_column='label_witness_day')
    if (train.empty or not pd.to_datetime(train.label_witness_day).le(TRAIN_END).all()
            or not pd.to_datetime(train.date).between(TRAIN_START, TRAIN_END).all()
            or train.hit_5.isna().any()):
        raise ValueError('Training labels must be complete and witnessed inside the fixed window')
    _, scored_input, _ = load_score_input(panel_result)
    started = _actual()
    out.mkdir(parents=True, exist_ok=False)
    fitted, training_rows = {'lr': {}, 'lgbm': {}}, []
    try:
        for stage in (1, 2, 3):
            own = train.loc[train.stage.eq(stage)]
            target = own.hit_5.astype(int)
            if own.empty or set(target.unique()) != {0, 1}:
                raise ValueError(f'Stage {stage} lacks a complete binary training target; no substitute model')
            values = own.loc[:, list(FEATURES)]
            if np.isinf(values.to_numpy(dtype=float, na_value=np.nan)).any():
                raise ValueError('Nonfinite native training observations are not silently repaired')
            lr = make_pipeline(SimpleImputer(strategy='median', add_indicator=True),
                StandardScaler(), LogisticRegression(**LR_PARAMS))
            lr.fit(values, target)
            imputer = SimpleImputer(strategy='median', add_indicator=True)
            lgbm = LGBMClassifier(**LGBM_PARAMS)
            lgbm.fit(imputer.fit_transform(values), target)
            fitted['lr'][stage] = lr
            fitted['lgbm'][stage] = {'imputer': imputer, 'model': lgbm}
            training_rows.append({'stage': stage, 'rows': len(own), 'positive_labels': int(target.sum()),
                'negative_labels': int(len(target) - target.sum()),
                'max_label_witness_day': pd.to_datetime(own.label_witness_day).max().date().isoformat()})
            print(json.dumps({'stage_fit_complete': stage, 'rows': len(own),
                'model_refit': True}, ensure_ascii=False), flush=True)
        proof = {'generation_id': out.name, 'feature_contract_id': result['feature_contract_id'],
            'feature_semantics': result['feature_semantics'], 'train_start': TRAIN_START,
            'train_end': TRAIN_END, 'labels_complete_through': pd.to_datetime(train.label_witness_day).max().date().isoformat(),
            'training_panel_sha256': result['events_sha256'], 'data_available_at': result['available_at'],
            'recipe_id': RECIPE_ID, 'label_end_purge_applied': True, 'model_refit': True,
            'label_end_column': 'label_witness_day', 'purge': purge, 'training_stage_counts': training_rows,
            'panel_result': str(panel_result), 'panel_result_sha256': digest(panel_result),
            'training_event_panel': str(panel), 'producer_source_proof': result['source_proof'],
            'legacy_weight_recovery_claimed': False, 'historical_holdout_performance_claimed': False,
            'new_fit_started_at': started}
        manifest = save_generation(fitted, out / 'models', training_proof=proof)
        scope = result['latest_scope']
        frozen_scores = score_saved_generation(out / 'models', panel_result, out / 'scores')
        summaries = []
        for saved in frozen_scores['score_batches']:
            kind = saved['model_kind']
            score_frame = pd.read_csv(saved['score_file'], dtype={'symbol': str, 'date': str})
            if score_frame.symbol.tolist() != scored_input.symbol.tolist():
                raise ValueError('Frozen score-file identities or row order differ from exact D input')
            errors = []
            for stage in (1, 2, 3):
                positions = np.flatnonzero(scored_input.stage.to_numpy() == stage)
                if not len(positions):
                    continue
                values = scored_input.iloc[positions].loc[:, list(FEATURES)]
                if kind == 'lr':
                    expected = fitted[kind][stage].predict_proba(values)[:, 1]
                else:
                    own = fitted[kind][stage]
                    expected = own['model'].booster_.predict(own['imputer'].transform(values), num_threads=1)
                actual = score_frame.iloc[positions].model_hit5_probability.to_numpy()
                if not np.allclose(expected, actual, rtol=1e-10, atol=1e-12):
                    raise ValueError('Actual persisted classifier differs from its original fitted probabilities')
                errors.append(float(np.max(np.abs(expected - actual))))
            summaries.append({**saved, 'max_actual_roundtrip_probability_error': max(errors)})
        complete = {'schema': 'shortline_native_new_generation_training_and_frozen_score_v1',
            'started_at': started, 'completed_at': _actual(), 'status': 'completed_new_fit_and_frozen_score_readback',
            'panel_result': str(panel_result), 'training_proof': proof,
            'model_manifest': str(out / 'models' / 'manifest.json'),
            'model_available_at': manifest['model_available_at'], 'score_batches': summaries,
            'source_scope': scope, 'new_fit_model_refit': True, 'score_model_refit': False,
            'legacy_outputs_modified': False, 'engine_advanced': False,
            'strategy_replayed': False, 'returns': None, **FLAGS}
        _new(out / 'result.json', complete)
        print(json.dumps({'status': complete['status'], 'out': str(out), 'training_stage_counts': training_rows,
            'score_batches': summaries, 'execution_admitted': False}, ensure_ascii=False), flush=True)
        return complete
    except Exception as error:
        _new(out / 'failure.json', {'schema': 'shortline_native_generation_failure_v1',
            'started_at': started, 'failed_at': _actual(), 'exception_type': type(error).__name__,
            'message': str(error), 'completed_stage_fits': training_rows,
            'partial_artifacts_preserved': True, 'returns': None, **FLAGS})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--panel', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    train_and_score(args.panel, args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
