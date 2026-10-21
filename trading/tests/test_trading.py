from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from trading import api, settings, store
from trading.config import StrategyConfig
from trading.contracts import (ExecutionEvent, IntentKind, OrderIntent, PortfolioIntent, RunContext,
                               SignalFrame, SignalKind, SignalUnavailable, TradingMode)
from trading.engine import advance, choose_targets, corporate_actions, simulate_order
from trading.feed import HistoricalFeed
from trading.ledger import Ledger, new_state
from trading.research.experiment import ScoreFeed
from trading.rules import cents, fees, price_limits
from trading.signals import from_qlib
from trading.worker import step
from trading.xtquant_adapter import XtQuantAdapter
from webapp.backend.app import data as web_data, main, settings as web_settings, store as web_store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "trading/db.sqlite3")
    monkeypatch.setattr(settings, "DATASET", tmp_path / "feed")
    for module in (web_settings, web_store, web_data, main):
        monkeypatch.setattr(module, "STORAGE", tmp_path / "web", raising=False)
    store.initialize()


@pytest.fixture
def config():
    return StrategyConfig(initial_cash=10000, holdings=1, retain_rank=2, gross_limit=.8, single_limit=.8)


@pytest.fixture
def feed():
    days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    symbols = ["600000.SSE", "000001.SZSE"]
    row = {"open": 10., "close": 10., "preclose": 10., "isST": 0, "tradestatus": 1}
    return SimpleNamespace(calendar=days, rows={d: {s: dict(row) for s in symbols} for d in days},
        instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in symbols},
        actions=[], volumes=pd.DataFrame(1000000., index=days, columns=symbols),
        manifest={"start": days[0], "end": days[-1]},
        next_day=lambda d: days[days.index(d) + 1] if d != days[-1] else "9999-12-31",
        scores=lambda day, config: [{"symbol": symbols[0], "score": 1.}])


def test_cash_changes_only_after_fill_and_min_fee_once(config):
    book = Ledger(new_state(10000), config)
    order = book.submit("a", "2024-01-02", "600000.SSE", "buy", 200, 1000)
    assert book.state["cash"] == 1000000 and order["status"] == "submitted"
    book.fill("f1", "a", 100, 1000, "2024-01-02", "2024-01-03")
    assert order["status"] == "partial"
    assert book.cash_available() >= 0
    book.fill("f2", "a", 100, 1000, "2024-01-02", "2024-01-03")
    assert order["status"] == "filled"
    assert book.state["cash"] == 1000000 - 200000 - fees(200000, "buy", config)


def test_duplicate_callback_and_conflict(config):
    book = Ledger(new_state(10000), config)
    book.submit("a", "2024-01-02", "600000.SSE", "buy", 100, 1000)
    book.fill("f1", "a", 100, 1000, "2024-01-02", "2024-01-03")
    before = deepcopy(book.state)
    assert not book.fill("f1", "a", 100, 1000, "2024-01-02", "2024-01-03")
    assert before == book.state
    with pytest.raises(ValueError):
        book.fill("f1", "a", 99, 1000, "2024-01-02", "2024-01-03")


def test_T1_and_cash_and_lot_rejections(config):
    book = Ledger(new_state(10000), config)
    assert book.submit("lot", "2024-01-02", "600000.SSE", "buy", 99, 1000)["reason"] == "buy_lot_must_be_100"
    assert book.submit("cash", "2024-01-02", "600000.SSE", "buy", 20000, 1000)["reason"] == "insufficient_cash"
    book.submit("buy", "2024-01-02", "600000.SSE", "buy", 100, 1000)
    book.fill("f", "buy", 100, 1000, "2024-01-02", "2024-01-03")
    assert book.submit("sell", "2024-01-02", "600000.SSE", "sell", 100, 1000)["reason"] == "insufficient_sellable_T1"
    assert book.available("600000.SSE", "2024-01-03") == 100


