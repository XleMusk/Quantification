from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import (PortfolioIntent, RunContext, SecurityId, Signal, SignalFrame, SignalKind,
                               SignalTime, TradingMode, decision_time)
from trading.engine import advance
from trading.expected_returns import EqualSlotReturnPolicy, checked_return_intent
from trading.ledger import Ledger, new_state
from trading.research.experiment import replay, load_replay_checkpoint
from trading.research.family_ridge import training_rows
from trading.research.return_cost_replay import fit_return_training, SavedReturnFeed


DAY, NEXT = "2025-01-27", "2025-02-05"
A, B = "600000.SSE", "000001.SZSE"
CONTEXT = RunContext("standalone_replay", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=1, retain_rank=2, gross_limit=.8, single_limit=.8,
                        rebalance_sessions=1)


def frame(values, day=DAY, kind=SignalKind.EXPECTED_RETURN, horizon=20):
    stamp = SignalTime(day + "T15:00:00+08:00", decision_time(day))
    return SignalFrame(day, tuple(Signal(SecurityId(s), v, kind, stamp, horizon_sessions=horizon)
                                  for s, v in values.items()), "test_return", CONTEXT)


def feed(values):
    row = {"open": 10., "close": 10., "preclose": 10., "tradestatus": 1, "isST": 0}
    return SimpleNamespace(calendar=[DAY, NEXT], rows={d: {s: deepcopy(row) for s in (A, B)} for d in (DAY, NEXT)},
                           instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in (A, B)}, actions=[],
                           volumes=pd.DataFrame(1000000., index=[DAY, NEXT], columns=[A, B]),
                           next_day=lambda day: NEXT if day == DAY else "9999-12-31",
                           scores=lambda day, config: frame(values, day))


def held_state():
    state = new_state(2000)
    state["positions"][A] = [{"qty": 800, "available": DAY}]
    state["prices"][A] = 1000
    return state


def test_cost_selector_keeps_tiny_edge_incumbent_but_allows_large_edge_switch():
    raw, net = EqualSlotReturnPolicy(False), EqualSlotReturnPolicy(True)
    for values, expected in (({A: .01, B: .0101}, A), ({A: .01, B: .03}, B)):
        base = feed(values)
        raw_intent = raw(frame(values), Ledger(held_state(), CONFIG), base, CONFIG)
        state = held_state()
        net_intent = net(frame(values), Ledger(state, CONFIG), base, CONFIG)
        assert dict(raw_intent.weights) == {B: .8}
        assert dict(net_intent.weights) == {expected: .8}
        assert not state["orders"] and not state["fills"]
        assert state["events"][-1]["estimate_is_execution"] is False


@pytest.mark.parametrize("expensive,price", [(B, 1000.), ("688183.SSE", 60.)])
@pytest.mark.parametrize("cost_aware", [False, True])
def test_unbuyable_target_has_zero_forecast_benefit_not_free_positive_alpha(expensive, price, cost_aware):
    values = {A: .01, expensive: .99}
    base = feed(values)
    base.rows[DAY][expensive] = {**base.rows[DAY][B], "close": price}
    state = new_state(10000)
    intent = EqualSlotReturnPolicy(cost_aware)(frame(values), Ledger(state, CONFIG), base, CONFIG)
    assert dict(intent.weights) == {A: .8}
    event = state["events"][-1]
    # The return-only frontier persists the expensive row only if selected/held;
    # compare its sole-symbol policy directly to expose its zero-capacity receipt.
    one = new_state(10000)
    EqualSlotReturnPolicy(cost_aware)(frame({expensive: .99}), Ledger(one, CONFIG), base, CONFIG)
    row = one["events"][-1]["diagnostics"][0]
    assert row["close_legal_position_quantity"] == row["close_legal_notional_cents"] == 0
    assert row["forecast_relative_benefit_cents"] == 0
    assert event["selected_zero_legal_quantity"] == 0


def test_return_intent_uses_close_not_future_open_and_only_next_session_executes():
    first, changed = feed({A: .01, B: .0101}), feed({A: .01, B: .0101})
    changed.rows[NEXT][B]["open"] = 1000
    states = [held_state(), held_state()]
    for state, base in zip(states, (first, changed)):
        advance(state, base, DAY, CONFIG, 0, portfolio_policy=EqualSlotReturnPolicy(True))
        assert not state["orders"] and state["pending_targets"] == {A: .8}
    assert states[0] == states[1]
    empty = new_state(10000)
    advance(empty, first, DAY, CONFIG, 0, portfolio_policy=EqualSlotReturnPolicy(True))
    assert not empty["orders"]
    advance(empty, first, NEXT, CONFIG, 1, portfolio_policy=EqualSlotReturnPolicy(True))
    assert empty["orders"] and all(order["day"] == NEXT for order in empty["orders"].values())
    assert empty["cash"] >= 0 and all(lot["available"] > NEXT for lots in empty["positions"].values() for lot in lots)


