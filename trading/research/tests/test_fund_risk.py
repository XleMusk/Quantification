from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import PortfolioIntent, RunContext, TradingMode, decision_time
from trading.engine import advance, allocate_replay_target, reduce_replay_target
from trading.ledger import new_state
from trading.research.fund_risk import FundRiskFeed, equal_risk_weights, risk_table, static_training_allocation

DAY, NEXT = "2024-01-31", "2024-02-01"
SYMBOLS = ("600000.SSE", "000001.SZSE")
CONTEXT = RunContext("risk-test", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, gross_limit=.8,
                        single_limit=.8, signal="equal_weight", rebalance_sessions=1).validate()


def intent():
    return PortfolioIntent.full_target(dict.fromkeys(SYMBOLS, .4), context=CONTEXT,
        decided_at=decision_time(DAY), execution_not_before=NEXT, source="research:risk")


def test_erc_matches_analytic_inverse_volatility_only_when_correlations_are_common():
    covariance = np.diag([.01, .04, .09])
    weights, shares = equal_risk_weights(covariance)
    assert weights == pytest.approx([6 / 11, 3 / 11, 2 / 11], abs=1e-5)
    assert shares == pytest.approx([1 / 3] * 3, abs=1e-5)
    correlation = np.array([[1, .8, .1], [.8, 1, .2], [.1, .2, 1]])
    weights, shares = equal_risk_weights(correlation)
    assert not np.allclose(weights, [1 / 3] * 3, atol=.01)
    assert shares == pytest.approx([1 / 3] * 3, abs=1e-5)


@pytest.mark.parametrize("covariance", [np.zeros((3, 3)), [[1, 2], [2, 1]], [[1, np.nan], [np.nan, 1]]])
def test_invalid_covariance_is_not_replaced_with_equal_weights(covariance):
    with pytest.raises(ValueError, match="covariance"):
        equal_risk_weights(covariance)


def test_covariance_allocations_do_not_use_future_prices_or_cash():
    days = pd.bdate_range("2023-01-02", "2024-03-01").strftime("%Y-%m-%d").tolist()
    rng = np.random.default_rng(17)
    prices = np.exp(rng.normal(0, .01, (len(days), 3)).cumsum(axis=0)) * 10
    symbols = ("510300.SSE", "511010.SSE", "518880.SSE")
    raw = pd.DataFrame([{"date": day, "symbol": symbol, "close": prices[i, j]}
                        for i, day in enumerate(days) for j, symbol in enumerate(symbols)])
    base = SimpleNamespace(raw=raw, actions=[], calendar=days)
    first = risk_table(base, symbols, DAY)
    base.raw.loc[base.raw.date.gt(DAY), "close"] *= 500
    base.actions = [{"symbol": symbols[0], "dividOperateDate": NEXT, "dividCashPsBeforeTax": "1"}]
    pd.testing.assert_frame_equal(first, risk_table(base, symbols, DAY))
    assert first.observations.eq(252).all() and first.window_last.eq(DAY).all()


def test_static_2023_calibration_never_borrows_evaluation_returns_or_cash():
    days = pd.bdate_range("2023-01-03", "2024-03-01").strftime("%Y-%m-%d").tolist()
    symbols = ("510300.SSE", "511010.SSE", "518880.SSE")
    rng = np.random.default_rng(29)
    prices = np.exp(rng.normal(0, .01, (len(days), 3)).cumsum(axis=0)) * 10
    base = SimpleNamespace(calendar=days, actions=[], raw=pd.DataFrame([
        {"date": day, "symbol": symbol, "close": prices[i, j]}
        for i, day in enumerate(days) for j, symbol in enumerate(symbols)]))
    calibration = static_training_allocation(base, symbols)
    assert calibration["window_last"] == "2023-12-29"
    assert calibration["observations"] == sum(day < "2024-01-01" for day in days) - 1
    assert calibration["uses_evaluation_returns"] is False
    base.raw.loc[base.raw.date.ge("2024-01-01"), "close"] *= 500
    base.actions = [{"symbol": symbols[0], "dividOperateDate": NEXT, "dividCashPsBeforeTax": "999"}]
    assert static_training_allocation(base, symbols) == calibration
    base.calendar = base.calendar[1:]
    with pytest.raises(ValueError, match="complete2023"):
        static_training_allocation(base, symbols)


