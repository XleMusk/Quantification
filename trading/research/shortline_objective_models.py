"""Independent matched LightGBM objectives; old native models are untouched."""
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer

from .baostock_daily_slice import digest
from .shortline_frozen_model import FEATURES, STAGES, CN, LGBM_PARAMS, _imputer_state, _transform

SCHEMA = 'native_shortline_matched_objective_generation_v1'
ARMS = ('binary', 'return')
LABEL_COLUMNS = {'hit_5', 'h5_return', 'label_witness_day', 'max_label_day', 'label_status'}
FLAGS = dict(candidate_panel_only=True, strategy_validated=False, selection_enabled=False,
    execution_admitted=False, historical_publication_verified=False, returns_reportable=False)


def clock():
    return datetime.now(CN).isoformat()


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def fit_generation(train, out, *, proof):
    """Explicit new fit of both arms on identical event IDs and preprocessing."""
    from lightgbm import LGBMClassifier, LGBMRegressor
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError('New independent generation required')
    required = {'event_id', 'date', 'stage', 'hit_5', 'h5_return', 'max_label_day', *FEATURES}
    if (not required <= set(train) or train.empty or train.event_id.duplicated().any()
            or not train.date.between('2024-01-02', '2025-12-31').all()
            or not train.max_label_day.between('2024-01-02', '2025-12-31').all()
            or not train.stage.isin(STAGES).all() or not train.hit_5.isin([0,1]).all()
            or not np.isfinite(train.h5_return).all()
            or np.isinf(train.loc[:,list(FEATURES)].to_numpy(dtype=float)).any()):
        raise ValueError('Exact purged joint training events and finite targets required')
    if proof.get('joint_sample_for_both_arms') is not True or proof.get('new_generation_not_old_weight_recovery') is not True:
        raise ValueError('Explicit matched new-generation proof required')
    out.mkdir(parents=True, exist_ok=False)
    started, entries, counts = clock(), [], []
    for stage in STAGES:
        own = train.loc[train.stage.eq(stage)].sort_values('event_id', kind='stable')
        if set(own.hit_5.unique()) != {0,1}:
            raise ValueError('No substitute model for an incomplete binary stage')
        values = own.loc[:,list(FEATURES)]
        imputer = SimpleImputer(strategy='median', add_indicator=True)
        transformed = imputer.fit_transform(values)
        state = _imputer_state(imputer)
        preprocessing = out / f'preprocessing_s{stage}.json'
        write(preprocessing, state)
        for arm in ARMS:
            params = {**LGBM_PARAMS, 'objective': 'binary' if arm == 'binary' else 'regression'}
            fitted = LGBMClassifier(**params) if arm == 'binary' else LGBMRegressor(**params)
            fitted.fit(transformed, own.hit_5.astype(int) if arm == 'binary' else own.h5_return)
            payload = out / f'{arm}_s{stage}.txt'
            fitted.booster_.save_model(str(payload), num_iteration=-1)
            entries.append(dict(arm=arm, stage=stage, model_file=payload.name,
                model_sha256=digest(payload), preprocessing_file=preprocessing.name,
                preprocessing_sha256=digest(preprocessing), parameters=params))
        counts.append(dict(stage=stage, rows=len(own), positive_hit5=int(own.hit_5.sum()),
            mean_training_h5_price_return=float(own.h5_return.mean()),
            maximum_label_day=str(own.max_label_day.max())))
        print(json.dumps(dict(stage='matched_stage_fit', price_stage=stage, rows=len(own), new_model_refit=True)), flush=True)
    manifest = dict(schema=SCHEMA, status='completed_independent_matched_generation',
        fit_started_at=started, model_available_at=clock(), model_refit=True,
        features=list(FEATURES), arms=list(ARMS), stages=list(STAGES), entries=entries,
        training_counts=counts, training_proof=proof,
        score_semantics={'binary':'hit5_probability','return':'expected_next_open_five_session_price_return'},
        old_models_modified=False, future_labels_used_for_score=False, **FLAGS)
    write(out/'manifest.json', manifest)
    return manifest


