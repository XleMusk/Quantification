from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import _execute_session_opening, _mark_session_close, advance
from trading.ledger import Ledger, new_state


def test_opening_needs_no_execution_close_and_closing_marks_the_same_ledger_separately():
    prior, day, following = "2024-01-02", "2024-01-03", "2024-01-04"
    symbol = "600001.SSE"
    config = StrategyConfig(initial_cash=10000, single_limit=.3, slippage_bps=0)
    state = new_state(config.initial_cash)
    state.update(day=prior, pending_targets={symbol: .2})
    book = Ledger(state, config, RunContext("split_kernel_fixture", TradingMode.PAPER))
    opening = {symbol: dict(open=10., preclose=10., tradestatus=1, isST=0, adjustflag="3")}
    feed = SimpleNamespace(calendar=[prior, day, following], actions=[],
        instruments={symbol: {"ipoDate": "1991-01-01", "outDate": ""}},
        volumes=pd.DataFrame({symbol: [1_000_000.]}, index=[prior]),
        next_day=lambda value: [prior, day, following][[prior, day, following].index(value)+1])
    _execute_session_opening(book, feed, day, opening, before_events=0)
    order = state["orders"][f"{day}:{symbol}:buy"]
    assert order["status"] == "filled" and order["filled"] == 200
    assert len(state["fills"]) == 2
    assert state["cash"] == 1_000_000 - 200_000 - order["fees"]
    assert book.quantity(symbol) == 200
    assert book.available(symbol, day) == 0 and book.available(symbol, following) == 200
    assert state["prices"][symbol] == 1000 and state["pending_targets"] is None
    assert state["day"] == prior  # The caller owns the separate phase commit.
    cash, fills = state["cash"], deepcopy(state["fills"])
    observed = []
    action_view = SimpleNamespace(record_entitlements=lambda value, date: observed.append((value, date)))
    assert _mark_session_close(book, feed, day,
        {symbol: {"close": 12., "tradestatus": 1}}, action_view=action_view) == []
    assert state["prices"][symbol] == 1200 and book.equity() == cash + 240_000
    assert state["cash"] == cash and state["fills"] == fills
    assert observed == [(book, day)]
    state["intraday_open_session"] = {"day": day}
    before = deepcopy(state)
    with pytest.raises(ValueError, match="intraday session"):
        advance(state, None, following, config, 0, context=book.context)
    assert state == before
