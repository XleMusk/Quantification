import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import (ExecutionEvent, OrderIntent, Price, PriceBasis, RunContext,
                               SecurityId, ShareQuantity, TradingMode)
from trading.engine import advance, corporate_actions, simulate_order
from trading.ledger import Ledger, new_state
from trading.rules import ETF_PROFILE, EQUITY_ETF_PROFILES, execution_rule, fees
from trading.simulated_broker import SimulatedBrokerAdapter


DAY = "2024-01-02"
FUND = "510300.SSE"
PROFILES = {FUND: ETF_PROFILE}
CONTEXT = RunContext("precision_test", TradingMode.REPLAY)


def test_explicit_equity_fund_profiles_keep_STAR_fund_lots_and_dated_limits():
    for symbol in EQUITY_ETF_PROFILES:
        rule = execution_rule(symbol, "2024-01-02", EQUITY_ETF_PROFILES)
        assert rule.price_unit == "mill" and rule.equity_etf
        assert rule.quantity.rounded(150, "buy") == 100
        assert rule.limits(1., "2024-01-02") == ((800, 1200) if symbol == "588000.SSE" else (900, 1100))
    with pytest.raises(ValueError, match="rule span"):
        execution_rule("588000.SSE", "2026-01-02", EQUITY_ETF_PROFILES)
    for unsupported in ("511010.SSE", "518880.SSE", "513500.SSE"):
        with pytest.raises(ValueError, match="Unverified"):
            execution_rule(unsupported, DAY, {unsupported: ETF_PROFILE})


def book():
    return Ledger(new_state(10000), StrategyConfig(), CONTEXT, instrument_profiles=PROFILES)


def request(symbol=FUND, price=None, qty=100):
    return OrderIntent("buy_" + symbol, CONTEXT, DAY, SecurityId(symbol), "buy", ShareQuantity(qty),
                       price if price is not None else Price.from_rmb("3.937", PriceBasis.RAW, "mill"), "test")


def fill(order, identifier, qty, price=None):
    return ExecutionEvent(identifier, order.id, CONTEXT, DAY, order.security, "buy", ShareQuantity(qty),
                          price if price is not None else order.limit_price, "2024-01-03")


def test_price_keeps_old_cent_wire_and_explicit_new_mill_wire():
    old = Price(393, PriceBasis.RAW)
    assert old.to_dict() == {"schema_version": 1, "contract_type": "Price", "value": 393,
                             "unit": "cent", "currency": "CNY", "basis": "unadjusted"}
    quote = Price.from_rmb("3.937", PriceBasis.RAW, "mill")
    assert quote.value == 3937 and quote.cents is None and quote.rmb == Decimal("3.937")
    wire = json.loads(json.dumps(quote.to_dict()))
    assert wire["schema_version"] == 2 and Price.from_dict(wire) == quote
    wire["schema_version"] = 1
    with pytest.raises(ValueError, match="unit"):
        Price.from_dict(wire)
    with pytest.raises(ValueError, match="tick"):
        Price.from_rmb("3.937", PriceBasis.RAW, "cent")
    with pytest.raises(ValueError, match="tick"):
        Price.from_rmb("3.9371", PriceBasis.RAW, "mill")
    with pytest.raises(ValueError):
        Price(393, PriceBasis.RAW, 3937)


def test_fund_tick_profile_and_stock_tick_are_not_inferred_from_price():
    value = request()
    before = new_state(10000)
    ledger = Ledger(before, StrategyConfig(), CONTEXT)
    with pytest.raises(ValueError, match="Unsupported"):
        ledger.submit_intent(value)
    assert before == new_state(10000)
    ledger = book()
    with pytest.raises(ValueError, match="quote unit"):
        ledger.submit_intent(request(price=Price(394, PriceBasis.RAW)))
    with pytest.raises(ValueError, match="quote unit"):
        ledger.submit_intent(request(symbol="600000.SSE"))
    for day in ("2023-12-29", "2026-10-01"):
        with pytest.raises(ValueError, match="rule span"):
            execution_rule(FUND, day, PROFILES)
    with pytest.raises(ValueError):
        execution_rule("513500.SSE", DAY, {"513500.SSE": ETF_PROFILE})


def test_mixed_quotes_use_one_cash_ledger_and_readback_preserves_units():
    ledger = book()
    broker = SimulatedBrokerAdapter(ledger)
    for order in (request("600000.SSE", Price(1000, PriceBasis.RAW)), request()):
        assert broker.submit(order)["status"] == "submitted"
        assert broker.apply_execution(fill(order, "f_" + order.id, 100))
        ledger.mark_price(order.security.symbol, order.limit_price.value, DAY, price_unit=order.limit_price.unit)
    assert ledger.state["cash"] == 1000000 - 100000 - fees(100000, "buy", ledger.config) - 39370 - 500
    assert ledger.equity() == 1000000 - fees(100000, "buy", ledger.config) - 500
    assert ledger.available(FUND, DAY) == 0 and ledger.available(FUND, "2024-01-03") == 100
    ledger.state["day"] = DAY
    rows = {row["symbol"]: row for row in broker.read("orders", trading_date=DAY)["rows"]}
    assert rows[FUND]["price_mills"] == 3937 and "price_cents" not in rows[FUND]
    assert rows["600000.SSE"]["price_cents"] == 1000
    trades = broker.read("trades", trading_date=DAY)["rows"]
    assert trades[-1]["price_mills"] == 3937 and trades[-1]["fees_cents"] == 500


