import numpy as np
import pandas as pd
import pytest

from trading.research.profitability_portfolios import profitability_pool, same_period_pool, ranked_profitability, rank_z, CASES


def test_known_financial_pool_and_rank_prior():
    pool = pd.DataFrame({"symbol": ["a", "b", "c", "d"], "volatility": [1., 2., 3., 4.], "momentum": [0., .1, .2, .3]})
    reports = pd.DataFrame({"symbol": ["a", "b", "c", "d"], "source_pub_date": ["2025-01-30"] * 4,
        "source_stat_date": ["2024-12-31"] * 4, "roeAvg_provider_value": [-.1, 0., .2, np.nan],
        "gpMargin_provider_value": [.1, .2, .4, .3]})
    current, audit = profitability_pool(pool, reports, "2025-01-31")
    assert current.index.tolist() == ["a", "b", "c"]
    assert audit["common_financial_pool"] == 3
    assert current.loc["a", "profitability"] < current.loc["b", "profitability"] < current.loc["c", "profitability"]
    assert len(ranked_profitability(current, CASES[0])) == len(ranked_profitability(current, CASES[2])) == 3
    other, _ = profitability_pool(pool.iloc[::-1], reports.iloc[::-1].assign(roeAvg_provider_value=reports.iloc[::-1].roeAvg_provider_value * 100), "2025-01-31")
    other["future_return"] = [-100, 100, -100]
    for case in CASES:
        assert [(r["symbol"], r["score"]) for r in ranked_profitability(current, case)] == [
            (r["symbol"], r["score"]) for r in ranked_profitability(other, case)]
    reports.loc[0, "source_pub_date"] = "2025-01-31"
    with pytest.raises(ValueError, match="Same-day/future"):
        profitability_pool(pool, reports, "2025-01-31")


def test_constant_observed_rank_is_not_missing_imputation():
    assert rank_z(pd.Series([0., 0., 0.])).tolist() == [0., 0., 0.]
    with pytest.raises(ValueError, match="missing is not zero"):
        rank_z(pd.Series([1., np.nan]))


def test_same_period_latest_sufficient_cohort_and_recomputed_ranks():
    pool = pd.DataFrame({"symbol": list("abcdefg"), "volatility": [1.] * 7, "momentum": [0.] * 7,
        "source_pub_date": ["2025-04-01"] * 7,
        "source_stat_date": ["2024-09-30"] * 3 + ["2024-12-31"] * 3 + ["2025-03-31"],
        "roeAvg_provider_value": [10., 20., 30., 1., 2., 3., 100.],
        "gpMargin_provider_value": [10., 20., 30., 1., 2., 3., 100.], "profitability": [999.] * 7})
    current, audit = same_period_pool(pool, "2025-04-30", 3, 70)
    assert current.index.tolist() == list("def")
    assert audit["selected_stat_date"] == "2024-12-31" and audit["excluded_period_members"] == 4
    assert audit["coverage"] == 3 / 70
    assert current.profitability.tolist() == pytest.approx([-np.sqrt(1.5), 0., np.sqrt(1.5)])
    other, other_audit = same_period_pool(pool.iloc[::-1].assign(future_return=100), "2025-04-30", 3, 70)
    pd.testing.assert_frame_equal(current, other.drop(columns="future_return"))
    assert other_audit == audit and pool.profitability.eq(999).all()
    with pytest.raises(ValueError, match="No report-period"):
        same_period_pool(pool, "2025-04-30", 4, 70)
    for field, value in [("source_pub_date", "2025-04-30"), ("source_stat_date", "")]:
        invalid = pool.copy()
        invalid.loc[0, field] = value
        with pytest.raises(ValueError, match="Missing period or same-day/future"):
            same_period_pool(invalid, "2025-04-30", 3, 70)
