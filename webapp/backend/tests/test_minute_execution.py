"""Minute diagnostics preserve causal intervals and the single account ledger."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.minute_replay import replay_minutes
from webapp.backend.app import minute_execution as api


SYMBOL = "600000.SSE"
DAY = "2026-10-19"
CALENDAR = ["2026-10-16", DAY, "2026-10-20", "2026-10-21"]
MARKET = {"price_basis": "raw", "pre_close": 10., "is_st": False, "tradestatus": 1,
          "listing_date": "2010-01-04", "out_date": "", "instrument_profiles": {}}


def bar(time, **fields):
    return {"date": DAY + "T" + time + "+08:00", "symbol": SYMBOL,
            "open": 10., "high": 10.03, "low": 9.98, "close": 10.01,
            "volume": 10000., "auction": False, **fields}


def replay(bars, **kwargs):
    return replay_minutes(bars, **{"symbol": SYMBOL, "side": "buy", "qty": 300, "limit_price": 10.05,
                                  "available_at": DAY + "T09:30:00+08:00", "as_of": DAY + "T09:34:00+08:00",
                                  "market": MARKET, "calendar": CALENDAR, **kwargs})


def test_whole_interval_must_follow_available_time_and_no_future_bars_affect_result():
    rows = [bar("09:31:00"), bar("09:32:00"), bar("09:33:00")]
    result = replay(rows, available_at=DAY + "T09:30:30+08:00", as_of=DAY + "T09:32:00+08:00")
    assert result["filled_qty"] == 100
    assert result["fills"][0]["timestamp"] == DAY + "T09:32:00+08:00"
    assert result["fills"][0]["interval_start"] == DAY + "T09:31:00+08:00"
    # A later extreme/invalid source value is not read before the cutoff.
    changed = [*rows[:2], bar("09:33:00", high=100000, open=100000, volume=-1)]
    assert replay(changed, available_at=DAY + "T09:30:30+08:00", as_of=DAY + "T09:32:00+08:00") == result


def test_minute_capacity_and_cumulative_order_fees_use_shared_ledger():
    result = replay([bar("09:31:00"), bar("09:32:00"), bar("09:33:00")])
    assert result["status"] == "available" and result["filled_qty"] == 300
    assert [fill["qty"] for fill in result["fills"]] == [100, 100, 100]
    assert all(fill["participation"] == .01 for fill in result["fills"])
    assert all(fill["price_rmb"] == 10.03 for fill in result["fills"])
    assert result["total_fees_rmb"] == 5.03
    assert result["cash_after_rmb"] == 96985.97
    assert result["sellable_after"] == 0 and result["quantity_after"] == 300
    assert result["execution_admitted"] is False and result["persistent_account_modified"] is False


def test_zero_volume_queue_unknown_intrabar_touch_and_closing_auction_are_not_fills():
    result = replay([bar("09:31:00", volume=0),
                     bar("09:32:00", open=11., high=11., low=11., close=11.),
                     bar("09:33:00", open=10.08, high=10.1, low=9.99),
                     bar("14:58:00")], as_of=DAY + "T15:00:00+08:00")
    assert result["filled_qty"] == 0
    assert result["skipped_minutes"]["zero_minute_volume"] == 1
    assert result["skipped_minutes"]["price_limit_queue_unknown"] == 1
    assert result["skipped_minutes"]["limit_not_marketable_at_interval_open"] == 1
    assert result["skipped_minutes"]["auction_or_outside_continuous_session"] == 1


def test_disposable_copy_preserves_source_and_t1_sellability():
    state = new_state(100000)
    state["day"] = CALENDAR[0]
    state["positions"] = {SYMBOL: [{"qty": 200, "available": DAY}, {"qty": 100, "available": "2026-10-20"}]}
    state["prices"] = {SYMBOL: 1000}
    before = deepcopy(state)
    result = replay([bar("09:31:00"), bar("09:32:00")], side="sell", qty=200, limit_price=9.95, state=state)
    assert result["filled_qty"] == 200 and result["quantity_after"] == 100
    assert result["sellable_after"] == 0 and state == before
    blocked = replay([bar("09:31:00")], side="sell", qty=300, limit_price=9.95, state=state)
    assert blocked["order"]["status"] == "rejected"
    assert blocked["reason"] == "insufficient_sellable_T1"
    assert blocked["filled_qty"] == 0 and state == before


def test_input_ticks_lots_cash_limits_and_calendar_evidence():
    with pytest.raises(ValueError, match="tick"):
        replay([bar("09:31:00")], limit_price=10.001)
    invalid_source = replay([bar("09:31:00", open=10.001)])
    assert invalid_source["status"] == "unavailable" and invalid_source["reason"] == "minute_quote_off_declared_tick"
    rejected = replay([bar("09:31:00")], qty=150)
    assert rejected["order"]["reason"] == "buy_lot_must_be_100"
    low_cash = replace(StrategyConfig(), initial_cash=1000)
    risk = replay([bar("09:31:00")], qty=100, config=low_cash)
    assert risk["status"] == "unavailable" and risk["reason"] == "single_security_risk_limit"
    fee_cash = replace(low_cash, single_limit=1, gross_limit=1)
    cash_rejected = replay([bar("09:31:00", open=9.98, high=9.99, low=9.97, close=9.98)],
                           qty=100, limit_price=9.99, config=fee_cash)
    assert cash_rejected["order"]["reason"] == "insufficient_cash" and cash_rejected["filled_qty"] == 0
    assert replay([], calendar=CALENDAR[:2])["reason"] == "missing_next_exchange_session_for_T1"
    assert replay([], market={**MARKET, "listing_date": None})["reason"] == "incomplete_daily_eligibility_context"
    assert replay([], market={**MARKET, "tradestatus": 0})["reason"] == "suspended"
    assert replay([], market={**MARKET, "is_st": True})["reason"] == "risk_warning_buy_blocked"
    assert replay([], limit_price=11.01)["reason"] == "limit_price_outside_daily_range"


def test_context_api_uses_metadata_only_and_never_writes_saved_account(monkeypatch):
    state = new_state(100000)
    state["day"] = CALENDAR[0]
    run = {"id": "run", "mode": "replay", "dataset": "frozen", "config": StrategyConfig().to_dict(), "state": state}
    source = SimpleNamespace(instruments={SYMBOL: {"ipoDate": "2010-01-04", "outDate": ""}}, calendar=CALENDAR,
                             rows={DAY: {SYMBOL: {"preclose": 10., "isST": 0, "tradestatus": 1, "adjustflag": "3",
                                                  "close": 999999, "volume": 999999999999}}}, actions=[],
                             instrument_profiles={}, manifest={"source": "synthetic", "version": "fixed"})
    monkeypatch.setattr(api.trading_store, "get", lambda identifier, full=False: run if identifier == "run" else None)
    monkeypatch.setattr(api, "HistoricalFeed", lambda path: source)
    chart_calls = []
    def minute_chart(*args):
        chart_calls.append(args)
        return {"dataset": {"fingerprint": "minute-fixed"}, "prices": [bar("09:31:00")], "coverage": {"missing": 0}, "warnings": []}
    monkeypatch.setattr(api.minute_data, "minute_chart", minute_chart)
    payload = {"minute_dataset_id": "minute", "symbol": SYMBOL, "side": "buy", "qty": 100, "limit_price": 10.05,
               "available_at": DAY + "T09:30:00+08:00", "as_of": DAY + "T09:31:00+08:00", "run_id": "run"}
    before = deepcopy(run)
    result = api.preview(payload)
    assert result["filled_qty"] == 100 and run == before
    assert "close" not in result["daily_context"]["fields_used"]
    assert "volume" not in result["daily_context"]["fields_used"]
    assert chart_calls == [("minute", SYMBOL, "1m", payload["as_of"])]
    assert api.preview({**payload, "run_id": "missing"})["status"] == "unavailable"
