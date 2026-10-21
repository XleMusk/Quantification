"""Pure 50-session traded-amount attention groups on one fixed stock pool.

Amounts are original CNY traded amounts, not net flow and not the CRSP
close-times-share-volume proxy. Matrix rows must already be the caller's full
exact source calendar, including missing/suspended observations as missing or
nonpositive values. This component never compresses active sessions.
"""
from copy import deepcopy
from datetime import date
import math

import numpy as np
import pandas as pd


START, END = "2024-01-02", "2026-09-30"
WINDOW = 50
ARMS = ("all", "high", "normal", "low")
METHOD_ID = "own_reported_CNY_amount_50session_midrank_attention_v1"
FLAGS = {"execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "engine_advanced": False, "orders_generated": False, "ledger_mutated": False,
    "network_accessed": False, "future_labels_used_for_score": False}
SPEC = {"id": METHOD_ID, "start": START, "end": END, "window": WINDOW,
    "decision_schedule": "exact_original_calendar_between_START_END_dates_19_then_every20",
    "activity": "all50_original_CNY_amount_and_share_volume_finite_strictly_positive",
    "midrank": "1+count(prior49_amount_lt_current)+0.5*count(prior49_amount_eq_current)",
    "high": "rank>=46", "low": "rank<=5", "normal": "5<rank<46_including_fractional_ties",
    "common_pool": "unchanged_LowRiskFeed.pool_with_features_at_all_exact_decision_days",
    "unknown": "any_common_pool_activity_unknown_reject_entire_batch_all_four_arms_NO_CHANGE",
    "ranking": "original_momentum_desc_symbol_asc_full_list_runner_top20_no_replacement",
    "known_empty_group": "explicit_empty_list_not_unknown",
    "price_volume_proxy": False, "net_flow_claimed": False, **FLAGS}


def _ordered_dates(values, name):
    result = list(values)
    if (any(not isinstance(day, str) or date.fromisoformat(day).isoformat() != day for day in result)
            or result != sorted(set(result))):
        raise ValueError(name + " must preserve distinct ordered exact source sessions")
    return result


def decision_days(calendar):
    sessions = _ordered_dates(calendar, "calendar")
    if START not in sessions or END not in sessions:
        raise ValueError("The original fixed START and END must be covered; never shift the schedule")
    return [day for day in sessions if START <= day <= END][19::20]


def activity_ranks(amounts, volumes, days):
    """Return requested own-D ranks only; unknown 50-session windows are NaN."""
    if (not isinstance(amounts, pd.DataFrame) or not isinstance(volumes, pd.DataFrame)
            or not amounts.index.equals(volumes.index) or not amounts.columns.equals(volumes.columns)
            or amounts.columns.has_duplicates):
        raise ValueError("Amount and volume matrices must share one exact calendar and original symbol order")
    calendar = _ordered_dates(amounts.index, "matrix calendar")
    requested = _ordered_dates(days, "decision days")
    indices = {day: index for index, day in enumerate(calendar)}
    if any(day not in indices for day in requested):
        raise ValueError("Every requested source D must occur in the original complete calendar")
    amount_values = amounts.to_numpy(dtype=float, na_value=np.nan)
    volume_values = volumes.to_numpy(dtype=float, na_value=np.nan)
    output = np.full((len(requested), len(amounts.columns)), np.nan)
    for row_number, day in enumerate(requested):
        end = indices[day] + 1
        if end < WINDOW:
            continue
        window = amount_values[end-WINDOW:end]
        volume_window = volume_values[end-WINDOW:end]
        valid = (np.isfinite(window) & (window > 0) & np.isfinite(volume_window) & (volume_window > 0)).all(axis=0)
        current, previous = window[-1], window[:-1]
        ranks = 1 + (previous < current).sum(axis=0) + .5*(previous == current).sum(axis=0)
        output[row_number, valid] = ranks[valid]
    return pd.DataFrame(output, index=pd.Index(requested, name=amounts.index.name), columns=amounts.columns.copy())


def eligible_group(rank):
    if isinstance(rank, (bool, str)):
        return None
    try:
        value = float(rank)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not 1 <= value <= WINDOW:
        return None
    return "high" if value >= 46 else "low" if value <= 5 else "normal"


def rank_group(pool, arm):
    """Return every group member in original momentum order, never top-up.

    Unknown activity anywhere in the common pool raises even for ``all``;
    the runner must retain that explicit failure as NO_CHANGE plus coverage.
    An empty returned list therefore means a known empty group.
    """
    if arm not in ARMS:
        raise ValueError("Use one of the four fixed attention arms")
    rows = list(pool)
    if any(eligible_group(row.get("activity_rank")) is None for row in rows):
        raise ValueError("common_pool_activity_rank_unknown")
    if len({row["symbol"] for row in rows}) != len(rows):
        raise ValueError("The unchanged common pool must contain unique original symbols")
    selected = []
    for row in rows:
        momentum = row.get("momentum")
        if isinstance(momentum, bool) or not isinstance(momentum, (int, float, np.number)) or not math.isfinite(float(momentum)):
            raise ValueError("The original common-pool momentum must remain finite")
        group = eligible_group(row["activity_rank"])
        if arm == "all" or group == arm:
            selected.append({**deepcopy(row), "score": float(momentum), "activity_group": group,
                "activity_status": "known_50_exact_active_sessions"})
    return sorted(selected, key=lambda row: (-row["score"], row["symbol"]))
