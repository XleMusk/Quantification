from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.engine import advance, corporate_actions
from trading.ledger import Ledger, new_state
from trading.research.loss_attribution import order_costs
from trading.research.tests import test_replay_resume
from trading.research.turnover_attribution import credited_shares, daily_exposure, project_orders, target_realization


def test_execution_trace_records_participation_gap_without_changing_fills_or_fees():
    feed = test_replay_resume.ReplayResumeTests().feed()
    symbol = "600000.SSE"
    feed.volumes.loc[:, symbol] = 30000.
    config = StrategyConfig(initial_cash=140000)
    state = new_state(config.initial_cash)
    advance(state, feed, feed.calendar[0], config, 0)
    advance(state, feed, feed.calendar[1], config, 1)
    trace = next(e for e in state["events"] if e["kind"] == "target_execution_diagnostic")
    order = state["orders"][trace["order_ids"][0]]
    assert trace["held_before"] == 0 and trace["mechanical_reason"] == "new_entry"
    assert trace["engine_target_quantity"] == order["qty"] == 2200
    assert trace["requested_quantity"] == 2200 and trace["filled_quantity"] == trace["held_after"] == 300
    assert trace["remaining_quantity_gap"] == 1900
    assert trace["constraint_flags"] == ["prior_volume_participation"]
    assert len(state["fills"]) == 2 and order["fees"] == 503
    assert state["cash"] == 14000000 - 303300 - 503
    before = deepcopy(state)
    rows, traces, _ = project_orders(state, {})
    assert state == before and len(traces) == 1
    assert rows.reason_basis.iloc[0] == "saved_engine_execution_diagnostic"
    assert order_costs(list(state["orders"].values()), config).fees_cny.sum() == 5.03


def test_unsubmitted_T1_and_below_lot_targets_are_not_filled_or_silently_zeroed():
    feed = test_replay_resume.ReplayResumeTests().feed()
    config = StrategyConfig(initial_cash=140000)
    symbol = "600000.SSE"
    state = new_state(config.initial_cash)
    state["positions"] = {symbol: [{"qty": 100, "available": feed.calendar[2]}]}
    state["cash"] -= 100000
    state["pending_targets"] = {}
    advance(state, feed, feed.calendar[1], config, 1)
    trace = next(e for e in state["events"] if e["kind"] == "target_execution_diagnostic")
    assert trace["sellable_before"] == 0 and trace["held_after"] == 100
    assert trace["remaining_quantity_gap"] == -100 and trace["constraint_flags"] == ["sellable_T1"]
    assert not state["orders"]
    small = new_state(100)
    small["pending_targets"] = {symbol: .2}
    advance(small, feed, feed.calendar[1], replace(config, initial_cash=100), 1)
    trace = next(e for e in small["events"] if e["kind"] == "target_execution_diagnostic")
    assert trace["ideal_fractional_quantity"] > 0 and trace["engine_target_quantity"] == 0
    assert trace["mechanical_reason"] == "below_minimum_no_entry" and not small["orders"]


def test_legacy_share_projection_includes_exact_credit_and_retains_unknown_cause():
    config = StrategyConfig()
    book = Ledger(new_state(config.initial_cash), config)
    symbol, day = "600000.SSE", "2024-01-02"
    book.submit("b", day, symbol, "buy", 100, 1000)
    book.fill("b1", "b", 100, 1000, day, "2024-01-03")
    action = {"symbol": symbol, "dividRegistDate": day, "dividOperateDate": "2024-01-03",
              "dividStockMarketDate": "2024-01-03", "dividCashPsBeforeTax": "0",
              "dividStocksPs": "0", "dividReserveToStockPs": ".5"}
    feed = type("Feed", (), {"actions": [action]})()
    corporate_actions(book, feed, day, at_close=True)
    corporate_actions(book, feed, "2024-01-03")
    book.submit("s", "2024-01-03", symbol, "sell", 150, 1000)
    book.fill("s1", "s", 150, 1000, "2024-01-03", "2024-01-04")
    rows, traces, _ = project_orders(book.state, {(symbol, "2024-01-03"): action})
    assert not traces and rows.held_before.tolist() == [0, 150]
    assert rows.mechanical_reason.tolist() == ["new_entry", "full_exit_cause_unknown"]
    assert rows.business_exit_reason.isna().all()
    with pytest.raises(ValueError, match="pinned source"):
        project_orders(book.state, {})
    with pytest.raises(ValueError, match="fractional"):
        credited_shares({"symbol": symbol, "day": "2024-01-03", "entitlement": 1},
                        {(symbol, "2024-01-03"): action})
    bad = deepcopy(book.state)
    bad["positions"][symbol] = [{"qty": 1, "available": "2024-01-04"}]
    with pytest.raises(ValueError, match="terminal"):
        project_orders(bad, {(symbol, "2024-01-03"): action})
    bad["broker_binding"] = {"broker_account_kind": "simulated"}
    with pytest.raises(PermissionError):
        project_orders(bad, {})


