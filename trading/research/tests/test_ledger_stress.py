from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.research.ledger_stress import (LiquidationTailFeed, doubled_cost, liquidation_checkpoint,
                                           outage_result, run, tail_result, checked_reference_equivalence)


def endpoint():
    anchor = "2026-09-30"
    config = StrategyConfig(initial_cash=10000, rebalance_sessions=1, holdings=2, retain_rank=2)
    state = new_state(2000)
    state["day"] = anchor
    symbols = ["600000.SSE", "300001.SZSE"]
    state["positions"] = {symbol: [{"qty": 400, "available": anchor}] for symbol in symbols}
    state["prices"] = dict.fromkeys(symbols, 1000)
    state["price_units"] = {"511010.SSE": "mill"}
    state["prices"]["511010.SSE"] = 101000
    state["positions"]["511010.SSE"] = []
    state["pending_targets"] = dict.fromkeys(symbols, .4)
    row = {"open": 10., "close": 10., "preclose": 10., "adjustflag": "3", "tradestatus": 1, "isST": 0}
    base = SimpleNamespace(calendar=["2026-09-29", anchor], actions=[], manifest={"replay_only": True},
                           instruments={symbol: {"ipoDate": "2000-01-01", "outDate": ""} for symbol in symbols},
                           rows={anchor: {symbol: dict(row) for symbol in symbols}},
                           volumes=pd.DataFrame(1e6, index=[anchor], columns=symbols))
    checkpoint = {"state": state, "source": "fixture", "daily": [], "config": config.to_dict()}
    return base, checkpoint, config


def test_locked_board_limits_prevent_even_predecided_exit_and_keep_original():
    base, checkpoint, config = endpoint()
    original = deepcopy(checkpoint)
    feed = LiquidationTailFeed(base, checkpoint["state"], config, 1.)
    state = liquidation_checkpoint(checkpoint, config)["state"]
    assert "511010.SSE" not in state["prices"] and not state["price_units"]
    book = Ledger(state, config)
    for index, day in enumerate(feed.synthetic_dates[:2]):
        result = advance(state, feed, day, config, index)
        assert not state["fills"]
        assert book.quantity("600000.SSE") == book.quantity("300001.SZSE") == 400
        assert state["cash"] == 200000
        assert all(event["reason"] == "limit_down_queue_unknown" for event in result["events"]
                   if event["kind"] == "order_blocked")
    assert book.state["prices"]["600000.SSE"] == 810
    assert book.state["prices"]["300001.SZSE"] == 640
    assert book.equity() == 780000
    advance(state, feed, feed.synthetic_dates[2], config, 2)
    assert not any(book.quantity(symbol) for symbol in feed.symbols)
    assert all(fill["side"] == "sell" for fill in state["fills"].values())
    assert checkpoint == original


def test_thin_reopening_capacity_and_future_sellability_are_not_bypassed():
    base, checkpoint, config = endpoint()
    # Make one stock unavailable until the fourth artificial session.
    checkpoint["state"]["positions"]["300001.SZSE"][0]["available"] = "2026-10-04"
    feed = LiquidationTailFeed(base, checkpoint["state"], config, .01)
    state = liquidation_checkpoint(checkpoint, config)["state"]
    book = Ledger(state, config)
    for index, day in enumerate(feed.synthetic_dates):
        advance(state, feed, day, config, index)
    assert book.quantity("600000.SSE") == 100
    assert book.quantity("300001.SZSE") == 200
    assert all(fill["day"] >= "2026-10-04" for fill in state["fills"].values() if fill["symbol"] == "300001.SZSE")
    assert state["cash"] >= 0
    assert not any(order["status"] in {"submitted", "partial"} for order in state["orders"].values())


def test_cost_stress_changes_only_declared_execution_fields():
    config = StrategyConfig()
    stressed = doubled_cost(config)
    fields = {"commission", "minimum_commission", "sell_tax", "transfer_fee", "slippage_bps"}
    for name, value in config.to_dict().items():
        if name in fields:
            assert stressed.to_dict()[name] == pytest.approx(value * 2)
        else:
            assert stressed.to_dict()[name] == value


