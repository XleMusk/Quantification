"""Synthetic lifecycle checks, not historical performance or admission evidence."""

from copy import deepcopy

import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.engine import ReplayRiskExitIntent, advance
from trading.ledger import Ledger, new_state
from trading.research.shortline_decision_v2 import BATCH_SCHEMA, ShortlineDecisionFeedV2, fixed_exit_policy
from trading.research.shortline_decision_v3 import FiveSessionExpiryPolicy, replay_decisions
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config


A, B = "600001.SSE", "600002.SSE"
CONFIG = _config(100000, 5)


def panel(count=10):
    days = pd.bdate_range("2026-10-15", periods=count).strftime("%Y-%m-%d").tolist()
    bars = pd.DataFrame([
        {"date": day, "symbol": symbol, "open": 10., "high": 10.05, "low": 9.95,
         "close": 10., "preclose_for_limits": 10., "volume": 10_000_000.,
         "amount": 100_000_000., "is_st": False}
        for day in days for symbol in (A, B)])
    base = ShortlinePanelFeed(bars, {}, [A, B])
    base.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""} for symbol in (A, B)}
    return base, days


def batch(day, symbols=(A,)):
    return {"schema": BATCH_SCHEMA, "day": day, "status": "complete", "source": "synthetic_only",
            "candidates": [{"symbol": symbol, "score": 2. - index} for index, symbol in enumerate(symbols)]}


def runner(base, batches):
    feed = ShortlineDecisionFeedV2(base, batches)
    state = new_state(CONFIG.initial_cash)
    target = fixed_exit_policy(feed, state, CONFIG)
    policy = FiveSessionExpiryPolicy()
    def step(index):
        return advance(state, feed, base.calendar[index], CONFIG, index,
                       target_policy=target, replay_risk_exit_policy=policy)
    return feed, state, policy, step


def test_missing_signal_still_expires_on_fifth_actual_holding_session_and_shared_ledger_sells_on_sixth():
    base, days = panel()
    # Missing unheld universe rows do not forbid reducing the known owned book.
    del base.rows[days[5]][B]
    del base.rows[days[6]][B]
    result = replay_decisions(base, {days[0]: batch(days[0])}, config=CONFIG)
    state, daily = result["state"], result["daily"]
    buy = [fill for fill in state["fills"].values() if fill["side"] == "buy"]
    sell = [fill for fill in state["fills"].values() if fill["side"] == "sell"]
    assert buy and {fill["day"] for fill in buy} == {days[1]}
    assert sell and {fill["day"] for fill in sell} == {days[6]}
    assert sum(fill["qty"] for fill in buy) == sum(fill["qty"] for fill in sell)
    assert not any(event["kind"] == "risk_exit_scheduled" for day in daily[:5] for event in day["events"])
    assert any(event["kind"] == "risk_exit_scheduled" for event in daily[5]["events"])
    assert any(event["kind"] == "no_decision" and event["reason"] == "shortline_v2_decision_batch_missing"
               for event in daily[5]["events"])
    assert not any(event["kind"] == "targets_generated" for event in daily[5]["events"])
    assert Ledger(state, CONFIG).quantity(A) == 0
    assert state["cash"] < int(CONFIG.initial_cash * 100)  # Shared fees, no account/cash reset.
    assert state["targets"] == {A: .16}  # Original missing-signal evidence not overwritten by a fake target.
    assert result["execution_admitted"] is False and result["strategy_validated"] is False


def test_mixed_expired_and_recent_holdings_keep_exact_nonexpired_shares_despite_price_change():
    base, days = panel()
    feed, state, policy, step = runner(base, {days[0]: batch(days[0]), days[3]: batch(days[3], (A, B))})
    for index in range(6):
        step(index)
    book = Ledger(state, CONFIG)
    recent_qty = book.quantity(B)
    cash_before = state["cash"]
    assert recent_qty > 0 and book.quantity(A) > 0
    # A weight-based reconstruction here would trade B at its new opening price.
    base.rows[days[6]][B].update(open=10.8, high=10.9, low=10.7, close=10.8)
    step(6)
    assert book.quantity(A) == 0 and book.quantity(B) == recent_qty
    assert state["cash"] > cash_before
    assert not any(order["day"] == days[6] and order["symbol"] == B for order in state["orders"].values())
    assert not any(order["day"] == days[6] and order["side"] == "buy" for order in state["orders"].values())
    execution = [event for event in state["events"] if event["day"] == days[6] and event["kind"] == "risk_exit_execution"]
    assert execution[0]["remaining_quantities"] == {A: 0}


