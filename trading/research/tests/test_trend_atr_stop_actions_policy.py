"""New action-aware stop integration on synthetic, real Engine transitions."""
from copy import deepcopy

import pandas as pd

from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config
from trading.research.trend_atr_stop_actions_policy import (
    AtrActionsStopFeed, AtrEntryStopActionsPolicy, STATE_KEY)


SYMBOL = "600001.SSE"
DAYS = pd.bdate_range("2024-05-20", periods=10).strftime("%Y-%m-%d").tolist()
CONTEXT = RunContext("synthetic_action_aware_entry_ATR_v2", TradingMode.REPLAY)
CONFIG = _config(100000, 5)


class Provider:
    price_basis_id = "synthetic_one_adjusted_basis_with_capital_transfer"
    def __init__(self, base):
        self.base, self.through, self.calls = base, DAYS[-1], []

    def snapshot(self, day, symbol):
        assert day <= self.through, "A close stop cannot request a future source session"
        self.calls.append(day)
        index = DAYS.index(day)
        factor = 2. if index < 3 else 3.
        return {"date": day, "price_basis_id": self.price_basis_id,
            "price_unit": "CNY_per_share", "close": self.base.rows[day][symbol]["close"]*factor,
            "atr": 1. if index == 0 else 99., "raw_to_signal_factor": factor,
            "regime": 1, "trend_active": True, "source": "synthetic_saved_same_D_adjusted_and_raw"}


def fixture():
    closes = [10., 10., 10., 20/3, 5.9, 5.8, 5.7, 5.8, 5.9, 6.]
    bars = pd.DataFrame([{"date": day, "symbol": SYMBOL, "open": closes[index],
        "high": closes[index]+.1, "low": closes[index]-.1, "close": closes[index],
        "preclose_for_limits": closes[index-1] if index and index != 3 else closes[index],
        "volume": 10_000_000., "amount": 100_000_000., "is_st": False}
        for index, day in enumerate(DAYS)])
    base = ShortlinePanelFeed(bars, {DAYS[0]: [{"symbol": SYMBOL, "score": 1.}]}, [SYMBOL])
    base.instruments[SYMBOL] = {"ipoDate": "2000-01-01", "outDate": ""}
    base.actions = [{"symbol": SYMBOL, "dividRegistDate": DAYS[1], "dividOperateDate": DAYS[3],
        "dividStockMarketDate": DAYS[6], "dividPayDate": DAYS[3],
        "dividCashPsBeforeTax": "0", "dividStocksPs": "0", "dividReserveToStockPs": "0.5"}]
    provider = Provider(base)
    policy = AtrEntryStopActionsPolicy(provider)
    return base, provider, policy, AtrActionsStopFeed(base, policy), new_state(CONFIG.initial_cash)


def step(state, feed, provider, policy, index):
    provider.through = DAYS[index]
    return advance(state, feed, DAYS[index], CONFIG, index, context=CONTEXT,
                   replay_risk_exit_policy=policy)


def test_real_Engine_share_credit_keeps_first_fill_N_then_settled_partial_stop_and_restore():
    base, provider, policy, feed, state = fixture()
    for index in range(3):
        step(state, feed, provider, policy, index)
    anchor = deepcopy(state[STATE_KEY]["anchors"][SYMBOL])
    initial_qty = Ledger(state, CONFIG, CONTEXT).quantity(SYMBOL)
    entry_fills = deepcopy(state["fills"])
    assert initial_qty > 0 and initial_qty % 100 == 0
    assert anchor["decision_day"] == DAYS[0] and anchor["N"] == 1.
    step(state, feed, provider, policy, 3)
    book = Ledger(state, CONFIG, CONTEXT)
    assert book.quantity(SYMBOL) == initial_qty*3//2
    assert book.available(SYMBOL, DAYS[3]) == initial_qty
    assert state["fills"] == entry_fills  # A share credit never invents a buy callback.
    assert state[STATE_KEY]["assessments"][SYMBOL]["status"] == "not_triggered"
    assert state[STATE_KEY]["anchors"][SYMBOL] == anchor
    assert state[STATE_KEY]["assessments"][SYMBOL]["holding_reconciliation"]["settlement_reconciled"] is True
    # A new policy instance restores the same held origin, entry N and stop.
    state = deepcopy(state)
    policy = AtrEntryStopActionsPolicy(provider)
    feed = AtrActionsStopFeed(base, policy)
    step(state, feed, provider, policy, 4)
    assert state[STATE_KEY]["anchors"][SYMBOL]["N"] == 1.
    assert state[STATE_KEY]["anchors"][SYMBOL]["entry_price"] == anchor["entry_price"]
    assert state[STATE_KEY]["assessments"][SYMBOL]["status"] == "stop_triggered"
    step(state, feed, provider, policy, 5)
    assert Ledger(state, CONFIG, CONTEXT).quantity(SYMBOL) == initial_qty//2
    assert state["pending_risk_exits"]["execution_on"] == DAYS[6]
    step(state, feed, provider, policy, 6)
    assert Ledger(state, CONFIG, CONTEXT).quantity(SYMBOL) == 0
    assert {fill["day"] for fill in state["fills"].values() if fill["side"] == "buy"} == {DAYS[1]}
    assert {fill["day"] for fill in state["fills"].values() if fill["side"] == "sell"} == {DAYS[5], DAYS[6]}


def test_uncommitted_share_action_is_unknown_and_does_not_repair_inventory_or_invent_fills():
    base, provider, policy, feed, state = fixture()
    for index in range(4):
        step(state, feed, provider, policy, index)
    # Corrupt evidence, not an alternative economic action: remove its saved commit.
    state = deepcopy(state)
    state["events"] = [event for event in state["events"] if event["kind"] != "corporate_action"]
    before_positions, before_fills = deepcopy(state["positions"]), deepcopy(state["fills"])
    policy = AtrEntryStopActionsPolicy(provider)
    feed = AtrActionsStopFeed(base, policy)
    provider.through = DAYS[4]
    book = Ledger(state, CONFIG, CONTEXT)
    frame = SignalFrame.from_legacy([{"symbol": SYMBOL, "score": 1.}], DAYS[4],
        "synthetic_unchanged_candidate", CONTEXT)
    assert isinstance(policy.filter_frame(frame, DAYS[4], book, feed), SignalUnavailable)
    assert state[STATE_KEY]["assessments"][SYMBOL]["status"] == "unknown"
    assert state["positions"] == before_positions and state["fills"] == before_fills
    assert policy(DAYS[4], book, feed) is None
