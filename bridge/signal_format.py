"""Signal file format: Parquet schema + read/write utilities.

Columns:
    datetime    date     - trading date
    vt_symbol   string   - vnpy code (e.g. "600000.SSE")
    qlib_code   string   - Qlib code (e.g. "SH600000")
    score       float64  - model prediction score (higher = more bullish)
    direction   int8     - 1=BUY, -1=SELL, 0=HOLD
    weight      float64  - position weight (0.0 ~ 1.0), NaN if unused
"""

from pathlib import Path
from datetime import date
from typing import Optional, Dict, Any

import pandas as pd
import numpy as np

# Expected dtypes for validation
SIGNAL_DTYPES = {
    "datetime": "datetime64[ns]",
    "vt_symbol": "string",
    "qlib_code": "string",
    "score": "float64",
    "direction": "int8",
    "weight": "float64",
}


def validate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure required columns and types."""
    required = list(SIGNAL_DTYPES.keys())
    missing = set(required) - set(df.columns)
    if missing:
        raise ValueError(f"Signal file missing columns: {missing}")
    return df[required]


def write_signals(df: pd.DataFrame, path: Path) -> None:
    """Validate and write signals to Parquet."""
    df = validate_signals(df)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def read_signals(path: Path) -> pd.DataFrame:
    """Read signal file."""
    df = pd.read_parquet(path)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.date
    return df


def get_signal_for_symbol(
    path: Path,
    vt_symbol: str,
    target_date: date,
) -> Optional[Dict[str, Any]]:
    """Look up signal for a specific symbol on a specific date.
    Returns None if no signal found. Cache-friendly: caller
    should pre-load the DataFrame for repeated lookups.

    Args:
        path: Path to signal Parquet file
        vt_symbol: vnpy symbol like "600000.SSE"
        target_date: trading date to look up

    Returns:
        dict with keys {score, direction, weight} or None
    """
    df = read_signals(path)
    row = df[(df["vt_symbol"] == vt_symbol) & (df["datetime"] == target_date)]
    if row.empty:
        return None
    r = row.iloc[0]
    return {
        "score": float(r["score"]),
        "direction": int(r["direction"]),
        "weight": float(r["weight"]) if not np.isnan(r["weight"]) else None,
    }


def read_signals_for_date(path: Path, target_date: date) -> pd.DataFrame:
    """Read signals valid for a specific trading date."""
    df = read_signals(path)
    return df[df["datetime"] == target_date]
