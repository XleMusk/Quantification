import numpy as np
import pandas as pd
import pytest

from trading.research.financials import VALUES
from trading.research.profitability_inputs import (bind_inputs, margin_evidence, rank_profiles, specifications,
                                                  bind_valuation, qarp_ranks, QARP_CASES)
from trading.research.profitability_portfolios import CASES, rank_z, ranked_profitability


def inputs():
    reports = pd.DataFrame({"symbol": list("abc"), "pubDate": ["2025-04-01"] * 3,
        "statDate": ["2025-03-31"] * 3, **{field: [1., 2., 3.] for field in VALUES}})
    reports["roeAvg"] = [-.1, .2, .3]
    reports["gpMargin"] = [.9, .2, .4]
    reports["netProfit"] = [-10., 20., np.nan]
    pool = pd.DataFrame({"symbol": list("abc"), "date": ["2025-05-30"] * 3,
        "source_pub_date": reports.pubDate, "source_stat_date": reports.statDate,
        "roeAvg_provider_value": reports.roeAvg, "gpMargin_provider_value": reports.gpMargin,
        "volatility": [3., 2., 1.], "momentum": [-.1, .2, .3]})
    pool["profitability"] = (rank_z(pool.roeAvg_provider_value) + rank_z(pool.gpMargin_provider_value)) / 2
    return pool, reports


def test_binds_exact_quarter_without_imputing_or_changing_proxies():
    pool, reports = inputs()
    actual = bind_inputs(pool, reports)
    assert actual.symbol.tolist() == list("abc")
    assert np.isnan(actual.loc[2, "netProfit_provider_value"])
    pd.testing.assert_series_equal(actual.profitability, pool.profitability)
    changed = reports.copy()
    changed.loc[0, "roeAvg"] *= 100
    with pytest.raises(ValueError, match="proxy changed"):
        bind_inputs(pool, changed)
    pool.loc[0, "source_pub_date"] = pool.loc[0, "date"]
    with pytest.raises(ValueError, match="strictly earlier"):
        bind_inputs(pool, reports)


def test_ratio_percent_ambiguous_unknown_and_nonmatching_are_distinct():
    frame = pd.DataFrame({"symbol": list("abcdef"), "pubDate": ["2025-04-01"] * 6,
        "statDate": ["2025-03-31"] * 6, "npMargin": [.2, 20., 0., .2, .8, .2],
        "netProfit": [20., 20., 0., 20., 20., 20.], "MBRevenue": [100., 100., 100., np.nan, 100., 0.]})
    result = margin_evidence(frame)
    assert result.arithmetic_status.tolist() == ["ratio_supported", "percent_supported", "ambiguous_both",
        "unknown_inputs_or_nonpositive_revenue", "neither_supported", "unknown_inputs_or_nonpositive_revenue"]
    assert result.loc[[3, 5], "net_profit_over_main_revenue"].isna().all()
    assert frame.loc[1, "npMargin"] == 20.


def test_profiles_consume_existing_rank_buffers_not_future_outcomes():
    pool, reports = inputs()
    features = bind_inputs(pool, reports)
    ranks = {case: pd.DataFrame(ranked_profitability(features.set_index("symbol"), case)[:2]).assign(
        rank=range(1, 3)) for case in CASES}
    profiles, candidates = rank_profiles(features, ranks, 1, 2)
    common = profiles.loc[profiles.group.eq("common_pool")].iloc[0]
    assert common.roeAvg_negative == 1 and common.netProfit_negative == 1 and common.netProfit_observed == 2
    assert len(candidates) == 8
    changed = features.iloc[::-1].assign(future_return=999)
    other, _ = rank_profiles(changed, ranks, 1, 2)
    pd.testing.assert_frame_equal(profiles, other)
    ranks[CASES[0]].loc[0, "symbol"] = "z"
    with pytest.raises(ValueError, match="ordering"):
        rank_profiles(features, ranks, 1, 2)


def test_missing_economic_measures_do_not_become_zero_or_proxy_reproductions():
    result = specifications()
    assert len(result["public_profitability_measures"]) == 6
    assert {r["status"] for r in result["public_profitability_measures"].values()} == {"blocked_exact_reproduction"}
    assert result["existing_proxy"]["annual_accounting_equivalence"] is False
    assert result["existing_proxy"]["units_converted"] is False


def test_valuation_binding_keeps_same_members_and_rejects_unavailable_or_changed_inputs():
    pool, reports = inputs()
    features = bind_inputs(pool, reports)
    values = pool[["date", "symbol", "momentum", "volatility"]].copy()
    values["peTTM"], values["pbMRQ"] = [-10., 20., 30.], [1., 2., 3.]
    values["earnings_yield"], values["book_yield"] = 1 / values.peTTM, 1 / values.pbMRQ
    joined = bind_valuation(features, values)
    assert joined.symbol.tolist() == features.symbol.tolist() and joined.loc[0, "earnings_yield"] == -.1
    assert joined.loc[0, "book_yield_rank_percentile"] == 1.
    with pytest.raises(ValueError, match="no imputation"):
        bind_valuation(features, values.iloc[:2])
    values.loc[0, "volatility"] += 1
    with pytest.raises(ValueError, match="price features differ"):
        bind_valuation(features, values)


def test_fixed_qarp_valuation_formula_ignores_future_returns_and_no_new_pool_filter():
    pool, reports = inputs()
    features = bind_inputs(pool, reports)
    values = pool[["date", "symbol", "momentum", "volatility"]].copy()
    values["peTTM"], values["pbMRQ"] = [-10., 20., 30.], [1., 2., 3.]
    values["earnings_yield"], values["book_yield"] = 1 / values.peTTM, 1 / values.pbMRQ
    joined = bind_valuation(features, values)
    assert np.allclose(joined.qarp_score, (joined.profitability + joined.value_score) / 2)
    for case in QARP_CASES:
        first = qarp_ranks(joined, case, 3)
        other = qarp_ranks(joined.iloc[::-1].assign(future_return=100), case, 3)
        assert first.symbol.tolist() == other.symbol.tolist()
        assert first.score.tolist() == other.score.tolist() and len(first) == len(joined)