@pytest.mark.parametrize("damage", ["missing", "status_unknown", "amount_unknown"])
def test_current_owned_raw_must_be_known_before_any_mutation(damage):
    base, days = panel()
    feed, state, policy, step = runner(base, {days[0]: batch(days[0])})
    step(0)
    step(1)
    if damage == "missing":
        del base.rows[days[2]][A]
    else:
        base.rows[days[2]][A]["tradestatus" if damage == "status_unknown" else "amount"] = None
    previous = deepcopy(state)
    with pytest.raises(ValueError, match="Unpriceable risk-exit holding"):
        step(2)
    assert state == previous


def test_unknown_exact_next_raw_defers_without_fabricated_exit_and_retries_later_known_session():
    base, days = panel()
    feed, state, policy, step = runner(base, {days[0]: batch(days[0])})
    for index in range(5):
        step(index)
    saved = base.rows[days[6]].pop(A)
    step(5)
    assert state.get("pending_risk_exits") is None
    assert any(event["day"] == days[5] and event["kind"] == "risk_exit_deferred"
               and "raw_or_status_unavailable" in event["reason"] for event in state["events"])
    previous = deepcopy(state)
    with pytest.raises(ValueError, match="Unpriceable risk-exit holding"):
        step(6)
    assert state == previous
    base.rows[days[6]][A] = saved
    step(6)
    assert Ledger(state, CONFIG).quantity(A) > 0
    step(7)
    assert Ledger(state, CONFIG).quantity(A) == 0
    assert {fill["day"] for fill in state["fills"].values() if fill["side"] == "sell"} == {days[7]}


def test_locked_limit_down_and_zero_liquidity_retain_residual_then_clear_once():
    base, days = panel(count=11)
    base.rows[days[6]][A].update(open=9., high=9., low=9., close=9.)
    # Day 7 can be quoted, but the original prior-day participation capacity is zero.
    base.volumes.at[days[6], A] = 0.
    feed, state, policy, step = runner(base, {days[0]: batch(days[0])})
    for index in range(8):
        step(index)
    assert Ledger(state, CONFIG).quantity(A) > 0
    assert not any(fill["side"] == "sell" for fill in state["fills"].values())
    assert any(event["day"] == days[6] and event.get("reason") == "limit_down_queue_unknown"
               for event in state["events"])
    failed_orders = [order for order in state["orders"].values() if order["day"] == days[7] and order["side"] == "sell"]
    assert len(failed_orders) == 1 and failed_orders[0]["filled"] == 0 and failed_orders[0]["status"] == "cancelled"
    step(8)
    assert Ledger(state, CONFIG).quantity(A) == 0
    assert len([order for order in state["orders"].values() if order["day"] == days[8] and order["side"] == "sell"]) == 1
    assert {fill["day"] for fill in state["fills"].values() if fill["side"] == "sell"} == {days[8]}
    before = deepcopy(state)
    with pytest.raises(ValueError, match="twice or backwards"):
        step(8)
    assert state == before


def test_complete_signal_cannot_reenter_during_risk_exit_execution_session_but_can_enter_later():
    base, days = panel()
    feed, state, policy, step = runner(base, {day: batch(day) for day in days})
    for index in range(7):
        step(index)
    assert Ledger(state, CONFIG).quantity(A) == 0
    assert not any(fill["side"] == "buy" and fill["day"] == days[6] for fill in state["fills"].values())
    step(7)
    assert Ledger(state, CONFIG).quantity(A) > 0


