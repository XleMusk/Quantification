"""Explicit preopen/full-exit combination through the shared Engine/Ledger."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading import engine
from trading.config import StrategyConfig
from trading.contracts import PortfolioIntent, PreopenPortfolioIntent, RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config
from trading.target_exit_lifecycle import FULL_EXIT_LIFECYCLE, PREOPEN_FULL_EXIT_LIFECYCLE


A, B = "600001.SSE", "600002.SSE"
DAYS = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]
CONFIG = _config(100000, 5)
CONTEXT = RunContext("new_preopen_full_exit_research", TradingMode.REPLAY)


class NoCloseStrategy(ShortlinePanelFeed):
    def signal_frame(self, *args, **kwargs):
        raise AssertionError("A frozen preopen path cannot consume the execution close strategy")


def setup(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat("2026-10-30T20:00:00+08:00"))
    bars = pd.DataFrame([{"date": day, "symbol": symbol, "open": 10., "high": 10.2, "low": 9.8,
        "close": 10., "preclose_for_limits": 10., "volume": 10000000., "amount": 100000000., "is_st": False}
        for day in DAYS for symbol in (A, B)])
    feed = NoCloseStrategy(bars, {}, [A, B])
    for record in feed.instruments.values():
        record["ipoDate"] = "1991-04-03"
    return feed, new_state(CONFIG.initial_cash)


def publication(index, weights=None, *, context=CONTEXT):
    day, source = DAYS[index], DAYS[index-1]
    pub = "2026-10-17T12:00:00+08:00" if day == "2026-10-19" else source + "T18:00:00+08:00"
    intent = (PortfolioIntent.no_change("saved_unknown_hold", context=context, source="frozen_native_lowvol_plan")
        if weights is None else PortfolioIntent.full_target(weights, context=context, source="frozen_native_lowvol_plan",
            reason="explicit_saved_exit_all" if not weights else "saved_full_target"))
    return PreopenPortfolioIntent(source, day, pub, intent, explicit_exit_all=weights == {})


def step(state, feed, index, weights=None, **options):
    return engine.advance(state, feed, DAYS[index], CONFIG, index, context=CONTEXT,
        preopen_intent=publication(index, weights), full_exit_lifecycle=PREOPEN_FULL_EXIT_LIFECYCLE, **options)


def book(state):
    return Ledger(state, CONFIG, CONTEXT)


def test_weekend_full_target_and_no_change_preserve_real_clocks_and_never_schedule_E_close_strategy(monkeypatch):
    feed, state = setup(monkeypatch)
    result = step(state, feed, 2, {A: .2})
    quantity, cash = book(state).quantity(A), state["cash"]
    assert quantity > 0 and book(state).available(A, DAYS[2]) == 0
    target = state["full_exit_lifecycle"]["latest_target"]
    assert target["source_day"] == "2026-10-16" and target["decided_on"] == "2026-10-17"
    assert target["decided_at"] == target["published_at"] == "2026-10-17T12:00:00+08:00"
    assert target["execution_not_before"] == DAYS[2]
    assert state["full_exit_lifecycle"]["target_version"] == 1 and state["pending_targets"] is None
    assert all(event["day"] == DAYS[2] for event in result["events"])
    state = json.loads(json.dumps(state))
    lost_binding = deepcopy(state)
    lost_binding["full_exit_lifecycle"].update(target_version=0, latest_target=None)
    before = deepcopy(lost_binding)
    with pytest.raises(ValueError, match="last preopen full target"):
        step(lost_binding, feed, 3)
    assert lost_binding == before
    step(state, feed, 3)
    assert book(state).quantity(A) == quantity and state["cash"] == cash
    assert state["full_exit_lifecycle"]["target_version"] == 1
    assert state["pending_targets"] is None and len(state["orders"]) == 1
    assert not any(event["kind"] == "targets_generated" for event in state["events"])


def test_blocked_then_partial_full_exit_survives_no_change_and_json_restore_without_old_buy_retry(monkeypatch):
    feed, state = setup(monkeypatch)
    step(state, feed, 1, {A: .2})
    original = book(state).quantity(A)
    feed.rows[DAYS[2]][A].update(open=9., high=9.1, low=8.9, close=9., preclose=10., volume=10000.)
    feed.volumes.at[DAYS[2], A] = 10000.
    feed.rows[DAYS[3]][A].update(open=9.1, high=9.2, low=9., close=9.1, preclose=9.)
    feed.rows[DAYS[4]][A].update(open=9.1, high=9.2, low=9., close=9.1, preclose=9.1)
    step(state, feed, 2, {})
    request = deepcopy(state["full_exit_lifecycle"]["remaining_full_exits"][A])
    assert request["remaining_quantity"] == original and request["requested_on"] == "2026-10-17"
    assert any(event.get("reason") == "limit_down_queue_unknown" for event in state["events"])
    state = json.loads(json.dumps(state))
    step(state, feed, 3)
    remaining = book(state).quantity(A)
    assert 0 < remaining < original
    assert state["full_exit_lifecycle"]["remaining_full_exits"][A]["remaining_quantity"] == remaining
    assert state["orders"][f"{DAYS[3]}:{A}:sell"]["status"] == "cancelled"
    state = json.loads(json.dumps(state))
    step(state, feed, 4)
    assert not book(state).quantity(A) and not state["full_exit_lifecycle"]["remaining_full_exits"]
    assert state["full_exit_lifecycle"]["target_version"] == 2 and state["pending_targets"] is None
    assert len([order for order in state["orders"].values() if order["side"] == "buy"]) == 1
    assert sum(fill["qty"] for fill in state["fills"].values() if fill["side"] == "sell") == original


def test_new_positive_full_target_replaces_residual_before_its_own_open_and_no_change_keeps_it(monkeypatch):
    feed, state = setup(monkeypatch)
    step(state, feed, 1, {A: .2})
    feed.rows[DAYS[2]][A].update(tradestatus=0, volume=0., amount=0.)
    feed.volumes.at[DAYS[2], A] = 0.
    step(state, feed, 2, {})
    assert A in state["full_exit_lifecycle"]["remaining_full_exits"]
    result = step(state, feed, 3, {A: .2})
    quantity = book(state).quantity(A)
    assert quantity > 0 and A not in state["full_exit_lifecycle"]["remaining_full_exits"]
    assert state["full_exit_lifecycle"]["target_version"] == 3
    replacement = [event for event in result["events"] if event["kind"] == "full_exit_replaced_by_new_target"]
    assert len(replacement) == 1 and replacement[0]["replacement_version"] == 3
    assert not any(event.get("mechanical_reason") == "residual_full_exit" for event in result["events"])
    step(state, feed, 4)
    assert book(state).quantity(A) == quantity and not state["full_exit_lifecycle"]["remaining_full_exits"]


def test_st_T1_residual_and_original_quote_action_method_binding_guards_remain_before_mutation(monkeypatch):
    feed, state = setup(monkeypatch)
    state["positions"] = {A: [{"qty": 100, "available": DAYS[2]}], B: [{"qty": 137, "available": DAYS[1]}]}
    state["prices"] = {A: 1000, B: 1000}
    feed.rows[DAYS[1]][A]["isST"] = feed.rows[DAYS[2]][A]["isST"] = 1
    first = step(state, feed, 1)
    assert book(state).quantity(A) == 100 and book(state).quantity(B) == 137 and not state["orders"]
    assert state["full_exit_lifecycle"]["remaining_full_exits"][A]["source"] == "engine_forced_exit"
    assert any("sellable_T1" in event.get("constraint_flags", []) for event in first["events"])
    before = deepcopy(state)
    invalid = [
        {"full_exit_lifecycle": None, "preopen_intent": publication(2)},
        {"full_exit_lifecycle": FULL_EXIT_LIFECYCLE, "preopen_intent": publication(2)},
        {"full_exit_lifecycle": PREOPEN_FULL_EXIT_LIFECYCLE},
        {"full_exit_lifecycle": PREOPEN_FULL_EXIT_LIFECYCLE, "preopen_intent": publication(2),
            "context": RunContext(CONTEXT.account_id, TradingMode.PAPER)},
    ]
    for options in invalid:
        with pytest.raises(ValueError):
            engine.advance(state, feed, DAYS[2], CONFIG, 2, **({"context": CONTEXT} | options))
        assert state == before
    feed.rows[DAYS[2]][A]["preclose"] = float("nan")
    with pytest.raises(ValueError, match="raw prices"):
        step(state, feed, 2)
    assert state == before
    feed.rows[DAYS[2]][A]["preclose"] = 10.
    feed.validate_session = lambda *args: (_ for _ in ()).throw(ValueError("source_action_unknown"))
    with pytest.raises(ValueError, match="source_action_unknown"):
        step(state, feed, 2)
    assert state == before
    del feed.validate_session
    cold = new_state(CONFIG.initial_cash)
    with pytest.raises(ValueError, match="cannot be mixed"):
        engine.advance(cold, feed, DAYS[1], CONFIG, 1, context=CONTEXT,
            preopen_intent=publication(1), full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
    assert cold == new_state(CONFIG.initial_cash)
    step(state, feed, 2)
    assert book(state).quantity(A) == 0 and book(state).quantity(B) == 137
    assert state["full_exit_lifecycle"]["latest_target"] is None
    assert state["full_exit_lifecycle"]["target_version"] == 0
    assert not state["full_exit_lifecycle"]["remaining_full_exits"]