def test_static_and_bond_controls_keep_declared_targets_without_covariance_feedback():
    symbols = ("510300.SSE", "511010.SSE", "518880.SSE")
    base = SimpleNamespace(calendar=[DAY, NEXT, "2024-02-29", "2024-03-01"],
                           manifest={"end": "2024-03-01"})
    table = pd.DataFrame([{"date": day, "symbol": symbol, "weight_fraction": fraction}
                          for day, fractions in [(DAY, [.1, .2, .7]), ("2024-02-29", [.7, .2, .1])]
                          for symbol, fraction in zip(symbols, fractions)])
    calibration = {"window_last": "2023-12-29", "weight_fractions": dict(zip(symbols, [.2, .5, .3]))}
    fixed = FundRiskFeed(base, table, "static_2023_equal_risk", symbols, "2024-03-01", static_allocation=calibration)
    bond = FundRiskFeed(base, table, "sovereign_bond_sleeve", symbols, "2024-03-01")
    dynamic = FundRiskFeed(base, table, "monthly_equal_risk", symbols, "2024-03-01")
    config = replace(CONFIG, holdings=3, retain_rank=3)
    target = PortfolioIntent.full_target(dict.fromkeys(symbols, .8 / 3), context=CONTEXT,
                                        decided_at=decision_time(DAY), execution_not_before=NEXT)
    for day, following in [(DAY, NEXT), ("2024-02-29", "2024-03-01")]:
        target = replace(target, decided_at=decision_time(day), execution_not_before=following)
        assert dict(allocate_replay_target(target, fixed.allocate, config).weights) == pytest.approx(
            dict(zip(symbols, [.16, .4, .24])))
        assert dict(allocate_replay_target(target, bond.allocate, config).weights) == dict(zip(symbols, [0., .8, 0.]))
    assert dict(dynamic.allocate(target).weights) == pytest.approx(dict(zip(symbols, [.56, .16, .08])))
    for calibration in (None, {"window_last": DAY, "weight_fractions": dict.fromkeys(symbols, 1 / 3)}):
        with pytest.raises(ValueError, match="before replay"):
            FundRiskFeed(base, table, "static_2023_equal_risk", symbols, "2024-03-01", static_allocation=calibration)


def test_explicit_allocation_can_redistribute_without_weakening_reduction_overlay():
    policy = lambda target: replace(target, weights=((SYMBOLS[0], .2), (SYMBOLS[1], .6)))
    assert dict(allocate_replay_target(intent(), policy, CONFIG).weights)[SYMBOLS[1]] == .6
    with pytest.raises(ValueError, match="only reduce"):
        reduce_replay_target(intent(), policy)


@pytest.mark.parametrize("change", [
    {"weights": (("600001.SSE", .4),)}, {"weights": ((SYMBOLS[0], .81),)},
    {"weights": ((SYMBOLS[0], .6), (SYMBOLS[1], .3))},
    {"context": RunContext("other", TradingMode.REPLAY)}, {"execution_not_before": "2024-02-02"},
])
def test_allocation_identity_universe_next_session_and_caps_are_enforced(change):
    with pytest.raises(ValueError):
        allocate_replay_target(intent(), lambda target: replace(target, **change), CONFIG)


def test_allocation_executes_in_the_sole_ledger_next_session_and_invalid_input_is_atomic():
    row = {"open": 10., "close": 10., "preclose": 10., "isST": 0, "tradestatus": 1}
    feed = SimpleNamespace(calendar=[DAY, NEXT], symbols=SYMBOLS,
        rows={day: {s: dict(row) for s in SYMBOLS} for day in (DAY, NEXT)}, actions=[],
        instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in SYMBOLS},
        volumes=pd.DataFrame(1000000., index=[DAY, NEXT], columns=SYMBOLS), manifest={},
        next_day=lambda d: NEXT if d == DAY else "9999-12-31",
        scores=lambda day, config: [{"symbol": s, "score": 1.} for s in SYMBOLS])
    state = new_state(CONFIG.initial_cash)
    before = deepcopy(state)
    bad = lambda target: replace(target, weights=(("600001.SSE", .4),))
    with pytest.raises(ValueError):
        advance(state, feed, DAY, CONFIG, 0, portfolio_policy=bad)
    assert state == before
    with pytest.raises(ValueError, match="replay-only"):
        advance(state, feed, DAY, CONFIG, 0, context=RunContext("paper", TradingMode.PAPER), portfolio_policy=bad)
    assert state == before
    policy = lambda target: replace(target, weights=((SYMBOLS[0], .2), (SYMBOLS[1], .6)))
    advance(state, feed, DAY, CONFIG, 0, portfolio_policy=policy)
    assert not state["fills"] and state["pending_targets"] == dict(zip(SYMBOLS, [.2, .6]))
    advance(state, feed, NEXT, CONFIG, 1, portfolio_policy=policy)
    assert all(fill["day"] == NEXT for fill in state["fills"].values())
    assert sum(lot["qty"] for lot in state["positions"][SYMBOLS[1]]) > sum(
        lot["qty"] for lot in state["positions"][SYMBOLS[0]])
