import numpy as np
import pandas as pd
import pytest

from trading.research import continuous_momentum
from trading.research.continuous_momentum import (check_fractional_continuation, continuous_rank, control_basis,
                                                fractional_allocation_contract, information_discreteness)
from trading.research.low_risk import SKIP, WINDOW


def test_id_window_zero_and_missing_observations():
    returns = np.zeros(WINDOW + 1)
    returns[1:WINDOW - SKIP + 1] = .001
    prices = pd.DataFrame({"stock": np.cumprod(1 + returns)})
    full = information_discreteness(prices).iloc[-1, 0]
    assert full == -1
    terminal = prices.copy()
    terminal.iloc[-SKIP:, 0] *= 3
    assert information_discreteness(terminal).iloc[-1, 0] == full
    zeros = returns.copy()
    zeros[100] = 0
    assert np.isclose(information_discreteness(pd.DataFrame({"stock": np.cumprod(1 + zeros)})).iloc[-1, 0],
                      -(WINDOW - SKIP - 1) / (WINDOW - SKIP))
    prices.iloc[100, 0] = np.nan
    assert np.isnan(information_discreteness(prices).iloc[-1, 0])


def test_id_distinguishes_discrete_from_continuous_winner():
    continuous = np.full(WINDOW + 1, .001)
    discrete = np.full(WINDOW + 1, -.001)
    continuous[0] = discrete[0] = 0
    discrete[100] = .7
    frame = pd.DataFrame({"continuous": np.cumprod(1 + continuous),
                          "discrete": np.cumprod(1 + discrete)})
    values = information_discreteness(frame).iloc[-1]
    assert values.continuous == -1
    assert values.discrete > .9


def test_conditional_quintiles_and_deterministic_boundary_ties():
    pool = [{"symbol": f"{i:06}", "momentum": 100 - i, "information_discreteness": 0}
            for i in range(100)]
    for i in range(12, 16):
        pool[i]["information_discreteness"] = -1
    pool[90]["information_discreteness"] = -1
    assert [row["symbol"] for row in continuous_rank(pool[::-1])] == [f"{i:06}" for i in range(12, 16)]
    tied = [{"symbol": f"{i:06}", "momentum": 1, "information_discreteness": 0}
            for i in range(26)]
    assert [row["symbol"] for row in continuous_rank(tied[::-1])] == ["000000", "000001"]
    assert continuous_rank([]) == []


def test_control_refresh_only_for_rejected_changed_source_prefix(monkeypatch):
    def failed(*args):
        raise ValueError("Past economic action changed for an involved symbol; full replay required")
    monkeypatch.setattr(continuous_momentum, "saved_stock_controls", failed)
    with pytest.raises(ValueError, match="Past economic"):
        control_basis(None, None, None, None, False)
    controls, reason = control_basis(None, None, None, None, True)
    assert controls == [] and reason.startswith("Past economic")
    def unrelated(*args):
        raise ValueError("Controls need completed stock accounts on the exact same source/rule/cost inputs")
    monkeypatch.setattr(continuous_momentum, "saved_stock_controls", unrelated)
    with pytest.raises(ValueError, match="Controls need"):
        control_basis(None, None, None, None, True)
    monkeypatch.setattr(continuous_momentum, "saved_stock_controls", lambda *args: [{"status": "completed"}])
    assert control_basis(None, None, None, None, True) == ([{"status": "completed"}], None)


def test_fraction_policy_cannot_switch_after_credit_or_on_an_unrelated_failure():
    floor = {"fractional_allocation": fractional_allocation_contract("floor")}
    ceil = {"fractional_allocation": fractional_allocation_contract("ceil")}
    checkpoint = {"state": {"events": []}}
    failed = {"error": "ValueError: Fractional/undated bonus shares require reconciliation"}
    check_fractional_continuation({}, floor, checkpoint, failed)
    check_fractional_continuation(floor, floor, checkpoint, failed)
    for old, new, state, case in ((floor, ceil, checkpoint, failed), (floor, {}, checkpoint, failed),
            ({}, floor, checkpoint, {"error": "missing source"}),
            ({}, floor, {"state": {"events": [], "corporate_action_reconciliations": {
                "credit": {"kind": "ordinary_share_allocation"}}}}, failed)):
        with pytest.raises(ValueError, match="fractional-share policy"):
            check_fractional_continuation(old, new, state, case)
