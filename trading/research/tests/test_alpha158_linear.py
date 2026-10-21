import numpy as np
import pandas as pd
import pytest

from trading.research.alpha158_linear import ID, SIGNALS, SEEDS, equal_seed_ranks, fit_fixed, predict_fixed, saved_score, standardized_target, validate_saved_protocol
from trading.research.experiment import export_scores


def test_allseed_ranks_require_complete_pool_and_do_not_reward_score_scale():
    index = pd.MultiIndex.from_product([pd.to_datetime(["2026-01-05", "2026-01-06"]), ["a", "b"]], names=["date", "symbol"])
    scores = {17: pd.Series([1, 2, 3, 4], index=index), 29: pd.Series([1000, 0, -5, 8], index=index),
              43: pd.Series([.1, .2, .7, .4], index=index)}
    result = equal_seed_ranks(scores)
    assert result.iloc[0] == pytest.approx(2 / 3)
    assert result.iloc[1] == pytest.approx(5 / 6)
    changed = {seed: values * (1000 if seed == 29 else 1) for seed, values in scores.items()}
    pd.testing.assert_series_equal(result, equal_seed_ranks(changed))
    changed[43] = changed[43].iloc[:-1]
    with pytest.raises(ValueError, match="identical prediction pools"):
        equal_seed_ranks(changed)
    with pytest.raises(ValueError, match="All fixed seeds"):
        equal_seed_ranks({seed: scores[seed] for seed in SEEDS[:-1]})


def test_linear_preprocessing_is_training_only_and_missing_labels_rejected():
    index = pd.MultiIndex.from_product([pd.to_datetime(["2024-01-02", "2024-01-03"]), ["a", "b", "c"]], names=["date", "symbol"])
    train = pd.DataFrame({"x": [1., 3., np.nan, 2., 4., 8.], "constant": 1.}, index=index)
    target = pd.Series([.1, .2, -.1, .3, -.4, .6], index=index)
    medians = train.median().astype("float32")
    model, scaler = fit_fixed(train, target, medians)
    expected = train.fillna(medians)
    np.testing.assert_allclose(scaler.mean_, expected.mean())
    np.testing.assert_allclose(scaler.scale_[0], expected.x.std(ddof=0))
    assert scaler.scale_[1] == 1
    before = scaler.mean_.copy()
    evaluation = train.copy() * 1e4
    prediction = predict_fixed(model, scaler, evaluation, medians)
    np.testing.assert_array_equal(scaler.mean_, before)
    assert prediction.index.equals(evaluation.index) and np.isfinite(prediction).all()
    normalized = standardized_target(target)
    np.testing.assert_allclose(normalized.groupby(level="date").mean(), 0, atol=1e-15)
    missing = target.copy()
    missing.iloc[0] = np.nan
    with pytest.raises(ValueError, match="observed training labels"):
        fit_fixed(train, missing, medians)


def test_saved_seed_availability_is_not_silently_replaced(tmp_path):
    index = pd.MultiIndex.from_product([pd.to_datetime(["2026-01-05"]), ["a", "b"]], names=["date", "symbol"])
    values = pd.Series([1., 2.], index=index, name="score")
    frame = export_scores(values)
    path = tmp_path / "scores.parquet"
    frame.to_parquet(path, index=False)
    pd.testing.assert_series_equal(saved_score(path), values)
    frame.loc[0, "available_at"] = "2026-01-06T07:05:00+00:00"
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="availability"):
        saved_score(path)


def test_saved_continuation_rejects_parameter_or_cost_changes():
    from copy import deepcopy
    source = {"portfolio_config": {"commission": .0003}, "config": {"segments": {"train": ["2024-01-01", "2024-12-31"]}}}
    previous = {"purpose": ID, "status": "partial_alpha158_linear_diagnostics", "strategy_validated": False,
                "execution_admitted": False, "results": [{"signal": signal} for signal in SIGNALS],
                "protocol": {"id": ID, "signals": list(SIGNALS), "alpha": 1., "parameters_searched": False,
                    "fresh_holdout_claimed": False, "execution_admitted": False, "portfolio": source["portfolio_config"],
                    "segments": source["config"]["segments"],
                    "signal_portfolio_configs": {signal: source["portfolio_config"] for signal in SIGNALS}}}
    validate_saved_protocol(previous, source)
    changed = deepcopy(previous)
    changed["protocol"]["alpha"] = 2.
    with pytest.raises(ValueError, match="same terminal fixed model"):
        validate_saved_protocol(changed, source)
    changed = deepcopy(previous)
    changed["protocol"]["portfolio"]["commission"] = 0.
    with pytest.raises(ValueError, match="same terminal fixed model"):
        validate_saved_protocol(changed, source)