def test_partial_callback_fragmentation_does_not_change_cash_fees_or_reserve():
    complete, partial = book(), book()
    order = request()
    complete.submit_intent(order)
    complete.apply_execution(fill(order, "all", 100))
    partial.submit_intent(order)
    for number, qty in enumerate((1, 4, 95)):
        event = fill(order, str(number), qty)
        assert partial.apply_execution(event)
        before = deepcopy(partial.state)
        assert not partial.apply_execution(event)
        assert before == partial.state
        assert partial.cash_available() >= 0
    one, many = complete.state["orders"][order.id], partial.state["orders"][order.id]
    assert one["notional"] == many["notional"] == 39370
    assert one["notional_minor"] == many["notional_minor"] == 393700
    assert one["fees"] == many["fees"] == 500
    assert one["reserve"] == many["reserve"] == 0
    assert complete.state["cash"] == partial.state["cash"]
    assert partial.state["fills"]["0"]["notional"] == 394
    assert partial.state["fills"]["1"]["notional"] == 1575
    assert sum(row["notional"] for row in partial.state["fills"].values()) == 39370
    before = deepcopy(partial.state)
    with pytest.raises(ValueError, match="Conflicting execution"):
        partial.fill("0", order.id, 1, 3937, DAY, "2024-01-03")
    assert before == partial.state


def fixture_feed():
    days = ["2024-01-02", "2024-01-03", "2024-01-04"]
    rows = {day: {"600000.SSE": {"open": 10., "close": 10., "preclose": 10., "tradestatus": 1, "isST": 0},
                     FUND: {"open": 3.937, "close": 3.949, "preclose": 3.937, "tradestatus": 1, "isST": 0,
                            "adjustflag": "3"}}
            for day in days}
    return SimpleNamespace(calendar=days, rows=rows, actions=[], instrument_profiles=PROFILES,
        instruments={"600000.SSE": {"ipoDate": "2000-01-01", "outDate": ""},
                     FUND: {"ipoDate": "2012-05-28", "outDate": ""}},
        volumes=pd.DataFrame(1000000., index=days, columns=["600000.SSE", FUND]),
        next_day=lambda day: days[days.index(day) + 1] if day != days[-1] else "2024-01-05",
        scores=lambda day, config: [{"symbol": "600000.SSE", "score": 1.}, {"symbol": FUND, "score": .5}])


def test_actual_engine_targets_mixed_quotes_and_rounds_turnover_in_cash_units():
    feed = fixture_feed()
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, single_limit=.4,
                            gross_limit=.8, slippage_bps=0)
    state = new_state(config.initial_cash)
    advance(state, feed, feed.calendar[0], config, 0, context=CONTEXT)
    row = advance(state, feed, feed.calendar[1], config, 1, context=CONTEXT)
    ledger = Ledger(state, config, CONTEXT, instrument_profiles=PROFILES)
    assert ledger.quantity(FUND) == 1000 and ledger.quantity("600000.SSE") == 400
    assert state["prices"][FUND] == 3949 and state["price_units"] == {FUND: "mill"}
    assert row["turnover_value"] == 7937
    assert row["fees"] == 10.04 and row["equity"] == 10001.96
    saved = json.loads(json.dumps(state))
    resumed = Ledger(saved, config, CONTEXT, instrument_profiles=PROFILES)
    assert resumed.equity() == ledger.equity()
    without_profile = deepcopy(state)
    del feed.instrument_profiles
    with pytest.raises(ValueError):
        advance(without_profile, feed, feed.calendar[2], config, 2, context=CONTEXT)
    assert without_profile == state


