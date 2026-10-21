"""Synthetic settlement fixtures; no broker orders or historical-return claims."""
from copy import deepcopy
import json

import pytest

from trading.config import StrategyConfig
from trading.contracts import (ExecutionEvent, OrderIntent, Price, PriceBasis, RunContext,
                               SecurityId, ShareQuantity, TradingMode)
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.rules import FUND_H1_2026_PROFILES, execution_rule
from trading.simulated_broker import SimulatedBrokerAdapter
from trading.tests.test_quote_precision import fixture_feed


DAY, NEXT = "2024-01-02", "2024-01-03"
CONTEXT = RunContext("t0_fixture", TradingMode.REPLAY)
T0 = {s: f"sse_{s[:6]}_secondary_T0_2024_2025" for s in ("511010.SSE", "518880.SSE")}


def intent(symbol, side="buy", qty=100, price="5.011"):
    return OrderIntent(f"{side}:{symbol}", CONTEXT, DAY, SecurityId(symbol), side,
                       ShareQuantity(qty), Price.from_rmb(price, PriceBasis.RAW, "mill"), "fixture")


def event(order, identifier, qty=None, *, available=DAY, same_day=True):
    return ExecutionEvent(identifier, order.id, CONTEXT, DAY, order.security, order.side,
                          ShareQuantity(qty or order.quantity.shares), order.limit_price, available,
                          same_day_resale=same_day)


@pytest.mark.parametrize("symbol,kind", [("511010.SSE", "bond_etf"), ("518880.SSE", "gold_etf")])
def test_t0_rules_are_explicit_dated_and_not_equity_profiles(symbol, kind):
    rule = execution_rule(symbol, DAY, T0)
    assert rule.fund_type == kind and rule.is_fund and not rule.equity_etf
    assert rule.same_day_resale and rule.price_unit == "mill"
    assert rule.quantity.rounded(150, "buy") == 100
    assert rule.limits(5.011, DAY) == (4510, 5512)
    with pytest.raises(ValueError, match="rule span"):
        execution_rule(symbol, "2026-01-02", T0)
    with pytest.raises(ValueError):
        execution_rule(symbol, DAY)


@pytest.mark.parametrize("symbol,kind", [("511010.SSE", "bond_etf"), ("518880.SSE", "gold_etf"),
                                       ("588000.SSE", "equity_etf")])
def test_new_reviewed_h1_profile_does_not_extend_old_or_unknown_profiles(symbol, kind):
    profiles = {symbol: FUND_H1_2026_PROFILES[symbol]}
    rule = execution_rule(symbol, "2026-06-30", profiles)
    assert rule.fund_type == kind and rule.price_unit == "mill"
    assert rule.same_day_resale == (kind != "equity_etf")
    assert rule.quantity.rounded(199, "buy") == 100
    for day in ("2023-12-29", "2026-07-01"):
        with pytest.raises(ValueError, match="rule span"):
            execution_rule(symbol, day, profiles)
    with pytest.raises(ValueError, match="rule span"):
        execution_rule(symbol, "2026-06-30", {symbol: profiles[symbol].replace("20260630", "20260930")})
    with pytest.raises(ValueError, match="rule span"):
        execution_rule(symbol, "2026-06-30", {symbol: []})


def test_t0_callbacks_roundtrip_and_restart_use_the_single_cash_ledger():
    ledger = Ledger(new_state(140000), StrategyConfig(), CONTEXT, instrument_profiles=T0)
    broker = SimulatedBrokerAdapter(ledger)
    buy = intent("518880.SSE", qty=1000)
    broker.submit(buy)
    first = event(buy, "f1", 1)
    assert broker.apply_execution(first)
    assert not broker.apply_execution(first)
    assert ledger.available(buy.security.symbol, DAY) == 1
    broker.apply_execution(event(buy, "f2", 999))
    assert ledger.state["orders"][buy.id]["notional"] == 501100
    assert ledger.state["orders"][buy.id]["fees"] == 500
    assert ledger.available(buy.security.symbol, DAY) == 1000
    ledger = Ledger(json.loads(json.dumps(ledger.state)), ledger.config, CONTEXT, instrument_profiles=T0)
    sell = intent("518880.SSE", side="sell", qty=1000, price="5.020")
    ledger.submit_intent(sell)
    assert ledger.apply_execution(event(sell, "f3"))
    assert ledger.quantity(sell.security.symbol) == 0
    assert ledger.state["cash"] == 14000000 - 100
    assert sum(f["fees"] for f in ledger.state["fills"].values()) == 1000
    assert ledger.state["fills"]["f1"]["available_day"] == DAY


