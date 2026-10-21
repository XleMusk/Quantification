"""New pure anchor reconstruction against synthetic shared Engine accounting."""
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR

import pandas as pd

from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.engine import advance, ordinary_share_terms
from trading.ledger import Ledger, new_state
from trading.research.action_aware_holding_anchor import action_aware_continuous_anchor as anchor
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config


A = "600001.SSE"
DAYS = pd.bdate_range("2024-01-02", periods=8).strftime("%Y-%m-%d").tolist()
CONFIG = _config(10000, 5)
CONTEXT = RunContext("synthetic_action_aware_anchor", TradingMode.REPLAY)


class Feed(ShortlinePanelFeed):
    def signal_frame(self, day, config, context):
        if day == DAYS[0]:
            rows = [{"symbol": A, "score": 1.}]
        elif day in getattr(self, "exit_days", ()):
            rows = []
        else:
            return SignalUnavailable("synthetic_hold_exact_shares")
        return SignalFrame.from_legacy(rows, day, "research:synthetic_action_anchor", context)


def fixture(*, stock="0.2", reserve="0.3", exit_days=()):
    bars = pd.DataFrame([{"date": d, "symbol": A, "open": 10., "high": 10.1, "low": 9.9,
        "close": 10., "preclose_for_limits": 10., "volume": 10_000_000., "amount": 100_000_000.,
        "is_st": False} for d in DAYS])
    feed = Feed(bars, {}, [A]); feed.instruments[A]["ipoDate"] = "2000-01-01"
    feed.exit_days = exit_days
    feed.actions = [{"symbol": A, "dividRegistDate": DAYS[1], "dividOperateDate": DAYS[3],
        "dividPayDate": DAYS[3], "dividStockMarketDate": DAYS[5] if Decimal(stock)+Decimal(reserve) else "",
        "dividCashPsBeforeTax": "0.1", "dividStocksPs": stock, "dividReserveToStockPs": reserve,
        "bonus_taxable_cash_ps": "0.1"}]
    return new_state(CONFIG.initial_cash), feed


def step(state, feed, index):
    return advance(state, feed, DAYS[index], CONFIG, index, context=CONTEXT)


def read(state, feed, index):
    before = deepcopy(state)
    result = anchor(state, A, feed.calendar, DAYS[index], feed.actions, context=CONTEXT)
    assert state == before
    return result


def test_known_cash_only_does_not_change_original_buy_anchor_or_call_allocation():
    state, feed = fixture(stock="0", reserve="0")
    for i in range(4): step(state, feed, i)
    result, reason = read(state, feed, 3)
    assert reason is None and result["fill_day"] == DAYS[1] and result["decision_day"] == DAYS[0]
    assert result["remaining_qty"] == state["entitlements"][A+":"+DAYS[3]]
    assert result["action_lots"][0]["allocated_shares"] == 0
    assert "corporate_action_reconciliations" not in state


def test_stock_plus_transfer_actual_Engine_lots_preserve_origin_after_original_FIFO_sold_and_listing_lock():
    state, feed = fixture(exit_days=(DAYS[3], DAYS[4]))
    for i in range(4): step(state, feed, i)
    entitlement = state["entitlements"][A+":"+DAYS[3]]
    first, reason = read(state, feed, 3)
    assert reason is None and first["remaining_qty"] == entitlement * 3//2
    assert first["action_lots"][0]["allocated_shares"] == entitlement//2
    assert first["action_lots"][0]["available_day"] == DAYS[5]
    step(state, feed, 4)  # Original T+1 shares sell; gifted shares remain locked.
    later, reason = read(state, feed, 4)
    assert reason is None and later["fill_id"] == first["fill_id"]
    assert later["remaining_qty"] == entitlement//2
    assert Ledger(state, CONFIG, CONTEXT).available(A, DAYS[4]) == 0
    assert all(lot["available"] == DAYS[5] for lot in state["positions"][A])
    step(state, feed, 5)
    assert read(state, feed, 5) == (None, "flat")


def test_existing_fractional_allocation_receipt_is_read_not_reexecuted():
    state, feed = fixture(stock="0", reserve="0.333")
    calls = []
    def receipt(action, quantity, context):
        calls.append(quantity)
        nominal = quantity * Decimal("0.333")
        return {"kind": "ordinary_share_allocation", "account_id": context.account_id,
            "mode": context.mode.value, "registered_quantity": quantity,
            "action_terms": ordinary_share_terms(action),
            "allocated_shares": int(nominal.to_integral_value(rounding=ROUND_FLOOR)),
            "source_kind": "explicit_replay_assumption", "source_ref": "synthetic_fixed_floor_not_broker_fact",
            "allocation_policy": "ordinary_fraction_floor_v1"}
    feed.ordinary_share_allocation_receipt = receipt
    for i in range(4): step(state, feed, i)
    result, reason = read(state, feed, 3)
    assert reason is None and calls == [100]
    assert result["remaining_qty"] == 133 and result["action_lots"][0]["allocated_shares"] == 33
    assert result["action_lots"][0]["allocation_source_kind"] == "explicit_replay_assumption"


def test_missing_commit_unknown_terms_quantity_or_settlement_residual_remain_unknown_without_repair():
    state, feed = fixture()
    for i in range(4): step(state, feed, i)
    trials = []
    missing = deepcopy(state)
    missing["events"] = [e for e in missing["events"] if e["kind"] != "corporate_action"]
    trials.append((missing, feed.actions))
    terms = deepcopy(feed.actions); terms[0]["dividReserveToStockPs"] = None
    trials.append((deepcopy(state), terms))
    quantity = deepcopy(state); quantity["positions"][A][-1]["qty"] += 1
    trials.append((quantity, feed.actions))
    listing = deepcopy(state); listing["positions"][A][-1]["available"] = DAYS[4]
    trials.append((listing, feed.actions))
    for saved, actions in trials:
        before = deepcopy(saved)
        origin, reason = anchor(saved, A, feed.calendar, DAYS[3], actions, context=CONTEXT)
        assert origin is None and reason not in (None, "flat") and saved == before
