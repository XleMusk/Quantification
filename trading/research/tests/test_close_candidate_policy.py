"""Real advance checks for the declared, bound close-candidate filter."""
from copy import deepcopy
from dataclasses import replace
import json

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, SignalFrame, TradingMode
from trading.engine import advance
from trading.ledger import Ledger, new_state


P, D, E, F = "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08"
A, B, C = "600001.SSE", "600002.SSE", "600003.SSE"
CONTEXT = RunContext("close_candidate_policy_fixture", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2,
                        gross_limit=.8, single_limit=.4, rebalance_sessions=1, slippage_bps=0)
POLICY = {"id": "fixed_close_candidate_fixture_v1", "capacity": "uncapped",
          "capital_policy": "known_close_minimum_entry"}


class OriginalFeed:
    def __init__(self):
        self.calendar = [P, D, E, F]
        row = {"open": 10., "close": 10., "preclose": 10., "tradestatus": 1,
               "isST": 0, "adjustflag": "3"}
        self.rows = {day: {symbol: deepcopy(row) for symbol in (A, B, C)} for day in self.calendar}
        self.volumes = pd.DataFrame(1_000_000., index=self.calendar, columns=[A, B, C])
        self.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""} for symbol in (A, B, C)}
        self.actions, self.instrument_profiles = [], {}

    def next_day(self, day):
        index = self.calendar.index(day)
        return self.calendar[index + 1] if index + 1 < len(self.calendar) else "2025-01-09"

    def signal_frame(self, day, config, context):
        return SignalFrame.from_legacy([{"symbol": A, "score": 2.}, {"symbol": B, "score": 1.}],
                                       day, "fixture:original_full_rank", context)


class CloseFeed(OriginalFeed):
    def __init__(self, attack=None):
        super().__init__()
        self.close_candidate_policy = deepcopy(POLICY)
        self.attack, self.close_calls = attack, []

    def close_signal_frame(self, day, config, context, original_batch, book):
        self.close_calls.append(day)
        if self.attack == "add":
            added = SignalFrame.from_legacy([{"symbol": C, "score": .5}],
                day, original_batch.source, context).signals[0]
            return replace(original_batch, signals=original_batch.signals + (added,))
        if self.attack == "score":
            changed = replace(original_batch.signals[0], value=3.)
            return replace(original_batch, signals=(changed, *original_batch.signals[1:]))
        if self.attack == "reorder":
            return replace(original_batch, signals=original_batch.signals[::-1])
        return original_batch


def step(state, feed, day, number):
    return advance(state, feed, day, CONFIG, number, context=CONTEXT)


@pytest.mark.parametrize("attack", ["add", "score", "reorder"])
def test_close_hook_cannot_add_change_scores_or_reorder_original_signals(attack):
    state, feed = new_state(CONFIG.initial_cash), CloseFeed(attack)
    with pytest.raises(ValueError, match="unchanged ordered subsequence"):
        step(state, feed, D, 0)
    assert feed.close_calls == [D]
    assert not state["orders"] and not state["fills"]
    assert state["pending_targets"] is None
    assert not any(event["kind"] == "targets_generated" for event in state["events"])


@pytest.mark.parametrize("change", ["id", "capacity", "capital_policy", "remove"])
def test_checkpoint_rejects_changed_or_removed_close_candidate_binding(change):
    state, original = new_state(CONFIG.initial_cash), CloseFeed()
    step(state, original, D, 0)
    assert state["close_candidate_policy"] == POLICY
    # Exercise persisted checkpoint data rather than a shared Python state alias.
    restored = json.loads(json.dumps(state))
    feed = OriginalFeed() if change == "remove" else CloseFeed()
    if change != "remove":
        feed.close_candidate_policy[change] += "_changed"
    before = deepcopy(restored)
    with pytest.raises(ValueError, match="Close candidate policy cannot change"):
        step(restored, feed, E, 1)
    assert restored == before


def test_no_hook_retains_original_targets_orders_fills_and_T1_path():
    plain_state, close_state = new_state(CONFIG.initial_cash), new_state(CONFIG.initial_cash)
    plain, declared = OriginalFeed(), CloseFeed()
    for number, day in enumerate((D, E)):
        step(plain_state, plain, day, number)
        step(close_state, declared, day, number)
    assert declared.close_calls == [D, E]
    assert "close_candidate_policy" not in plain_state
    assert close_state["close_candidate_policy"] == POLICY
    unchanged = deepcopy(close_state)
    unchanged.pop("close_candidate_policy")
    assert plain_state == unchanged
    assert plain_state["fills"] and {fill["day"] for fill in plain_state["fills"].values()} == {E}
    book = Ledger(plain_state, CONFIG, CONTEXT)
    assert book.quantity(A) > 0 and book.available(A, E) == 0
    assert book.available(A, F) == book.quantity(A)