def test_share_projection_moves_explicit_successor_security_transfer():
    config = StrategyConfig(initial_cash=140000)
    book = Ledger(new_state(config.initial_cash), config)
    source, successor = "601989.SSE", "600150.SSE"
    book.submit("b", "2025-08-01", source, "buy", 1300, 466)
    book.fill("b1", "b", 1300, 466, "2025-08-01", "2025-08-04")
    book.state["positions"][source] = []
    book.state["positions"][successor] = [{"qty": 174, "available": "2025-09-16"}]
    book.state["sequence"] += 1
    book.state["events"].append({
        "sequence": book.state["sequence"], "day": "2025-09-05",
        "kind": "successor_security_transfer", "source_symbol": source,
        "target_symbol": successor, "source_quantity": 1300,
        "allocated_quantity": 174, "policy": "floor",
    })
    book.submit("s", "2025-10-09", successor, "sell", 174, 3444)
    book.fill("s1", "s", 174, 3444, "2025-10-09", "2025-10-10")
    rows, traces, _ = project_orders(book.state, {})
    assert not traces
    assert rows.held_before.tolist() == [0, 174]
    assert rows.symbol.tolist() == [source, successor]


def test_daily_exposure_excludes_unpaid_cash_and_retains_empty_sessions():
    marks = [{"date": "2026-01-05", "equity": 10000, "cash": 10000, "receivables": 0,
              "holdings": 0, "stale_symbols": []},
             {"date": "2026-01-06", "equity": 10000, "cash": 9500, "receivables": 200,
              "holdings": 1, "stale_symbols": ["600000.SSE"]}]
    rows, summary = daily_exposure(marks)
    assert rows.invested_value_cny.tolist() == [0, 300]
    assert summary["mean_invested_weight"] == .015
    assert summary["mean_nonempty_invested_weight"] == .03
    assert summary["mean_holdings"] == .5 and summary["empty_holding_days"] == 1
    assert summary["stale_valuation_days"] == 1
    bad = deepcopy(marks)
    bad[1]["receivables"] = 501
    with pytest.raises(ValueError, match="disagree"):
        daily_exposure(bad)


def realization_fixture():
    targets = {"600000.SSE": .04, "600519.SSE": .04}
    account = {"events": [{"kind": "targets_generated", "day": "2026-01-05", "signal_date": "2026-01-05",
                           "execution_not_before": "2026-01-06", "targets": targets}]}
    traces = [{"day": "2026-01-06", "symbol": symbol, "target_weight": .04,
               "mechanical_reason": "new_entry" if desired else "below_minimum_no_entry",
               "has_execution_open": True, "engine_mark": mark, "price_unit": "cent",
               "open_equity_cents": 14000000, "engine_target_quantity": desired, "held_after": after,
               "held_before": 0,
               "minimum_buy_quantity": 100, "quantity_step": 100, "remaining_quantity_gap": desired - after}
              for symbol, mark, desired, after in [("600000.SSE", 5000, 100, 0), ("600519.SSE", 20000, 0, 0)]]
    return account, traces


def test_target_budget_rounding_and_execution_gaps_are_separate_without_fills():
    account, traces = realization_fixture()
    before = deepcopy(account), deepcopy(traces)
    industries = pd.DataFrame([{"date": "2026-01-05", "symbol": "600000.SSE", "industry": "bank"},
                               {"date": "2026-01-07", "symbol": "600519.SSE", "industry": "future"}])
    rows, anchors, summary = target_realization(account, traces, StrategyConfig(), industries=industries)
    assert (account, traces) == before
    assert rows.target_budget_cny.tolist() == [5600, 5600]
    assert rows.rounding_gap_cny.tolist() == [600, 5600]
    assert rows.execution_quantity_gap_cny.tolist() == [5000, 0]
    assert rows.minimum_rule_gap_cny.tolist() == [0, 0]
    assert rows.total_target_gap_cny.sum() == 11200
    assert summary["mean_below_minimum_budget_fraction"] == .5
    assert summary["mean_rounded_target_weight"] == pytest.approx(5000 / 140000)
    assert summary["mean_selected_held_after_weight"] == 0
    assert summary["below_minimum_no_entry_records"] == 1
    assert anchors.industry_target_weight_coverage.iloc[0] == .5
    assert summary["industry_complete_anchors"] == 0
    assert summary["maximum_industry_fraction_on_complete_anchors"] is None
    assert pd.isna(rows.loc[rows.symbol.eq("600519.SSE"), "industry"]).all()