def test_t0_wire_is_explicit_and_old_t1_wire_is_unchanged():
    order = intent("518880.SSE")
    fill = event(order, "f1")
    payload = fill.to_dict()
    assert payload["schema_version"] == 2 and payload["availability_rule"] == "same_day_resale"
    assert ExecutionEvent.from_dict(json.loads(json.dumps(payload))) == fill
    payload["availability_rule"] = "unknown"
    with pytest.raises(ValueError):
        ExecutionEvent.from_dict(payload)
    old = event(order, "old", available=NEXT, same_day=False)
    assert old.to_dict()["schema_version"] == 1 and "availability_rule" not in old.to_dict()
    assert ExecutionEvent.from_dict(old.to_dict()) == old
    with pytest.raises(ValueError):
        event(order, "undeclared", same_day=False)


def test_wrong_resale_rule_never_mutates_stock_or_t0_holdings():
    ledger = Ledger(new_state(140000), StrategyConfig(), CONTEXT, instrument_profiles=T0)
    gold = intent("518880.SSE")
    ledger.submit_intent(gold)
    before = deepcopy(ledger.state)
    with pytest.raises(ValueError, match="resale rule"):
        ledger.apply_execution(event(gold, "late", available=NEXT, same_day=False))
    assert ledger.state == before
    stock = OrderIntent("stock", CONTEXT, DAY, SecurityId("600000.SSE"), "buy",
                        ShareQuantity(100), Price(1000, PriceBasis.RAW), "fixture")
    ledger.submit_intent(stock)
    before = deepcopy(ledger.state)
    with pytest.raises(ValueError, match="resale rule"):
        ledger.apply_execution(event(stock, "early"))
    with pytest.raises(ValueError, match="settlement day"):
        ledger.fill("early_direct", stock.id, 100, 1000, DAY, DAY)
    assert ledger.state == before


def test_real_engine_assigns_t0_sellability_without_same_day_signal_execution():
    feed = fixture_feed()
    feed.instrument_profiles = {**feed.instrument_profiles, **T0}
    for symbol, price in (("511010.SSE", 145.123), ("518880.SSE", 5.011)):
        feed.instruments[symbol] = {"ipoDate": "2013-03-25", "outDate": ""}
        feed.volumes[symbol] = 1000000.
        for day in feed.calendar:
            feed.rows[day][symbol] = {"open": price, "close": price, "preclose": price,
                                     "tradestatus": 1, "isST": 0, "adjustflag": "3"}
    feed.scores = lambda day, config: [{"symbol": s, "score": 1.} for s in feed.instruments]
    config = StrategyConfig(initial_cash=140000, holdings=4, retain_rank=4, single_limit=.2,
                            gross_limit=.8, slippage_bps=0)
    state = new_state(config.initial_cash)
    advance(state, feed, DAY, config, 0, context=CONTEXT)
    assert not state["orders"]
    advance(state, feed, NEXT, config, 1, context=CONTEXT)
    ledger = Ledger(state, config, CONTEXT, instrument_profiles=feed.instrument_profiles)
    for symbol in T0:
        assert ledger.quantity(symbol) > 0 and ledger.available(symbol, NEXT) == ledger.quantity(symbol)
        assert all(f["available_day"] == f["day"] for f in state["fills"].values() if f["symbol"] == symbol)
    for symbol in ("600000.SSE", "510300.SSE"):
        assert ledger.quantity(symbol) > 0 and ledger.available(symbol, NEXT) == 0
    assert {o["day"] for o in state["orders"].values()} == {NEXT}
