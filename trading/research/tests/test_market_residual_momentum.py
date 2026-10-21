import numpy as np
import pandas as pd
import pytest

from trading.research.market_residual_momentum import score


def fixture():
    dates = pd.bdate_range("2020-01-01", periods=530).strftime("%Y-%m-%d")
    t = np.arange(529, dtype=float)
    market_returns = .0003 + .008 * np.sin(t * .23)
    stock_returns = .0007 + 1.4 * market_returns + .004 * np.cos(t * .17)
    market = pd.Series(100 * np.r_[1., np.cumprod(1 + market_returns)], index=dates)
    stocks = pd.DataFrame({"A": 20 * np.r_[1., np.cumprod(1 + stock_returns)]}, index=dates)
    return stocks, market, str(dates[-1])


def test_exact_504_intercept_ols_and_231_formation_match_independent_calculation():
    stocks, market, day = fixture()
    result = score(stocks, market, day)
    p = stocks.A.iloc[-505:].to_numpy()
    m = market.iloc[-505:].to_numpy()
    r, rm = p[1:] / p[:-1] - 1, m[1:] / m[:-1] - 1
    alpha, beta = np.linalg.lstsq(np.c_[np.ones(504), rm], r, rcond=None)[0]
    e = r - alpha - beta * rm
    formation = r[-252:-21]
    residual = e[-252:-21]
    row = result["rows"][0]
    assert result["status"] == "complete" and len(formation) == 231
    assert result["formation_start"] == stocks.index[-252]
    assert result["formation_end"] == stocks.index[-22]
    assert row["raw"] == pytest.approx(p[-22] / p[-253] - 1)
    assert row["standardized"] == pytest.approx(formation.mean() / formation.std(ddof=1))
    assert row["residual_std"] == pytest.approx(residual.mean() / residual.std(ddof=1))
    assert row["alpha"] == pytest.approx(alpha)
    assert row["beta"] == pytest.approx(beta)


def test_future_price_changes_and_extra_rows_do_not_change_past_scores_or_inputs():
    stocks, market, _ = fixture()
    day = str(stocks.index[510])
    expected = score(stocks, market, day)
    original_stock, original_market = stocks.copy(), market.copy()
    stocks.loc[stocks.index > day, "A"] = np.nan
    market.loc[market.index > day] = -1e100
    assert score(stocks, market, day) == expected
    pd.testing.assert_frame_equal(stocks.loc[:day], original_stock.loc[:day])
    pd.testing.assert_series_equal(market.loc[:day], original_market.loc[:day])
    before_s, before_m = stocks.copy(), market.copy()
    score(stocks, market, day)
    pd.testing.assert_frame_equal(stocks, before_s)
    pd.testing.assert_series_equal(market, before_m)


def test_missing_halt_and_exact_calendar_never_fill_or_change_the_common_pool():
    stocks, market, day = fixture()
    stocks["HALT"] = stocks.A
    stocks.loc[stocks.index[-300], "HALT"] = np.nan
    result = score(stocks, market, day)
    assert result["status"] == "partial"
    assert [row["symbol"] for row in result["rows"]] == ["A"]
    assert result["unknown"] == {"HALT": "stock_504_return_history_missing_or_invalid"}
    omitted = score(stocks.drop(stocks.index[-100]), market, day)
    assert omitted["rows"] == [] and omitted["reason"] == "stock_market_exact_calendar_mismatch"
    missing_market = market.copy()
    missing_market.iloc[-100] = np.nan
    assert score(stocks, missing_market, day)["reason"] == "market_prices_missing_or_invalid"


def test_warmup_degenerate_regression_and_duplicate_dates_are_explicit():
    stocks, market, day = fixture()
    assert score(stocks.iloc[-504:], market.iloc[-504:], day)["reason"] == "market_504_return_warmup"
    flat = pd.Series(100., index=market.index)
    assert score(stocks, flat, day)["reason"] == "market_variance_degenerate"
    stocks["EXACT"] = market / 5
    result = score(stocks, market, day)
    assert result["unknown"]["EXACT"] == "formation_standard_deviation_degenerate"
    assert [row["symbol"] for row in result["rows"]] == ["A"]
    duplicate = pd.concat([stocks, stocks.iloc[[-1]]])
    with pytest.raises(ValueError, match="unique and chronological"):
        score(duplicate, market, day)