def replay_endpoint():
    base, checkpoint, config = endpoint()
    checkpoint["daily"] = [{"date": checkpoint["state"]["day"], "equity": 10000., "cash": 2000.,
                            "receivables": 0., "holdings": 2, "fees": 0., "turnover_value": 0.,
                            "new_orders": 0, "stale_symbols": []}]
    return base, checkpoint, config, [checkpoint["state"]["day"]]


def test_suspension_keeps_unknown_loss_and_does_not_execute_until_reopened(tmp_path):
    base, checkpoint, config, dates = replay_endpoint()
    original = deepcopy(checkpoint)
    result = tail_result(base, checkpoint, config, dates, tmp_path / "suspension", 1., suspended_sessions=2)
    assert result["status"] == "completed" and result["synthetic_sessions_completed"] == 7
    assert result["stale_valuation_sessions"] == 2
    assert all(row["loss_from_anchor"] is None and row["sold_shares"] == 0 for row in result["sessions"][:2])
    assert all(row["sold_shares"] == 0 for row in result["sessions"][:4])
    assert result["blocked_reasons"] == {"suspended": 4, "limit_down_queue_unknown": 4}
    assert result["remaining_quantity"] == 0 and result["sold_shares"] == 800
    assert result["buy_fills"] == 0 and result["unobserved_loss_bound"] is None
    assert checkpoint == original


def test_one_missing_held_quote_stops_whole_account_and_restoration_matches_reference(tmp_path):
    base, checkpoint, config, dates = replay_endpoint()
    original = deepcopy(checkpoint)
    reference = tmp_path / "existing_complete_tail"
    tail_result(base, checkpoint, config, dates, reference, 1.)
    result = outage_result(base, checkpoint, config, dates, tmp_path / "outage", reference)
    assert result["status"] == "completed" and result["synthetic_sessions_completed"] == 5
    failure = result["interruption"]
    assert failure["status"] == "failed" and failure["completed_sessions"] == 1
    assert failure["account_unchanged"] and failure["current_equity"] is failure["loss_during_outage"] is None
    assert failure["missing_symbol"] == "300001.SZSE"
    assert result["recovery"]["new_sessions"] == 5 and result["recovery"]["reused_sessions"] == 1
    assert result["recovery"]["same_execution_state_and_daily_as_reference"]
    assert result["sold_shares"] == 800 and result["remaining_quantity"] == 0
    assert checkpoint == original


def test_failed_suspended_prefix_never_reports_unknown_loss_as_zero(tmp_path):
    base, checkpoint, config, dates = replay_endpoint()
    base.instruments["600000.SSE"]["outDate"] = "2026-10-03"
    result = tail_result(base, checkpoint, config, dates, tmp_path / "unresolved", 1., suspended_sessions=2)
    assert result["status"] == "failed" and result["synthetic_sessions_completed"] == 2
    assert result["stale_valuation_sessions"] == 2
    assert result["worst_incremental_loss"] is result["last_incremental_loss"] is None
    assert result["remaining_quantity"] == 800 and result["sold_shares"] == 0


def test_disruption_mode_refuses_implicit_full_cost_replay():
    with pytest.raises(ValueError, match="no repeated cost replay"):
        run("unused", tail_suite="disruptions")


def test_old_tail_event_compatibility_never_ignores_cash_fill_or_target_changes():
    base, checkpoint, config = endpoint()
    state = liquidation_checkpoint(checkpoint, config)["state"]
    day = "2026-10-01"
    book = Ledger(state, config)
    book.emit(day, "targets_generated", targets={}, execution_not_before="2026-10-02")
    current = deepcopy(state)
    current["events"][-1].update(signal_source="synthetic:predeclared_full_liquidation", decision_reason=None)
    Ledger(current, config).emit(day, "target_execution_diagnostic", schema_version=1)
    receipt = checked_reference_equivalence(current, state, [day])
    assert receipt["current_informational_tail_events"] == 1
    for key, replacement in (("cash", current["cash"] - 1), ("fills", {"extra": {"fees": 1}})):
        changed = deepcopy(current)
        changed[key] = replacement
        with pytest.raises(ValueError, match="financial account"):
            checked_reference_equivalence(changed, state, [day])
    current["events"][-2]["targets"] = {"600000.SSE": .2}
    with pytest.raises(ValueError, match="target decision changed"):
        checked_reference_equivalence(current, state, [day])