def test_sell_reservation_prevents_double_sale(config):
    book = Ledger(new_state(10000), config)
    book.state["positions"]["600000.SSE"] = [{"qty": 100, "available": "2024-01-02"}]
    book.submit("one", "2024-01-02", "600000.SSE", "sell", 100, 1000)
    assert book.submit("two", "2024-01-02", "600000.SSE", "sell", 100, 1000)["status"] == "rejected"


def test_cancel_confirmation_preserves_partial_holdings(config):
    book = Ledger(new_state(10000), config)
    order = book.submit("a", "2024-01-02", "600000.SSE", "buy", 200, 1000)
    book.fill("f", "a", 100, 1000, "2024-01-02", "2024-01-03")
    book.request_cancel("a", "2024-01-02")
    assert order["status"] == "cancel_pending" and order["reserve"] > 0
    book.confirm_cancel("a", "2024-01-02")
    assert order["status"] == "cancelled" and order["reserve"] == 0 and book.quantity("600000.SSE") == 100


def test_order_idempotency_conflict(config):
    book = Ledger(new_state(10000), config)
    one = book.submit("a", "2024-01-02", "600000.SSE", "buy", 100, 1000)
    assert book.submit("a", "2024-01-02", "600000.SSE", "buy", 100, 1000) is one
    with pytest.raises(ValueError):
        book.submit("a", "2024-01-02", "600000.SSE", "buy", 200, 1000)


def test_rule_effective_date_and_rounding():
    assert price_limits(10, "2026-07-03", True) == (950, 1050)
    assert price_limits(10, "2026-07-06", True) == (900, 1100)
    assert cents("1.005") == 101
    with pytest.raises(ValueError):
        price_limits(10, "2024-01-02", board="bse")


@pytest.mark.parametrize("symbol", ["688001.SSE", "300001.SZSE"])
def test_multiboard_targets_trade_at_fifteen_percent_open_in_shared_engine(feed, config, symbol):
    for day, rows in feed.rows.items():
        rows[symbol] = dict(rows["600000.SSE"])
    feed.rows[feed.calendar[1]][symbol].update(open=11.5, close=11.5)
    feed.instruments[symbol] = {"ipoDate": "2000-01-01", "outDate": ""}
    feed.volumes[symbol] = 1000000.
    feed.scores = lambda day, cfg: [{"symbol": symbol, "score": 1.}]
    state = new_state(config.initial_cash)
    advance(state, feed, feed.calendar[0], config, 0)
    assert state["pending_targets"] == {symbol: .8} and not state["fills"]
    result = advance(state, feed, feed.calendar[1], config, 1)
    order = state["orders"][f"{feed.calendar[1]}:{symbol}:buy"]
    assert order["status"] == "filled" and result["turnover_value"] > 0
    if symbol.startswith("688"):
        assert order["qty"] >= 200 and order["qty"] % 100 != 0
    else:
        assert order["qty"] % 100 == 0
    assert Ledger(state, config).available(symbol, feed.calendar[1]) == 0
    assert Ledger(state, config).available(symbol, feed.calendar[2]) == order["qty"]


def test_rank_buffer_and_unfilled_slots_hold_cash(config):
    candidates = [{"symbol": "new", "score": 2}, {"symbol": "held", "score": 1}]
    assert choose_targets(candidates, ["held"], config) == {"held": .8}
    assert choose_targets([], ["held"], config) == {}
    other = StrategyConfig(holdings=5, single_limit=.2, gross_limit=.8)
    assert sum(choose_targets(candidates[:1], [], other).values()) == .16


def test_pending_target_contract_distinguishes_no_change_and_empty_exit():
    assert PortfolioIntent.from_legacy_pending(None).kind == IntentKind.NO_CHANGE
    empty = PortfolioIntent.from_legacy_pending({})
    assert empty.kind == IntentKind.FULL_TARGET and empty.to_legacy_pending() == {}
    assert PortfolioIntent.no_change("missing_batch").to_legacy_pending() is None
    with pytest.raises(ValueError, match="not supported"):
        PortfolioIntent.delta({"600000.SSE": -.2}).to_legacy_pending()


