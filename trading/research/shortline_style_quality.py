"""Validate shortline signal breadth against full-market signal-date counts."""

from pathlib import Path

import numpy as np
import pandas as pd


MARKET_COUNT_COLUMNS = ["market_row_count", "market_up_count", "market_down_count"]


def reject_known_legacy_sidecar(source: Path) -> None:
    if Path(source).name.lower() == "emotion_event_features_v1.csv":
        raise ValueError(
            "Known contaminated emotion_event_features_v1.csv is retired: its "
            "pre_close-derived returns/breadth disagree with the corrected "
            "physical-price panel. Provide an explicit derived sidecar; its "
            "breadth will also be checked against full-market counts."
        )


def validate_sidecar_breadth(sidecar: pd.DataFrame, reference: pd.DataFrame, source: Path) -> dict:
    """Check available daily breadth without labels, fitting or trading admission.

    Counts must be carried by the corrected full-market annotation, not inferred
    by counting selected events. Sidecar gaps remain explicit to the caller.
    """
    reject_known_legacy_sidecar(source)
    missing = sorted(set(MARKET_COUNT_COLUMNS + ["date"]) - set(reference.columns))
    if missing:
        raise ValueError(f"Corrected panel lacks full-market breadth evidence: {missing}")
    if not {"date", "market_breadth"} <= set(sidecar.columns):
        raise ValueError("Sidecar lacks date/market_breadth")
    ref = reference[["date", *MARKET_COUNT_COLUMNS]].copy()
    ref["date"] = pd.to_datetime(ref["date"], errors="raise").dt.strftime("%Y-%m-%d")
    ref = ref.drop_duplicates()
    if ref["date"].duplicated().any():
        raise ValueError("Corrected panel has inconsistent full-market counts for a date")
    counts = ref[MARKET_COUNT_COLUMNS].apply(pd.to_numeric, errors="raise")
    if (not np.isfinite(counts.to_numpy()).all() or counts.lt(0).any().any()
            or counts.mod(1).ne(0).any().any() or counts["market_row_count"].le(0).any()
            or (counts["market_up_count"] + counts["market_down_count"]).gt(counts["market_row_count"]).any()):
        raise ValueError("Invalid full-market counts in corrected panel")
    ref["expected_breadth"] = (counts["market_up_count"] - counts["market_down_count"]) / counts["market_row_count"]
    daily = sidecar[["date", "market_breadth"]].copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="raise").dt.strftime("%Y-%m-%d")
    daily["market_breadth"] = pd.to_numeric(daily["market_breadth"], errors="coerce")
    daily = daily.drop_duplicates()
    if daily["date"].duplicated().any():
        raise ValueError("Style sidecar has inconsistent market_breadth for a date")
    observed = daily.loc[daily["market_breadth"].notna()].copy()
    if not np.isfinite(observed["market_breadth"]).all() or observed["market_breadth"].abs().gt(1).any():
        raise ValueError("Style sidecar market_breadth must be finite and within [-1,1]")
    joined = observed.merge(ref[["date", "expected_breadth"]], on="date", validate="one_to_one")
    if joined.empty:
        raise ValueError("No sidecar dates have corrected full-market breadth evidence")
    delta = (joined["market_breadth"] - joined["expected_breadth"]).abs()
    mismatch = delta.gt(1e-10)
    if mismatch.any():
        sample = joined.loc[mismatch, "date"].head(3).tolist()
        raise ValueError(f"Style sidecar market_breadth mismatch on {int(mismatch.sum())} dates (sample {sample}); rebuild from physical full-market fields")
    return {
        "status": "checked_signal_date_full_market_counts",
        "checked_dates": int(len(joined)),
        "reference_dates_without_sidecar_breadth": int(len(ref) - len(joined)),
        "max_absolute_breadth_delta": float(delta.max()),
        "future_labels_loaded": False,
        "model_refit": False,
        "execution_admitted": False,
    }
