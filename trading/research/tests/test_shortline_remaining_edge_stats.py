import numpy as np
import pandas as pd
import pytest

from trading.research.shortline_remaining_edge_stats import analyze


def event(date, symbol, stage, selected, outcome, *, entry="proxy_clear", exit="proxy_clear"):
    return {
        "date": date, "symbol": symbol, "stage": stage, "model_kind": "lr",
        "candidate_selected": selected, "entry_status": entry,
        "h1_return": outcome, "h5_return": outcome,
        "h1_exit_status": exit, "h5_exit_status": exit,
    }


def detail(result, *, date="2025-01-02", stage=1, population="endpoint_known", horizon="h1"):
    rows = result["detail"]
    return rows[(rows.date == date) & (rows.stage == stage)
                & (rows.population == population) & (rows.horizon == horizon)].iloc[0]


def summary(result, *, stage="all", weighting="date_stage_basket_equal", horizon="h1",
            origin_sample="all_days", segment="all", population="endpoint_known"):
    rows = result["summary"]
    return rows[(rows.stage == stage) & (rows.weighting == weighting)
                & (rows.horizon == horizon) & (rows.origin_sample == origin_sample)
                & (rows.segment == segment) & (rows.population == population)].iloc[0]


def test_unknown_is_missing_and_original_selected_completeness_is_visible():
    result = analyze(pd.DataFrame([
        event("2025-01-02", "a", 1, True, np.nan, entry="unknown", exit="unknown"),
        event("2025-01-02", "b", 1, False, 0.2),
    ]), ["2025-01-02"])
    row = detail(result)
    assert row.pool_count == 2 and row.selected_count == 1
    assert row.pool_outcome_missing == 1 and row.selected_outcome_missing == 1
    assert row.entry_unknown_count == 1 and row.exit_unknown_count == 1
    assert np.isnan(row.selected_mean_return) and np.isnan(row.difference)
    aggregate = summary(result)
    assert aggregate.paired_baskets_count == 0
    assert aggregate.complete_selected_baskets_count == 0
    assert np.isnan(aggregate.difference)


def test_comparison_is_same_date_stage_and_pool_includes_selected():
    result = analyze(pd.DataFrame([
        event("2025-01-02", "a", 1, True, 0.2),
        event("2025-01-02", "b", 1, False, 0.0),
        event("2025-01-02", "c", 2, False, -0.8),
        event("2025-01-03", "d", 1, False, 0.9),
    ]), ["2025-01-02", "2025-01-03"])
    row = detail(result)
    assert row.same_stage_pool_mean_return == pytest.approx(0.1)
    assert row.difference == pytest.approx(0.1)
    assert summary(result).paired_baskets_count == 1
    assert summary(result).difference == pytest.approx(0.1)
    assert np.isnan(detail(result, date="2025-01-03").selected_mean_return)


def test_clear_population_keeps_blocked_and_unknown_formation_denominators():
    result = analyze(pd.DataFrame([
        event("2025-01-02", "a", 1, True, 0.4, entry="proxy_blocked"),
        event("2025-01-02", "b", 1, True, 0.2),
        event("2025-01-02", "c", 1, False, 0.0),
        event("2025-01-02", "d", 1, False, 0.8, exit="unknown"),
    ]), ["2025-01-02"])
    endpoint, clear = detail(result), detail(result, population="entry_exit_proxy_clear")
    assert endpoint.pool_count == clear.pool_count == 4
    assert endpoint.selected_count == clear.selected_count == 2
    assert endpoint.selected_mean_return == pytest.approx(0.3)
    assert clear.selected_mean_return == pytest.approx(0.2)
    assert clear.entry_blocked_count == 1 and clear.exit_unknown_count == 1
    assert clear.pool_outcome_missing == 2 and clear.selected_outcome_missing == 1
    assert endpoint.pair_complete_for_original_selected
    assert not clear.pair_complete_for_original_selected
    assert summary(result, population="entry_exit_proxy_clear").complete_selected_baskets_count == 0


def test_fixed_five_session_origins_never_shift_for_missing_or_unselected_days():
    days = list(pd.bdate_range("2025-01-02", periods=11).strftime("%Y-%m-%d"))
    result = analyze(pd.DataFrame([
        event(days[0], "a", 1, True, np.nan, entry="unknown"),
        event(days[1], "b", 1, True, 0.8),
        event(days[5], "c", 1, False, 0.2),
        event(days[10], "d", 1, True, 0.4),
    ]), days)
    assert list(result["nonoverlap_origins"].date) == [days[0], days[5], days[10]]
    row = summary(result, horizon="h5", origin_sample="fixed_nonoverlap_5")
    assert row.formation_day_count == 3 and row.selected_baskets_count == 2
    assert row.paired_baskets_count == 1 and row.selected_mean_return == pytest.approx(0.4)
    assert summary(result, horizon="h5").paired_baskets_count == 2


def test_basket_equal_and_day_equal_are_distinct_without_independent_trade_claims():
    events = []
    for date, stage, outcome in (("2025-01-02", 1, 0.02), ("2025-01-02", 2, 0.06),
                                 ("2025-01-03", 1, 0.10)):
        events += [event(date, "selected", stage, True, outcome), event(date, "pool", stage, False, 0.0)]
    result = analyze(pd.DataFrame(events), ["2025-01-02", "2025-01-03"])
    basket = summary(result)
    daily = summary(result, weighting="day_equal")
    assert basket.difference == pytest.approx(0.03)
    assert daily.difference == pytest.approx(0.035)
    assert basket.aggregation_unit_count == 3 and daily.aggregation_unit_count == 2
    assert summary(result, stage=2).difference == pytest.approx(0.03)
    assert result["metadata"]["basket_or_day_counts_are_not_independent_trade_counts"]
    assert not result["flags"]["costs_applied"] and not result["flags"]["execution_admitted"]


def test_fixed_half_year_segments_are_descriptive_and_missing_segment_is_null():
    result = analyze(pd.DataFrame([
        event("2025-06-30", "a", 1, True, 0.2),
        event("2025-06-30", "b", 1, False, 0.0),
        event("2025-07-01", "a", 1, False, 0.9),
    ]), ["2025-06-30", "2025-07-01"])
    assert summary(result, segment="H1").difference == pytest.approx(0.1)
    assert summary(result, segment="H2").selected_baskets_count == 0
    assert np.isnan(summary(result, segment="H2").difference)
    assert "not independent holdout" in result["metadata"]["segments"]["H2"]