def test_missing_model_batch_preserves_held_position(feed, config):
    model_feed = ScoreFeed(feed, pd.DataFrame(columns=["datetime", "instrument", "score", "available_at"]))
    assert isinstance(model_feed.scores(feed.calendar[0], config), SignalUnavailable)
    state = new_state(config.initial_cash)
    state["positions"]["600000.SSE"] = [{"qty": 100, "available": feed.calendar[0]}]
    result = advance(state, model_feed, feed.calendar[0], config, 0)
    assert state["pending_targets"] is None and Ledger(state, config).quantity("600000.SSE") == 100
    assert not state["orders"] and result["events"][-1]["kind"] == "no_decision"


def test_valid_empty_scores_explicitly_exit_next_session(feed, config):
    feed.scores = lambda day, cfg: []
    state = new_state(config.initial_cash)
    state["positions"]["600000.SSE"] = [{"qty": 100, "available": feed.calendar[0]}]
    first = advance(state, feed, feed.calendar[0], config, 0)
    assert state["pending_targets"] == {} and not state["orders"]
    assert first["events"][-1]["kind"] == "targets_generated"
    advance(state, feed, feed.calendar[1], config, 1)
    assert Ledger(state, config).quantity("600000.SSE") == 0
    assert any(order["side"] == "sell" for order in state["orders"].values())


def test_rule_feed_and_simulator_consume_typed_contracts(feed, config, monkeypatch):
    rule_feed = HistoricalFeed.__new__(HistoricalFeed)
    rule_feed.__dict__.update(vars(feed))
    context = RunContext("paper-account", TradingMode.PAPER)
    batch = rule_feed.signal_frame(feed.calendar[0], config, context)
    assert isinstance(batch, SignalFrame) and batch.source == "rule:" + config.signal
    received = []
    submit, fill = Ledger.submit_intent, Ledger.apply_execution
    def capture_submit(book, intent):
        received.append(intent)
        return submit(book, intent)
    def capture_fill(book, event):
        received.append(event)
        return fill(book, event)
    monkeypatch.setattr(Ledger, "submit_intent", capture_submit)
    monkeypatch.setattr(Ledger, "apply_execution", capture_fill)
    state = new_state(config.initial_cash)
    advance(state, rule_feed, feed.calendar[0], config, 0, context=context)
    advance(state, rule_feed, feed.calendar[1], config, 1, context=context)
    assert any(isinstance(value, OrderIntent) for value in received)
    assert any(isinstance(value, ExecutionEvent) for value in received)
    assert all(value.context == context for value in received)
    assert state["fills"] and state["pending_targets"] is None


def test_proxy_scores_are_not_bypassed_by_base_rule_frame(feed, config):
    from trading.research.seat_trend import TrendFeed
    base = HistoricalFeed.__new__(HistoricalFeed)
    base.__dict__.update(vars(feed))
    trend = TrendFeed(base, {feed.calendar[0]: [{"symbol": "000001.SZSE", "score": 2.}]})
    state = new_state(config.initial_cash)
    advance(state, trend, feed.calendar[0], config, 0)
    assert state["candidates"] == [{"symbol": "000001.SZSE", "score": 2.}]
    assert set(state["pending_targets"]) == {"000001.SZSE"}


def test_missing_trend_batch_is_not_an_empty_liquidation_target(feed, config):
    from trading.research.seat_trend import TrendFeed
    state = new_state(config.initial_cash)
    state["positions"]["600000.SSE"] = [{"qty": 100, "available": feed.calendar[0]}]
    result = advance(state, TrendFeed(feed, {}), feed.calendar[0], config, 0)
    assert state["pending_targets"] is None and not state["orders"]
    assert result["events"][-1]["reason"] == "trend_score_batch_missing"
    assert Ledger(state, config).quantity("600000.SSE") == 100


