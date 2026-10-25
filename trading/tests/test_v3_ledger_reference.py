"""V3 L01/L03 reference accounting scenarios.

These examples are intentionally small enough to recompute by hand.  They
exercise the same Ledger used by replay and paper simulation; they never call
a broker client or grant execution authority.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import corporate_actions
from trading.ledger import Ledger, new_state
from trading.rules import fees


DAY = "2024-01-02"
NEXT = "2024-01-03"
FOLLOWING = "2024-01-04"
SYMBOL = "600000.SSE"


@pytest.fixture
def config():
    return StrategyConfig(initial_cash=10000, slippage_bps=0)


def test_v3_l01_cash_holdings_fees_t1_and_duplicate_fill_are_recomputable(config):
    """A split fill and a next-session sale conserve the manually computed account."""
    state = new_state(config.initial_cash)
    book = Ledger(state, config, RunContext("v3-l01", TradingMode.REPLAY))

    buy = book.submit("buy-1", DAY, SYMBOL, "buy", 200, 1000)
    # 2,000.00 CNY gross + 5.00 commission + 0.02 transfer fee.
    assert fees(200000, "buy", config, day=DAY) == 502
    assert buy["reserve"] == 200502 and state["cash"] == 1000000

    assert book.fill("fill-1", "buy-1", 100, 1000, DAY, NEXT)
    before_duplicate = deepcopy(state)
    assert not book.fill("fill-1", "buy-1", 100, 1000, DAY, NEXT)
    assert state == before_duplicate
    with pytest.raises(ValueError, match="Conflicting execution"):
        book.fill("fill-1", "buy-1", 99, 1000, DAY, NEXT)
    assert state == before_duplicate

    assert book.fill("fill-2", "buy-1", 100, 1000, DAY, NEXT)
    assert state["cash"] == 799498  # 1,000,000 - 200,000 - 502 cents.
    assert book.quantity(SYMBOL) == 200
    assert book.available(SYMBOL, DAY) == 0
    assert book.available(SYMBOL, NEXT) == 200

    too_soon = book.submit("sell-too-soon", DAY, SYMBOL, "sell", 100, 1100)
    assert too_soon["status"] == "rejected"
    assert too_soon["reason"] == "insufficient_sellable_T1"

    sell = book.submit("sell-1", NEXT, SYMBOL, "sell", 100, 1100)
    assert sell["status"] == "submitted"
    assert fees(110000, "sell", config, day=NEXT) == 556  # 5.00 + 0.01 + 0.55 CNY.
    assert book.fill("sell-fill-1", "sell-1", 100, 1100, NEXT, FOLLOWING)
    assert state["cash"] == 908942  # 799,498 + 110,000 - 556 cents.
    assert book.quantity(SYMBOL) == 100
    book.mark_price(SYMBOL, 1100, NEXT)
    assert book.equity() == 1018942  # cash plus the remaining 100-share mark.

    # The durable event journal records one event per accepted transition and
    # keeps the order/fill identifiers needed to recompute the account.
    assert [event["sequence"] for event in state["events"]] == list(range(1, 7))
    assert {event["kind"] for event in state["events"]} >= {"submitted", "partial", "filled", "rejected"}
    assert state["events"][-1]["execution_id"] == "sell-fill-1"


def test_v3_l03_entitlement_receivable_and_payment_are_explicit(config):
    """An entitlement is a receivable until its stated payment date."""
    state = new_state(config.initial_cash)
    state["positions"][SYMBOL] = [{"qty": 100, "available": DAY}]
    book = Ledger(state, config, RunContext("v3-l03", TradingMode.REPLAY))
    feed = SimpleNamespace(actions=[{
        "symbol": SYMBOL,
        "dividRegistDate": DAY,
        "dividOperateDate": NEXT,
        "dividPayDate": FOLLOWING,
        "dividCashPsBeforeTax": "1",
        "dividStocksPs": "0",
        "dividReserveToStockPs": "0",
    }], manifest={})

    corporate_actions(book, feed, DAY, at_close=True)
    corporate_actions(book, feed, NEXT)
    identifier = f"{SYMBOL}:{NEXT}"
    # 100 CNY gross dividend less the configured 20% reserve = 80 CNY.
    assert state["cash"] == 1000000
    assert state["receivables"] == {identifier: 8000}
    assert book.quantity(SYMBOL) == 100

    corporate_actions(book, feed, FOLLOWING)
    assert state["cash"] == 1008000
    assert state["receivables"] == {}
    before_duplicate_payment = deepcopy(state)
    corporate_actions(book, feed, FOLLOWING)
    assert state == before_duplicate_payment
    assert [event["kind"] for event in state["events"]] == ["corporate_action", "dividend_paid"]
