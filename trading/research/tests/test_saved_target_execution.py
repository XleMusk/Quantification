"""Saved sizing explanations stay distinct from orders, fills and new quotes."""
from copy import deepcopy

from trading.research import opening_daily_ledger as ledger
from trading.research.saved_target_execution import project_saved_target_execution
from trading.research.tests.test_opening_daily_ledger import saved, repin, NOW, D


def event(symbol="688001.SSE", **fields):
    return {"day": D, "kind": "target_execution_diagnostic", "schema_version": 1,
        "symbol": symbol, "mechanical_reason": "new_entry", "target_weight": .8,
        "open_equity_cents": 2_000_000, "engine_mark": 10000, "price_unit": "cent",
        "minimum_buy_quantity": 200, "quantity_step": 1, "held_before": 0,
        "engine_target_quantity": 160, "requested_quantity": 0, "filled_quantity": 0,
        "constraint_flags": ["legal_order_quantity"], "order_ids": [], **fields}


def test_exact_day_copy_preserves_original_trace_and_distinguishes_minimum_and_units():
    star = event()
    main = event("600001.SSE", mechanical_reason="below_minimum_no_entry", engine_mark=20000,
        minimum_buy_quantity=100, quantity_step=100, engine_target_quantity=0, constraint_flags=[])
    mill = event("510001.SSE", engine_mark=5001, price_unit="mill", minimum_buy_quantity=100)
    events = [star, main, mill, {**star, "day": "2026-10-15"}, {"day": D, "kind": "order_blocked"}]
    before = deepcopy(events)
    rows = project_saved_target_execution(events, D)
    assert len(rows) == 3 and events == before
    assert all({key: row[key] for key in original} == original
        for row, original in zip(rows, (star, main, mill)))
    assert rows[0]["projection"]["target_budget_rmb"] == 16000
    assert [row["projection"]["minimum_buy_notional_rmb"] for row in rows] == [20000, 20000, 500.1]
    assert rows[0]["projection"]["budget_below_minimum_buy_notional"] is True
    assert rows[2]["projection"]["budget_below_minimum_buy_notional"] is False
    assert all(row["projection"]["is_BrokerQuote"] is False
        and not row["projection"]["fees_included"] and not row["projection"]["slippage_included"] for row in rows)


def test_legacy_absence_and_unrecorded_values_stay_unknown_without_current_rules():
    assert project_saved_target_execution(None, D) is None
    assert project_saved_target_execution([], D) is None
    assert project_saved_target_execution([{"day": D, "kind": "order_blocked"}], D) is None
    rows = project_saved_target_execution([
        event(price_unit="unsupported"), event(target_weight=None, engine_mark=None),
        event(schema_version=2), event(target_weight=float("nan"), minimum_buy_quantity=None)], D)
    assert rows[0]["projection"]["target_budget_rmb"] == 16000
    assert rows[0]["projection"]["minimum_buy_notional_rmb"] is None
    assert rows[1]["projection"]["target_budget_rmb"] is None
    assert rows[1]["projection"]["minimum_buy_notional_rmb"] is None
    assert rows[2]["projection"]["target_budget_rmb"] is None
    assert rows[3]["projection"]["budget_below_minimum_buy_notional"] is None


def test_saved_ledger_exposes_zero_order_trace_without_source_mutation(saved):
    case = saved["source_head"]["cases"][saved["case_id"]]
    case["state"].update(orders={}, fills={}, positions={}, prices={}, cash=10_000_000,
        events=[event(), event("600001.SSE", day="2026-10-15")])
    case["daily"][-1].update(cash=100000, equity=100000, fees=0)
    repin(saved)
    before = {path: path.read_bytes() for path in saved["data"].rglob("*") if path.is_file()}
    result = ledger.opening_daily_ledger(saved["data"], "alpha", saved["case_id"], now=NOW)
    assert result["order_count"] == result["fill_count"] == result["blocked_count"] == 0
    assert result["target_execution_diagnostic_count"] == 1
    assert result["target_execution_diagnostics"][0]["projection"]["target_budget_rmb"] == 16000
    assert before == {path: path.read_bytes() for path in saved["data"].rglob("*") if path.is_file()}


def test_unknown_source_and_old_ledger_have_null_trace_counts(saved):
    result = ledger.opening_daily_ledger(saved["data"], "alpha", saved["case_id"], now=NOW)
    assert result["target_execution_diagnostics"] is None
    assert result["target_execution_diagnostic_count"] is None
    (saved["source"] / "head.json").write_text("{}", encoding="utf-8")
    result = ledger.opening_daily_ledger(saved["data"], "alpha", saved["case_id"], now=NOW)
    assert result["status"] == "unavailable"
    assert result["target_execution_diagnostics"] is result["target_execution_diagnostic_count"] is None


def test_minimum_buy_comparison_applies_only_to_new_positive_weight_entries():
    traces = [
        event(),
        event(mechanical_reason="independent_replay_risk_exit", target_weight=0,
            held_before=100, engine_target_quantity=0),
        event(mechanical_reason="internal_reweight", target_weight=.16,
            held_before=100, engine_target_quantity=160),
        event(mechanical_reason="independent_risk_only_hold_quantity", target_weight=None,
            held_before=100, engine_target_quantity=100),
    ]
    before = deepcopy(traces)
    rows = project_saved_target_execution(traces, D)
    assert traces == before
    assert rows[0]["projection"]["budget_below_minimum_buy_notional"] is True
    assert all(row["projection"]["budget_below_minimum_buy_notional"] is None for row in rows[1:])
    assert rows[1]["projection"]["target_budget_rmb"] == 0
    assert rows[2]["projection"]["target_budget_rmb"] == 3200
    assert rows[3]["projection"]["target_budget_rmb"] is None
    assert all(row["projection"]["minimum_buy_notional_rmb"] == 20000 for row in rows)