def test_invalid_typed_score_semantics_fail_before_account_mutation(feed, config):
    from dataclasses import replace
    context = RunContext("standalone_replay", TradingMode.REPLAY)
    batch = SignalFrame.from_legacy(feed.scores(feed.calendar[0], config), feed.calendar[0], "test", context)
    expected_return = replace(batch, signals=(replace(batch.signals[0], kind=SignalKind.EXPECTED_RETURN, horizon_sessions=5),))
    state = new_state(config.initial_cash)
    state["pending_targets"] = {"600000.SSE": .8}
    before = deepcopy(state)
    for invalid in (expected_return, batch.with_context(RunContext("other-account", TradingMode.REPLAY))):
        feed.scores = lambda day, cfg: invalid
        with pytest.raises(ValueError):
            advance(state, feed, feed.calendar[0], config, 0)
        assert state == before


def test_simulator_rejects_explicit_adjusted_quotes(feed, config):
    book = Ledger(new_state(10000), config)
    before = deepcopy(book.state)
    quote = {**feed.rows[feed.calendar[0]]["600000.SSE"], "adjustflag": "2"}
    with pytest.raises(ValueError, match="adjusted"):
        simulate_order(book, feed, feed.calendar[0], "600000.SSE", "buy", 100, quote, 1000000)
    assert book.state == before


def test_fixed_250_member_hand_calculated_cash_and_T1(config):
    # 600000.SSE raw opens: 2024-01-03 6.59, 2024-01-04 6.64.
    # At 10 bps slippage these become 6.60 buy and 6.63 sell.
    buy_price = cents(6.59 * (1 + config.slippage_bps / 10000))
    sell_price = cents(6.64 * (1 - config.slippage_bps / 10000))
    assert (buy_price, sell_price) == (660, 663)
    book = Ledger(new_state(10000), config)
    buy = book.submit("reference-buy", "2024-01-03", "600000.SSE", "buy", 100, buy_price)
    assert buy["status"] == "submitted"
    book.fill("reference-buy-fill", "reference-buy", 100, buy_price, "2024-01-03", "2024-01-04")
    assert book.state["cash"] == 933499  # 10000 - 660 - (5 minimum fee + .01 transfer)
    assert book.submit("early-sell", "2024-01-03", "600000.SSE", "sell", 100, sell_price)["reason"] == "insufficient_sellable_T1"
    sell = book.submit("reference-sell", "2024-01-04", "600000.SSE", "sell", 100, sell_price)
    assert sell["status"] == "submitted"
    book.fill("reference-sell-fill", "reference-sell", 100, sell_price, "2024-01-04", "2024-01-05")
    assert book.state["cash"] == 999265  # + 663 - (5 minimum fee + .01 transfer + .33 tax)
    assert book.quantity("600000.SSE") == 0


def test_signal_close_then_next_open_not_same_day(feed, config):
    state = new_state(config.initial_cash)
    advance(state, feed, feed.calendar[0], config, 0)
    assert not state["orders"]
    advance(state, feed, feed.calendar[1], config, 1)
    assert state["orders"] and all(o["day"] == feed.calendar[1] for o in state["orders"].values())
    assert state["cash"] >= 0


def test_limits_and_suspensions_block_fills(feed, config):
    for change in ({"open": 11.}, {"tradestatus": 0}, {"isST": 1}):
        book = Ledger(new_state(10000), config)
        quote = {**feed.rows[feed.calendar[1]]["600000.SSE"], **change}
        simulate_order(book, feed, feed.calendar[1], "600000.SSE", "buy", 100, quote, 1000000)
        assert not book.state["fills"] and book.state["events"][-1]["kind"] == "order_blocked"


