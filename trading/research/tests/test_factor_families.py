import numpy as np
import pandas as pd
import pytest

from trading.research.factor_families import forward_open_labels, rank_group_means, redundancy, summarize_decay


def test_labels_use_full_calendar_next_open_and_keep_missing_endpoint():
    dates = pd.bdate_range("2024-01-02", periods=9)
    opening = pd.DataFrame({"A": np.arange(10., 19.)}, index=dates)
    result = forward_open_labels(opening, 5)
    assert result.iloc[0, 0] == pytest.approx(16 / 11 - 1)
    opening.iloc[6, 0] = np.nan
    assert pd.isna(forward_open_labels(opening, 5).iloc[0, 0])
    opening.iloc[1, 0] = 0
    assert pd.isna(forward_open_labels(opening, 1).iloc[0, 0])


def test_group_membership_is_not_changed_by_missing_future_labels():
    date = pd.Timestamp("2024-01-02")
    index = pd.MultiIndex.from_product([[date], list("abcdefghij")], names=["date", "symbol"])
    features = pd.DataFrame({"F": np.arange(10.)}, index=index)
    labels = pd.Series(np.arange(10.), index=index)
    labels.iloc[9] = np.nan
    row = rank_group_means(features, labels, min_pairs=5).iloc[0]
    assert row.pairs == 9
    assert row.missing_labels == 1
    assert row.q5_excess == 8 - 4


def test_direction_remains_training_direction_when_later_sign_flips():
    definitions = pd.DataFrame([{"name": "F", "family": "trend", "category": "price"}])
    daily = pd.DataFrame([{"factor": "F", "horizon": 5, "segment": segment,
                           "rank_ic": ic, "q1_excess": -ic, "q5_excess": ic,
                           "pairs": 100, "missing_labels": 0}
                          for segment, ic in (("train", -.1), ("valid", .2), ("test", -.3))])
    result = summarize_decay(daily, definitions, min_days=1).set_index("segment")
    assert result.train_direction.eq("low").all()
    assert result.loc["valid", "oriented_mean_rank_ic"] == -.2
    assert not result.loc["valid", "diagnostic_positive"]
    assert result.loc["test", "diagnostic_positive"]
    assert result.net_incremental_value.isna().all()
    assert not result.account_ready.any()


def test_training_redundancy_preserves_sign_mirrors_and_all_definitions():
    dates = pd.bdate_range("2024-01-02", periods=3)
    index = pd.MultiIndex.from_product([dates, list("abcdef")], names=["date", "symbol"])
    x = np.tile(np.arange(6.), 3)
    features = pd.DataFrame({"F": x, "MIRROR": -x, "CONSTANT": 1.}, index=index)
    definitions = pd.DataFrame({"name": features.columns})
    profiles, edges, anchors = redundancy(features, dates, definitions, min_pairs=5)
    assert len(profiles) == 3 and len(anchors) == 3
    assert len(edges) == 1
    assert edges.iloc[0].mean_rank_correlation == pytest.approx(-1)
    assert profiles.set_index("name").loc["CONSTANT", "cluster_size"] == 1
