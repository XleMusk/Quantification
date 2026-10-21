"""Fixed full-target lifecycle through the actual Engine and sole Ledger.

Synthetic sessions prove execution continuation, not strategy performance.
"""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading import engine
from trading.contracts import PortfolioIntent, RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config
from trading.target_exit_lifecycle import FULL_EXIT_LIFECYCLE


A = "600001.SSE"
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
        "2026-10-16", "2026-10-19", "2026-10-20"]
CONFIG = _config(100000, 5)


def setup(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda:
        datetime.fromisoformat("2026-10-31T20:00:00+08:00"))
    bars = pd.DataFrame([{
        "date": day, "symbol": A, "open": 10., "high": 10.2,
        "low": 9.8, "close": 10., "preclose_for_limits": 10.,
        "volume": 10_000_000., "amount": 100_000_000., "is_st": False,
    } for day in DAYS])
    feed = ShortlinePanelFeed(bars, {}, [A])
    feed.instruments[A]["ipoDate"] = "1999-01-01"
    return feed, RunContext("synthetic_fixed_full_exit", TradingMode.REPLAY), new_state(CONFIG.initial_cash)


def step(feed, context, state, index, targets=None, *, method=FULL_EXIT_LIFECYCLE):
    day = DAYS[index]
    metadata = {"context": context, "decided_at": day + "T18:00:00+08:00",
                "execution_not_before": DAYS[index + 1], "source": "synthetic_fixed_target"}
    intent = (PortfolioIntent.no_change("synthetic_unknown", **metadata) if targets is None
              else PortfolioIntent.full_target(targets, **metadata))
    return engine.advance(state, feed, day, CONFIG, index, context=context,
                          published_close_intent=intent, full_exit_lifecycle=method)


def book(state, context):
    return Ledger(state, CONFIG, context)


def halt(feed, index):
    feed.rows[DAYS[index]][A].update(tradestatus=0, volume=0., amount=0.)
    feed.volumes.at[DAYS[index], A] = 0.


def enter_then_remove(feed, context, state, *, method=FULL_EXIT_LIFECYCLE):
    step(feed, context, state, 0, {A: .2}, method=method)
    step(feed, context, state, 1, {}, method=method)
    original = book(state, context).quantity(A)
    assert original > 0
    assert book(state, context).available(A, DAYS[1]) == 0
    return original


def test_halted_full_exit_survives_no_change_json_restore_and_zero_prior_capacity(monkeypatch):
    feed, context, state = setup(monkeypatch)
    original = enter_then_remove(feed, context, state)
    halt(feed, 2)
    step(feed, context, state, 2)
    lifecycle = state["full_exit_lifecycle"]
    request = deepcopy(lifecycle["remaining_full_exits"][A])
    assert request["target_version"] == 2 and request["requested_on"] == DAYS[1]
    assert request["source"] == "synthetic_fixed_target"
    assert request["remaining_quantity"] == original
    assert lifecycle["target_version"] == 2 and lifecycle["latest_target"]["weights"] == {}
    assert state["pending_targets"] is None
    assert any(e["kind"] == "order_blocked" and e.get("reason") == "suspended" for e in state["events"])
    state = json.loads(json.dumps(state))
    # The first resumed open has zero prior-day participation, so the actual
    # unfinished sell remains after cancellation rather than being forgotten.
    step(feed, context, state, 3)
    assert book(state, context).quantity(A) == original
    assert state["full_exit_lifecycle"]["remaining_full_exits"][A] == request
    step(feed, context, state, 4)
    assert book(state, context).quantity(A) == 0
    assert state["full_exit_lifecycle"]["remaining_full_exits"] == {}
    assert state["full_exit_lifecycle"]["target_version"] == 2
    assert any(e["kind"] == "full_exit_completed" for e in state["events"])
    buys = [f for f in state["fills"].values() if f["side"] == "buy"]
    sells = [f for f in state["fills"].values() if f["side"] == "sell"]
    assert sum(f["qty"] for f in buys) == sum(f["qty"] for f in sells)
    assert all(f["day"] > DAYS[1] and f["available_day"] > f["day"] for f in sells)
    assert len([o for o in state["orders"].values() if o["side"] == "buy"]) == 1