def test_low_capacity_partial_then_cancel(feed, config):
    book = Ledger(new_state(10000), config)
    simulate_order(book, feed, feed.calendar[1], "600000.SSE", "buy", 300, feed.rows[feed.calendar[1]]["600000.SSE"], 10000)
    order = list(book.state["orders"].values())[0]
    assert order["filled"] == 100 and order["status"] == "cancelled"


def test_dividend_entitlement_record_ex_pay_dates(feed, config):
    book = Ledger(new_state(10000), config)
    book.state["positions"]["600000.SSE"] = [{"qty": 100, "available": "2024-01-02"}]
    feed.actions = [{"symbol": "600000.SSE", "dividRegistDate": "2024-01-02", "dividOperateDate": "2024-01-03",
                     "dividPayDate": "2024-01-04", "dividCashPsBeforeTax": "1", "dividStocksPs": "0", "dividReserveToStockPs": "0"}]
    corporate_actions(book, feed, "2024-01-02", at_close=True)
    corporate_actions(book, feed, "2024-01-03")
    assert book.state["cash"] == 1000000 and sum(book.state["receivables"].values()) == 8000
    corporate_actions(book, feed, "2024-01-04")
    assert book.state["cash"] == 1008000 and not book.state["receivables"]


def test_day_replay_rejects_duplicate(feed, config):
    state = new_state(10000)
    advance(state, feed, feed.calendar[0], config, 0)
    with pytest.raises(ValueError):
        advance(state, feed, feed.calendar[0], config, 0)


def test_run_persistence_resume_and_serialized_sessions(feed, config):
    run = store.create("test", config.to_dict(), feed.calendar[0], feed.calendar[-1], "persistent-key")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: step(run["id"], feed), range(2)))
    assert len(store.daily(run["id"])) == 2
    store.control(run["id"], "pause")
    assert not step(run["id"], feed)
    store.initialize()
    assert store.get(run["id"])["cursor"] == 2
    store.control(run["id"], "resume")
    while store.get(run["id"])["status"] == "running":
        step(run["id"], feed)
    assert len(store.daily(run["id"])) == 4
    assert (settings.DATA_ROOT / "results" / run["id"] / "orders.csv").exists()


def test_failed_session_rolls_back(feed, config, monkeypatch):
    from trading import worker
    run = store.create("test", config.to_dict(), feed.calendar[0], feed.calendar[-1], "rollback-key")
    def broken(state, *args, **kwargs):
        state["cash"] = -123
        raise RuntimeError("interrupted before atomic commit")
    monkeypatch.setattr(worker, "advance", broken)
    with pytest.raises(RuntimeError):
        step(run["id"], feed)
    assert store.get(run["id"], full=True)["state"]["cash"] == 1000000
    assert not store.daily(run["id"])


@pytest.mark.parametrize("mode,expected", [("replay", TradingMode.REPLAY), ("paper", TradingMode.PAPER)])
def test_worker_propagates_account_and_mode(feed, config, monkeypatch, mode, expected):
    from trading import worker
    run = store.create("context", config.to_dict(), feed.calendar[0], feed.calendar[-1], "context-key", mode)
    received = []
    def capture(state, *args, **kwargs):
        received.append(kwargs["context"])
        return advance(state, *args, **kwargs)
    monkeypatch.setattr(worker, "advance", capture)
    assert step(run["id"], feed, datetime(2024, 1, 2, 16, 16, tzinfo=timezone(timedelta(hours=8))))
    assert received == [RunContext(run["id"], expected)]


def test_paper_waits_for_closed_session(feed, config):
    run = store.create("paper", config.to_dict(), "2024-01-02", "2024-01-05", "paper-key", "paper")
    assert not step(run["id"], feed, datetime(2024, 1, 2, 15, 0))
    assert step(run["id"], feed, datetime(2024, 1, 2, 16, 15))
    assert not step(run["id"], feed, datetime(2024, 1, 2, 16, 20))


