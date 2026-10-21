from types import SimpleNamespace
from dataclasses import replace

import pytest

from trading.config import StrategyConfig
from trading.engine import simulate_order
from trading.ledger import Ledger, new_state
from trading.rules import (cents, listing_limit_reason, price_limits, restructuring_ex_price,
                           fees, stock_board, stock_quantity_rule)


@pytest.mark.parametrize("symbol,board", [("600000.SSE", "main"), ("000001.SZSE", "main"),
                                        ("300001.SZSE", "chinext"), ("301001.SZSE", "chinext"),
                                        ("688001.SSE", "star")])
def test_stock_board(symbol, board):
    assert stock_board(symbol) == board


@pytest.mark.parametrize("symbol", ["510300.SSE", "159001.SZSE", "688001.SZSE", "300001.SSE",
                                   "920001.BSE", "689009.SSE"])
def test_unsupported_instruments_do_not_inherit_main_board_rules(symbol):
    with pytest.raises(ValueError, match="Unsupported"):
        stock_board(symbol)


@pytest.mark.parametrize("board", ["chinext", "star"])
@pytest.mark.parametrize("is_st", [False, True])
@pytest.mark.parametrize("day", ["2023-01-03", "2024-01-02", "2026-07-03", "2026-07-06"])
def test_twenty_percent_boards_include_risk_warning_stocks(board, is_st, day):
    assert price_limits(10, day, is_st, board) == (800, 1200)


def test_2023_established_main_board_limits_and_earlier_boundary():
    assert price_limits(10, "2023-01-03") == (900, 1100)
    assert price_limits(10, "2023-01-03", True) == (950, 1050)
    with pytest.raises(ValueError, match="from 2023"):
        price_limits(10, "2022-12-30")


def test_2023_tax_cut_flows_through_the_existing_ledger():
    config = StrategyConfig()
    book = Ledger(new_state(140000), config)
    assert book.fee(10000000, "sell", "600000.SSE", "2023-08-25") == 13100
    assert book.fee(10000000, "sell", "600000.SSE", "2023-08-28") == 8100
    assert book.fee(10000000, "buy", "600000.SSE", "2023-08-25") == 3100
    assert book.fee(10000000, "sell", "600000.SSE", "2024-01-02") == fees(10000000, "sell", config)
    assert fees(10000000, "sell", config, day="2023-08-25", equity_etf=True) == 3000
    assert fees(10000000, "sell", replace(config, sell_tax=0), day="2023-08-25") == 3100


def test_star_quantity_minimum_step_limit_and_residual_exit():
    rule = stock_quantity_rule("688001.SSE")
    assert rule.rejection(199, "buy") == "buy_minimum_200"
    assert rule.rounded(199, "buy") == 0
    assert rule.rounded(201, "buy") == 201
    assert rule.rejection(201, "buy") is None
    assert rule.rejection(100000, "buy") is None
    assert rule.rejection(100001, "buy") == "quantity_above_board_order_limit"
    assert rule.rounded(100001, "buy") == 100000
    assert rule.rejection(199, "sell", 500) == "sell_minimum_200_or_full_exit"
    assert rule.rounded(199, "sell", 500) == 0
    assert rule.rejection(199, "sell", 199) is None
    assert rule.rounded(199, "sell", 199) == 199
    assert rule.rejection(201, "sell", 500) is None


@pytest.mark.parametrize("symbol,maximum", [("600000.SSE", 1000000), ("300001.SZSE", 300000)])
def test_hundred_share_boards_limit_and_full_odd_exit(symbol, maximum):
    rule = stock_quantity_rule(symbol)
    assert rule.rejection(101, "buy") == "buy_lot_must_be_100"
    assert rule.rounded(101, "buy") == 100
    assert rule.rejection(maximum, "buy") is None
    assert rule.rejection(maximum + 100, "buy") == "quantity_above_board_order_limit"
    assert rule.rejection(101, "sell", 200) == "odd_lot_must_be_full_exit"
    assert rule.rounded(101, "sell", 101) == 101


