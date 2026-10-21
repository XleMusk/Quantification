"""A known carried price cannot bypass native source-status rejection."""
from copy import deepcopy
import json

import pytest

from trading.config import StrategyConfig
from trading.ledger import new_state
from trading.research.opening_pack import _ledger_preflight


@pytest.mark.parametrize("status,st,source_day,expected_buy,expected_sell", [
    (0, 0, "2026-10-16", "native_source_suspended", "native_source_suspended"),
    (None, 0, "2026-10-16", "native_source_trade_status_unknown", "native_source_trade_status_unknown"),
    (1, None, "2026-10-16", "native_source_ST_unknown", "native_source_ST_unknown"),
    (1, 1, "2026-10-16", "native_source_ST_buy_excluded", None),
    (1, 0, "2026-10-15", "native_exact_source_status_missing", "native_exact_source_status_missing"),
])
def test_native_source_unknowns_and_st_new_buys_stay_rejected(tmp_path, status, st, source_day, expected_buy, expected_sell):
    config = StrategyConfig()
    state = new_state(config.initial_cash)
    state.update(day="2026-10-16", positions={"600001.SSE": [{"qty": 100, "available": "2026-10-16"}]})
    account = tmp_path / "account.json"
    account.write_text(json.dumps(state), encoding="utf-8")
    before = account.read_bytes()
    symbols = ("600001.SSE", "000002.SZSE")
    item = {"result": {"strategy_id": "synthetic_native", "source_task_family": "native_preopen",
        "source_artifacts": str(tmp_path), "source_cutoff": "2026-10-16",
        "source_quote_status_by_symbol": {s: {"trading_date": source_day, "tradestatus": status, "is_st": st} for s in symbols}},
        "execution_config": config.to_dict(), "orders": [
            {"symbol": symbols[0], "side": "sell", "qty": 100, "price_rmb": "10.00", "source_status": "blocked_research_close_not_tradable" if status != 1 else "paper_preview_not_created"},
            {"symbol": symbols[1], "side": "buy", "qty": 100, "price_rmb": "20.00", "source_status": "paper_preview_not_created"}]}
    result = _ledger_preflight(item, "2026-10-19")
    for row, expected in zip(result["rows"], (expected_sell, expected_buy)):
        assert row["preflight_status"] == ("rejected" if expected else "accepted")
        assert row["preflight_reason"] == expected
    assert result["fills_created"] == 0 and result["saved_ledger_mutated"] is False
    assert account.read_bytes() == before