def load_generation(path):
    import lightgbm as lgb
    path = Path(path).resolve()
    folder = path.parent if path.is_file() else path
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('schema') != SCHEMA or manifest.get('features') != list(FEATURES)
            or manifest.get('arms') != list(ARMS) or manifest.get('stages') != list(STAGES)):
        raise ValueError('Explicit independent matched objective generation required')
    models = {}
    for entry in manifest['entries']:
        key = entry['arm'], entry['stage']
        if key in models or key[0] not in ARMS or key[1] not in STAGES:
            raise ValueError('Duplicate or unknown objective model identity')
        payload = folder / entry['model_file']
        preprocessing = folder / entry['preprocessing_file']
        if (payload.parent != folder or preprocessing.parent != folder
                or digest(payload) != entry['model_sha256']
                or digest(preprocessing) != entry['preprocessing_sha256']):
            raise ValueError('Frozen model payload changed or escaped its generation')
        state = json.loads(preprocessing.read_text(encoding='utf-8'))
        booster = lgb.Booster(model_file=str(payload))
        if booster.num_feature() != len(state['output_features']):
            raise ValueError('Frozen preprocessing/model dimension mismatch')
        models[key] = state, booster
    if set(models) != {(arm,s) for arm in ARMS for s in STAGES}:
        raise ValueError('All six matched objective models required')
    return manifest, models


def score_features(generation, features):
    """Frozen inference only; label-bearing frames are explicitly rejected."""
    required = {'event_id','date','symbol','stage',*FEATURES}
    if (set(features) != required or LABEL_COLUMNS & set(features)
            or features.event_id.duplicated().any() or not features.stage.isin(STAGES).all()
            or np.isinf(features.loc[:,list(FEATURES)].to_numpy(dtype=float)).any()):
        raise ValueError('Inference needs unique feature-only events; no future labels')
    _, models = generation
    frames = []
    for arm in ARMS:
        result = features.loc[:,['event_id','date','symbol','stage']].copy()
        result['model_kind'], result['model_score'] = arm, np.nan
        for stage in STAGES:
            mask = features.stage.eq(stage)
            if not mask.any():
                continue
            preprocessing, booster = models[arm,stage]
            values = features.loc[mask,list(FEATURES)].to_numpy(dtype=float)
            result.loc[mask,'model_score'] = booster.predict(_transform(values,preprocessing), num_threads=1)
        if not np.isfinite(result.model_score).all():
            raise ValueError('Frozen score incomplete; no fallback model or refit')
        frames.append(result)
    return pd.concat(frames,ignore_index=True)


def select_scores(scores, scopes, days):
    tables, decisions, plans = [], [], {arm:{} for arm in ARMS}
    for arm in ARMS:
        for day in days:
            own = scores.loc[scores.model_kind.eq(arm)&scores.date.eq(day)].sort_values('symbol',kind='stable').copy()
            own['stage_rank_pct'] = own.groupby('stage').model_score.rank(ascending=False,method='first',pct=True)
            known = scopes[day]['stage_membership_complete']
            own['rank_pass'] = own.stage_rank_pct.le(.10)&known
            own = own.sort_values(['model_score','symbol','stage'],ascending=[False,True,True],kind='stable')
            selected = own.loc[own.rank_pass].head(5)
            own['candidate_selected'] = own.event_id.isin(selected.event_id)
            tables.append(own)
            reason = ('stage_membership_incomplete_whole_day_NO_CHANGE' if not known
                else 'no_fixed_rank_candidates_NO_CHANGE' if selected.empty else 'fixed_stagewise_rank_top5')
            plan = dict(decision='FULL_TARGET' if len(selected) else 'NO_CHANGE',reason=reason,
                symbols=selected.symbol.tolist())
            plans[arm][day] = plan
            decisions.append(dict(model_kind=arm,date=day,**plan))
    return pd.concat(tables,ignore_index=True), decisions, plans