def test_expiry_keeps_explicit_ledger_availability_lock_and_retries_after_release():
    base, days = panel()
    feed, state, policy, step = runner(base, {})
    book = Ledger(state, CONFIG)
    # An explicit delayed-availability fill exercises the shared settlement
    # constraint; it is not a claim that ordinary A-share settlement is T+6.
    book.submit("synthetic_locked_buy", days[0], A, "buy", 100, 1000)
    book.fill("synthetic_locked_fill", "synthetic_locked_buy", 100, 1000, days[0], days[6])
    for index in range(6):
        step(index)
    assert book.quantity(A) == 100 and not any(fill["side"] == "sell" for fill in state["fills"].values())
    diagnostic = [event for event in state["events"] if event["day"] == days[5]
                  and event["kind"] == "target_execution_diagnostic" and event["symbol"] == A]
    assert diagnostic[0]["constraint_flags"] == ["sellable_T1"]
    assert diagnostic[0]["remaining_quantity_gap"] == -100
    step(6)
    assert book.quantity(A) == 0
    assert {fill["day"] for fill in state["fills"].values() if fill["side"] == "sell"} == {days[6]}


def test_policy_mode_and_identity_cannot_change_or_disappear_before_mutation():
    base, days = panel()
    feed, state, policy, step = runner(base, {})
    previous = deepcopy(state)
    with pytest.raises(ValueError, match="replay-only"):
        advance(state, feed, days[0], CONFIG, 0, context=RunContext("paper", TradingMode.PAPER),
                replay_risk_exit_policy=policy)
    assert state == previous
    step(0)
    previous = deepcopy(state)
    with pytest.raises(ValueError, match="cannot be removed"):
        advance(state, feed, days[1], CONFIG, 1)
    assert state == previous
    class Different(FiveSessionExpiryPolicy):
        policy_id = "different_policy"
    with pytest.raises(ValueError, match="cannot be changed"):
        advance(state, feed, days[1], CONFIG, 1, replay_risk_exit_policy=Different())
    assert state == previous
    other = new_state(CONFIG.initial_cash)
    advance(other, feed, days[0], CONFIG, 0)
    previous = deepcopy(other)
    with pytest.raises(ValueError, match="cannot be changed"):
        advance(other, feed, days[1], CONFIG, 1, replay_risk_exit_policy=policy)
    assert other == previous


def test_risk_intent_is_typed_next_session_existing_holding_only():
    base, days = panel()
    feed, state, _, _ = runner(base, {})
    class Bad(FiveSessionExpiryPolicy):
        def __call__(self, day, book, feed):
            return ReplayRiskExitIntent(book.context, day, feed.next_day(day), (B,), "synthetic", "unheld")
    with pytest.raises(ValueError, match="nonheld symbol"):
        advance(state, feed, days[0], CONFIG, 0, replay_risk_exit_policy=Bad())


@pytest.mark.parametrize("bad_field", ["context", "decision_day", "delayed_execution", "weight_target"])
def test_public_hook_cannot_change_identity_delay_exit_or_return_weight_targets(bad_field):
    base, days = panel()
    feed, state, _, _ = runner(base, {})
    class Bad(FiveSessionExpiryPolicy):
        def __call__(self, day, book, feed):
            if bad_field == "weight_target":
                from trading.contracts import PortfolioIntent
                return PortfolioIntent.full_target({B: .2})
            return ReplayRiskExitIntent(
                RunContext("another_account", TradingMode.REPLAY) if bad_field == "context" else book.context,
                days[0] if bad_field == "decision_day" else day,
                days[4] if bad_field == "delayed_execution" else feed.next_day(day),
                (A,), "synthetic", "invalid_identity")
    # First use on day 1 leaves a valid decision timestamp for the mismatched-day case.
    class Start(Bad):
        def __call__(self, day, book, feed):
            return None if day == days[0] else super().__call__(day, book, feed)
    policy = Start()
    advance(state, feed, days[0], CONFIG, 0, replay_risk_exit_policy=policy)
    with pytest.raises(ValueError, match="typed share-reduction|account/session identity"):
        advance(state, feed, days[1], CONFIG, 1, replay_risk_exit_policy=policy)
