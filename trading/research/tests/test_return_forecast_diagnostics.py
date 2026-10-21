from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from trading.expected_returns import POLICY_ID
from trading.research.family_ridge import FAMILIES, HORIZON
from trading.research.return_forecast_diagnostics import conditional_folds, selection_event


def fold_fixture():
    calendar = pd.bdate_range("2024-01-02", "2024-12-31")
    anchors = [0, 22, 44, 66, 88, 110, 132, 154, 175, 198]
    batches, training, counts = {}, [], []
    for position in anchors:
        day = calendar[position]
        batch = pd.DataFrame({"risk": [-1., 0., 1., 3.], "trend": [0.] * 4,
                              "volume_stability": [0.] * 4}, index=pd.Index(["A", "B", "C", "Z"], name="symbol"))
        batches[day] = batch
        observed = batch.iloc[:3].copy()
        observed["forward_return"] = [-.01, 0, .01]
        observed["sample_weight"] = 1 / 3
        observed["date"] = day
        training.append(observed.reset_index().set_index(["date", "symbol"]))
        counts.append({"date": day, "exit_date": calendar[position + HORIZON + 1],
                       "signal_rows": 4, "labeled_rows": 3})
    return (pd.concat(training).sort_index(), pd.concat(batches, names=["date", "symbol"]).sort_index(),
            pd.DataFrame(counts), calendar)


def test_same_decision_exit_is_not_training_and_unknown_head_is_not_replaced():
    train, features, counts, calendar = fold_fixture()
    result, panel = conditional_folds(train, features, counts, calendar, 1)
    assert result["monthly_units"] == 1
    blocked = next(row for row in result["skipped"] if row["date"] == calendar[175].strftime("%Y-%m-%d"))
    assert blocked["matured_training_months"] == 7
    fold = result["folds"][0]
    assert fold["training_months"] == 9 and fold["last_training_exit"] < fold["date"]
    assert fold["unknown_labels"] == 1 and fold["head_known_labels"] == 0
    assert fold["complete_head_relative_label"] is None
    assert panel.loc[panel.head_selected, "symbol"].tolist() == ["Z"]
    assert result["uncertainty_calibrated"] is False and result["whole_pipeline_out_of_sample"] is False


def test_wrong_evaluation_exit_fails_before_a_fit():
    train, features, counts, calendar = fold_fixture()
    counts.loc[len(counts) - 1, "exit_date"] = calendar[220]
    with pytest.raises(ValueError, match="scheduled exit"):
        conditional_folds(train, features, counts, calendar, 1)


def selection_fixture(cost=True):
    forecasts = pd.DataFrame({**{name: [0., 0., 0.] for name in FAMILIES},
                              "expected_return": [.003, .004, .001]}, index=pd.Index(["A", "B", "C"], name="symbol"))
    details = []
    for symbol, mu, held, include, exclude in [("A", .003, 100, 0, 200), ("B", .004, 0, 150, 0), ("C", .001, 0, 150, 0)]:
        benefit = 100000 * mu
        details.append({"symbol": symbol, "expected_return_fraction": mu, "held_quantity": held,
                        "close_legal_notional_cents": 100000, "forecast_relative_benefit_cents": benefit,
                        "estimated_include_cost_cents": include, "estimated_exclude_cost_cents": exclude,
                        "selection_utility_cents": benefit - (include - exclude if cost else 0)})
    event = {"day": "2025-01-31", "policy_id": POLICY_ID, "cost_aware": cost, "horizon_sessions": HORIZON,
             "cost_scale": 1, "estimate_is_execution": False, "candidate_count": 3,
             "selected_symbols": ["A" if cost else "B"], "cost_changed_members": ["A", "B"] if cost else [],
             "estimated_departed_exit_cost_cents": 0, "estimated_selected_transition_cost_cents": 0 if cost else 350,
             "estimated_selected_legal_gross": .8, "selected_zero_legal_quantity": 0, "diagnostics": details}
    return event, forecasts


def test_saved_frontier_cost_benefit_retention_are_cny_estimates_without_mutation():
    event, forecasts = selection_fixture()
    before = deepcopy(event)
    row, members = selection_event(event, forecasts, 1)
    assert event == before
    assert row["changed_slots"] == 1 and row["added_to_raw_frontier"]["held_before"] == 1
    assert row["forecast_benefit_given_up_cny"] == pytest.approx(1)
    assert row["estimated_cost_saving_cny"] == pytest.approx(3.5)
    assert row["estimated_net_utility_gain_cny"] == pytest.approx(2.5)
    assert members.loc[members.selected, "symbol"].tolist() == ["A"]
    raw_event, forecasts = selection_fixture(False)
    raw, _ = selection_event(raw_event, forecasts, 1)
    assert raw["changed_slots"] == 0 and raw["estimated_net_utility_gain_cny"] == 0


def test_forecast_or_frontier_mismatch_fails_instead_of_inventing_a_comparison():
    event, forecasts = selection_fixture()
    forecasts.loc["A", "expected_return"] = np.nan
    with pytest.raises(ValueError, match="frozen forecasts"):
        selection_event(event, forecasts, 1)
    event, forecasts = selection_fixture()
    event["cost_changed_members"] = ["A", "C"]
    with pytest.raises(ValueError, match="fixed additive selector"):
        selection_event(event, forecasts, 1)