def test_partial_full_exit_uses_actual_residual_and_finishes_next_session(monkeypatch):
    feed, context, state = setup(monkeypatch)
    original = enter_then_remove(feed, context, state)
    feed.volumes.at[DAYS[1], A] = 10_000.
    step(feed, context, state, 2)
    remaining = book(state, context).quantity(A)
    assert 0 < remaining < original
    assert state["full_exit_lifecycle"]["remaining_full_exits"][A]["remaining_quantity"] == remaining
    first_sell = state["orders"][f"{DAYS[2]}:{A}:sell"]
    assert first_sell["status"] == "cancelled" and first_sell["filled"] == original - remaining
    assert book(state, context).available(A, DAYS[2]) == remaining
    step(feed, context, state, 3)
    assert book(state, context).quantity(A) == 0
    assert state["full_exit_lifecycle"]["remaining_full_exits"] == {}
    assert sum(f["qty"] for f in state["fills"].values() if f["side"] == "sell") == original


def test_new_full_positive_target_cancels_old_exit_at_its_own_close(monkeypatch):
    feed, context, state = setup(monkeypatch)
    original = enter_then_remove(feed, context, state)
    halt(feed, 2)
    step(feed, context, state, 2, {A: .2})
    lifecycle = state["full_exit_lifecycle"]
    assert lifecycle["target_version"] == 3
    assert lifecycle["latest_target"]["decided_on"] == DAYS[2]
    assert lifecycle["latest_target"]["weights"] == {A: .2}
    assert lifecycle["remaining_full_exits"] == {}
    replacement = [e for e in state["events"] if e["kind"] == "full_exit_replaced_by_new_target"]
    assert len(replacement) == 1
    assert replacement[0]["original_request"]["target_version"] == 2
    assert replacement[0]["replacement_version"] == 3
    step(feed, context, state, 3)
    step(feed, context, state, 4)
    assert book(state, context).quantity(A) > 0
    assert not any(f["side"] == "sell" and f["qty"] == original for f in state["fills"].values())
    assert lifecycle["target_version"] == 3


def test_partial_reweight_is_not_a_persistent_full_exit(monkeypatch):
    feed, context, state = setup(monkeypatch)
    step(feed, context, state, 0, {A: .2})
    step(feed, context, state, 1, {A: .1})
    original = book(state, context).quantity(A)
    feed.volumes.at[DAYS[1], A] = 10_000.
    step(feed, context, state, 2)
    after_attempt = book(state, context).quantity(A)
    assert 0 < after_attempt < original
    assert state["full_exit_lifecycle"]["remaining_full_exits"] == {}
    order_count = len(state["orders"])
    step(feed, context, state, 3)
    assert book(state, context).quantity(A) == after_attempt
    assert len(state["orders"]) == order_count


def test_blocked_buy_is_not_retried_by_no_change(monkeypatch):
    feed, context, state = setup(monkeypatch)
    step(feed, context, state, 0, {A: .2})
    halt(feed, 1)
    step(feed, context, state, 1)
    step(feed, context, state, 2)
    step(feed, context, state, 3)
    assert book(state, context).quantity(A) == 0 and not state["fills"]
    assert state["pending_targets"] is None
    assert state["full_exit_lifecycle"]["target_version"] == 1
    assert state["full_exit_lifecycle"]["remaining_full_exits"] == {}


def test_missing_held_quote_refuses_before_mutation_and_preserves_exit(monkeypatch):
    feed, context, state = setup(monkeypatch)
    enter_then_remove(feed, context, state)
    halt(feed, 2)
    step(feed, context, state, 2)
    del feed.rows[DAYS[3]][A]
    before = deepcopy(state)
    with pytest.raises(ValueError, match="Unpriceable holding"):
        step(feed, context, state, 3)
    assert state == before


def test_old_default_keeps_original_one_attempt_behavior(monkeypatch):
    feed, context, state = setup(monkeypatch)
    original = enter_then_remove(feed, context, state, method=None)
    halt(feed, 2)
    step(feed, context, state, 2, method=None)
    step(feed, context, state, 3, method=None)
    step(feed, context, state, 4, method=None)
    assert "full_exit_lifecycle" not in state
    assert book(state, context).quantity(A) == original
    assert not any(f["side"] == "sell" for f in state["fills"].values())


@pytest.mark.parametrize("change", ["attach", "disable", "method"])
def test_method_cannot_be_added_disabled_or_changed_after_first_session(monkeypatch, change):
    feed, context, state = setup(monkeypatch)
    step(feed, context, state, 0, {A: .2}, method=None if change == "attach" else FULL_EXIT_LIFECYCLE)
    before = deepcopy(state)
    method = (FULL_EXIT_LIFECYCLE if change == "attach" else None if change == "disable" else "other_method")
    with pytest.raises(ValueError, match="lifecycle"):
        step(feed, context, state, 1, method=method)
    assert state == before
