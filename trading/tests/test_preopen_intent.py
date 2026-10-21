"""Explicit overnight publications use real simulated Engine/Ledger execution."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json

import pytest

from trading import engine
from trading.contracts import IntentKind, PortfolioIntent, PreopenPortfolioIntent, RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.tests.test_published_close_intent import A, B, CONFIG, D, E, F, feed as original_feed


CONTEXT = RunContext("explicit_preopen_case", TradingMode.REPLAY)


@pytest.fixture(autouse=True)
def actual_clock(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(F + "T20:00:00+08:00"))


def feed():
    value = original_feed()
    for rows in value.rows.values():
        for row in rows.values():
            row.update(high=10.5, low=9.5, volume=1_000_000., amount=10_000_000., adjustflag="3")
    return value


def envelope(weights=None, *, source_day=D, execution_day=E, published_at=None,
             context=CONTEXT, no_change=False, explicit_exit_all=False):
    inner = (PortfolioIntent.no_change("unknown_coverage_hold_exact_shares", context=context, source="synthetic_saved_publication")
        if no_change else PortfolioIntent.full_target({B: .8} if weights is None else weights,
            reason="explicit_research_exit_all" if explicit_exit_all else "saved_research_target",
            context=context, source="synthetic_saved_publication"))
    return PreopenPortfolioIntent(source_day, execution_day, published_at or execution_day + "T01:00:00+08:00",
        inner, explicit_exit_all=explicit_exit_all)


def step(state, base, day, number, *, context=CONTEXT, config=CONFIG, **options):
    return engine.advance(state, base, day, config, number, context=context, **options)


def background(*, context=CONTEXT, held=False):
    state, base = new_state(9000 if held else 10000), feed()
    if held:
        state["positions"][A] = [{"qty": 100, "available": D}]
        state["prices"][A] = 1000
    step(state, base, D, 0, context=context)
    base.calls.clear()
    return state, base


@pytest.mark.parametrize("mode", [TradingMode.REPLAY, TradingMode.PAPER])
def test_overnight_typed_json_publication_buys_at_exact_open_with_cash_fees_and_T1(mode):
    context = RunContext(CONTEXT.account_id, mode)
    state, base = background(context=context)
    saved = json.loads(json.dumps(envelope(context=context).to_dict()))
    publication = PreopenPortfolioIntent.from_dict(saved)
    assert publication.to_dict() == saved
    assert not state["orders"] and state["pending_targets"] is None
    state = json.loads(json.dumps(state))
    result = step(state, base, E, 1, context=context, preopen_intent=publication)
    book = Ledger(state, CONFIG, context)
    order = state["orders"][E + ":" + B + ":buy"]
    assert order["status"] == "filled" and order["qty"] == 800
    assert order["fees"] == 508 and state["cash"] == 199492
    assert book.quantity(B) == 800
    assert book.available(B, E) == 0 and book.available(B, F) == 800
    assert state["pending_targets"] is None
    assert base.calls == []  # No E-close signal was used at E's opening.
    receipt = state["last_preopen_intent"]
    assert receipt["schema"] == "simulation_preopen_portfolio_intent_receipt_v1"
    assert receipt["source_day"] == D and receipt["execution_day"] == E
    assert receipt["published_at"] == E + "T01:00:00+08:00"
    assert receipt["envelope"] == publication.to_dict()
    assert receipt["execution_admitted"] is False
    assert result["preopen_intent_frontier"] == receipt
    assert state["preopen_execution_binding"] == {"schema": "simulation_preopen_execution_binding_v1",
        "context": context.to_dict(), "config": CONFIG.to_dict()}
    restored = json.loads(json.dumps(state))
    next_pub = envelope(source_day=E, execution_day=F, context=context, no_change=True)
    step(restored, base, F, 2, context=context, preopen_intent=next_pub)
    assert restored["cash"] == 199492 and Ledger(restored, CONFIG, context).quantity(B) == 800
    assert len(restored["orders"]) == 1
    before = deepcopy(restored)
    with pytest.raises(ValueError, match="twice|backwards"):
        step(restored, base, F, 2, context=context, preopen_intent=next_pub)
    assert restored == before


def test_existing_position_no_change_is_exact_shares_but_explicit_empty_target_sells():
    state, base = background(held=True)
    result = step(state, base, E, 1, preopen_intent=envelope(no_change=True))
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 100
    assert not state["orders"] and state["pending_targets"] is None
    assert result["preopen_intent_frontier"]["envelope"]["intent"]["kind"] == "NO_CHANGE"
    assert base.calls == []
    state, base = background(held=True)
    cash_before = state["cash"]
    step(state, base, E, 1, preopen_intent=envelope({}, explicit_exit_all=True))
    order = state["orders"][E + ":" + A + ":sell"]
    assert order["status"] == "filled" and order["qty"] == 100
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 0
    assert state["cash"] == cash_before + 100000 - order["fees"]
    assert state["pending_targets"] is None and base.calls == []


def test_preopen_no_change_forced_st_preserves_137_shares_of_other_holding():
    # Existing settled holdings total 2,370 RMB; cash plus holdings is 10,000.
    state, base = new_state(7630), feed()
    state["positions"] = {A: [{"qty": 100, "available": D}], B: [{"qty": 137, "available": D}]}
    state["prices"] = {A: 1000, B: 1000}
    step(state, base, D, 0)
    base.calls.clear()
    base.rows[E][A]["isST"] = 1
    cash_before = state["cash"]
    result = step(state, base, E, 1, preopen_intent=envelope(no_change=True))
    book = Ledger(state, CONFIG, CONTEXT)
    order = state["orders"][E + ":" + A + ":sell"]
    assert order["status"] == "filled" and order["qty"] == 100
    assert book.quantity(A) == 0
    assert book.quantity(B) == 137 and book.available(B, E) == 137
    assert {row["symbol"] for row in state["orders"].values()} == {A}
    assert len(state["orders"]) == 1 and result["new_orders"] == 1
    assert state["cash"] == cash_before + 100000 - order["fees"]
    assert state["pending_targets"] is None and base.calls == []
    assert result["preopen_intent_frontier"]["envelope"]["intent"]["kind"] == "NO_CHANGE"


def test_invalid_clocks_sessions_identity_caps_and_typed_kind_reject_before_any_mutation(monkeypatch):
    factories = [
        lambda: envelope(published_at=D + "T15:00:00+08:00"),
        lambda: envelope(published_at=E + "T09:30:00+08:00"),
        lambda: envelope(published_at=E + "T10:00:00+08:00"),
        lambda: envelope(source_day="2025-01-03"),
        lambda: envelope(execution_day=F),
        lambda: envelope(context=RunContext("other_account", TradingMode.REPLAY)),
        lambda: envelope(context=RunContext(CONTEXT.account_id, TradingMode.PAPER)),
        lambda: envelope({B: .9}),
        lambda: envelope({}),
        lambda: envelope(no_change=True, explicit_exit_all=True),
        lambda: envelope(explicit_exit_all=True),
        lambda: PreopenPortfolioIntent(D, E, E + "T01:00:00+08:00",
            PortfolioIntent(IntentKind.DELTA, ((B, .1),), context=CONTEXT, source="unsupported_delta")),
        lambda: PreopenPortfolioIntent(D, E, E + "T01:00:00+08:00",
            PortfolioIntent.full_target({B: .8}, context=CONTEXT, decided_at=D + "T18:00:00+08:00",
                execution_not_before=E, source="EOD_metadata_cannot_be_reinterpreted")),
    ]
    for make in factories:
        state, base = background(held=True)
        before = deepcopy(state)
        with pytest.raises(ValueError):
            step(state, base, E, 1, preopen_intent=make())
        assert state == before and base.calls == []
    state, base = background(held=True)
    before = deepcopy(state)
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(E + "T00:30:00+08:00"))
    with pytest.raises(ValueError):
        step(state, base, E, 1, preopen_intent=envelope())
    assert state == before and base.calls == []


def test_pending_or_overlay_conflicts_and_bad_current_reference_reject_before_mutation():
    conflicts = [
        {"target_policy": lambda value: value}, {"portfolio_policy": lambda value: value},
        {"lot_allocation": engine.LOT_ALLOCATION}, {"commission_deadband": True},
        {"published_close_intent": PortfolioIntent.no_change("EOD_conflict", context=CONTEXT,
            decided_at=E + "T18:00:00+08:00", execution_not_before=F, source="old_route")},
        {"published_close_target_policy": lambda value: value},
        {"replay_risk_exit_policy": lambda value: value},
    ]
    for options in conflicts:
        state, base = background(held=True)
        before = deepcopy(state)
        with pytest.raises(ValueError):
            step(state, base, E, 1, preopen_intent=envelope(), **options)
        assert state == before and base.calls == []
    for field, value in (("pending_targets", {}), ("pending_targets", {A: .2}),
            ("pending_published_close_intent", {}), ("pending_risk_exits", {})):
        state, base = background(held=True)
        state[field] = value
        before = deepcopy(state)
        with pytest.raises(ValueError):
            step(state, base, E, 1, preopen_intent=envelope())
        assert state == before and base.calls == []
    for field, value in (("adjustflag", "2"), ("tradestatus", None), ("preclose", float("nan"))):
        state, base = background()
        base.rows[E][B][field] = value
        before = deepcopy(state)
        with pytest.raises(ValueError):
            step(state, base, E, 1, preopen_intent=envelope())
        assert state == before and base.calls == []


def test_restored_preopen_binding_cannot_be_disabled_changed_or_skip_exact_source():
    state, base = background()
    step(state, base, E, 1, preopen_intent=envelope())
    for context, config, publication in (
        (CONTEXT, CONFIG, None),
        (RunContext("other_account", TradingMode.REPLAY), CONFIG,
            envelope(source_day=E, execution_day=F, context=RunContext("other_account", TradingMode.REPLAY))),
        (CONTEXT, replace(CONFIG, initial_cash=20000), envelope(source_day=E, execution_day=F)),
    ):
        restored = json.loads(json.dumps(state))
        before = deepcopy(restored)
        with pytest.raises(ValueError):
            step(restored, base, F, 2, context=context, config=config, preopen_intent=publication)
        assert restored == before
    restored = json.loads(json.dumps(state))
    restored["day"] = D
    before = deepcopy(restored)
    with pytest.raises(ValueError):
        step(restored, base, F, 2, preopen_intent=envelope(source_day=E, execution_day=F))
    assert restored == before


def test_default_none_is_unchanged_and_old_eod_contract_still_forbids_same_day_execution():
    left, right, left_feed, right_feed = new_state(10000), new_state(10000), feed(), feed()
    first = step(left, left_feed, D, 0)
    second = step(right, right_feed, D, 0, preopen_intent=None)
    assert first == second and left == right
    assert "preopen_execution_binding" not in left and "preopen_intent_frontier" not in first
    with pytest.raises(ValueError, match="EOD targets cannot execute"):
        PortfolioIntent.full_target({B: .8}, context=CONTEXT, decided_at=E + "T01:00:00+08:00",
            execution_not_before=E, source="same_day_EOD_still_invalid")