def test_incomplete_legacy_or_overridden_targets_never_get_zero_gap_means():
    account, traces = realization_fixture()
    rows, _, summary = target_realization(account, traces[:1], StrategyConfig())
    assert summary["fully_observed_execution_anchors"] == 0
    assert summary["mean_rounding_gap_weight"] is None
    assert rows.trace_status.tolist() == ["saved_attempt_mark", "missing_saved_attempt"]
    traces[1]["engine_mark"] = 0
    rows, _, summary = target_realization(account, traces, StrategyConfig())
    assert rows.trace_status.iloc[1] == "saved_mark_unknown"
    assert summary["mean_selected_held_after_weight"] is None
    traces[1]["target_weight"] = 0
    traces[1]["mechanical_reason"] = "forced_risk_exit"
    rows, _, _ = target_realization(account, traces, StrategyConfig())
    assert rows.trace_status.iloc[1] == "execution_target_override"
    with pytest.raises(ValueError, match="Duplicate"):
        target_realization(account, traces + traces[:1], StrategyConfig())


def test_star_step_target_is_not_legal_new_entry_and_odd_lot_exit_is_preserved():
    account, traces = realization_fixture()
    account["events"][0]["targets"] = {"688001.SSE": .04}
    trace = traces[0]
    trace.update(symbol="688001.SSE", engine_mark=10000, engine_target_quantity=56,
                 held_before=0, held_after=0, remaining_quantity_gap=56, minimum_buy_quantity=200, quantity_step=1)
    rows, _, summary = target_realization(account, [trace], StrategyConfig())
    assert rows.rounded_target_value_cny.iloc[0] == 5600
    assert rows.quantity_rule_only_target_value_cny.iloc[0] == 0
    assert rows.minimum_rule_gap_cny.iloc[0] == 5600
    assert rows.post_quantity_rule_gap_cny.iloc[0] == 0
    assert summary["below_minimum_budget_records"] == 1
    trace.update(held_before=256, held_after=56, remaining_quantity_gap=0)
    rows, _, _ = target_realization(account, [trace], StrategyConfig())
    assert rows.quantity_rule_only_target_value_cny.iloc[0] == 5600
    assert rows.minimum_rule_gap_cny.iloc[0] == 0


def test_explicit_zero_budget_slots_are_separate_from_positive_target_realization():
    account, traces = realization_fixture()
    account["events"][0]["targets"]["600519.SSE"] = 0
    rows, anchors, summary = target_realization(account, traces[:1], StrategyConfig())
    assert rows.symbol.tolist() == ["600000.SSE"]
    assert anchors.targets.iloc[0] == 1 and anchors.supplied_target_slots.iloc[0] == 2
    assert summary["explicit_zero_budget_slots"] == 1
    assert summary["fully_observed_execution_anchors"] == 1
    assert summary["mean_gross_target_weight"] == .04
    assert anchors.gross_target_weight.iloc[0] == .04
    account["events"][0]["targets"]["600000.SSE"] = 0
    rows, anchors, summary = target_realization(account, [], StrategyConfig())
    assert rows.empty and summary["explicit_zero_budget_slots"] == 2
    assert summary["fully_observed_execution_anchors"] == 0
    assert summary["mean_selected_held_after_weight"] is None
    assert summary["mean_gross_target_weight"] == 0


@pytest.mark.parametrize("change", ["negative", "single_cap", "gross_cap", "timing"])
def test_zero_budget_support_preserves_saved_target_constraints(change):
    account, traces = realization_fixture()
    event = account["events"][0]
    if change == "timing":
        event["execution_not_before"] = event["signal_date"]
    else:
        event["targets"] = ({"600000.SSE": -.01} if change == "negative" else
                            {"600000.SSE": .21} if change == "single_cap" else
                            {f"{600000 + i}.SSE": .2 for i in range(5)})
    with pytest.raises(ValueError, match="timing or constraints"):
        target_realization(account, traces, StrategyConfig())
