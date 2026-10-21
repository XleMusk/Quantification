"""Pure, fixed market-residual momentum signals from saved closing prices.

This daily 504-observation, market-only adaptation is not the paper's monthly
36-month Fama--French three-factor implementation. Callers must mask halted or
otherwise unusable stock prices to NaN before calling; prices alone cannot
identify a suspension. This module never fills prices, reads sources or trades.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


ESTIMATION_RETURNS = 504
FORMATION_RETURNS = 231
SKIP = 21
ARMS = ("raw", "standardized", "residual_std")
ZERO_STD = 1e-12  # Numerical degeneracy only; not a fitted trading threshold.


def _dates(index) -> pd.Index:
    parsed = pd.DatetimeIndex(pd.to_datetime(index, errors="raise"))
    if parsed.tz is not None or not parsed.equals(parsed.normalize()):
        raise ValueError("Expected timezone-naive exact session dates")
    return pd.Index(parsed.strftime("%Y-%m-%d"))


def _past(value, day):
    result = value.copy(deep=False)
    result.index = _dates(result.index)
    result = result.loc[result.index <= day]
    if result.index.has_duplicates or not result.index.is_monotonic_increasing:
        raise ValueError("Past session dates must be unique and chronological")
    return result


def score(stocks: pd.DataFrame, market: pd.Series, day: str) -> dict:
    """Return common-universe scores or explicit unknown reasons.

    ``stocks`` contains adjusted stock closes, one symbol per column; ``market``
    contains index closes. The last 505 exact common session prices through D
    yield 504 returns for intercept OLS. The 231 formation returns are exactly
    D-251..D-21. Raw momentum is P[D-21]/P[D-252]-1; the other two arms use the
    formation mean divided by sample standard deviation. A stock unknown for
    any arm is unknown for all arms so the caller cannot compare different pools.

    The market index supplies the exact session grid. An omitted stock date or
    an extra stock date within that window is not silently removed by an inner
    join. Market/stock rows after D never enter a numerical calculation.
    """
    if not isinstance(stocks, pd.DataFrame) or not isinstance(market, pd.Series):
        raise TypeError("Expected stock DataFrame and market Series of closes")
    day = str(_dates([day])[0])
    if stocks.columns.has_duplicates or any(not isinstance(s, str) or not s for s in stocks.columns):
        raise ValueError("Stock symbols must be distinct nonempty strings")
    stocks, market = _past(stocks, day), _past(market, day)
    result = dict(status="unavailable", date=day, window_start=None,
                  formation_start=None, formation_end=None,
                  estimation_returns=ESTIMATION_RETURNS,
                  formation_returns=FORMATION_RETURNS, rows=[], unknown={}, reason=None)

    def unavailable(reason):
        result["reason"] = reason
        result["unknown"] = {symbol: reason for symbol in stocks.columns}
        return result

    if day not in stocks.index or day not in market.index:
        return unavailable("exact_formation_day_missing")
    if len(market) < ESTIMATION_RETURNS + 1:
        return unavailable("market_504_return_warmup")
    market = market.iloc[-(ESTIMATION_RETURNS + 1):]
    expected = market.index
    stocks = stocks.loc[(stocks.index >= expected[0]) & (stocks.index <= day)]
    result.update(window_start=str(expected[0]),
                  formation_start=str(expected[-252]), formation_end=str(expected[-22]))
    if not stocks.index.equals(expected):
        return unavailable("stock_market_exact_calendar_mismatch")
    try:
        market_prices = market.to_numpy(dtype=float)
    except (ValueError, TypeError):
        return unavailable("market_prices_nonnumeric")
    if not (np.isfinite(market_prices).all() and (market_prices > 0).all()):
        return unavailable("market_prices_missing_or_invalid")
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        market_returns = market_prices[1:] / market_prices[:-1] - 1
    if not np.isfinite(market_returns).all():
        return unavailable("market_returns_invalid")
    centered_market = market_returns - market_returns.mean()
    market_ss = float(centered_market @ centered_market)
    if not np.isfinite(market_ss) or market_ss <= ZERO_STD ** 2 * (ESTIMATION_RETURNS - 1):
        return unavailable("market_variance_degenerate")
    formation = slice(-252, -21)
    # No cross-stock operation affects a score; per-symbol missingness is explicit.
    for symbol in stocks.columns:
        try:
            prices = stocks[symbol].to_numpy(dtype=float)
        except (ValueError, TypeError):
            result["unknown"][symbol] = "stock_prices_nonnumeric"
            continue
        if not (np.isfinite(prices).all() and (prices > 0).all()):
            result["unknown"][symbol] = "stock_504_return_history_missing_or_invalid"
            continue
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            returns = prices[1:] / prices[:-1] - 1
            raw = prices[-22] / prices[-253] - 1
        if not np.isfinite(returns).all() or not np.isfinite(raw):
            result["unknown"][symbol] = "stock_returns_invalid"
            continue
        # Closed-form OLS with an intercept, equivalent to lstsq([1, market], y).
        beta = float(centered_market @ (returns - returns.mean()) / market_ss)
        alpha = float(returns.mean() - beta * market_returns.mean())
        residuals = returns - alpha - beta * market_returns
        raw_formation, residual_formation = returns[formation], residuals[formation]
        raw_std = float(raw_formation.std(ddof=1))
        residual_std = float(residual_formation.std(ddof=1))
        if (not np.isfinite([alpha, beta, raw_std, residual_std]).all()
                or raw_std <= ZERO_STD or residual_std <= ZERO_STD):
            result["unknown"][symbol] = "formation_standard_deviation_degenerate"
            continue
        row = dict(symbol=symbol, raw=float(raw),
                   standardized=float(raw_formation.mean() / raw_std),
                   residual_std=float(residual_formation.mean() / residual_std),
                   alpha=alpha, beta=beta)
        if not np.isfinite([row[key] for key in (*ARMS, "alpha", "beta")]).all():
            result["unknown"][symbol] = "score_nonfinite"
            continue
        result["rows"].append(row)
    result["status"] = "partial" if result["unknown"] else "complete"
    return result
