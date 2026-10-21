"""Focused fixtures for the fixed affordability screen; no replay or new orders."""
from copy import deepcopy

from trading.config import StrategyConfig
from trading.research.leader_momentum_capital import affordable_ranked_candidates


DAY = "2026-04-30"
CONFIG = StrategyConfig()
EQUITY = 10_000_000


def ranked(symbols):
    return [{"symbol": symbol, "score": 20 - index, "evidence": {"source": "fixed D"}}
            for index, symbol in enumerate(symbols)]


def quotes(prices):
    return {symbol: {"close": price, "adjustflag": "3"} for symbol, price in prices.items()}


def test_original_rank_continues_after_fee_and_star_minimum_exclusions_without_mutation():
    symbols = ["600001.SSE", "688001.SSE", "600002.SSE", "300001.SZSE",
               "688002.SSE", "600003.SSE", "600004.SSE", "600005.SSE"]
    candidates = ranked(symbols)
    rows = quotes(dict(zip(symbols[:-1], [159.95, 80., 10., 100., 75., 20., 30.])))
    before = deepcopy((candidates, rows))
    result = affordable_ranked_candidates(candidates, EQUITY, {}, rows, CONFIG, DAY)
    assert result["status"] == "complete" and result["slot_budget_cents"] == 1_600_000
    assert result["candidates"] == candidates[2:7]
    assert [(r["symbol"], r["minimum_buy_quantity"], r["minimum_buy_cost_cents"])
            for r in result["excluded"]] == [(symbols[0], 100, 1_600_016), (symbols[1], 200, 1_600_516)]
    # The eighth unknown row is unnecessary once the original five slots fill.
    assert (candidates, rows) == before
    result["candidates"][0]["evidence"]["source"] = "output copy"
    assert candidates[2]["evidence"]["source"] == "fixed D"


def test_held_quantity_bypasses_new_entry_budget_but_does_not_rescue_lower_ranks():
    symbols = [f"60000{i}.SSE" for i in range(1, 7)]
    candidates = ranked(symbols)
    rows = quotes({symbol: 10. for symbol in symbols})
    rows[symbols[0]]["close"] = 2000.
    held = {symbols[0]: 17, symbols[-1]: 100, "600099.SSE": 100}
    before = deepcopy(held)
    result = affordable_ranked_candidates(candidates, EQUITY, held, rows, CONFIG, DAY)
    assert result["status"] == "complete" and result["candidates"] == candidates[:5]
    assert not result["excluded"] and held == before
    assert symbols[-1] not in [r["symbol"] for r in result["candidates"]]
    assert "600099.SSE" not in [r["symbol"] for r in result["candidates"]]


def test_needed_unknown_is_incomplete_while_all_known_unaffordable_is_complete_empty():
    candidates = ranked(["600001.SSE", "600002.SSE", "600003.SSE"])
    rows = quotes({"600001.SSE": 10., "600003.SSE": 10.})
    result = affordable_ranked_candidates(candidates, EQUITY, {}, rows, CONFIG, DAY)
    assert result["status"] == "incomplete" and result["candidates"] == []
    assert result["reason"] == "candidate_raw_close_unknown:600002.SSE"
    unsupported = ranked(["600001.SSE", "830001.BSE", "600003.SSE"])
    result = affordable_ranked_candidates(unsupported, EQUITY, {},
        quotes({"600001.SSE": 10., "830001.BSE": 10., "600003.SSE": 10.}), CONFIG, DAY)
    assert result["status"] == "incomplete" and result["candidates"] == []
    assert result["reason"] == "candidate_execution_rule_unknown:830001.BSE"
    rows = quotes({r["symbol"]: 200. for r in candidates})
    result = affordable_ranked_candidates(candidates, EQUITY, {}, rows, CONFIG, DAY)
    assert result["status"] == "complete" and result["candidates"] == []
    assert result["reason"] == "known_no_affordable_candidates" and len(result["excluded"]) == 3
