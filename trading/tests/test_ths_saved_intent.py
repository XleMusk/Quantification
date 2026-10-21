"""Synthetic conversion evidence only; no actual broker account or quotes."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from trading.config import StrategyConfig
from trading.ths.adapter import check_plan
from trading.ths import saved_intent as consumer


NOW = datetime.fromisoformat("2026-10-19T09:35:00+08:00")
DAY = "2026-10-19"


def snapshot(positions=None, cash=10_000_000):
    positions = positions or {}
    return {"kind": "account", "source": "ths_user_export", "account_alias": "synthetic-only",
            "trading_date": DAY, "observed_at": NOW.isoformat(), "cash_available": cash,
            "cash_total": cash, "cash_frozen": 0, "cash_basis": "cash_balance_includes_frozen",
            "positions": {s: {"quantity": q, "sellable": a} for s, (q, a, _) in positions.items()},
            "marks": {s: p for s, (q, _, p) in positions.items() if q}}


def request(kind="NO_CHANGE", weights=None, exits=()):
    return {"kind": kind, "weights": weights, "remaining_exit_symbols": list(exits),
            "source_day": "2026-10-16", "execution_day": DAY, "strategy_id": "synthetic-fixed",
            "target_version": 3, "source_known_at": "2026-10-16T18:30:00+08:00",
            "execution_config": StrategyConfig().to_dict(), "refs": {"synthetic": True},
            "research_only": True, "candidate_panel_only": True, "execution_admitted": False,
            "selection_enabled": False, "strategy_validated": False, "prefill_allowed": False}


def quotes(*symbols):
    return {"schema": "ths_raw_quote_observation_v1", "source": "ths_controls",
            "account_alias": "synthetic-only", "trading_date": DAY,
            "observed_at": NOW.isoformat(), "price_basis": "raw_rmb",
            "prices": {s: {"price_rmb": "10.00", "preclose_rmb": "10.00",
                           "is_st": False, "tradable": True} for s in symbols}}


@pytest.fixture(autouse=True)
def canonical_source(monkeypatch):
    # Source/hash/method checks belong to read_request; these focused tests
    # isolate the consumer's observed-account conversion, using explicit mocks.
    monkeypatch.setattr(consumer, "read_request", lambda payload, *, current=None: deepcopy(payload))


def prepare(intent, account, price=None, **kwargs):
    return consumer.prepare_saved_intent({"saved_account_intent": intent,
        "strategy_id": intent["strategy_id"], "source_cutoff": intent["source_day"],
        "execution_day": intent["execution_day"]}, account, price,
                                         current=NOW, **kwargs)


def quantities(result):
    return {row["symbol"]: row["target_qty"] for row in result["portfolio"]["targets"]}


def assert_research_only(result):
    assert result["next_plan"] is None
    for key in ("prefill_allowed", "submitted", "client_interacted", "saved",
                "selection_enabled", "strategy_validated", "execution_admitted", "source_tasks_mutated"):
        assert result[key] is False
    assert result["research_only"] is True and result["candidate_panel_only"] is True
    if result["diagnostic_next_plan"] is not None:
        with pytest.raises(ValueError, match="not eligible"):
            check_plan(result["diagnostic_next_plan"], current=NOW)


def test_nochange_keeps_observed_shares_without_quotes_or_research_quantities():
    account = snapshot({"600001.SSE": (300, 200, 1000), "515880.SSE": (700, 700, 55)})
    before = deepcopy(account)
    intent = request()
    intent["research_positions_ignored"] = {"600001.SSE": 9900}
    result = prepare(intent, account)
    assert quantities(result) == {"600001.SSE": 300, "515880.SSE": 700}
    assert account == before and result["adapter_status"] == "no_change"
    assert_research_only(result)


def test_residual_full_exit_preserves_zero_task_when_t1_blocked():
    account = snapshot({"600001.SSE": (300, 100, 1000), "000002.SZSE": (800, 800, 1200)})
    intent = request(exits=["600001.SSE"])
    before = deepcopy(intent)
    result = prepare(intent, account, quotes("600001.SSE"))
    assert quantities(result) == {"600001.SSE": 0, "000002.SZSE": 800}
    step = next(row for row in result["adjustments"] if row["symbol"] == "600001.SSE")
    assert step["remaining_qty"] == step["remaining_exit_qty"] == 300
    assert step["validation_reason"] == "insufficient_sellable_T1"
    assert step["status"] == "blocked"
    assert result["adapter_status"] == "blocked" and intent == before
    assert_research_only(result)


def test_full_target_uses_saved_weights_not_ranking_and_observed_equity():
    account = snapshot({"600001.SSE": (100, 100, 1000)})
    intent = request("FULL_TARGET", {"600009.SSE": .13, "000007.SZSE": .07})
    result = prepare(intent, account, quotes("600001.SSE", "600009.SSE", "000007.SZSE"))
    assert result["weight_sizing"]["equity_cents"] == 10_100_000
    assert quantities(result) == {"600001.SSE": 0, "600009.SSE": 1300, "000007.SZSE": 700}
    assert [row["target_weight"] for row in result["weight_sizing"]["targets"]] == [.07, 0, .13]
    assert result["diagnostic_next_plan"]["side"] == "sell"
    assert_research_only(result)


def test_sell_first_and_buys_never_spend_unfilled_sales():
    account = snapshot({"600001.SSE": (1000, 1000, 1000)}, cash=0)
    result = prepare(request("FULL_TARGET", {"600009.SSE": .2}), account,
                     quotes("600001.SSE", "600009.SSE"))
    assert result["adjustments"][0]["side"] == "sell"
    buy = result["adjustments"][1]
    assert buy["validation_reason"] == "insufficient_cash"
    assert "buy_funding_exceeds_observed_available_cash" in buy["risk_blockers"]
    assert result["diagnostic_next_plan"]["symbol"] == "600001.SSE"
    assert_research_only(result)


@pytest.mark.parametrize("mutation", ["missing", "public", "wrong_account", "missing_status", "stale", "mill_price"])
def test_changes_require_fresh_raw_same_account_broker_quotes(mutation):
    price = quotes("600009.SSE")
    if mutation == "missing":
        price = None
    elif mutation == "public":
        price["source"] = "public_quote"
    elif mutation == "wrong_account":
        price["account_alias"] = "other"
    elif mutation == "missing_status":
        del price["prices"]["600009.SSE"]["is_st"]
    elif mutation == "stale":
        price["observed_at"] = (NOW - timedelta(minutes=6)).isoformat()
    else:
        price["prices"]["600009.SSE"]["price_rmb"] = "10.001"
    with pytest.raises((ValueError, KeyError)):
        prepare(request("FULL_TARGET", {"600009.SSE": .1}), snapshot(), price)


def test_nochange_cash_only_is_not_missing_price_or_full_liquidation():
    result = prepare(request(), snapshot())
    assert quantities(result) == {} and result["adjustments"] == []
    assert result["saved_account_intent"]["weights"] is None
    assert_research_only(result)
    full = prepare(request("FULL_TARGET", {}), snapshot({"600001.SSE": (100, 100, 1000)}),
                   quotes("600001.SSE"))
    assert quantities(full) == {"600001.SSE": 0}
    assert full["saved_account_intent"]["weights"] == {}


def test_strict_rejects_outside_changes_mainboard_scope_preserves_without_normalizing():
    account = snapshot({"515880.SSE": (400, 400, 55)})
    intent = request("FULL_TARGET", {"600009.SSE": .1, "300001.SZSE": .1})
    with pytest.raises(ValueError, match="Unsupported"):
        prepare(intent, account, quotes("600009.SSE", "300001.SZSE"))
    result = prepare(intent, account, quotes("600009.SSE"), execution_scope="mainboard_only")
    assert quantities(result) == {"515880.SSE": 400, "300001.SZSE": 0, "600009.SSE": 1000}
    assert len(result["scope_exclusions"]) == 2
    assert next(row for row in result["weight_sizing"]["targets"]
                if row["symbol"] == "600009.SSE")["target_weight"] == .1
    assert_research_only(result)
    # An unsupported symbol with an explicit zero and no holding requires no
    # trade and no instrument/quote inference, so strict scope may retain it.
    unchanged = prepare(request("FULL_TARGET", {"515880.SSE": 0}), snapshot())
    assert quantities(unchanged) == {"515880.SSE": 0}


def test_held_marks_size_but_never_supply_limit_and_reject_config_date_mutation():
    account = snapshot({"600001.SSE": (100, 100, 2000)})
    with pytest.raises(ValueError, match="broker quote"):
        prepare(request("FULL_TARGET", {"600001.SSE": .1}), account)
    price = quotes("600001.SSE")
    price["prices"]["600001.SSE"].update(price_rmb="20.00", preclose_rmb="20.00")
    result = prepare(request("FULL_TARGET", {"600001.SSE": .1}), account, price)
    assert quantities(result) == {"600001.SSE": 500}
    assert result["weight_sizing"]["targets"][0]["valuation_price_cents"] == 2000
    with pytest.raises(ValueError, match="configuration"):
        prepare(request(), account, config=StrategyConfig(single_limit=.1))
    wrong = deepcopy(account)
    wrong["trading_date"] = "2026-10-16"
    with pytest.raises(ValueError, match="trading date"):
        prepare(request(), wrong)
