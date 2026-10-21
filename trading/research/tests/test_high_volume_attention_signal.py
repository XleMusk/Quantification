"""Fixed own-history activity groups, without sources, labels or Engine."""
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from trading.research.high_volume_attention_signal import (
    ARMS, END, START, activity_ranks, decision_days, eligible_group, rank_group)


def test_exact50_including_current_cannot_borrow_future_or_compress_warmup():
    days = pd.bdate_range("2023-01-02", periods=70).strftime("%Y-%m-%d").tolist()
    amounts = pd.DataFrame({"A": np.arange(1., 71.)}, index=days)
    volumes = amounts*100
    selected = [days[48], days[49], days[50]]
    ranks = activity_ranks(amounts, volumes, selected)
    assert ranks.index.tolist() == selected and np.isnan(ranks.iloc[0, 0])
    assert ranks.iloc[1:, 0].tolist() == [50., 50.]
    amounts.loc[days[51]:, "A"] = -99999
    pd.testing.assert_frame_equal(ranks, activity_ranks(amounts, volumes, selected))


def test_midrank_ties_and_any_halt_or_missing_observation_stay_unknown():
    days = pd.bdate_range("2023-01-02", periods=50).strftime("%Y-%m-%d").tolist()
    amounts = pd.DataFrame(100., index=days, columns=["tied", "halted", "missing"])
    volumes = pd.DataFrame(1000., index=days, columns=amounts.columns)
    volumes.at[days[10], "halted"] = 0.
    amounts.at[days[20], "missing"] = np.nan
    ranks = activity_ranks(amounts, volumes, [days[-1]])
    assert ranks.at[days[-1], "tied"] == 25.5
    assert ranks.loc[days[-1], ["halted", "missing"]].isna().all()
    assert [eligible_group(rank) for rank in (1, 5, 5.5, 25.5, 45.5, 46, 50)] == [
        "low", "low", "normal", "normal", "normal", "high", "high"]
    assert all(eligible_group(rank) is None for rank in (None, np.nan, np.inf, 0, 51, True))


def test_all_arms_share_original_momentum_order_no_input_changes_and_unknown_never_empty_list():
    pool = [{"symbol": "B", "momentum": -.1, "activity_rank": 49.},
        {"symbol": "A", "momentum": -.1, "activity_rank": 47.},
        {"symbol": "C", "momentum": .5, "activity_rank": 25.5}]
    before = deepcopy(pool)
    assert [row["symbol"] for row in rank_group(pool, "all")] == ["C", "A", "B"]
    assert [row["symbol"] for row in rank_group(pool, "high")] == ["A", "B"]
    assert [row["symbol"] for row in rank_group(pool, "normal")] == ["C"]
    assert rank_group(pool, "low") == []
    assert all(row["score"] == row["momentum"] for row in rank_group(pool, "all"))
    assert pool == before
    pool[0]["activity_rank"] = np.nan
    for arm in ARMS:
        with pytest.raises(ValueError, match="common_pool_activity_rank_unknown"):
            rank_group(pool, arm)


def test_schedule_is_original_range_20th_then_every20_not_rolled_to_new_start():
    calendar = pd.bdate_range("2023-01-02", END).strftime("%Y-%m-%d").tolist()
    original = [day for day in calendar if START <= day <= END]
    assert decision_days(calendar) == original[19::20]
    assert decision_days(calendar)[0] == original[19]
    with pytest.raises(ValueError, match="never shift"):
        decision_days([day for day in calendar if day > START])