def test_api_origin_idempotency_and_live_disabled(feed, config, monkeypatch):
    monkeypatch.setattr(api, "HistoricalFeed", lambda: feed)
    monkeypatch.setattr(api, "freeze", lambda feed: settings.DATASET)
    body = {"name": "test", "start": feed.calendar[0], "end": feed.calendar[-1], "config": config.to_dict()}
    headers = {"X-Local-Request": "1", "Idempotency-Key": "api-idempotent-key"}
    with TestClient(main.app) as client:
        assert client.post("/api/trading/runs", json=body).status_code == 403
        one = client.post("/api/trading/runs", json=body, headers=headers)
        assert one.status_code == 202, one.text
        assert client.post("/api/trading/runs", json=body, headers=headers).json()["id"] == one.json()["id"]
        assert client.get("/api/trading/overview").json()["broker"]["live_enabled"] is False
        assert client.post("/api/trading/runs", json={**body, "mode": "live"}, headers=headers).status_code == 400


def test_qlib_signal_availability_gate():
    frame = pd.DataFrame([{"datetime": "2024-01-02", "instrument": "SH600000", "score": .2, "available_at": "2024-01-02T15:00:00+08:00"}])
    assert from_qlib(frame, "2024-01-02")[0]["symbol"] == "600000.SSE"
    frame.loc[0, "available_at"] = "2024-01-03T15:00:00+08:00"
    with pytest.raises(ValueError):
        from_qlib(frame, "2024-01-02")


def test_xtquant_submission_is_not_a_fill_and_default_is_locked():
    calls = []
    trader = SimpleNamespace(order_stock=lambda *args: calls.append(args) or 123, cancel_order_stock=lambda *args: 0)
    constants = SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11)
    adapter = XtQuantAdapter(trader, "test-only", constants)
    adapter.connected = True
    with pytest.raises(PermissionError):
        adapter.submit("a", "600000.SSE", "buy", 100, 10)
    assert not calls
    adapter.live_enabled = True  # Fake SDK only; app never enables this path.
    assert adapter.submit("a", "600000.SSE", "buy", 100, 10)["status"] == "submitted"
    assert not adapter.events
    adapter.submit("a", "600000.SSE", "buy", 100, 10)
    assert len(calls) == 1
    assert adapter.cancel(123)["status"] == "cancel_pending"
    adapter.on_disconnected()
    with pytest.raises(ConnectionError):
        adapter.submit("b", "600000.SSE", "buy", 100, 10)


def test_dataset_end_purchase_unlocks_when_next_session_arrives(feed, config):
    feed.next_day = lambda day: HistoricalFeed.next_day(feed, day)
    book = Ledger(new_state(10000), config)
    day = feed.calendar[-1]
    simulate_order(book, feed, day, "600000.SSE", "buy", 100, feed.rows[day]["600000.SSE"], 1000000)
    assert book.available("600000.SSE", day) == 0
    feed.calendar.append("2024-01-08")
    assert book.available("600000.SSE", "2024-01-08") == 100


def test_future_prices_do_not_change_prior_features():
    feed = HistoricalFeed.__new__(HistoricalFeed)
    days = pd.bdate_range("2023-01-02", periods=180).strftime("%Y-%m-%d").tolist()
    symbol = "600000.SSE"
    feed.calendar, feed.symbols = days, [symbol]
    feed.closes = pd.DataFrame({symbol: [10 + i * .05 + (i % 7) * .01 for i in range(180)]}, index=days)
    feed.amounts = pd.DataFrame(100000000., index=days, columns=[symbol])
    feed.rows = {d: {symbol: {"tradestatus": 1, "isST": 0}} for d in days}
    feed.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""}}
    feed.manifest = {"selection_date": "2022-12-30"}
    feed._features = {}
    config = StrategyConfig()
    before = feed.scores(days[150], config)
    assert before
    feed.closes.loc[days[151]:, symbol] *= 100
    feed.amounts.loc[days[151]:, symbol] = 0
    feed._features = {}
    assert feed.scores(days[150], config) == before


