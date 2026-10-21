import numpy as np
import pandas as pd
import pytest

from trading.research.family_ridge import (FAMILIES, TRAIN_END, family_matrix,
                                          expanding_predictions, fit_training, training_rows,
                                          validate_expanding_resume, EXPANDING_ID, EXPANDING_SIGNAL)


def inputs(end="2025-02-28"):
    calendar = pd.bdate_range("2024-01-01", end)
    months = calendar.to_series().groupby(calendar.to_period("M")).max().to_list()
    rng = np.random.default_rng(7)
    frames = {day: pd.DataFrame(rng.normal(size=(100, 3)),
                               index=pd.Index([f"symbol{i}" for i in range(100)], name="symbol"),
                               columns=list(FAMILIES)) for day in months}
    features = pd.concat(frames, names=["date", "symbol"])
    forward = pd.DataFrame(rng.normal(size=(len(calendar), 100)), index=calendar,
                           columns=frames[months[0]].index)
    return features, forward, calendar


def test_ridge_training_never_uses_later_features_or_cross_year_labels():
    features, labels, calendar = inputs()
    model, train, counts = fit_training(features, labels, calendar)
    assert counts.loc[counts.labeled_rows.gt(0), "exit_date"].max() <= pd.Timestamp(TRAIN_END)
    assert counts.loc[counts.date.eq(pd.Timestamp("2024-12-31")), "purged_cross_boundary"].item() == 100
    assert train.index.get_level_values("date").max() < pd.Timestamp(TRAIN_END)
    changed = features.copy()
    changed.loc[changed.index.get_level_values("date") > pd.Timestamp(TRAIN_END)] = 1e9
    late = labels.copy()
    late.loc[late.index > pd.Timestamp(TRAIN_END)] = -1e9
    other, other_train, _ = fit_training(changed, late, calendar)
    np.testing.assert_array_equal(model.coef_, other.coef_)
    pd.testing.assert_frame_equal(train, other_train)


def test_training_months_get_equal_weight_not_equal_number_of_stocks():
    features, labels, calendar = inputs()
    labels.loc[pd.Timestamp("2024-06-28")] = np.nan
    with pytest.raises(ValueError, match="Insufficient observed"):
        training_rows(features, labels, calendar)
    train, _ = training_rows(features, inputs()[1], calendar)
    np.testing.assert_allclose(train.sample_weight.groupby(level="date").sum(), 1)
    assert train.target_rank.groupby(level="date").mean().abs().max() < 1e-15


def test_family_ranks_preserve_equal_recipe_geometry_before_learning():
    pool = [{"symbol": str(i), "volatility": i + 1, "momentum": 3 - i,
             "median_amount": 3e7, "RSQR30": i, "WVMA20": i} for i in range(4)]
    matrix = family_matrix(pool, {"RSQR30": {"sign": 1}, "WVMA20": {"sign": -1}})
    assert matrix.columns.tolist() == list(FAMILIES)
    assert matrix.mean().abs().max() == 0
    assert matrix.trend.nunique() == 1
    assert matrix.risk.iloc[0] > matrix.risk.iloc[-1]


def test_expanding_uses_only_matured_labels_and_keeps_past_scores_fixed():
    features, labels, calendar = inputs("2025-03-31")
    scores, models, counts = expanding_predictions(features, labels, calendar)
    assert [row['date'] for row in models] == ['2025-01-31', '2025-02-28', '2025-03-31']
    assert [row['training_months'] for row in models] == [12, 12, 13]
    for row in models:
        assert pd.Timestamp(row['last_training_exit']) <= pd.Timestamp(row['training_cutoff']) < pd.Timestamp(row['date'])
    np.testing.assert_allclose(training_rows(features, labels, calendar, train_end='2025-01-30')[0]
                               .sample_weight.groupby(level='date').sum(), 1)
    cutoff = pd.Timestamp(models[0]['training_cutoff'])
    changed = labels.copy()
    for day in features.index.get_level_values('date').unique():
        location = calendar.get_loc(day) + 21
        if location >= len(calendar) or calendar[location] > cutoff:
            changed.loc[day] = 1e9
    future = features.copy()
    future.loc[future.index.get_level_values('date') > pd.Timestamp(models[0]['date'])] *= 1e6
    other_scores, other_models, _ = expanding_predictions(future, changed, calendar)
    np.testing.assert_array_equal(list(models[0]['coefficients'].values()), list(other_models[0]['coefficients'].values()))
    pd.testing.assert_frame_equal(scores.loc['2025-01-31'], other_scores.loc['2025-01-31'])
    assert (counts.loc[counts.model_date.eq(pd.Timestamp('2025-01-31')) & counts.purged_cross_boundary.gt(0), 'labeled_rows'] == 0).all()


def test_expanding_rejects_missing_current_features_and_unobserved_cutoff():
    features, labels, calendar = inputs()
    with pytest.raises(ValueError, match='evaluation'):
        expanding_predictions(features.loc[:TRAIN_END], labels, calendar)
    with pytest.raises(ValueError, match='pinned calendar'):
        training_rows(features, labels, calendar, train_end='2025-02-01')


def test_saved_expanding_rejects_refitted_contract_or_native_input():
    from copy import deepcopy
    protocol = {'id': EXPANDING_ID, 'signals': [EXPANDING_SIGNAL], 'alpha': 1.,
                'fresh_holdout_claimed': False, 'execution_admitted': False,
                'stock_inputs': {'price_view': 'prices', 'native_view': 'features', 'listing_master': 'master'}}
    previous = {'status': 'failed_expanding_family_ledger', 'purpose': EXPANDING_ID,
                'execution_admitted': False, 'models': [{'date': '2025-01-31'}],
                'results': [{'signal': EXPANDING_SIGNAL}], 'protocol': deepcopy(protocol)}
    validate_expanding_resume(previous, protocol)
    changed = deepcopy(protocol)
    changed['alpha'] = 2.
    with pytest.raises(ValueError, match='alpha'):
        validate_expanding_resume(previous, changed)
    changed = deepcopy(protocol)
    changed['stock_inputs']['native_view'] = 'new_features'
    with pytest.raises(ValueError, match='native_view'):
        validate_expanding_resume(previous, changed)