def test_first_five_listing_sessions_use_calendar_not_elapsed_days():
    days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"]
    assert listing_limit_reason("2024-01-02", days[4], days) == "unsupported_first_five_listing_sessions"
    assert listing_limit_reason("2024-01-02", days[5], days) is None
    assert listing_limit_reason("2024-01-01", days[0], days) == "listing_calendar_incomplete"
    assert listing_limit_reason("2000-01-01", days[0], days) is None
    assert listing_limit_reason("2024-01-03", days[0], days) == "not_yet_listed"
    assert listing_limit_reason("", days[0], days) == "missing_listing_date"


def test_star_partial_fill_and_T1_use_the_existing_cash_share_ledger():
    symbol = "688001.SSE"
    days = ["2024-01-02", "2024-01-03", "2024-01-04"]
    feed = SimpleNamespace(calendar=days, instruments={symbol: {"ipoDate": "2000-01-01"}},
                           next_day=lambda day: days[days.index(day) + 1])
    quote = {"open": 11.5, "preclose": 10., "isST": 0, "tradestatus": 1, "adjustflag": "3"}
    config = StrategyConfig()
    book = Ledger(new_state(10000), config)
    simulate_order(book, feed, days[0], symbol, "buy", 201, quote, 2000)
    buy = book.state["orders"][f"{days[0]}:{symbol}:buy"]
    assert buy["qty"] == 201 and buy["filled"] == 20 and buy["status"] == "cancelled"
    assert book.quantity(symbol) == 20 and book.available(symbol, days[0]) == 0
    assert book.state["cash"] < 1000000 and book.cash_available() == book.state["cash"]
    assert book.submit("premature", days[0], symbol, "sell", 20, 1150)["reason"] == "insufficient_sellable_T1"
    assert book.available(symbol, days[1]) == 20
    simulate_order(book, feed, days[1], symbol, "sell", 20, quote, 2000)
    assert book.quantity(symbol) == 0
    assert book.state["orders"][f"{days[1]}:{symbol}:sell"]["status"] == "filled"
    assert book.state["cash"] < 1000000  # Both sides paid costs, despite an unchanged opening.


def test_engine_blocks_unsupported_listing_period_before_submitting():
    symbol = "300001.SZSE"
    day = "2024-01-02"
    feed = SimpleNamespace(calendar=[day], instruments={symbol: {"ipoDate": day}})
    quote = {"open": 10., "preclose": 10., "isST": 0, "tradestatus": 1, "adjustflag": "3"}
    book = Ledger(new_state(10000), StrategyConfig())
    simulate_order(book, feed, day, symbol, "buy", 100, quote, 1000000)
    assert not book.state["orders"] and not book.state["fills"]
    assert book.state["events"][-1]["reason"] == "unsupported_first_five_listing_sessions"


def test_no_holder_entitlement_restructuring_formula_and_conditional_ex_price():
    item = {"shares_before": "868324647", "debt_shares": "296162627", "investor_shares": "902125385",
            "holder_shares": "0", "debt_cash_equivalent": "1776975762", "investor_cash": "1096746343.70",
            "cash_dividend": "0"}
    assert cents(restructuring_ex_price("2.68", item)) == 252
    assert restructuring_ex_price("2.00", item) == 2
    for field, value in (("holder_shares", "1"), ("cash_dividend", "0.5"),
                         ("shares_before", "0"), ("debt_shares", "1.5"), ("investor_cash", "NaN")):
        with pytest.raises(ValueError, match="no-holder-entitlement"):
            restructuring_ex_price("2.68", {**item, field: value})


def test_no_holder_reference_includes_separate_investor_donation():
    item = {"shares_before": "323270000", "debt_shares": "0", "investor_shares": "161635000",
            "holder_shares": "0", "debt_cash_equivalent": "0", "investor_cash": "403763890",
            "cash_dividend": "0", "investor_donation_cash": "73000000"}
    assert cents(restructuring_ex_price("9.10", item)) == 705
    assert str(restructuring_ex_price("2.80", item)) == "2.80"
    assert cents(restructuring_ex_price("9.10", {**item, "investor_donation_cash": ""})) == 690
    for value in ("-1", "NaN", "Infinity"):
        with pytest.raises(ValueError, match="no-holder-entitlement"):
            restructuring_ex_price("9.10", {**item, "investor_donation_cash": value})