def test_odd_lot_full_exit_and_bonus_unlock(feed, config):
    book = Ledger(new_state(10000), config)
    book.state["positions"]["600000.SSE"] = [{"qty": 100, "available": "2024-01-02"}]
    feed.actions = [{"symbol": "600000.SSE", "dividRegistDate": "2024-01-02", "dividOperateDate": "2024-01-03",
                     "dividStockMarketDate": "2024-01-04", "dividStocksPs": ".15", "bonus_taxable_cash_ps": ".15",
                     "dividCashPsBeforeTax": ".10", "dividPayDate": "2024-01-03"}]
    corporate_actions(book, feed, "2024-01-02", at_close=True)
    corporate_actions(book, feed, "2024-01-03")
    assert book.state["cash"] == 1000500
    assert book.quantity("600000.SSE") == 115
    assert book.available("600000.SSE", "2024-01-03") == 100
    assert book.available("600000.SSE", "2024-01-04") == 115
    assert book.submit("odd", "2024-01-04", "600000.SSE", "sell", 15, 1000)["status"] == "rejected"
    assert book.submit("exit", "2024-01-04", "600000.SSE", "sell", 115, 1000)["status"] == "submitted"
    book.fill("exit-fill", "exit", 115, 1000, "2024-01-04", "2024-01-05")
    assert book.quantity("600000.SSE") == 0


@pytest.mark.parametrize("basis,cash,reason", [(None, ".6", "bonus taxable basis"),
                                              ("NaN", ".6", "bonus taxable basis"),
                                              (".4", "0", "exceeds cash dividend")])
def test_unknown_or_unfunded_bonus_tax_stops_before_distribution_mutation(feed, config, basis, cash, reason):
    book = Ledger(new_state(10000), config)
    book.state["positions"]["600000.SSE"] = [{"qty": 1000, "available": "2024-01-02"}]
    feed.actions = [{"symbol": "600000.SSE", "dividRegistDate": "2024-01-02", "dividOperateDate": "2024-01-03",
                     "dividStockMarketDate": "2024-01-04", "dividStocksPs": ".4", "bonus_taxable_cash_ps": basis,
                     "dividCashPsBeforeTax": cash, "dividPayDate": "2024-01-03"}]
    corporate_actions(book, feed, "2024-01-02", at_close=True)
    before = deepcopy(book.state)
    with pytest.raises(ValueError, match=reason):
        corporate_actions(book, feed, "2024-01-03")
    assert book.state == before


def test_limit_down_sell_stays_held(feed, config):
    book = Ledger(new_state(10000), config)
    book.state["positions"]["600000.SSE"] = [{"qty": 100, "available": "2024-01-02"}]
    simulate_order(book, feed, "2024-01-03", "600000.SSE", "sell", 100,
                   {**feed.rows["2024-01-03"]["600000.SSE"], "open": 9.}, 1000000)
    assert book.quantity("600000.SSE") == 100
    assert book.state["events"][-1]["reason"] == "limit_down_queue_unknown"


@pytest.mark.parametrize("problem", ["delisted", "missing", "nan", "zero"])
def test_unresolved_held_value_stops_before_account_changes(feed, config, problem):
    state = new_state(10000)
    symbol, day = "600000.SSE", "2024-01-03"
    state["positions"][symbol] = [{"qty": 100, "available": "2024-01-02"}]
    state["prices"][symbol] = 1000
    if problem == "delisted":
        feed.instruments[symbol]["outDate"] = day
    elif problem == "missing":
        del feed.rows[day][symbol]
    else:
        feed.rows[day][symbol]["close"] = float("nan") if problem == "nan" else 0
    before = deepcopy(state)
    with pytest.raises(ValueError, match="holding"):
        advance(state, feed, day, config, 1)
    assert state == before


