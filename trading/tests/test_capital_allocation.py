from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import PortfolioIntent, RunContext, TradingMode, decision_time
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.portfolio_allocation import LOT_ALLOCATION, OPEN_LOT_ALLOCATION, allocate_close_lots, allocate_open_lots
from trading.research.experiment import load_replay_checkpoint, replay
from trading.rules import FUND_Q3_2026_PROFILES


DAY, NEXT = "2025-01-27", "2025-02-05"
SYMBOLS = ("600000.SSE", "000001.SZSE", "600001.SSE")
CONTEXT = RunContext("lot_test", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=3, retain_rank=3, gross_limit=.9, single_limit=.5)


def intent(weights):
    return PortfolioIntent.full_target(weights, context=CONTEXT, decided_at=decision_time(DAY),
                                       execution_not_before=NEXT, source="fixed_test")


def make_feed():
    quotes = {s: {"open": price, "close": price, "preclose": price, "tradestatus": 1, "isST": 0}
              for s, price in zip(SYMBOLS, (23., 27., 33.))}
    return SimpleNamespace(calendar=[DAY, NEXT], rows={DAY: deepcopy(quotes), NEXT: deepcopy(quotes)},
                           instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in SYMBOLS},
                           actions=[], volumes=pd.DataFrame(1000000., index=[DAY, NEXT], columns=SYMBOLS),
                           next_day=lambda day: NEXT if day == DAY else "9999-12-31",
                           scores=lambda day, cfg: [{"symbol": s, "score": float(3 - i)} for i, s in enumerate(SYMBOLS)])


def test_nearest_lots_reduce_tracking_error_without_new_candidates_or_spending_cash_reserve():
    feed = make_feed()
    book = Ledger(new_state(10000), CONFIG, CONTEXT)
    original = intent({s: .3 for s in SYMBOLS})
    result = allocate_close_lots(original, book, DAY, feed.rows[DAY])
    assert dict(result.weights) == dict(zip(SYMBOLS, (.23, .27, .33)))
    assert result.context == original.context and result.execution_not_before == NEXT
    event = book.state["events"][-1]
    assert event["allocated_stock_cny"] == 8300 <= event["stock_budget_cny"] == 9000
    assert event["squared_error_after"] < event["squared_error_before"]
    assert book.state["cash"] == 1000000 and not book.state["orders"]


def test_unaffordable_lot_and_STAR_minimum_respect_cap():
    book = Ledger(new_state(10000), replace(CONFIG, single_limit=.3), CONTEXT)
    result = allocate_close_lots(intent({s: .3 for s in SYMBOLS}), book, DAY, make_feed().rows[DAY])
    assert dict(result.weights)[SYMBOLS[2]] == 0
    star = "688001.SSE"
    result = allocate_close_lots(intent({star: .3}), book, DAY, {star: {"close": 20.}})
    assert dict(result.weights)[star] == 0


def test_fund_sleeves_untouched_and_unknown_close_not_zeroed():
    config = replace(CONFIG, initial_cash=140000, single_limit=.2, gross_limit=.8)
    fund = "511010.SSE"
    book = Ledger(new_state(config.initial_cash), config, CONTEXT, instrument_profiles=FUND_Q3_2026_PROFILES)
    result = allocate_close_lots(intent({fund: .2, SYMBOLS[0]: .02}), book, DAY, make_feed().rows[DAY])
    assert dict(result.weights)[fund] == .2
    with pytest.raises(ValueError, match="Unavailable raw allocation close"):
        allocate_close_lots(intent({SYMBOLS[0]: .02}), book, DAY, {})


def test_current_close_not_future_open_and_no_same_session_order():
    first, second = make_feed(), make_feed()
    second.rows[NEXT][SYMBOLS[0]]["open"] = 25.
    states = [new_state(CONFIG.initial_cash) for _ in range(2)]
    for state, feed in zip(states, (first, second)):
        advance(state, feed, DAY, CONFIG, 0, context=CONTEXT, lot_allocation=LOT_ALLOCATION)
        assert not state["orders"]
    assert states[0]["pending_targets"] == states[1]["pending_targets"]
    assert states[0]["events"] == states[1]["events"]


def test_full_exit_is_not_removed_by_lot_or_fee_policy():
    feed = make_feed()
    feed.scores = lambda day, cfg: []
    state = new_state(CONFIG.initial_cash)
    state["positions"][SYMBOLS[0]] = [{"qty": 125, "available": DAY}]
    advance(state, feed, DAY, CONFIG, 0, context=CONTEXT, lot_allocation=LOT_ALLOCATION, commission_deadband=True)
    assert state["pending_targets"] == {}
    advance(state, feed, NEXT, CONFIG, 1, context=CONTEXT, lot_allocation=LOT_ALLOCATION, commission_deadband=True)
    assert Ledger(state, CONFIG).quantity(SYMBOLS[0]) == 0


def test_paper_reduction_and_mid_account_policy_change_rejected():
    feed, state = make_feed(), new_state(CONFIG.initial_cash)
    with pytest.raises(ValueError, match="replay-only"):
        advance(state, feed, DAY, CONFIG, 0, context=RunContext("paper", TradingMode.PAPER), lot_allocation=LOT_ALLOCATION)
    with pytest.raises(ValueError, match="reduction-only"):
        advance(state, feed, DAY, CONFIG, 0, lot_allocation=LOT_ALLOCATION, target_policy=lambda value: value)
    advance(state, feed, DAY, CONFIG, 0, lot_allocation=LOT_ALLOCATION)
    with pytest.raises(ValueError, match="cannot change"):
        advance(state, feed, NEXT, CONFIG, 1)


