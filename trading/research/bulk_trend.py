"""Existing Donchian rule on all pinned source-covered stock observations."""
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from ..contracts import SignalFrame
from .bulk_feed import numeric
from .seat_trend import TrendFeed, donchian_matrices


def trend_prices(feed):
    shape = (len(feed.calendar), len(feed.symbols))
    arrays = {field: np.full(shape, np.nan) for field in ("high", "low", "close")}
    codes = pd.Index(feed.codes)
    with pq.ParquetFile(feed.folder / "canonical_daily.parquet") as source:
        if source.num_row_groups != len(feed.calendar):
            raise ValueError("Trend price row groups do not match the pinned calendar")
        for i, day in enumerate(feed.calendar):
            frame = source.read_row_group(i, columns=["date", "code", "adjustflag", "high", "low",
                                                       "close", "volume", "tradestatus"]).to_pandas()
            members = codes.get_indexer(frame.code)
            if (not frame.date.eq(day).all() or frame.code.duplicated().any()
                    or not np.array_equal(members, feed.day_members[i])
                    or not frame.adjustflag.eq("2").all()):
                raise ValueError(f"Trend research identities/price basis changed: {day}")
            for field in ("high", "low", "close", "volume", "tradestatus"):
                frame[field] = numeric(frame[field])
            if (not frame.tradestatus.isin([0, 1]).all()
                    or not np.allclose(frame.close, feed.closes.iloc[i, members], rtol=0, atol=0, equal_nan=True)
                    or not np.allclose(frame.volume, feed.volumes.iloc[i, members], rtol=0, atol=0, equal_nan=True)):
                raise ValueError(f"Trend quotes/activity do not match the pinned feed: {day}")
            live = frame.tradestatus.eq(1) & frame.volume.gt(0)
            quotes = frame.loc[live, ["high", "low", "close"]]
            if (not np.isfinite(quotes).all().all() or quotes.le(0).any().any()
                    or quotes.high.lt(quotes.close).any() or quotes.low.gt(quotes.close).any()):
                raise ValueError(f"Invalid adjusted trend quotes: {day}")
            # Preserve the original rule's missing-bar/ATR behavior; no halt filling.
            for field in arrays:
                arrays[field][i, members[live]] = frame.loc[live, field]
            if (i + 1) % 200 == 0:
                print(f"Donchian price inputs: {i + 1}/{len(feed.calendar)} sessions", flush=True)
    return {field: pd.DataFrame(values, index=feed.calendar, columns=feed.symbols)
            for field, values in arrays.items()}


class BulkTrendFeed(TrendFeed):
    def __init__(self, base):
        self.base = base
        prices = trend_prices(base)
        self.trend = donchian_matrices(prices["high"], prices["low"], prices["close"])
        self.liquidity = base.amounts.rolling(20, min_periods=20).median().to_numpy()
        self.ipo = pd.to_datetime([base.instruments[s].get("ipoDate") or None for s in base.symbols])
        self.retired = pd.to_datetime([base.instruments[s].get("outDate") or None for s in base.symbols])
        self.supported = np.array([s not in base.unsupported_instruments for s in base.symbols])
        self.audit = {}
        self.data_checks = {"source_symbols": len(base.symbols), "supported_symbols": int(self.supported.sum()),
                            "calendar_sessions": len(base.calendar),
                            "rule": "Existing prior-high20/prior-low10, Wilder ATR20 ranking, daily targets, retain_rank100",
                            "missing_bars": "Masked, not forward-filled; existing TA-Lib interior-NaN propagation retained",
                            "native_alpha158_recomputed": False, "parameters_tuned": False}

    def scores(self, day, config):
        i = self.base.day_index[day]
        stamp = pd.Timestamp(day)
        active = self.trend["trend_active"][i]
        finite = np.isfinite(self.trend["score"][i])
        pool = (self.supported & active & finite & (self.liquidity[i] >= config.min_amount)
                & ((stamp - self.ipo).days >= config.min_listing_days)
                & (self.retired.isna() | (stamp < self.retired))
                & (day > self.base.manifest["selection_date"]))
        rows = self.base.rows.get(day, {})
        candidates = []
        for index in np.flatnonzero(pool):
            symbol = self.base.symbols[index]
            row = rows.get(symbol)
            if row is None or row["tradestatus"] != 1 or row["isST"] != 0 or row["volume"] <= 0:
                continue
            candidates.append({"symbol": symbol,
                               **{field: float(self.trend[field][i, index])
                                  for field in ("score", "entry_channel", "exit_channel", "atr")},
                               "median_amount": float(self.liquidity[i, index])})
        self.audit[day] = {"date": day, "source_symbols": len(self.base.symbols),
                           "supported_symbols": int(self.supported.sum()),
                           "active_supported": int((active & self.supported).sum()),
                           "active_without_finite_score": int((active & self.supported & ~finite).sum()),
                           "candidates": len(candidates)}
        return sorted(candidates, key=lambda row: (-row["score"], row["symbol"]))

    def signal_frame(self, day, config, context):
        return SignalFrame.from_legacy(self.scores(day, config), day, "rule:donchian_trend20_10_atr20", context)