def test_suspension_with_published_mark_is_visible_not_fabricated_fill(feed, config):
    state = new_state(10000)
    symbol, day = "600000.SSE", "2024-01-03"
    state["positions"][symbol] = [{"qty": 100, "available": "2024-01-02"}]
    state["prices"][symbol] = 1000
    state["pending_targets"] = {}
    feed.rows[day][symbol]["tradestatus"] = 0
    result = advance(state, feed, day, config, 1)
    assert result["stale_symbols"] == [symbol]
    assert not state["fills"] and Ledger(state, config).quantity(symbol) == 100


def test_fill_after_cancel_request_and_after_confirmation(config):
    book = Ledger(new_state(10000), config)
    book.submit("a", "2024-01-02", "600000.SSE", "buy", 300, 1000)
    book.request_cancel("a", "2024-01-02")
    book.fill("f", "a", 100, 1000, "2024-01-02", "2024-01-03")
    book.confirm_cancel("a", "2024-01-02")
    with pytest.raises(ValueError):
        book.fill("late", "a", 100, 1000, "2024-01-02", "2024-01-03")
    assert book.quantity("600000.SSE") == 100


def test_missing_score_timestamp_rejected():
    frame = pd.DataFrame([{"datetime": "2024-01-02", "instrument": "SH600000", "score": .2, "available_at": None}])
    with pytest.raises(ValueError):
        from_qlib(frame, "2024-01-02")


def test_broker_ambiguous_submission_not_blindly_retried():
    calls = []
    def ambiguous(*args):
        calls.append(args)
        raise TimeoutError("Submission result unknown")
    adapter = XtQuantAdapter(SimpleNamespace(order_stock=ambiguous), "fake", SimpleNamespace(STOCK_BUY=23, FIX_PRICE=11), True)
    adapter.connected = True
    with pytest.raises(TimeoutError):
        adapter.submit("a", "600000.SSE", "buy", 100, 10)
    assert adapter.submit("a", "600000.SSE", "buy", 100, 10)["status"] == "unknown"
    assert len(calls) == 1


def test_snapshot_is_part_of_request_identity(config):
    store.create("one", config.to_dict(), "2024-01-02", "2024-01-05", "snapshot-key", dataset="version-one")
    with pytest.raises(ValueError):
        store.create("two", config.to_dict(), "2024-01-02", "2024-01-05", "snapshot-key", dataset="version-two")


def test_feed_refresh_failure_visible_without_hiding_heartbeat():
    store.heartbeat()
    store.refresh_state("failed", "provider unavailable")
    health = store.health()
    assert health["online"] and health["refresh"]["error"] == "provider unavailable"


def test_interrupted_export_does_not_publish_false_completion(feed, config, monkeypatch):
    from trading import worker
    run = store.create("export", config.to_dict(), feed.calendar[0], feed.calendar[0], "export-key")
    step(run["id"], feed)
    real_export = worker.export
    def interrupted(*args, **kwargs):
        raise InterruptedError("Interrupted during artifact publication")
    monkeypatch.setattr(worker, "export", interrupted)
    with pytest.raises(InterruptedError):
        step(run["id"], feed)
    assert store.get(run["id"])["status"] == "running"
    monkeypatch.setattr(worker, "export", real_export)
    step(run["id"], feed)
    assert store.get(run["id"])["status"] == "completed"
    assert len(store.daily(run["id"])) == 1


def test_active_account_not_lost_behind_recent_list_limit(config):
    old = store.create("paper", config.to_dict(), "2026-09-26", "2027-09-26", "long-lived-paper", "paper")
    for index in range(51):
        run = store.create("later", config.to_dict(), "2024-01-02", "2024-01-05", f"later-{index}")
        store.control(run["id"], "pause")
    assert old["id"] not in {run["id"] for run in store.runs()}
    assert old["id"] in {run["id"] for run in store.runs(active_only=True)}
