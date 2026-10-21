"""Only the new fixed stop mechanics; synthetic data and shared Engine."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config
from trading.research.trend_atr_stop_policy import AtrEntryStopPolicy, AtrStopFeed, STATE_KEY


A, B = "600001.SSE", "600002.SSE"
DAYS = pd.bdate_range("2024-01-02", periods=10).strftime("%Y-%m-%d").tolist()
CONFIG = _config(100000, 5)
CTX = RunContext("synthetic_entry_ATR", TradingMode.REPLAY)


class Provider:
    price_basis_id = "synthetic_fixed_adjustflag2_source"
    def __init__(self, closes=None):
        self.rows = {day: {"date": day, "price_basis_id": self.price_basis_id,
            "price_unit": "CNY_per_share", "close": 2*(closes or {}).get(day, 10.),
            "atr": 1. if day == DAYS[0] else 99., "raw_to_signal_factor": 2.,
            "regime": 1, "trend_active": True, "source": "synthetic_saved_adjusted_and_raw_exact_D"}
            for day in DAYS}
        self.through, self.calls = DAYS[-1], []
    def snapshot(self, day, symbol):
        assert day <= self.through, "No next-session close may be read at a D decision"
        self.calls.append((day, symbol))
        return deepcopy(self.rows.get(day))


def raw(close=10.):
    return {"open": close, "high": close+.1, "low": close-.1, "close": close,
        "preclose": close, "volume": 10_000_000., "amount": 100_000_000.,
        "tradestatus": 1, "isST": 0, "adjustflag": "3"}


def holding():
    state = new_state(CONFIG.initial_cash)
    book = Ledger(state, CONFIG, CTX)
    book.submit("entry", DAYS[1], A, "buy", 300, 1000)
    book.fill("first", "entry", 100, 1000, DAYS[1], DAYS[2])
    feed = SimpleNamespace(calendar=DAYS, rows={d: {A: raw(), B: raw()} for d in DAYS})
    return book, feed


def test_first_actual_fill_and_entry_N_survive_partial_FIFO_turnover_and_recovery_unknown_shares():
    book, feed = holding()
    provider, policy = Provider(), None
    policy = AtrEntryStopPolicy(provider)
    provider.through = DAYS[1]
    policy.prepare(DAYS[1], book, feed)
    anchor = deepcopy(book.state[STATE_KEY]["anchors"][A])
    assert anchor["entry_price"] == 20. and anchor["N"] == 1. and anchor["stop_price"] == 18.
    assert anchor["decision_day"] == DAYS[0] and max(d for d,s in provider.calls) == DAYS[1]
    # More callbacks and a later buy cannot replace the continuous-holding anchor.
    book.fill("remaining", "entry", 200, 1000, DAYS[1], DAYS[2])
    book.submit("sell_original_lot", DAYS[2], A, "sell", 100, 1000)
    book.fill("sell_first", "sell_original_lot", 100, 1000, DAYS[2], DAYS[3])
    book.submit("later_buy", DAYS[2], A, "buy", 100, 1100)
    book.fill("later_fill", "later_buy", 100, 1100, DAYS[2], DAYS[3])
    provider.through = DAYS[2]
    restored = AtrEntryStopPolicy(provider)
    restored.prepare(DAYS[2], book, feed)
    assert book.state[STATE_KEY]["anchors"][A] == anchor and book.quantity(A) == 300
    # A legitimate share change with no matching fill is unknown, never repaired.
    book.state["positions"][A][0]["qty"] += 100
    before = deepcopy(book.state["positions"])
    provider.through = DAYS[3]
    frame = SignalFrame.from_legacy([{"symbol": A, "score": 2.}, {"symbol": B, "score": 1.}],
        DAYS[3], "original_donchian_fixture", CTX)
    assert isinstance(restored.filter_frame(frame, DAYS[3], book, feed), SignalUnavailable)
    assert book.state[STATE_KEY]["assessments"][A]["status"] == "unknown"
    assert book.state["positions"] == before and book.state[STATE_KEY]["anchors"][A] == anchor


def test_same_basis_mapping_prevents_false_raw_vs_adjusted_stop_and_unknown_is_not_exit():
    book, feed = holding()
    provider = Provider({DAYS[2]: 9.2})
    feed.rows[DAYS[2]][A] = raw(9.2)
    policy = AtrEntryStopPolicy(provider)
    provider.through = DAYS[2]
    assert policy(DAYS[2], book, feed) is None  # adjusted18.4>18; raw9.2 is not compared to18.
    assert book.state[STATE_KEY]["assessments"][A]["status"] == "not_triggered"
    provider.rows[DAYS[3]]["raw_to_signal_factor"] = 3.  # Contradicts raw/adjusted same-D ratio.
    provider.through = DAYS[3]
    assert policy(DAYS[3], book, feed) is None
    assert book.state[STATE_KEY]["assessments"][A]["status"] == "unknown"
    other = Provider(); other.price_basis_id = "different_frozen_source"
    with pytest.raises(ValueError, match="changed during recovery"):
        AtrEntryStopPolicy(other).prepare(DAYS[4], book, feed)


def test_shared_Engine_queues_close_stop_next_open_blocked_retry_and_only_new_regime_reentry():
    closes = dict(zip(DAYS, [10., 10., 9., 8.1, 8.2, 8.4, 8.6, 8.8, 9., 9.1]))
    bars = []
    for i,day in enumerate(DAYS):
        close = closes[day]
        bars.append({"date": day, "symbol": A, "open": close, "high": close if i == 3 else close+.1,
            "low": close if i == 3 else close-.1, "close": close,
            "preclose_for_limits": closes[DAYS[max(0,i-1)]], "volume": 10_000_000.,
            "amount": 100_000_000., "is_st": False})
    original = ShortlinePanelFeed(pd.DataFrame(bars), {d: [{"symbol": A, "score": 1.}] for d in DAYS}, [A])
    original.instruments[A] = {"ipoDate": "2000-01-01", "outDate": ""}
    provider = Provider(closes)
    for day in DAYS[6:]: provider.rows[day]["regime"] = 2
    policy, state = AtrEntryStopPolicy(provider), new_state(CONFIG.initial_cash)
    feed = AtrStopFeed(original, policy)
    for i,day in enumerate(DAYS[:6]):
        provider.through = day
        advance(state, feed, day, CONFIG, i, context=CTX, replay_risk_exit_policy=policy)
        if i == 2:
            assert state[STATE_KEY]["anchors"][A]["N"] == 1.
            assert state["pending_targets"] == {} and state["pending_risk_exits"]["execution_on"] == DAYS[3]
        if i == 3:
            assert Ledger(state, CONFIG, CTX).quantity(A) > 0
            assert any(e["kind"] == "order_blocked" and e.get("side") == "sell" for e in state["events"])
            assert state["pending_risk_exits"]["execution_on"] == DAYS[4]
    fills = list(state["fills"].values())
    # The unchanged daily weight target can rebalance once at D2 open, before
    # D2's close stop. It must not reset entry N or create later stopped buys.
    assert {f["day"] for f in fills if f["side"] == "buy"} == {DAYS[1], DAYS[2]}
    assert {f["day"] for f in fills if f["side"] == "sell"} == {DAYS[4]}
    assert all(f["available_day"] == DAYS[DAYS.index(f["day"])+1] for f in fills if f["side"] == "buy")
    assert not Ledger(state, CONFIG, CTX).quantity(A) and state["pending_targets"] == {}
    # Restore in the same account; the old-regime veto survives being flat.
    state = deepcopy(state); policy = AtrEntryStopPolicy(provider); feed = AtrStopFeed(original, policy)
    for i in (6, 7):
        provider.through = DAYS[i]
        advance(state, feed, DAYS[i], CONFIG, i, context=CTX, replay_risk_exit_policy=policy)
    assert Ledger(state, CONFIG, CTX).quantity(A) > 0
    assert {f["day"] for f in state["fills"].values() if f["side"] == "buy"} == {DAYS[1], DAYS[2], DAYS[7]}
    assert state[STATE_KEY]["anchors"][A]["decision_day"] == DAYS[6]