def test_detail_api_preserves_mixed_price_units_and_account_state(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from trading import api, store

    feed = fixture_feed()
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, single_limit=.4,
                            gross_limit=.8, slippage_bps=0)
    state = new_state(config.initial_cash)
    days = [advance(state, feed, day, config, number, context=CONTEXT)
            for number, day in enumerate(feed.calendar[:2])]
    before = deepcopy(state)
    run = {"id": CONTEXT.account_id, "name": "Mixed quote fixture, not historical data", "mode": "replay",
           "status": "completed", "start": feed.calendar[0], "end": feed.calendar[1], "cursor": 2,
           "dataset": "fixture", "config": config.to_dict(), "state": state}
    monkeypatch.setattr(store, "get", lambda identifier, full=False: deepcopy(run))
    monkeypatch.setattr(store, "daily", lambda identifier: deepcopy(days))
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        response = client.get("/api/trading/runs/" + CONTEXT.account_id)
    assert response.status_code == 200
    value = response.json()
    holdings = {row["symbol"]: row for row in value["holdings"]}
    assert holdings[FUND] == {"symbol": FUND, "quantity": 1000, "sellable": 0,
                             "price": 3.949, "price_unit": "mill", "value": 3949}
    assert holdings["600000.SSE"]["price"] == 10
    assert holdings["600000.SSE"]["value"] == 4000
    assert holdings["600000.SSE"]["price_unit"] == "cent"
    orders = {row["symbol"]: row for row in value["orders"]}
    assert orders[FUND]["limit_price"] == 3937 and orders[FUND]["limit_price_rmb"] == 3.937
    assert orders["600000.SSE"]["limit_price"] == 1000 and orders["600000.SSE"]["limit_price_rmb"] == 10
    for fill in value["fills"]:
        assert fill["price_rmb"] == (3.937 if fill["symbol"] == FUND else 10)
        assert fill["price_unit"] == ("mill" if fill["symbol"] == FUND else "cent")
    assert value["equity"] == days[-1]["equity"] == 10001.96
    assert all(order["reserve"] == 0 for order in value["orders"])
    assert value["equity"] == pytest.approx(value["available_cash"] + value["receivables"] + sum(row["value"] for row in value["holdings"]))
    assert state == before


def test_fund_limit_up_is_blocked_and_sell_has_no_equity_stamp_or_transfer_charge():
    feed, ledger = fixture_feed(), book()
    quote = dict(feed.rows[DAY][FUND], open=4.331)
    simulate_order(ledger, feed, DAY, FUND, "buy", 100, quote, 1000000)
    assert not ledger.state["orders"]
    assert ledger.state["events"][-1]["reason"] == "limit_up_queue_unknown"
    assert fees(100000, "sell", ledger.config, equity_etf=True) == 500
    assert fees(100000, "sell", ledger.config) == 551


@pytest.mark.parametrize("field,value", [("open", 3.9371), ("close", float("nan")),
                                        ("tradestatus", None), ("isST", None), ("adjustflag", None)])
def test_invalid_fund_quote_fails_before_account_mutation(field, value):
    feed = fixture_feed()
    feed.rows[DAY][FUND][field] = value
    state = new_state(10000)
    state["pending_targets"] = {FUND: .1}
    before = deepcopy(state)
    with pytest.raises(ValueError):
        advance(state, feed, DAY, StrategyConfig(), 0, context=CONTEXT)
    assert state == before


def test_odd_lot_sell_cannot_make_cash_negative_or_consume_holdings():
    ledger = book()
    ledger.state["cash"] = 0
    ledger.state["positions"][FUND] = [{"qty": 1, "available": DAY}]
    order = OrderIntent("exit", CONTEXT, DAY, SecurityId(FUND), "sell", ShareQuantity(1),
                        Price.from_mills(3937, PriceBasis.RAW), "odd_lot_exit")
    assert ledger.submit_intent(order)["status"] == "submitted"
    event = ExecutionEvent("exit_fill", order.id, CONTEXT, DAY, order.security, "sell",
                           order.quantity, order.limit_price, "2024-01-03")
    before = deepcopy(ledger.state)
    with pytest.raises(ValueError, match="negative cash"):
        ledger.apply_execution(event)
    assert ledger.state == before


def test_fund_distribution_does_not_use_stock_dividend_tax_reserve():
    ledger = book()
    action = {"symbol": FUND, "dividRegistDate": DAY, "dividOperateDate": "2024-01-03",
              "dividPayDate": "2024-01-04", "dividCashPsBeforeTax": ".069"}
    feed = SimpleNamespace(actions=[action])
    ledger.state["positions"][FUND] = [{"qty": 100, "available": DAY}]
    corporate_actions(ledger, feed, DAY, at_close=True)
    corporate_actions(ledger, feed, "2024-01-03")
    assert sum(ledger.state["receivables"].values()) == 690
    assert ledger.state["cash"] == 1000000
    corporate_actions(ledger, feed, "2024-01-04")
    assert not ledger.state["receivables"] and ledger.state["cash"] == 1000690


def test_cached_quote_precision_detects_bad_ticks_without_changing_source():
    from trading.research.quote_precision import inspect
    frame = pd.DataFrame({"date": [DAY], "symbol": ["510300"], "open": [3.937],
                          "high": [3.945], "low": [3.9371], "close": [float("nan")]})
    before = frame.copy(deep=True)
    result = inspect(frame, "510300")
    assert result["price_cells"] == 4 and result["subcent_cells"] == 2
    assert result["invalid_mill_tick_cells"] == 2
    assert result["max_cent_rounding_error_rmb"] == "0.005"
    pd.testing.assert_frame_equal(frame, before)
