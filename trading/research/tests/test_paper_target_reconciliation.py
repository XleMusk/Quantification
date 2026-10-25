from copy import deepcopy

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.research.paper_target_reconciliation import (
    STATUS_READY,
    build_reconciliation,
    compare_native_stockdb_close,
)


DAY = "2026-10-23"
SYMBOLS = ["000001.SZSE", "600000.SSE"]


def _payload():
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2,
                            gross_limit=.8, single_limit=.5).to_dict()
    state = new_state(10000)
    state["prices"] = {SYMBOLS[0]: 1000, SYMBOLS[1]: 2000}
    state["positions"] = {
        SYMBOLS[0]: [{"qty": 200, "available": DAY}],
        SYMBOLS[1]: [{"qty": 100, "available": DAY}],
    }
    state["targets"] = {SYMBOLS[0]: .4, SYMBOLS[1]: .4}
    plan = {"execution_admitted": False, "prefill_allowed": False,
            "historical_target": {"weights": state["targets"]}}
    run = {"execution_admitted": False, "research_only": True, "config": config}
    return plan, run, state


def test_reconciliation_uses_shared_lot_allocator_and_does_not_mutate_account():
    plan, run, state = _payload()
    before = deepcopy(state)
    result = build_reconciliation(plan, run, state, {
        SYMBOLS[0]: {"close": 10.0, "tradestatus": "1", "adjustflag": "3"},
        SYMBOLS[1]: {"close": 20.0, "tradestatus": "1", "adjustflag": "3"},
    }, day=DAY)
    assert result["status"] == STATUS_READY
    assert result["execution_admitted"] is False
    assert result["orders_created"] is False
    assert result["ledger_mutated"] is False
    assert [(row["symbol"], row["estimated_target_qty"], row["delta_qty"])
            for row in result["rows"]] == [(SYMBOLS[0], 500, 300), (SYMBOLS[1], 300, 200)]
    assert state == before


def test_reconciliation_keeps_incomplete_reference_separate_from_ths_prefill():
    plan, run, state = _payload()
    result = build_reconciliation(plan, run, state, {
        SYMBOLS[0]: {"close": 10.0, "tradestatus": "1", "adjustflag": "3"},
    }, day=DAY)
    assert result["status"] == "PAPER_CLOSE_REFERENCE_INCOMPLETE"
    assert result["missing_symbols"] == [SYMBOLS[1]]
    assert all(row["ths_prefill_status"] == "reference_only_not_prefillable" for row in result["rows"])
    assert result["orders_created"] is False


def test_native_reference_compares_frozen_close_without_execution(tmp_path):
    import pandas as pd

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        '{"execution_admitted": false, "prefill_allowed": false, '
        '"historical_target": {"weights": {"000001.SZSE": 0.4, "600000.SSE": 0.4}}}',
        encoding="utf-8",
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    pd.DataFrame([
        {"symbol": "000001.SZSE", "date": DAY, "code": "000001", "close": 10.0},
        {"symbol": "600000.SSE", "date": DAY, "code": "600000", "close": 20.0},
    ]).to_csv(snapshot / "daily.csv", index=False)
    raw = tmp_path / "raw.parquet"
    pd.DataFrame([
        {"date": DAY, "code": "000001", "close": 10.0, "tradestatus": "1", "adjustflag": "3"},
        {"date": DAY, "code": "600000", "close": 20.0, "tradestatus": "1", "adjustflag": "3"},
    ]).to_parquet(raw, index=False)
    result = compare_native_stockdb_close(
        plan_path, snapshot, raw, day=DAY, output_dir=tmp_path / "out",
    )
    assert result["status"] == "COMPLETED_RESEARCH_ONLY_CROSS_SOURCE_CLOSE_REFERENCE"
    assert result["comparison"]["close_matches"] == 2
    assert result["execution_admitted"] is False
    assert result["client_interacted"] is False
