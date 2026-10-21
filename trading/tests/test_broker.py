from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from trading.broker import BrokerAdapter
from trading.config import StrategyConfig
from trading.contracts import (ExecutionEvent, OrderIntent, Price, PriceBasis, RunContext,
                               SecurityId, ShareQuantity, TradingMode)
from trading.engine import simulate_order
from trading.ledger import Ledger, new_state
from trading.rules import fees
from trading.simulated_broker import SimulatedBrokerAdapter


@pytest.fixture
def broker():
    context = RunContext("adapter-test", TradingMode.PAPER)
    book = Ledger(new_state(10000), StrategyConfig(), context)
    book.state["day"] = "2024-01-02"
    return SimulatedBrokerAdapter(book)


def intent(broker, **changes):
    value = OrderIntent("order-1", broker.ledger.context, "2024-01-02", SecurityId("000001.SZSE"),
                        "buy", ShareQuantity(200), Price(1000, PriceBasis.RAW), "test")
    return replace(value, **changes)


def test_broker_uses_existing_account_and_typed_events(broker):
    assert isinstance(broker, BrokerAdapter)
    request = intent(broker)
    initial = broker.ledger.state["cash"]
    order = broker.submit(request)
    assert broker.submit(request) is order
    assert broker.ledger.state["cash"] == initial
    assert broker.read("orders", trading_date=request.day)["rows"][0]["status"] == "accepted"
    fill = ExecutionEvent("fill-1", request.id, request.context, request.day, request.security, request.side,
                          ShareQuantity(100), request.limit_price, "2024-01-03")
    assert broker.apply_execution(fill) is True
    assert broker.apply_execution(fill) is False
    assert order["status"] == "partial"
    account = broker.read("account", trading_date=request.day)
    assert account["source"] == "simulated_ledger" and account["observed_at"] is None
    assert account["context"] == request.context.to_dict()
    assert account["positions"]["000001.SZSE"] == {"quantity": 100, "sellable": 0}
    assert account["cash_total"] == initial - 100000 - fees(100000, "buy", broker.ledger.config)
    assert account["cash_available"] < account["cash_total"]
    assert broker.cancel(request.id, day=request.day)["status"] == "cancelled"
    assert broker.read("account", trading_date=request.day)["cash_available"] == account["cash_total"]
    trades = broker.read("trades", trading_date=request.day)
    assert trades["rows"][0]["price_cents"] == 1000
    trades["rows"][0]["qty"] = 999
    assert broker.read("trades", trading_date=request.day)["rows"][0]["qty"] == 100
    broker.ledger.state["day"] = "2024-01-03"
    assert broker.read("account", trading_date="2024-01-03")["positions"]["000001.SZSE"]["sellable"] == 100
    assert not broker.read("orders", trading_date="2024-01-03")["rows"]


def test_simulation_context_and_dates_cannot_be_promoted(broker):
    before = deepcopy(broker.ledger.state)
    with pytest.raises(ValueError, match="account/mode"):
        broker.submit(intent(broker, context=RunContext("other", TradingMode.REPLAY)))
    assert broker.ledger.state == before
    for kind, day in (("invalid", "2024-01-02"), ("account", "2024-01-03"), ("trades", "2024-1-2")):
        with pytest.raises(ValueError):
            broker.read(kind, trading_date=day)
    broker.submit(intent(broker))
    before = deepcopy(broker.ledger.state)
    with pytest.raises(ValueError, match="predates"):
        broker.cancel("order-1", day="2024-01-01")
    assert broker.ledger.state == before
    with pytest.raises(NotImplementedError):
        broker.prefill({})


def test_engine_really_routes_order_fill_cancel_to_shared_adapter(broker, monkeypatch):
    calls = []

    class RecordingBroker(SimulatedBrokerAdapter):
        def submit(self, value):
            calls.append(("submit", value.id))
            return super().submit(value)

        def apply_execution(self, event):
            calls.append(("fill", event.quantity.shares))
            return super().apply_execution(event)

        def cancel(self, order_id, *, day):
            calls.append(("cancel", order_id))
            return super().cancel(order_id, day=day)

    monkeypatch.setattr("trading.engine.SimulatedBrokerAdapter", RecordingBroker)
    quote = {"open": 10., "preclose": 10., "isST": 0, "tradestatus": 1, "adjustflag": "3"}
    feed = SimpleNamespace(next_day=lambda day: "2024-01-03",
                           calendar=["2024-01-02", "2024-01-03"],
                           instruments={"000001.SZSE": {"ipoDate": "2000-01-01"}})
    simulate_order(broker.ledger, feed,
                   "2024-01-02", "000001.SZSE", "buy", 300, quote, 20000)
    assert [kind for kind, _ in calls] == ["submit", "fill", "fill", "cancel"]
    order = next(iter(broker.ledger.state["orders"].values()))
    assert order["status"] == "cancelled" and order["filled"] == 200
