"""Conditional outcome comparisons for a fixed, already selected event panel.

This module neither scores nor selects candidates.  Formation denominators always
refer to the supplied same-day/stage pool, including blocked and unknown rows.
The supplied returns are gross adjusted-price diagnostics, not account returns.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd


STATUSES = ("proxy_clear", "proxy_blocked", "unknown")
HORIZONS = ("h1", "h5")
POPULATIONS = ("endpoint_known", "entry_exit_proxy_clear")
FLAGS = {
    "candidate_panel_only": True,
    "execution_admitted": False,
    "selection_enabled": False,
    "strategy_validated": False,
    "returns_reportable": False,
    "costs_applied": False,
    "lot_constraints_applied": False,
    "real_fills_observed": False,
    "model_refit": False,
    "scores_recomputed": False,
}


def _validate(frame: pd.DataFrame, days: Sequence[str]) -> tuple[pd.DataFrame, list[str]]:
    dates = [str(day) for day in days]
    if not dates or dates != sorted(set(dates)):
        raise ValueError("days must be the nonempty, ordered unique formation calendar")
    if any(pd.Timestamp(day).strftime("%Y-%m-%d") != day for day in dates):
        raise ValueError("days must use ISO date strings")
    required = {
        "date", "symbol", "stage", "model_kind", "candidate_selected", "entry_status",
        *(f"{h}_return" for h in HORIZONS),
        *(f"{h}_exit_status" for h in HORIZONS),
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"missing columns: {missing}")
    data = frame.copy()
    for column in ("date", "symbol", "model_kind"):
        if data[column].isna().any():
            raise ValueError(f"{column} must be known")
        data[column] = data[column].astype(str)
    if not set(data["date"]).issubset(dates):
        raise ValueError("event date is outside the fixed formation calendar")
    if data["stage"].isna().any():
        raise ValueError("stage must be known at formation")
    if data.duplicated(["date", "symbol", "stage", "model_kind"]).any():
        raise ValueError("duplicate formation event/model")
    if not data["candidate_selected"].map(lambda x: isinstance(x, (bool, np.bool_))).all():
        raise ValueError("candidate_selected must contain explicit booleans")
    data["candidate_selected"] = data["candidate_selected"].astype(bool)
    for column in ("entry_status", *(f"{h}_exit_status" for h in HORIZONS)):
        if not data[column].isin(STATUSES).all():
            raise ValueError(f"invalid or missing status in {column}")
    for horizon in HORIZONS:
        column = f"{horizon}_return"
        data[column] = pd.to_numeric(data[column], errors="raise").astype(float)
        if np.isinf(data[column]).any():
            raise ValueError(f"infinite outcome in {column}")
    return data, dates


def _basket(group: pd.DataFrame, horizon: str, population: str) -> dict[str, Any]:
    selected = group["candidate_selected"]
    outcome = group[f"{horizon}_return"]
    endpoint_known = outcome.notna()
    eligible = pd.Series(True, index=group.index)
    if population == "entry_exit_proxy_clear":
        eligible = (group["entry_status"] == "proxy_clear") & (
            group[f"{horizon}_exit_status"] == "proxy_clear"
        )
    known = endpoint_known & eligible
    selected_known = known & selected
    pool_mean = float(outcome[known].mean()) if known.any() else np.nan
    selected_mean = float(outcome[selected_known].mean()) if selected_known.any() else np.nan
    result: dict[str, Any] = {
        "pool_count": len(group),
        "selected_count": int(selected.sum()),
        "endpoint_known_count": int(endpoint_known.sum()),
        "selected_endpoint_known_count": int((endpoint_known & selected).sum()),
        "population_eligible_count": int(eligible.sum()),
        "population_known_outcome_count": int(known.sum()),
        "selected_known_outcome_count": int(selected_known.sum()),
        "pool_outcome_missing": int((~known).sum()),
        "selected_outcome_missing": int((selected & ~known).sum()),
        "pair_complete_for_original_selected": bool(selected.any() and (known[selected]).all()),
        "selected_mean_return": selected_mean,
        "same_stage_pool_mean_return": pool_mean,
        "difference": selected_mean - pool_mean if selected_known.any() else np.nan,
    }
    for side, statuses in (
        ("entry", group["entry_status"]),
        ("exit", group[f"{horizon}_exit_status"]),
    ):
        for status in STATUSES:
            name = status.removeprefix("proxy_")
            mask = statuses == status
            result[f"{side}_{name}_count"] = int(mask.sum())
            result[f"selected_{side}_{name}_count"] = int((selected & mask).sum())
    return result


def _summary_row(baskets: pd.DataFrame, weighting: str) -> dict[str, Any]:
    chosen = baskets[baskets["selected_count"] > 0]
    paired = chosen[chosen["difference"].notna()]
    means = paired[["date", "selected_mean_return", "same_stage_pool_mean_return", "difference"]]
    if weighting == "day_equal":
        means = means.groupby("date", sort=False)[[
            "selected_mean_return", "same_stage_pool_mean_return", "difference"
        ]].mean()
    result: dict[str, Any] = {
        "formation_day_count": int(baskets["date"].nunique()),
        "selected_baskets_count": len(chosen),
        "paired_baskets_count": len(paired),
        "complete_selected_baskets_count": int(chosen["pair_complete_for_original_selected"].sum()),
        "incomplete_selected_baskets_count": int((~chosen["pair_complete_for_original_selected"]).sum()),
        "selected_baskets_without_known_outcome_count": len(chosen) - len(paired),
        "paired_days_count": int(paired["date"].nunique()),
        "aggregation_unit_count": len(means),
        "selected_count": int(chosen["selected_count"].sum()),
        "selected_known_outcome_count": int(chosen["selected_known_outcome_count"].sum()),
        "selected_outcome_missing": int(chosen["selected_outcome_missing"].sum()),
    }
    for column in ("selected_mean_return", "same_stage_pool_mean_return", "difference"):
        result[column] = float(means[column].mean()) if len(means) else np.nan
    return result


def analyze(frame: pd.DataFrame, days: Sequence[str]) -> dict[str, Any]:
    """Return detail, summaries and denominator coverage without any selection.

    Required outcome columns are ``h1_return``, ``h5_return`` (decimal returns)
    and their ``h1_exit_status``/``h5_exit_status``.  ``days`` is the complete
    pre-fixed formation calendar, including days with no selected events.  The
    fixed five-session origin list is always ``days[::5]`` before observing any
    selection, outcome or execution proxy.  H1 and H5 use all days; H5 also has
    that fixed-origin subtable.  H1/H2 below denote calendar-year halves, not
    return horizons, and are descriptive previously inspected segments.
    """
    data, dates = _validate(frame, days)
    origins = dates[::5]
    origin_set = set(origins)
    models = list(dict.fromkeys(data["model_kind"]))
    stages = list(dict.fromkeys(data["stage"]))
    if "all" in stages:
        raise ValueError("stage value 'all' is reserved for aggregate rows")
    grouped = {
        key: group for key, group in data.groupby(["date", "model_kind", "stage"], sort=False)
    }
    empty = data.iloc[:0]
    rows = []
    for date in dates:
        for model in models:
            for stage in stages:
                group = grouped.get((date, model, stage), empty)
                for horizon in HORIZONS:
                    for population in POPULATIONS:
                        rows.append({
                            "date": date, "model_kind": model, "stage": stage,
                            "horizon": horizon, "population": population,
                            "fixed_nonoverlap_5_origin": date in origin_set,
                            **_basket(group, horizon, population),
                        })
    detail = pd.DataFrame(rows)
    summary_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    if not detail.empty:
        counter_columns = [column for column in detail if column.endswith("_count")]
        counter_columns += ["pool_outcome_missing", "selected_outcome_missing"]
        for horizon in HORIZONS:
            origin_samples = ("all_days", "fixed_nonoverlap_5") if horizon == "h5" else ("all_days",)
            for origin_sample in origin_samples:
                horizon_rows = detail[detail["horizon"] == horizon]
                if origin_sample == "fixed_nonoverlap_5":
                    horizon_rows = horizon_rows[horizon_rows["fixed_nonoverlap_5_origin"]]
                for segment in ("all", "H1", "H2"):
                    segment_rows = horizon_rows
                    if segment != "all":
                        half = horizon_rows["date"].str[5:] <= "06-30"
                        segment_rows = horizon_rows[half if segment == "H1" else ~half]
                    for model in models:
                        for population in POPULATIONS:
                            current = segment_rows[
                                (segment_rows["model_kind"] == model)
                                & (segment_rows["population"] == population)
                            ]
                            for stage in [*stages, "all"]:
                                baskets = current if stage == "all" else current[current["stage"] == stage]
                                dimensions = {
                                    "model_kind": model, "stage": stage, "horizon": horizon,
                                    "population": population, "origin_sample": origin_sample,
                                    "segment": segment,
                                }
                                for weighting in ("date_stage_basket_equal", "day_equal"):
                                    summary_rows.append({
                                        **dimensions, "weighting": weighting,
                                        **_summary_row(baskets, weighting),
                                    })
                                coverage_rows.append({
                                    **dimensions,
                                    "formation_day_count": int(baskets["date"].nunique()),
                                    "formation_baskets_count": len(baskets),
                                    "selected_baskets_count": int((baskets["selected_count"] > 0).sum()),
                                    **{column: int(baskets[column].sum()) for column in counter_columns},
                                })
    return {
        "detail": detail,
        "summary": pd.DataFrame(summary_rows),
        "coverage": pd.DataFrame(coverage_rows),
        "nonoverlap_origins": pd.DataFrame({
            "date": origins, "formation_calendar_index": list(range(0, len(dates), 5)),
        }),
        "metadata": {
            "method_id": "fixed_native_selection_same_day_stage_remaining_edge_v1",
            "formation_days": dates,
            "return_interpretation": "gross conditional adjusted-price return, not investable account return",
            "pool_definition": "original same-date same-model same-stage formation pool, including selected",
            "unknown_and_blocked_preserved_in_formation_denominators": True,
            "summary_pool_means_use_same_baskets_as_known_selected_means": True,
            "weightings": {
                "date_stage_basket_equal": "each paired date-stage basket has equal weight",
                "day_equal": "mean paired stage baskets within each day, then equally weight paired days",
            },
            "segments": {
                "all": "all fixed formation days",
                "H1": "month-day <= 06-30, previously inspected descriptive segment, not independent holdout",
                "H2": "month-day >= 07-01, previously inspected descriptive segment, not independent holdout",
            },
            "five_day_all_origins_overlap": True,
            "fixed_nonoverlap_5_rule": "formation_days[::5], never shifted for selection, blockage or missing outcome",
            "basket_or_day_counts_are_not_independent_trade_counts": True,
            "model_or_period_selection_performed": False,
            "sharpe_cagr_success_rate_computed": False,
        },
        "flags": dict(FLAGS),
    }
