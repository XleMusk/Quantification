"""Leakage-safe time splits for close-based shortline labels."""

from __future__ import annotations

import pandas as pd


def closed_label_window(
    frame: pd.DataFrame,
    train_start: str,
    train_end: str,
    *,
    label_end_column: str = "run_end_date",
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Select events whose complete label ends inside a time window.

    Event date alone is insufficient for run-based continuation labels: a stage
    event near a split boundary can receive its label from later rows in the
    test period.  Missing label-end dates are excluded rather than inferred.
    """
    if label_end_column not in frame.columns:
        raise KeyError(f"Missing label-end column: {label_end_column}")
    dates = pd.to_datetime(frame["date"], errors="coerce")
    label_end = pd.to_datetime(frame[label_end_column], errors="coerce")
    start = pd.Timestamp(train_start)
    end = pd.Timestamp(train_end)
    in_window = dates.between(start, end, inclusive="both")
    label_closed = label_end.notna() & label_end.le(end)
    keep = in_window & label_closed
    stats = {
        "candidate_rows": int(in_window.sum()),
        "purged_rows": int((in_window & ~label_closed).sum()),
        "kept_rows": int(keep.sum()),
    }
    return frame.loc[keep].copy(), stats


def purge_training_labels(
    frame: pd.DataFrame,
    train_start: str,
    train_end: str,
    *,
    label_end_column: str = "run_end_date",
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Backward-compatible name for the training-window label purge."""
    return closed_label_window(
        frame, train_start, train_end, label_end_column=label_end_column
    )

