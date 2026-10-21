import numpy as np
import pandas as pd
import pytest

from trading.research.factors import LABEL
from trading.research.style_exposure import first_entry_pnl, normalize_industry, peer_controls, style_day


def test_style_membership_ignores_labels_and_keeps_invalid_turn_and_pe_unknown():
    rows = pd.DataFrame({"symbol": list("abcde"), "close": [10.] * 5, "volume": [100., 200., 300., 400., 500.],
                         "turn": [1., 1., 1., 1., 0.], "peTTM": [-5., 0., 5., 10., ""],
                         "pbMRQ": [1., 2., 3., 4., ""], LABEL: [.1, np.nan, .2, .3, .4]})
    styled, groups = style_day(rows)
    assert styled.loc[4, "size_bucket"] == "unknown" and pd.isna(styled.loc[4, "size_proxy_percentile"])
    assert styled.loc[:1, "positive_pe_percentile"].isna().all()
    assert styled.loc[:1, "nonpositive_pe"].eq(1).all() and pd.isna(styled.loc[4, "nonpositive_pe"])
    assert groups.observed.sum() == 4
    changed = rows.copy()
    changed[LABEL] = [-1., 9., np.nan, -5., 7.]
    changed.turn = changed.turn * 100
    other, _ = style_day(changed)
    pd.testing.assert_series_equal(styled.size_bucket, other.size_bucket)
    pd.testing.assert_series_equal(styled.size_proxy_percentile, other.size_proxy_percentile)


def test_industry_rejects_future_update_and_duplicate_security():
    rows = pd.DataFrame({"updateDate": ["2026-01-02"], "code": ["sh.600000"], "industry": ["bank"], "industryClassification": ["SW"]})
    assert normalize_industry(rows, "2026-01-05").iloc[0].symbol == "600000.SSE"
    with pytest.raises(ValueError, match="later than"):
        normalize_industry(rows, "2025-12-31")
    with pytest.raises(ValueError, match="Distinct"):
        normalize_industry(pd.concat([rows, rows]), "2026-01-05")


def test_first_entry_assignment_conserves_pnl_once_and_preserves_unknowns():
    pnl = pd.DataFrame({"symbol": ["a", "b", "c"], "observed_pnl_cny": [-10., 3., -2.],
                        "buy_notional_cny": [100., 200., 50.], "fees_cny": [1., 2., 1.]})
    buys = pd.DataFrame({"symbol": ["a", "a", "b"], "signal_date": ["2026-02-01", "2026-01-01", "2026-01-01"],
                         "order_id": ["3", "1", "2"], "size_bucket": ["Q5", "Q1", "Q3"], "industry": ["later", "first", ""]})
    classified, groups = first_entry_pnl(pnl, buys)
    assert classified.loc[classified.symbol.eq("a"), "industry"].iloc[0] == "first"
    assert classified.loc[classified.symbol.eq("c"), "size_bucket"].iloc[0] == "unknown"
    assert groups.groupby("classification").observed_pnl_cny.sum().eq(-9.).all()


def test_peers_exclude_all_targets_and_keep_empty_peer_cells_unknown():
    pool = pd.DataFrame({"symbol": ["a", "b", "c", "d"], "industry": ["x", "x", "x", "y"],
                         "size_bucket": ["Q1", "Q1", "Q2", "Q3"], LABEL: [9., 3., .1, .2]})
    selected = pool.loc[[0, 1, 3]].copy()
    values = peer_controls(selected, pool)
    assert values.loc[values.symbol.isin(["a", "b"]), "industry_peer_peer_label"].eq(.1).all()
    assert values.industry_size_peer_difference.isna().all()
    assert pd.isna(values.loc[values.symbol.eq("d"), "industry_peer_difference"].iloc[0])
