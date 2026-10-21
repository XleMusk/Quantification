"""Qlib/model scores enter before portfolio construction, never at the order API."""
import pandas as pd
import numpy as np

from .contracts import RunContext, SignalFrame, TradingMode
from .symbols import normalize_symbol


def frame_from_qlib(frame, signal_day, context=None):
    required = {"datetime", "instrument", "score", "available_at"}
    if not required.issubset(frame.columns):
        raise ValueError("Model scores need datetime, instrument, score, available_at")
    rows = frame.loc[pd.to_datetime(frame.datetime).dt.strftime("%Y-%m-%d").eq(signal_day)].copy()
    cutoff = pd.Timestamp(signal_day + " 15:05:00", tz="Asia/Shanghai")
    for value in rows.available_at:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("Scores need explicit offset-aware availability timestamps")
    availability = pd.to_datetime(rows.available_at, utc=True, format="mixed")
    if availability.isna().any() or (availability > cutoff).any():
        raise ValueError("Scores were unavailable at the decision time")
    if rows.instrument.duplicated().any() or not np.isfinite(pd.to_numeric(rows.score, errors="coerce")).all():
        raise ValueError("Invalid model score batch")
    rows["score"] = pd.to_numeric(rows.score)
    rows["available_at"] = availability
    rows = rows.sort_values(["score", "instrument"], ascending=[False, True])
    candidates = [{"symbol": normalize_symbol(row.instrument).vt_symbol, "score": float(row.score)}
                  for row in rows.itertuples()]
    if len({row["symbol"] for row in candidates}) != len(candidates):
        raise ValueError("Duplicate normalized instruments")
    context = context if context is not None else RunContext("standalone_replay", TradingMode.REPLAY)
    return SignalFrame.from_legacy(candidates, signal_day, "model_scores", context,
                                   available_at=[value.isoformat() for value in rows.available_at])


def from_qlib(frame, signal_day):
    """Compatibility output for callers that still consume ranked dictionaries."""
    return frame_from_qlib(frame, signal_day).ranked_candidates()
