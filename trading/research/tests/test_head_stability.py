import numpy as np
import pandas as pd
import pytest

from trading.research.head_stability import extract_heads, overlap, pairwise_heads, temporal_heads


def test_score_ties_use_funnel_symbol_order_and_keep_boundary_unknown_scale():
    index = pd.MultiIndex.from_product([[pd.Timestamp("2026-01-05")], [f"s{i:02}" for i in range(25)]], names=["date", "symbol"])
    score = pd.Series(1., index=index)
    heads, members, boundary = extract_heads(score, "fixed")
    assert heads[pd.Timestamp("2026-01-05")][5] == {f"s{i:02}" for i in range(5)}
    assert members.iloc[4].symbol == "s04" and len(members) == 20
    assert boundary.boundary_tied.all() and boundary.pool_ties_at_boundary_score.eq(25).all()
    assert boundary.score_gap_over_iqr.isna().all()
    score.iloc[0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        extract_heads(score, "fixed")


def test_overlap_and_temporal_transitions_are_membership_not_fills():
    assert overlap({"a", "b"}, {"b", "c"}) == {"intersection": 1, "shared_fraction": .5,
                                                   "jaccard": 1 / 3, "intersection_symbols": ["b"]}
    days = pd.date_range("2026-01-05", periods=2)
    heads = {signal: {day: {n: {f"s{i}" for i in range(offset, offset + n)} for n in (5, 10, 20)}
                      for day, offset in zip(days, (0, 1))}
             for signal in ("alpha158_seed17", "alpha158_seed29")}
    pairs = pairwise_heads(heads, days, "daily")
    assert len(pairs) == 6 and all(row["shared_fraction"] == 1 and row["seed_pair"] for row in pairs)
    temporal = temporal_heads(heads, days, "daily")
    assert len(temporal) == 6 and temporal[0]["shared_fraction"] == .8