def test_saved_full_pool_consumer_emits_typed_returns_not_numeric_diagnostics_text():
    base = feed({A: .01, B: .02})
    base.symbols = [A, B]
    predictions = pd.DataFrame({"expected_return": [.01, .02]},
                              index=pd.MultiIndex.from_tuples([(pd.Timestamp(DAY), B), (pd.Timestamp(DAY), A)],
                                                              names=["date", "symbol"])).sort_index()
    saved = SavedReturnFeed(base, predictions, CONFIG, [DAY, NEXT])
    signals = saved.signal_frame(DAY, CONFIG, CONTEXT)
    assert len(signals.signals) == 2
    assert all(s.kind == SignalKind.EXPECTED_RETURN and s.horizon_sessions == 20 for s in signals.signals)


def test_departed_holding_cannot_gain_a_forecast_and_its_exit_cost_is_recorded():
    state, base = held_state(), feed({B: .02})
    advance(state, base, DAY, CONFIG, 0, portfolio_policy=EqualSlotReturnPolicy(True))
    assert state["pending_targets"] == {B: .8}
    event = next(row for row in state["events"] if row["kind"] == "expected_return_selection")
    assert event["departed_holding_symbols"] == [A] and event["estimated_departed_exit_cost_cents"] > 0
    advance(state, base, NEXT, CONFIG, 1, portfolio_policy=EqualSlotReturnPolicy(True))
    assert Ledger(state, CONFIG).quantity(A) == 0


def test_score_horizon_paper_and_missing_price_fail_closed():
    for invalid in (frame({A: .2}, kind=SignalKind.SCORE), frame({A: .2}, horizon=5)):
        base, state = feed({A: .2}), held_state()
        base.scores = lambda day, cfg: invalid
        before = deepcopy(state)
        with pytest.raises(ValueError, match="horizon"):
            advance(state, base, DAY, CONFIG, 0, portfolio_policy=EqualSlotReturnPolicy(True))
        assert state == before
    with pytest.raises(ValueError, match="replay-only"):
        advance(new_state(10000), feed({A: .2}), DAY, CONFIG, 0,
                context=RunContext("paper", TradingMode.PAPER), portfolio_policy=EqualSlotReturnPolicy(True))
    bad = feed({A: .2})
    bad.rows[DAY][A]["close"] = float("nan")
    with pytest.raises(ValueError, match="Unknown decision quote"):
        EqualSlotReturnPolicy(True)(frame({A: .2}), Ledger(new_state(10000), CONFIG), bad, CONFIG)


def test_bad_intent_and_changed_resume_contract_rejected(tmp_path):
    batch = frame({A: .2})
    for targets in ({A: .9}, {B: .5}):
        invalid = PortfolioIntent.full_target(targets, context=CONTEXT, decided_at=decision_time(DAY), execution_not_before=NEXT)
        with pytest.raises(ValueError, match="limits"):
            checked_return_intent(batch, invalid, CONFIG, NEXT)
    base, policy = feed({A: .02}), EqualSlotReturnPolicy(True)
    folder = tmp_path / "replay"
    replay(base, CONFIG, [DAY, NEXT], folder, portfolio_policy=policy, portfolio_policy_contract=policy.contract(CONFIG))
    loaded = load_replay_checkpoint(folder, CONFIG, [DAY, NEXT], portfolio_policy_contract=policy.contract(CONFIG))
    assert len(loaded["daily"]) == 2
    with pytest.raises(ValueError, match="contract changed"):
        load_replay_checkpoint(folder, CONFIG, [DAY, NEXT], portfolio_policy_contract=EqualSlotReturnPolicy(False).contract(CONFIG))


def test_return_training_is_not_rank_target_and_label_maturity_is_required():
    calendar = pd.bdate_range("2024-01-02", "2025-02-28")
    months = calendar.to_series().groupby(calendar.to_period("M")).max().to_list()
    rng = np.random.default_rng(7)
    features = pd.concat({day: pd.DataFrame(rng.normal(size=(100, 3)), columns=["risk", "trend", "volume_stability"],
                                          index=pd.Index(range(100), name="symbol")) for day in months}, names=["date", "symbol"])
    labels = pd.DataFrame(rng.normal(size=(len(calendar), 100)) / 10, index=calendar)
    train, counts = training_rows(features, labels, calendar)
    model, target = fit_return_training(train, counts, calendar)
    assert target.groupby(level="date").mean().abs().max() < 1e-15
    assert not np.allclose(target, train.target_rank)
    scaled = train.copy()
    scaled["forward_return"] *= 10
    other, _ = fit_return_training(scaled, counts, calendar)
    np.testing.assert_allclose(other.coef_, model.coef_ * 10)
    changed = counts.copy()
    changed.loc[changed.labeled_rows.gt(0), "exit_date"] = pd.Timestamp("2025-01-02")
    with pytest.raises(ValueError, match="matured"):
        fit_return_training(train, changed, calendar)
