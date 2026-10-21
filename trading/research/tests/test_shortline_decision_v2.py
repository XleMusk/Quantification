from copy import deepcopy

import pandas as pd
import pytest

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.research.shortline_decision_v2 import (
    BATCH_SCHEMA, BREADTH_SCOPE, ShortlineDecisionFeedV2, fixed_exit_policy, replay_decisions,
)
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config


SYMBOL = "600001.SSE"
CONFIG = _config(100000, 5)


def raw_panel(count=4, *, locked_exit=False):
    days = pd.bdate_range("2026-10-15", periods=count).strftime("%Y-%m-%d").tolist()
    bars = pd.DataFrame([{"date": day, "symbol": SYMBOL, "open": 10., "high": 10.05,
                          "low": 9.95, "close": 10., "preclose_for_limits": 10.,
                          "volume": 10_000_000., "amount": 100_000_000., "is_st": False} for day in days])
    if locked_exit:
        bars.loc[bars.date.eq(days[2]), ["open", "high", "low", "close"]] = 9.
    base = ShortlinePanelFeed(bars, {}, [SYMBOL])
    # This fixture's observed segment starts after its documented listing.
    base.instruments[SYMBOL] = {"ipoDate": "2000-01-01", "outDate": ""}
    return base, days


def batch(day, candidates=None):
    return {"schema": BATCH_SCHEMA, "day": day, "status": "complete", "source": "synthetic_complete_batch",
            "candidates": [{"symbol": SYMBOL, "score": 1.}] if candidates is None else candidates}


def breadth(day, *, negative=False, scope=BREADTH_SCOPE):
    return {"day": day, "scope": scope, "verified": True, "complete": True,
            "source": "synthetic_full_market_counts", "expected_active_count": 3,
            "advance_count": 0 if negative else 2, "decline_count": 2 if negative else 0, "flat_count": 1}


def replay(base, batches, **options):
    return replay_decisions(base, batches, config=CONFIG, **options)


def test_complete_empty_day_exits_through_real_shared_orders_fills_and_cash():
    base, days = raw_panel()
    result = replay(base, {days[0]: batch(days[0]), days[1]: batch(days[1], [])})
    state = result["state"]
    fills = list(state["fills"].values())
    assert {(fill["day"], fill["side"]) for fill in fills} == {(days[1], "buy"), (days[2], "sell")}
    assert sum(fill["qty"] for fill in fills if fill["side"] == "buy") == sum(
        fill["qty"] for fill in fills if fill["side"] == "sell")
    assert Ledger(state, CONFIG).quantity(SYMBOL) == 0
    assert state["cash"] < int(CONFIG.initial_cash * 100)  # Real shared fees/slippage, not a reset.
    assert any(event["kind"] == "targets_generated" and event["targets"] == {}
               for event in result["daily"][1]["events"])
    assert result["execution_admitted"] is False and result["persistent_ledger_mutated"] is False


@pytest.mark.parametrize("missing", [None, "incomplete"])
def test_missing_or_incomplete_batch_preserves_existing_position(missing):
    base, days = raw_panel()
    batches = {days[0]: batch(days[0])}
    if missing == "incomplete":
        batches[days[1]] = {"schema": BATCH_SCHEMA, "day": days[1], "status": "incomplete"}
    result = replay(base, batches)
    assert Ledger(result["state"], CONFIG).quantity(SYMBOL) > 0
    assert {fill["side"] for fill in result["state"]["fills"].values()} == {"buy"}
    assert any(event["kind"] == "no_decision" for event in result["daily"][1]["events"])


