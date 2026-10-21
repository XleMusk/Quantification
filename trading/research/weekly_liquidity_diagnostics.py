"""Pure same-pool reversal diagnostics; no source reads, models or accounts."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .factor_families import rank_group_means
from .factors import daily_ic


METHOD_ID = "weekly_reported_turnover_conditioned_reversal_rank_diagnostic_v1"
STRATA = ("all", "T1", "T2", "T3", "T4", "T5")
ENDPOINTS = ("h5", "h21", "late5_21")
MIN_PAIRS = 100
SCOPES = ("2024", "2025", "2026_partial", "all")
SPEC = dict(method_id=METHOD_ID, strata=list(STRATA), endpoints=list(ENDPOINTS),
    min_pairs=MIN_PAIRS, pool="original_saved_D_matched_turnover_eligible_pool",
    turnover_groups="saved_D_turn_group_1_through_5_unchanged_no_future_label_filter",
    reversal_groups="within_each_original_D_stratum_average_pct_rank_Q1_le0.2_Q5_gt0.8",
    labels="provided_exact_scheduled_open_price_ratios_h5_h21_and_late5_21_no_subtraction",
    unknown="preserve_original_D_group_members_and_label_missingness_no_zero_fill_or_rebin",
    IC="existing_factors_daily_ic_pairwise_finite_Spearman_min100",
    group_means="existing_factor_families_rank_group_means_min100_min5_distinct_scores",
    summaries="date_equal_descriptive_means_2024_2025_2026_partial_and_all",
    paired="T1_minus_T5_late_only_same_D_both_finite_separately_for_IC_and_Qspread",
    inference="no_significance_HAC_p_values_annualization_or_account_returns",
    model_refit=False, selection_enabled=False, strategy_validated=False,
    execution_admitted=False, candidate_panel_only=True)


def _date(value):
    if not isinstance(value, str) or pd.Timestamp(value).strftime("%Y-%m-%d") != value:
        raise ValueError("Exact canonical saved source dates are required")
    return value


def _finite(value):
    return isinstance(value, (float, int, np.number)) and np.isfinite(value)


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def _scope_days(days, scope):
    if scope == "all":
        return days
    year = "2026" if scope == "2026_partial" else scope
    return [day for day in days if day[:4] == year]


def _mean(values):
    finite = [float(value) for value in values if _finite(value)]
    return float(np.mean(finite)) if finite else None


def _summaries(daily, days):
    result = []
    for scope in SCOPES:
        own_days = _scope_days(days, scope)
        for stratum in STRATA:
            for endpoint in ENDPOINTS:
                rows = [row for row in daily if row["date"] in own_days
                        and row["stratum"] == stratum and row["endpoint"] == endpoint]
                ics = [row["rank_ic"] for row in rows if _finite(row["rank_ic"])]
                spreads = [row["q5_minus_q1"] for row in rows if _finite(row["q5_minus_q1"])]
                result.append(dict(scope=scope, stratum=stratum, endpoint=endpoint,
                    registered_dates=own_days, registered_date_count=len(own_days),
                    source_pool_unavailable_dates=sum(row["pool_members"] is None for row in rows),
                    valid_IC_dates=len(ics), mean_rank_IC=_mean(ics),
                    positive_IC_fraction=float(np.mean(np.array(ics) > 0)) if ics else None,
                    valid_spread_dates=len(spreads), mean_Q5_minus_Q1_price_spread=_mean(spreads),
                    observed_label_pairs=sum(row["pairs"] for row in rows if row["pairs"] is not None),
                    missing_labels=sum(row["missing_labels"] for row in rows if row["missing_labels"] is not None),
                    strategy_return=None, independent_replications_claimed=False))
    return result


def _paired(daily, days):
    indexed = {(row["date"], row["stratum"]): row for row in daily if row["endpoint"] == "late5_21"}
    paired = []
    for day in days:
        low, high = indexed[(day, "T1")], indexed[(day, "T5")]
        valid_ic = _finite(low["rank_ic"]) and _finite(high["rank_ic"])
        valid_spread = _finite(low["q5_minus_q1"]) and _finite(high["q5_minus_q1"])
        paired.append(dict(date=day, endpoint="late5_21", low_stratum="T1", high_stratum="T5",
            both_IC_finite=valid_ic, delta_IC=low["rank_ic"] - high["rank_ic"] if valid_ic else None,
            both_spreads_finite=valid_spread,
            delta_Qspread=low["q5_minus_q1"] - high["q5_minus_q1"] if valid_spread else None))
    summaries = []
    for scope in SCOPES:
        selected = _scope_days(days, scope)
        rows = [row for row in paired if row["date"] in selected]
        ic_rows = [row for row in rows if row["both_IC_finite"]]
        spread_rows = [row for row in rows if row["both_spreads_finite"]]
        summaries.append(dict(scope=scope, endpoint="late5_21", comparison="T1_minus_T5",
            registered_dates=selected, same_D_valid_IC_dates=[row["date"] for row in ic_rows],
            paired_valid_IC_dates=len(ic_rows), mean_delta_IC=_mean([row["delta_IC"] for row in ic_rows]),
            same_D_valid_spread_dates=[row["date"] for row in spread_rows],
            paired_valid_spread_dates=len(spread_rows), mean_delta_Qspread=_mean([row["delta_Qspread"] for row in spread_rows]),
            significance_calculated=False, strategy_return=None))
    return paired, summaries


def evaluate(frame, labels, registered_days):
    """Consume fixed scores and independently generated saved endpoint labels.

    Every reversal quintile is fixed before inspecting label values. Missing
    endpoints never remove a member or move any original registered date.
    """
    days = [_date(day) for day in registered_days]
    if not days or days != sorted(set(days)):
        raise ValueError("Distinct ascending registered decision dates are required")
    required = ["reversal_score", "turn_group", "turn63_percent"]
    if (not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.MultiIndex)
            or list(frame.index.names) != ["date", "symbol"] or not frame.index.is_unique
            or not set(required) <= set(frame)):
        raise ValueError("One saved common-pool row per exact date/security is required")
    dates = frame.index.get_level_values("date")
    if any(_date(day) not in days for day in dates):
        raise ValueError("Saved signal rows are outside the registered original dates")
    own = frame[required].apply(pd.to_numeric, errors="raise").copy()
    if (not np.isfinite(own.to_numpy(dtype=float)).all()
            or not own.turn_group.isin(range(1, 6)).all() or own.turn63_percent.lt(0).any()):
        raise ValueError("The matched pool needs finite original scores and known reported-turnover groups")
    if set(labels) != set(ENDPOINTS):
        raise ValueError("Retain all three separately generated fixed endpoint labels")

    # Membership formation depends only on saved D scores, not label presence.
    prepared = {}
    for stratum in STRATA:
        scores = own if stratum == "all" else own.loc[own.turn_group.eq(int(stratum[1:]))]
        scores = scores[["reversal_score"]]
        by_day = {}
        for day, part in scores.groupby(level="date", sort=True):
            percentile = part.reversal_score.rank(method="average", pct=True)
            by_day[day] = dict(scores=part, q1=percentile.le(.2), q5=percentile.gt(.8))
        prepared[stratum] = dict(scores=scores, by_day=by_day)

    targets = {}
    for endpoint in ENDPOINTS:
        target = labels[endpoint]
        if (not isinstance(target, pd.Series) or not isinstance(target.index, pd.MultiIndex)
                or list(target.index.names) != ["date", "symbol"] or not target.index.is_unique
                or not target.index.isin(own.index).all()):
            raise ValueError("Endpoint labels must preserve original saved date/security identities")
        targets[endpoint] = pd.to_numeric(target, errors="raise").reindex(own.index)

    daily, groups = [], []
    original_present_days = set(dates)
    for stratum in STRATA:
        scores, by_day = prepared[stratum]["scores"], prepared[stratum]["by_day"]
        for endpoint in ENDPOINTS:
            target = targets[endpoint]
            ics = daily_ic(scores, target, min_pairs=MIN_PAIRS) if len(scores) else pd.DataFrame()
            means = rank_group_means(scores, target, min_pairs=MIN_PAIRS) if len(scores) else pd.DataFrame()
            mean_map = {row["date"]: row for row in means.to_dict("records")}
            for day in days:
                observed_pool = day in original_present_days
                part = by_day.get(day)
                members = part["scores"] if part else scores.iloc[:0]
                y = target.reindex(members.index)
                pair_mask = np.isfinite(y)
                mean = mean_map.get(day)
                ic = ics.at[day, "reversal_score"] if day in ics.index else np.nan
                q1_excess, q5_excess = (mean["q1_excess"], mean["q5_excess"]) if mean else (np.nan, np.nan)
                unique = members.reversal_score.nunique()
                reason = ("source_pool_unavailable_for_original_D" if not observed_pool else
                    "known_empty_turn_stratum" if not len(members) else
                    "insufficient_finite_label_pairs" if int(pair_mask.sum()) < MIN_PAIRS else
                    "constant_reversal_scores" if unique < 2 else
                    "insufficient_distinct_scores_for_quintiles" if unique < 5 else None)
                group = dict(date=day, stratum=stratum, endpoint=endpoint,
                    pool_members=len(members) if observed_pool else None,
                    observed_labels=int(pair_mask.sum()) if observed_pool else None,
                    missing_labels=int((~pair_mask).sum()) if observed_pool else None,
                    unique_scores=int(unique) if observed_pool else None,
                    maximum_score_tie_count=int(members.reversal_score.eq(members.reversal_score.max()).sum()) if len(members) else 0 if observed_pool else None,
                    membership_basis="original_D_stratum_average_pct_rank_before_labels")
                for name in ("q1", "q5"):
                    mask = part[name] if part else pd.Series(False, index=members.index, dtype=bool)
                    group[name + "_members"] = int(mask.sum()) if observed_pool else None
                    group[name + "_observed_labels"] = int((mask & pair_mask).sum()) if observed_pool else None
                    group[name + "_missing_labels"] = int((mask & ~pair_mask).sum()) if observed_pool else None
                    group[name + "_member_symbols"] = members.index.get_level_values("symbol")[mask].tolist() if observed_pool else None
                groups.append(group)
                daily.append(dict(date=day, stratum=stratum, endpoint=endpoint,
                    pool_members=group["pool_members"], pairs=group["observed_labels"],
                    missing_labels=group["missing_labels"], unique_scores=group["unique_scores"],
                    rank_ic=ic, q1_excess=q1_excess, q5_excess=q5_excess,
                    q5_minus_q1=q5_excess - q1_excess, reason=reason))
    daily.sort(key=lambda row: (row["date"], STRATA.index(row["stratum"]), ENDPOINTS.index(row["endpoint"])))
    paired, paired_summaries = _paired(daily, days)
    return _clean(dict(schema=METHOD_ID + "_result", spec=SPEC, registered_days=days,
        daily=daily, groups=groups, summaries=_summaries(daily, days),
        paired_late_daily=paired, paired_late_summaries=paired_summaries,
        engine_advanced=False, account_returns_calculated=False, significance_calculated=False,
        source_read=False, labels_reconstructed=False, candidate_panel_only=True,
        strategy_validated=False, selection_enabled=False, execution_admitted=False))
