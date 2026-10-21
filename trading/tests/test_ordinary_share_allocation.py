from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import corporate_actions, ordinary_share_terms
from trading.ledger import Ledger, new_state
from trading.research.continuous_momentum import ContinuousMomentumFeed, fractional_allocation_contract


ACTION = {"symbol": "688096.SSE", "dividRegistDate": "2025-07-14", "dividOperateDate": "2025-07-15",
          "dividPayDate": "2025-07-15", "dividStockMarketDate": "2025-07-16",
          "dividCashPsBeforeTax": ".15", "dividStocksPs": "0", "dividReserveToStockPs": ".4"}
IDENTIFIER = "688096.SSE:2025-07-15"


def fixture(policy=None, mode=TradingMode.REPLAY):
    state = new_state(140000)
    state["day"] = "2025-07-14"
    state["positions"][ACTION["symbol"]] = [{"qty": 146, "available": "2025-07-01"},
                                           {"qty": 147, "available": "2025-07-01"}]
    state["entitlements"][IDENTIFIER] = 293
    feed = SimpleNamespace(actions=[dict(ACTION)], manifest={})
    wrapper = ContinuousMomentumFeed.__new__(ContinuousMomentumFeed)
    wrapper.base = feed
    wrapper.fractional_allocation = fractional_allocation_contract(policy)
    context = RunContext("standalone_replay", mode)
    return state, Ledger(state, StrategyConfig(), context), wrapper


def actual_receipt(context, shares=118):
    return {"kind": "ordinary_share_allocation", "account_id": context.account_id, "mode": context.mode.value,
            "registered_quantity": 293, "action_terms": ordinary_share_terms(ACTION), "allocated_shares": shares,
            "source_kind": "broker_account_record", "source_ref": "synthetic account export receipt"}


def test_default_stops_without_inventing_a_share_or_cash():
    state, book, feed = fixture()
    before = deepcopy(state)
    with pytest.raises(ValueError, match="account-specific receipt"):
        corporate_actions(book, feed, "2025-07-15")
    assert state == before


@pytest.mark.parametrize("policy, shares", [("floor", 117), ("ceil", 118)])
def test_fixed_replay_assumption_credits_known_available_shares_and_survives_recovery(policy, shares):
    state, book, feed = fixture(policy)
    before_cash = state["cash"]
    corporate_actions(book, feed, "2025-07-15")
    assert book.quantity(ACTION["symbol"]) == 293 + shares
    assert book.available(ACTION["symbol"], "2025-07-15") == 293
    assert book.available(ACTION["symbol"], "2025-07-16") == 293 + shares
    assert state["cash"] == before_cash + int(Decimal(".15") * 293 * Decimal(".8") * 100)
    assert state["corporate_action_reconciliations"][IDENTIFIER]["allocated_shares"] == shares
    event = next(row for row in state["events"] if row["kind"] == "corporate_action")
    assert event["allocation_source_kind"] == "explicit_replay_assumption"
    assert event["nominal_bonus_shares"] == "117.2"
    restored = json.loads(json.dumps(state))
    restored_book = Ledger(restored, StrategyConfig())
    corporate_actions(restored_book, feed, "2025-07-16")
    assert restored_book.quantity(ACTION["symbol"]) == 293 + shares
    assert restored["cash"] == state["cash"]


def test_assumption_refused_outside_replay_and_on_bound_account():
    state, book, feed = fixture("floor", TradingMode.PAPER)
    before = deepcopy(state)
    with pytest.raises(ValueError, match="replay-only"):
        corporate_actions(book, feed, "2025-07-15")
    assert state == before
    state, book, feed = fixture("floor")
    state["broker_binding"] = {"account_id": "example"}
    before = deepcopy(state)
    with pytest.raises(ValueError, match="unbound replay"):
        corporate_actions(book, feed, "2025-07-15")
    assert state == before


def test_actual_receipt_not_assumption_and_wrong_economics_refused():
    for mode in (TradingMode.REPLAY, TradingMode.PAPER):
        state, book, feed = fixture(mode=mode)
        state["corporate_action_reconciliations"] = {IDENTIFIER: actual_receipt(book.context)}
        corporate_actions(book, feed, "2025-07-15")
        assert book.quantity(ACTION["symbol"]) == 411
        assert state["events"][0]["allocation_source_kind"] == "broker_account_record"
    for changes in ({"account_id": "other"}, {"registered_quantity": 292}, {"allocated_shares": 116},
                    {"allocated_shares": True}, {"source_ref": ""}, {"action_terms": {}}):
        state, book, feed = fixture("ceil")
        state["corporate_action_reconciliations"] = {IDENTIFIER: {**actual_receipt(book.context), **changes}}
        before = deepcopy(state)
        with pytest.raises(ValueError):
            corporate_actions(book, feed, "2025-07-15")
        assert state == before


def test_no_assumption_for_whole_shares_and_no_unknown_listing_override():
    state, book, feed = fixture("ceil")
    state["entitlements"][IDENTIFIER] = 300
    corporate_actions(book, feed, "2025-07-15")
    assert book.quantity(ACTION["symbol"]) == 413
    assert "corporate_action_reconciliations" not in state
    state, book, feed = fixture("ceil")
    feed.actions[0]["dividStockMarketDate"] = ""
    before = deepcopy(state)
    with pytest.raises(ValueError, match="Undated"):
        corporate_actions(book, feed, "2025-07-15")
    assert state == before


def test_unknown_cash_cannot_become_zero_in_an_allocation_receipt():
    state, book, feed = fixture("floor")
    feed.actions[0]["dividCashPsBeforeTax"] = ""
    before = deepcopy(state)
    with pytest.raises(ValueError, match="Unresolved ordinary allocation terms"):
        corporate_actions(book, feed, "2025-07-15")
    assert state == before