def test_complete_empty_without_following_raw_bar_is_no_change_and_v1_unchanged():
    base, days = raw_panel(count=3)
    result = replay(base, {days[0]: batch(days[0]), days[-1]: batch(days[-1], [])})
    assert Ledger(result["state"], CONFIG).quantity(SYMBOL) > 0
    tail = result["daily"][-1]
    assert any(event["kind"] == "no_decision" and event["reason"] == "shortline_v2_following_raw_panel_unavailable"
               for event in tail["events"])
    assert isinstance(base.signal_frame(days[1], CONFIG, RunContext("v1", TradingMode.REPLAY)), SignalUnavailable)
    assert base.signals == {}  # Wrapper never modifies the v1 source.


def test_cash_target_does_not_fabricate_exit_at_locked_limit_down():
    base, days = raw_panel(locked_exit=True)
    result = replay(base, {days[0]: batch(days[0]), days[1]: batch(days[1], [])})
    assert Ledger(result["state"], CONFIG).quantity(SYMBOL) > 0
    assert not any(fill["side"] == "sell" for fill in result["state"]["fills"].values())
    assert any(event["kind"] == "order_blocked" and event["reason"] == "limit_down_queue_unknown"
               for event in result["daily"][2]["events"])


def test_five_session_age_uses_actual_buy_fill_then_allows_later_reentry():
    base, days = raw_panel(count=9)
    feed = ShortlineDecisionFeedV2(base, {day: batch(day) for day in days})
    state = new_state(CONFIG.initial_cash)
    policy = fixed_exit_policy(feed, state, CONFIG)
    for index, day in enumerate(days[:7]):
        advance(state, feed, day, CONFIG, index, target_policy=policy)
    assert Ledger(state, CONFIG).quantity(SYMBOL) == 0
    fills = list(state["fills"].values())
    assert fills[0]["day"] == days[1] and fills[0]["side"] == "buy"
    assert fills[-1]["day"] == days[6] and fills[-1]["side"] == "sell"
    assert not any(fill["side"] == "buy" and fill["day"] == days[6] for fill in fills)
    assert any(event["kind"] == "targets_generated" and event.get("decision_reason") == "shortline_v2_fixed_five_session_holding_exit"
               for event in state["events"])
    advance(state, feed, days[7], CONFIG, 7, target_policy=policy)
    assert Ledger(state, CONFIG).quantity(SYMBOL) > 0
    assert list(state["fills"].values())[-1]["side"] == "buy"


@pytest.mark.parametrize("scope, exit_expected", [(BREADTH_SCOPE, True), ("candidate_panel", False)])
def test_only_negative_complete_verified_full_market_breadth_can_force_exit(scope, exit_expected):
    base, days = raw_panel()
    first, negative = batch(days[0]), batch(days[1])
    first["market_breadth"] = breadth(days[0])
    negative["market_breadth"] = breadth(days[1], negative=True, scope=scope)
    result = replay(base, {days[0]: first, days[1]: negative}, market_defense=True)
    assert (Ledger(result["state"], CONFIG).quantity(SYMBOL) == 0) is exit_expected
    if not exit_expected:
        assert any(event["kind"] == "no_decision" and event["reason"] == "shortline_v2_full_market_breadth_unavailable"
                   for event in result["daily"][1]["events"])


def test_false_complete_batch_and_future_labels_rejected_and_paper_stays_closed():
    base, days = raw_panel()
    malformed = batch(days[0])
    malformed.pop("candidates")
    with pytest.raises(ValueError, match="explicit candidate list"):
        ShortlineDecisionFeedV2(base, {days[0]: malformed})
    with pytest.raises(ValueError, match="future continuation labels"):
        ShortlineDecisionFeedV2(base, {days[0]: batch(days[0], [{"symbol": SYMBOL, "score": 1., "hit_5": True}])})
    feed = ShortlineDecisionFeedV2(base, {days[0]: batch(days[0])})
    state = new_state(CONFIG.initial_cash)
    original = deepcopy(state)
    with pytest.raises(ValueError, match="historical-replay-only"):
        advance(state, feed, days[0], CONFIG, 0, context=RunContext("paper", TradingMode.PAPER))
    assert state == original