def test_replay_checkpoint_retains_policy_and_cannot_resume_as_default(tmp_path):
    feed, folder = make_feed(), tmp_path / "case"
    replay(feed, CONFIG, [DAY, NEXT], folder, lot_allocation=LOT_ALLOCATION)
    checkpoint = load_replay_checkpoint(folder, CONFIG, [DAY, NEXT], lot_allocation=LOT_ALLOCATION)
    assert checkpoint["state"]["lot_allocation"] == LOT_ALLOCATION
    with pytest.raises(ValueError, match="allocation changed"):
        load_replay_checkpoint(folder, CONFIG, [DAY, NEXT])


def test_first_session_failure_preserves_policy_for_zero_length_checkpoint(tmp_path):
    feed, folder = make_feed(), tmp_path / "failed"
    del feed.rows[DAY][SYMBOLS[0]]
    with pytest.raises(ValueError, match="Unavailable raw allocation close"):
        replay(feed, CONFIG, [DAY, NEXT], folder, lot_allocation=LOT_ALLOCATION)
    checkpoint = load_replay_checkpoint(folder, CONFIG, [DAY, NEXT], lot_allocation=LOT_ALLOCATION)
    assert checkpoint["daily"] == [] and checkpoint["state"]["lot_allocation"] == LOT_ALLOCATION


def test_open_allocation_keeps_prior_weights_and_uses_no_future_close():
    first, second = make_feed(), make_feed()
    for feed in (first, second):
        feed.rows[NEXT][SYMBOLS[0]]["open"] = 23.10
    second.rows[NEXT][SYMBOLS[0]]["close"] = 50
    states = [new_state(CONFIG.initial_cash) for _ in range(2)]
    for state, feed in zip(states, (first, second)):
        advance(state, feed, DAY, CONFIG, 0, context=CONTEXT, lot_allocation=OPEN_LOT_ALLOCATION)
        assert state["pending_targets"] == {s: .3 for s in SYMBOLS}
        assert not state["orders"] and not any(e["kind"] == "close_lot_allocation" for e in state["events"])
        advance(state, feed, NEXT, CONFIG, 1, context=CONTEXT, lot_allocation=OPEN_LOT_ALLOCATION)
        event = next(e for e in state["events"] if e["kind"] == "open_lot_allocation")
        assert event["estimates"][0]["quantity_at_execution_mark"] == 100
        assert event["allocated_stock_cny"] <= event["stock_budget_cny"]
        assert state["cash"] >= 0 and all(o["qty"] == 100 for o in state["orders"].values())
    assert states[0]["orders"] == states[1]["orders"] and states[0]["fills"] == states[1]["fills"]


def test_open_targets_still_obey_T1_missing_quotes_and_original_risk_checks():
    feed, state = make_feed(), new_state(CONFIG.initial_cash)
    advance(state, feed, DAY, CONFIG, 0, context=CONTEXT, lot_allocation=OPEN_LOT_ALLOCATION)
    feed.rows[NEXT][SYMBOLS[0]]["open"] = 0
    advance(state, feed, NEXT, CONFIG, 1, context=CONTEXT, lot_allocation=OPEN_LOT_ALLOCATION)
    assert not any(o["symbol"] == SYMBOLS[0] for o in state["orders"].values())
    trace = next(e for e in state["events"] if e["kind"] == "target_execution_diagnostic" and e["symbol"] == SYMBOLS[0])
    assert not trace["has_execution_open"] and "missing_quote" in trace["constraint_flags"]
    held = new_state(CONFIG.initial_cash)
    held["positions"][SYMBOLS[0]] = [{"qty": 125, "available": "2025-02-06"}]
    held["pending_targets"] = {}
    advance(held, feed, NEXT, CONFIG, 1, context=CONTEXT, lot_allocation=OPEN_LOT_ALLOCATION)
    assert Ledger(held, CONFIG).quantity(SYMBOLS[0]) == 125 and not held["orders"]


def test_open_policy_is_replay_only_and_checkpoint_cannot_be_relabelled(tmp_path):
    feed, folder = make_feed(), tmp_path / "open"
    replay(feed, CONFIG, [DAY, NEXT], folder, lot_allocation=OPEN_LOT_ALLOCATION)
    assert load_replay_checkpoint(folder, CONFIG, [DAY, NEXT], lot_allocation=OPEN_LOT_ALLOCATION)["state"]["lot_allocation"] == OPEN_LOT_ALLOCATION
    with pytest.raises(ValueError, match="allocation changed"):
        load_replay_checkpoint(folder, CONFIG, [DAY, NEXT], lot_allocation=LOT_ALLOCATION)
    book = Ledger(new_state(10000), CONFIG, RunContext("paper", TradingMode.PAPER))
    with pytest.raises(ValueError, match="replay-only"):
        allocate_open_lots({SYMBOLS[0]: .3}, book, DAY, feed.rows[DAY])
