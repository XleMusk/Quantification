"""Real Engine preopen targets must enter pure corporate-action validation."""
import json

import pytest

from trading.contracts import PortfolioIntent, PreopenPortfolioIntent
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.research.tests import test_research_corporate_action_view as action_fixture


def preopen(state, feed, day, weights=None, *, session_number=0):
    previous = feed.calendar[feed.calendar.index(day) - 1]
    metadata = {"context": action_fixture.CONTEXT, "source": "research:synthetic-preopen-action-preflight"}
    intent = (PortfolioIntent.no_change("explicit_preopen_preserve_actual_shares", **metadata)
        if weights is None else PortfolioIntent.full_target(weights, **metadata))
    envelope = PreopenPortfolioIntent(previous, day, previous + "T18:30:00+08:00", intent)
    return advance(state, feed, day, action_fixture.CONFIG, session_number,
        context=action_fixture.CONTEXT, preopen_intent=envelope)


def test_partial_record_action_new_preopen_purchase_rejects_before_any_ledger_mutation(tmp_path):
    review = action_fixture.source(tmp_path / "partial", bonus=None)
    action_view = action_fixture.view(review)
    feed = action_fixture.raw_feed(action_view)
    state = new_state(action_fixture.CONFIG.initial_cash)
    before = action_fixture.serialized(state)
    with pytest.raises(ValueError):
        preopen(state, feed, action_fixture.DAYS[0], {action_fixture.S: .1})
    assert action_fixture.serialized(state) == before


def test_complete_preopen_record_entitlement_receivable_and_payment_use_one_ledger(tmp_path):
    action_view = action_fixture.view(action_fixture.source(tmp_path / "complete"))
    feed = action_fixture.raw_feed(action_view)
    state = new_state(action_fixture.CONFIG.initial_cash)
    symbol, days = action_fixture.S, action_fixture.DAYS
    identifier = symbol + ":" + days[2]
    preopen(state, feed, days[0], {symbol: .1})
    book = Ledger(state, action_fixture.CONFIG, action_fixture.CONTEXT)
    assert book.quantity(symbol) == state["entitlements"][identifier] == 100
    assert book.available(symbol, days[0]) == 0
    assert state["cash"] == 900_000 and len(state["orders"]) == len(state["fills"]) == 1
    assert state["pending_targets"] is None
    # Subsequent explicit NO_CHANGE sessions retain those actual shares. The
    # ex-date receivable and payment use the same original account and fills.
    preopen(state, feed, days[1], session_number=1)
    ex_row = preopen(state, feed, days[2], session_number=2)
    assert state["cash"] == 900_000 and state["receivables"] == {identifier: 800}
    assert ex_row["receivables"] == 8. and book.equity() == 1_000_800
    preopen(state, feed, days[3], session_number=3)
    assert state["cash"] == 900_800 and not state["receivables"]
    preopen(state, feed, days[4], session_number=4)
    assert state["cash"] == 900_800 and not state["receivables"]
    assert book.quantity(symbol) == 100 and state["entitlements"] == {identifier: 100}
    assert len(state["orders"]) == len(state["fills"]) == 1 and state["pending_targets"] is None
    assert len([e for e in state["events"] if e["kind"] == "corporate_action"]) == 1
    assert len([e for e in state["events"] if e["kind"] == "dividend_paid"]) == 1
    assert len([e for e in state["events"] if e["kind"] == "preopen_targets_received"]) == 1
    assert len([e for e in state["events"] if e["kind"] == "preopen_no_decision"]) == 4


def test_NO_CHANGE_empty_cash_book_is_not_blocked_or_turned_into_purchase_by_partial_action(tmp_path):
    action_view = action_fixture.view(action_fixture.source(tmp_path / "unrelated_partial", bonus=None))
    feed = action_fixture.raw_feed(action_view)
    state = new_state(action_fixture.CONFIG.initial_cash)
    symbol, days = action_fixture.S, action_fixture.DAYS
    preopen(state, feed, days[0])
    assert state["cash"] == 1_000_000 and not state["positions"]
    assert not state["orders"] and not state["fills"] and state["pending_targets"] is None
    assert state["entitlements"] == {symbol + ":" + days[2]: 0}
    assert any(e["kind"] == "preopen_no_decision" and e["quantity_retention"] is True for e in state["events"])
    assert not any(e["kind"] == "preopen_targets_received" for e in state["events"])
    preopen(state, feed, days[1], session_number=1)
    assert not state["positions"] and not state["orders"] and not state["fills"]
    assert state["cash"] == 1_000_000 and not state["receivables"]


def test_record_day_10am_action_evidence_cannot_authorize_0930_preopen_entry(tmp_path):
    review_file = action_fixture.source(tmp_path / "record_day_10am")
    event_day = action_fixture.DAYS[0]
    available = event_day + "T10:00:00+08:00"
    review = json.loads(review_file.read_text(encoding="utf8"))
    review["reviewed_at"] = available
    review["events"][0]["local_primary_evidence_available_at"] = available
    for item in review["events"][0]["official_sources"]:
        item["retrieved_at"] = available
    receipt_file = review_file.parent / "retrieval_receipt.json"
    receipt = json.loads(receipt_file.read_text(encoding="utf8"))
    for item in receipt["sources"]:
        item["started_at"] = event_day + "T09:59:00+08:00"
        item["retrieved_at"] = available
    action_fixture.save(receipt_file, receipt)
    action_fixture.save(review_file, review)
    action_view = action_fixture.view(review_file)
    assert action_view.events[0]["available_at"] == available
    assert action_view.events[0]["component_status"] == "complete_ordinary_components"
    feed = action_fixture.raw_feed(action_view)
    state = new_state(action_fixture.CONFIG.initial_cash)
    before = action_fixture.serialized(state)
    with pytest.raises(ValueError):
        preopen(state, feed, event_day, {action_fixture.S: .1})
    assert action_fixture.serialized(state) == before
    # The new opening-entry frontier must not replace the original record-close
    # frontier. A cash-only NO_CHANGE can still observe the explicit zero at
    # this close, since the complete evidence was available by 15:00.
    unchanged = new_state(action_fixture.CONFIG.initial_cash)
    preopen(unchanged, feed, event_day)
    assert unchanged["cash"] == 1_000_000
    assert not unchanged["orders"] and not unchanged["fills"] and not unchanged["positions"]
    assert unchanged["entitlements"] == {action_fixture.S + ":" + action_fixture.DAYS[2]: 0}
