from copy import deepcopy
from types import SimpleNamespace

import pandas as pd

from trading.config import StrategyConfig
from trading.contracts import PortfolioIntent, RunContext, TradingMode, decision_time
from trading.ledger import Ledger, new_state
from trading.portfolio_allocation_comparison import compare_target


DAY = "2025-01-27"
CONTEXT = RunContext("compare-test", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=3, retain_rank=3, gross_limit=.9, single_limit=.5)
SYMBOLS = ("600000.SSE", "000001.SZSE", "600001.SSE")


def test_comparison_separates_economic_target_from_cash_and_holdings_constraints():
    rows = {symbol: {"close": price} for symbol, price in zip(SYMBOLS, (23., 27., 33.))}
    intent = PortfolioIntent.full_target({symbol: .3 for symbol in SYMBOLS}, context=CONTEXT,
                                         decided_at=decision_time(DAY), execution_not_before="2025-02-05",
                                         source="comparison-test")
    book = Ledger(new_state(CONFIG.initial_cash), CONFIG, CONTEXT)
    before = deepcopy(book.state)
    result = compare_target(intent, book, DAY, rows)
    assert result["status"] == "EXECUTABLE_TARGET_READY"
    assert result["economic_target_weights"] == {symbol: .3 for symbol in SYMBOLS}
    assert result["orders_created"] is False and result["ledger_mutated"] is False
    assert book.state == before
    assert result["estimated_buy_cost_cents"] > 0


def test_comparison_reports_frozen_sellable_shortfall_without_mutating_ledger():
    rows = {symbol: {"close": price} for symbol, price in zip(SYMBOLS, (23., 27., 33.))}
    intent = PortfolioIntent.full_target({SYMBOLS[0]: 0., SYMBOLS[1]: .3, SYMBOLS[2]: .3},
                                         context=CONTEXT, decided_at=decision_time(DAY),
                                         execution_not_before="2025-02-05", source="comparison-test")
    book = Ledger(new_state(CONFIG.initial_cash), CONFIG, CONTEXT)
    book.state["positions"][SYMBOLS[0]] = [{"qty": 100, "available": "2025-02-05"}]
    before = deepcopy(book.state)
    result = compare_target(intent, book, DAY, rows)
    assert result["status"] == "BLOCKED_EXECUTION_CONSTRAINT"
    assert any(row["flag"] == "sellable_T1_shortfall" for row in result["blockers"])
    assert book.state == before
