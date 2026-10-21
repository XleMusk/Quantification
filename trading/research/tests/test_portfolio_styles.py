import numpy as np
import pandas as pd
import pytest

from trading.research.factors import LABEL
from trading.research.portfolio_styles import basket_observation, holding_basket, industry_groups, join_styles, summarize_dates
from trading.research.style_exposure import style_day


def test_styles_do_not_backfill_industries_or_rank_outside_pool():
    day = "2026-03-31"
    styles, _ = style_day(pd.DataFrame({"date": [day], "symbol": ["600000.SSE"],
        "close": [10], "volume": [1000], "turn": [1], "peTTM": [5], "pbMRQ": [1], LABEL: [np.nan]}))
    basket = pd.DataFrame({"date": [day, day], "symbol": ["600000.SSE", "600001.SSE"], "weight": [.4, .4]})
    industries = pd.DataFrame({"date": ["2026-04-01", day], "symbol": ["600000.SSE", "600001.SSE"],
        "industry": ["future", "exact"], "updateDate": ["2026-03-30", "2026-03-30"],
        "industryClassification": ["source", "source"]})
    joined = join_styles(basket, styles, industries)
    assert list(joined.industry) == ["", "exact"]
    assert list(joined.size_bucket) == ["Q3", "unknown"]
    observed = basket_observation(joined, day)
    assert observed["pool_weight_coverage"] == .5
    assert observed["size_proxy_percentile_coverage"] == .5
    assert observed["industry_coverage"] == .5
    assert observed["size_unknown_fraction"] == .5
    assert not any("label" in key for key in observed)
    groups = industry_groups(joined).set_index("industry")
    assert groups.loc["unknown", "stock_basket_fraction"] == .5
    assert groups.loc["exact", "equity_weight"] == .4


def test_held_marks_exclude_receivables_and_keep_stale_values():
    shares = pd.DataFrame({"date": ["2026-03-31"], "symbol": ["600000.SSE"],
                           "quantity": [100], "stale_valuation": [True]})
    raw = shares[["date", "symbol"]].assign(close=10.)
    mark = {"date": "2026-03-31", "equity": 2000, "cash": 900, "receivables": 100}
    held, unknown = holding_basket(shares, raw, mark)
    assert unknown == 0
    assert held.marked_value_cny.tolist() == [1000]
    assert held.weight.tolist() == [.5]
    assert held.stale_valuation.tolist() == [True]
    with pytest.raises(ValueError, match="disagree"):
        holding_basket(shares, raw.assign(close=11), mark)


def test_unknown_marks_never_produce_partial_exposure_or_zero_industry_risk():
    day = "2026-03-31"
    shares = pd.DataFrame({"date": [day, day], "symbol": ["600000.SSE", "600001.SSE"], "quantity": [100, 100]})
    raw = shares[["date", "symbol"]].assign(close=[10., np.nan])
    held, unknown = holding_basket(shares, raw, {"date": day, "equity": 3000, "cash": 900, "receivables": 100})
    assert unknown == 1
    observed = basket_observation(held, day, unknown)
    assert observed["gross_target"] is None
    assert observed["industry_coverage"] is None
    summary = summarize_dates(pd.DataFrame([observed]))
    assert summary["nonempty_known_anchors"] == 0
    assert summary["unknown_valuation_anchors"] == 1
    assert summary["full_anchor_mean_largest_industry_fraction"] is None
    empty = basket_observation(held.iloc[:0], day)
    assert empty["gross_target"] == 0
    assert empty["industry_coverage"] is None
