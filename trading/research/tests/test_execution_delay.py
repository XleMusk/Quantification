import numpy as np
import pandas as pd
import pytest

from trading.research.execution_delay import delayed_outcomes, paired_daily, summarize
from trading.research.factor_families import forward_open_labels, rank_group_means
from trading.research.factors import daily_ic


def test_delays_use_full_calendar_shift_both_endpoints_and_purge_together():
    calendar = pd.bdate_range("2024-12-16", periods=12)
    opening = pd.DataFrame(np.arange(1., 25.).reshape(12, 2), index=calendar)
    outcomes, common, purged = delayed_outcomes(opening.to_numpy(), 0, 5, 11)
    for delay in (0, 1, 2):
        np.testing.assert_allclose(outcomes[:, delay], forward_open_labels(opening, 5).iloc[delay])
    assert common.all() and not purged
    opening.iloc[2, 0] = np.nan
    outcomes, common, _ = delayed_outcomes(opening.to_numpy(), 0, 5, 11)
    assert np.isnan(outcomes[0, 1]) and np.isfinite(outcomes[0, [0, 2]]).all() and not common[0]
    outcomes, common, purged = delayed_outcomes(opening.to_numpy(), 4, 5, 11)
    assert purged and np.isnan(outcomes).all() and not common.any()
    with pytest.raises(ValueError):
        delayed_outcomes(opening.to_numpy(), 0, 3, 11)


def test_paired_statistics_match_existing_ic_and_current_rank_groups():
    index = pd.MultiIndex.from_product([[pd.Timestamp("2024-01-02")], list("abcdefghij")], names=["date", "symbol"])
    features = pd.DataFrame({"f": np.arange(10.), "g": [1, 1, 2, 3, np.nan, 5, 6, 7, 8, 9]}, index=index)
    outcomes = np.column_stack([np.arange(10.) / 100, np.arange(10., 0, -1) / 100, np.arange(10.) ** 2 / 100])
    outcomes[8, 1] = np.nan
    actual = paired_daily(features, outcomes, min_pairs=5)
    common = np.isfinite(outcomes).all(axis=1)
    for delay in (0, 1, 2):
        labels = pd.Series(np.where(common, outcomes[:, delay], np.nan), index=index)
        expected_ic = daily_ic(features, labels, min_pairs=5).iloc[0]
        expected_groups = rank_group_means(features, labels, min_pairs=5).set_index("factor")
        part = actual.loc[actual.delay_sessions.eq(delay)].set_index("factor")
        np.testing.assert_allclose(part.rank_ic, expected_ic)
        np.testing.assert_allclose(part.q1_excess, expected_groups.q1_excess)
        np.testing.assert_allclose(part.q5_excess, expected_groups.q5_excess)
        assert part.pairs.tolist() == [9, 8]
    assert actual.loc[actual.delay_sessions.eq(0), "individually_observed_pairs"].tolist() == [10, 9]
    assert actual.loc[actual.delay_sessions.eq(1), "individually_observed_pairs"].tolist() == [9, 8]


def test_orientation_stays_frozen_despite_delay_training_sign_reversal():
    rows = [{"factor": "f", "horizon": 5, "segment": segment, "delay_sessions": delay,
             "rank_ic": .1 if delay == 0 else -.1, "q5_excess": .02 if delay == 0 else -.02,
             "q1_excess": -.02 if delay == 0 else .02, "pairs": 10,
             "individually_observed_pairs": 10, "missing_common_labels": 0}
            for segment in ("train", "valid", "test") for delay in (0, 1, 2)]
    definitions = pd.DataFrame([{"name": "f", "family": "f", "category": "test"}])
    directions = pd.Series(["high"], index=pd.MultiIndex.from_tuples([("f", 5)]))
    summary = summarize(pd.DataFrame(rows), definitions, directions, min_days=1)
    assert summary.train_direction.eq("high").all()
    assert summary.loc[summary.delay_sessions.eq(0), "diagnostic_positive"].all()
    assert not summary.loc[summary.delay_sessions.gt(0), "diagnostic_positive"].any()
    np.testing.assert_allclose(summary.loc[summary.delay_sessions.gt(0), "ic_change_from_delay0"], -.2)
    np.testing.assert_allclose(summary.loc[summary.delay_sessions.gt(0), "excess_change_from_delay0"], -.04)
