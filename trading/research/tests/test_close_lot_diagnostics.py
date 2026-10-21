from copy import deepcopy

import pytest

from trading.config import StrategyConfig
from trading.research.close_lot_diagnostics import saved_close_lot_transitions


def fixture():
    weight = 100 * 5000 / 14000000
    estimate = {"symbol": "600000.SSE", "quantity_at_decision_close": 100,
                "close_rmb": 50., "target_weight": weight, "original_weight": .04,
                "minimum_buy_quantity": 100}
    allocation = {"sequence": 1, "day": "2026-01-05", "kind": "close_lot_allocation",
                  "policy": "close_stock_lot_tracking_v1", "stock_budget_cny": 5600,
                  "allocated_stock_cny": 5000, "estimates": [estimate]}
    target = {"sequence": 2, "kind": "targets_generated", "day": "2026-01-05", "signal_date": "2026-01-05",
              "execution_not_before": "2026-01-06", "targets": {"600000.SSE": weight}}
    trace = {"day": "2026-01-06", "symbol": "600000.SSE", "target_weight": weight,
             "has_execution_open": True, "engine_mark": 5000, "price_unit": "cent",
             "open_equity_cents": 14000000, "engine_target_quantity": 100,
             "quantity_step": 100, "minimum_buy_quantity": 100, "held_before": 0, "held_after": 100}
    marks = [{"date": "2026-01-05", "equity": 140000}]
    return {"events": [allocation, target]}, [trace], marks


def test_exact_close_intention_and_stored_weight_boundary_are_not_price_loss():
    account, traces, marks = fixture()
    before = deepcopy((account, traces, marks))
    rows, summary = saved_close_lot_transitions(account, traces, marks, StrategyConfig())
    assert (account, traces, marks) == before
    assert rows.exact_close_weight_open_quantity.iloc[0] == 100
    assert rows.decimal_stored_weight_quantity.iloc[0] == 0
    assert rows.engine_target_quantity.iloc[0] == 100
    assert rows.price_transition_quantity_gap.iloc[0] == rows.equity_transition_quantity_gap.iloc[0] == 0
    assert rows.stored_weight_rounding_quantity_gap.iloc[0] == 100
    assert rows.binary_arithmetic_quantity_gap.iloc[0] == -100
    assert rows.numeric_net_quantity_gap.iloc[0] == 0
    assert summary["stages"]["stored_weight_rounding"]["reduction_records"] == 1
    assert summary["numeric_net_counts"]["reduction_records"] == 0


@pytest.mark.parametrize("change", ["price", "equity"])
def test_real_price_and_equity_transition_not_mislabeled_numeric(change):
    account, traces, marks = fixture()
    if change == "price":
        traces[0]["engine_mark"] = 5001
    else:
        traces[0]["open_equity_cents"] = 13999999
    traces[0].update(engine_target_quantity=0, held_after=0)
    rows, _ = saved_close_lot_transitions(account, traces, marks, StrategyConfig())
    assert rows[change + "_transition_quantity_gap"].iloc[0] == 100
    assert rows.stored_weight_rounding_quantity_gap.iloc[0] == 0
    assert rows.binary_arithmetic_quantity_gap.iloc[0] == 0


def test_zero_unknown_and_mismatched_evidence_never_become_observed_fills():
    account, traces, marks = fixture()
    rows, summary = saved_close_lot_transitions(account, [], marks, StrategyConfig())
    assert summary["observed_records"] == 0 and rows.status.iloc[0] == "missing_saved_attempt"
    account["events"][0]["estimates"][0].update(quantity_at_decision_close=0, target_weight=0)
    account["events"][0]["allocated_stock_cny"] = 0
    account["events"][1]["targets"]["600000.SSE"] = 0
    rows, summary = saved_close_lot_transitions(account, [], marks, StrategyConfig())
    assert rows.status.iloc[0] == "explicit_zero_budget_no_saved_attempt" and summary["observed_records"] == 0
    assert rows.held_after.isna().all()
    account["events"][0]["allocated_stock_cny"] = 1
    with pytest.raises(ValueError, match="budget does not reconcile"):
        saved_close_lot_transitions(account, [], marks, StrategyConfig())
