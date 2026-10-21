"""Evening close intents reuse the next opening's existing Engine/Ledger path."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine
from trading.config import StrategyConfig
from trading.contracts import IntentKind, PortfolioIntent, RunContext, SignalUnavailable, TradingMode
from trading.ledger import Ledger, new_state

D, E, F = "2025-01-06", "2025-01-07", "2025-01-08"
A, B = "600000.SSE", "000001.SZSE"
CONTEXT = RunContext("explicit_evening_case", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=1, retain_rank=2, gross_limit=.8, single_limit=.8,
                        rebalance_sessions=1, slippage_bps=0)


@pytest.fixture(autouse=True)
def fixed_actual_clock(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(F + "T20:00:00+08:00"))


def feed():
    days = ["2025-01-03", D, E, F]  # Actual prior-volume context for D's old pending target.
    row = {"open": 10., "close": 10., "preclose": 10., "tradestatus": 1, "isST": 0}
    calls = []
    def scores(day, config):
        calls.append(day)
        return SignalUnavailable("no_new_model_publication")
    return SimpleNamespace(calendar=days, rows={day: {symbol: deepcopy(row) for symbol in (A, B)} for day in days},
        volumes=pd.DataFrame(1_000_000., index=days, columns=[A, B]),
        instruments={symbol: {"ipoDate": "2000-01-01", "outDate": ""} for symbol in (A, B)},
        actions=[], next_day=lambda day: days[days.index(day) + 1] if day != F else "2025-01-09",
        scores=scores, calls=calls)


def intent(weights=None, **changes):
    value = PortfolioIntent.full_target({B: .8} if weights is None else weights, context=CONTEXT,
        decided_at=D + "T18:45:00+08:00", execution_not_before=E, source="saved_publication:sha256:test")
    return replace(value, **changes)


def step(state, base, day, number, **options):
    return engine.advance(state, base, day, CONFIG, number, context=CONTEXT, **options)


def test_evening_intent_does_not_trade_D_open_and_checkpoint_executes_E_once_with_fees_T1():
    state, base = new_state(10000), feed()
    daily = step(state, base, D, 0, published_close_intent=intent())
    assert not state["orders"] and base.calls == [] and state["pending_targets"] == {B: .8}
    receipt = state["pending_published_close_intent"]
    assert receipt["intent"] == intent().to_dict() and receipt["decision_at"] == D + "T18:45:00+08:00"
    assert daily["event_time_frontier"] is None and daily["published_close_intent_frontier"] == receipt
    assert receipt["source_availability_verified_by_engine"] is False
    # JSON is the real checkpoint boundary, including the typed pending clock.
    restored = json.loads(json.dumps(state))
    result = step(restored, base, E, 1)
    book = Ledger(restored, CONFIG, CONTEXT)
    order = restored["orders"][E + ":" + B + ":buy"]
    assert order["status"] == "filled" and order["qty"] == 800 and result["new_orders"] == 1
    assert order["fees"] == 508 and restored["cash"] == 199492
    assert book.available(B, E) == 0 and book.available(B, F) == 800
    assert "pending_published_close_intent" not in restored and restored["pending_targets"] is None
    assert result["consumed_published_close_intent"] == receipt
    assert sum(event["kind"] == "published_close_intent_consumed" for event in restored["events"]) == 1
    before = deepcopy(restored)
    with pytest.raises(ValueError, match="twice"):
        step(restored, base, E, 1)
    assert restored == before


def test_D_open_executes_only_old_pending_then_evening_target_waits_for_E():
    state, base = new_state(10000), feed()
    state["pending_targets"] = {A: .4}
    step(state, base, D, 0, published_close_intent=intent())
    book = Ledger(state, CONFIG, CONTEXT)
    assert book.quantity(A) == 400 and book.quantity(B) == 0
    assert all(order["symbol"] == A for order in state["orders"].values())
    step(state, base, E, 1)
    assert book.quantity(A) == 0 and book.quantity(B) > 0


def held():
    value = new_state(9000)
    value["positions"][A] = [{"qty": 100, "available": D}]
    value["prices"][A] = 1000
    return value


def test_unknown_NO_CHANGE_holds_but_known_empty_full_target_exits_only_next_day():
    no_change = PortfolioIntent.no_change("unknown_coverage", context=CONTEXT, decided_at=D + "T18:45:00+08:00",
        execution_not_before=E, source="saved_unknown_publication")
    state, base = held(), feed()
    daily = step(state, base, D, 0, published_close_intent=no_change)
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 100 and state["pending_targets"] is None
    assert "pending_published_close_intent" not in state and not state["orders"]
    assert daily["published_close_intent_frontier"]["intent"]["kind"] == "NO_CHANGE"
    step(state, base, E, 1)
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 100 and not state["orders"]
    state, base = held(), feed()
    step(state, base, D, 0, published_close_intent=intent({}))
    assert state["pending_targets"] == {} and not state["orders"]
    step(state, base, E, 1)
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 0


@pytest.mark.parametrize("bad", [
    lambda: intent(context=None),
    lambda: intent(context=RunContext("other", TradingMode.REPLAY)),
    lambda: intent(context=RunContext("explicit_evening_case", TradingMode.PAPER)),
    lambda: intent(source=None), lambda: intent(decided_at=None), lambda: intent(execution_not_before=None),
    lambda: intent(decided_at=D + "T15:04:59+08:00"),
    lambda: intent(decided_at="2025-01-05T18:45:00+08:00"),
    lambda: intent(execution_not_before=F),
    lambda: PortfolioIntent(IntentKind.DELTA, ((B, .1),), context=CONTEXT, decided_at=D + "T18:45:00+08:00",
                            execution_not_before=E, source="delta"),
    lambda: intent({B: .9}),
])
def test_invalid_published_identity_clock_kind_caps_rejected_before_mutation(bad):
    state, base = held(), feed()
    before = deepcopy(state)
    with pytest.raises(ValueError):
        step(state, base, D, 0, published_close_intent=bad())
    assert state == before and base.calls == []


def test_future_decision_rejected_at_actual_clock_before_any_mutation(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(D + "T18:00:00+08:00"))
    state, before = held(), held()
    with pytest.raises(ValueError, match="clock"):
        step(state, feed(), D, 0, published_close_intent=intent())
    assert state == before


@pytest.mark.parametrize("option", [{"target_policy": lambda value: value}, {"portfolio_policy": lambda value: value},
                                   {"lot_allocation": engine.LOT_ALLOCATION}])
def test_overlay_conflicts_rejected_before_mutation(option):
    state = held(); before = deepcopy(state)
    with pytest.raises(ValueError, match="overlays"):
        step(state, feed(), D, 0, published_close_intent=intent(), **option)
    assert state == before


def test_off_due_and_paper_mode_rejected_before_mutation():
    for config, context, number in [(replace(CONFIG, rebalance_sessions=2), CONTEXT, 1),
                                    (CONFIG, RunContext(CONTEXT.account_id, TradingMode.PAPER), 0)]:
        state = held(); before = deepcopy(state)
        with pytest.raises(ValueError):
            engine.advance(state, feed(), D, config, number, context=context, published_close_intent=intent())
        assert state == before


@pytest.mark.parametrize("tamper", [lambda state: state["pending_targets"].update({B: .7}),
    lambda state: state["pending_published_close_intent"].update(decision_at=D + "T19:00:00+08:00"),
    lambda state: state["pending_published_close_intent"]["intent"].update(context=RunContext("other", TradingMode.REPLAY).to_dict())])
def test_pending_receipt_or_weights_tampering_rejected_before_open_mutation(tamper):
    state, base = new_state(10000), feed()
    step(state, base, D, 0, published_close_intent=intent())
    tamper(state); before = deepcopy(state)
    with pytest.raises(ValueError):
        step(state, base, E, 1)
    assert state == before


def test_pending_cannot_roll_over_skipped_exact_execution_session():
    state, base = new_state(10000), feed()
    step(state, base, D, 0, published_close_intent=intent())
    before = deepcopy(state)
    with pytest.raises(ValueError, match="skip"):
        step(state, base, F, 2)
    assert state == before


def test_legacy_default_state_events_and_daily_remain_exactly_identical():
    first, second = held(), held()
    left = step(first, feed(), D, 0)
    right = step(second, feed(), D, 0, published_close_intent=None)
    assert first == second and left == right
    assert "pending_published_close_intent" not in first and "published_close_intent_frontier" not in left
