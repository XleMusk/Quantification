from copy import deepcopy

import pandas as pd
import pytest

from trading.research.profitability_periods import period_counts


def fixture():
    pool = pd.DataFrame([{"date": "2026-04-30", "symbol": f"{i:06d}.SSE",
                          "source_stat_date": "2026-03-31" if i < 10 else "2025-12-31",
                          "source_pub_date": "2026-04-20"} for i in range(30)])
    ranks = pool[["date", "symbol"]].assign(rank=range(1, 31))
    return pool, ranks


def test_period_mix_and_candidate_difference_are_not_holdings_or_a_rerank():
    pool, ranks = fixture()
    original_pool, original_ranks = deepcopy(pool), deepcopy(ranks)
    result = period_counts(pool, ranks).set_index("source_stat_date")
    assert result.loc["2026-03-31", "pool_count"] == 10
    assert result.loc["2026-03-31", "head20_count"] == 10
    assert result.loc["2026-03-31", "head20_minus_pool_share"] == pytest.approx(1 / 6)
    pd.testing.assert_frame_equal(pool, original_pool)
    pd.testing.assert_frame_equal(ranks, original_ranks)


@pytest.mark.parametrize("change", ["unknown", "blank", "same_day", "future_period", "outside_pool", "short_head", "duplicate"])
def test_unknown_or_misaligned_saved_inputs_do_not_become_a_comparison(change):
    pool, ranks = fixture()
    if change == "unknown":
        pool.loc[0, "source_stat_date"] = None
    elif change == "blank":
        pool.loc[0, "source_stat_date"] = ""
    elif change == "same_day":
        pool.loc[0, "source_pub_date"] = "2026-04-30"
    elif change == "future_period":
        pool.loc[0, "source_stat_date"] = "2026-06-30"
    elif change == "outside_pool":
        ranks.loc[0, "symbol"] = "999999.SSE"
    elif change == "short_head":
        ranks = ranks.iloc[1:]
    else:
        pool = pd.concat([pool, pool.iloc[:1]], ignore_index=True)
    with pytest.raises(ValueError):
        period_counts(pool, ranks)
