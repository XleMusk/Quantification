"""Published reductions inspect the real post-opening Ledger, never a pre-open book."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine
from trading.config import StrategyConfig
from trading.contracts import IntentKind, PortfolioIntent, RunContext, SignalUnavailable, TradingMode
from trading.ledger import Ledger, new_state
from trading.research.shortline_decision_v2 import fixed_exit_policy, holding_origin


O, D, E, F = "2025-01-02", "2025-01-09", "2025-01-10", "2025-01-13"
A, B, C = "600000.SSE", "000001.SZSE", "600519.SSE"
CONTEXT = RunContext("published_reduction_case", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=3, retain_rank=3, gross_limit=.6, single_limit=.2,
                        rebalance_sessions=1, slippage_bps=0)
POLICY_ID = "test:fixed_five_session_published_reduction_v1"


@pytest.fixture(autouse=True)
def actual_clock(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(F + "T20:00:00+08:00"))


def feed():
    days = [O, "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08", D, E, F, "2025-01-14"]
    row = {"open": 10., "high": 10.5, "low": 9.5, "close": 10., "preclose": 10.,
           "tradestatus": 1, "isST": 0}
    return SimpleNamespace(calendar=days, rows={day: {s: deepcopy(row) for s in (A, B, C)} for day in days},
        volumes=pd.DataFrame(1_000_000., index=days, columns=[A, B, C]),
        instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in (A, B, C)}, actions=[],
        next_day=lambda day: days[days.index(day) + 1],
        scores=lambda day, config: SignalUnavailable("no_current_publication"), market_defense=False)


def intent(day=D, weights=None):
    return PortfolioIntent.full_target({A: .2, B: .2, C: .2} if weights is None else weights,
        context=CONTEXT, decided_at=day + "T18:45:00+08:00", execution_not_before=E if day == D else F,
        source="saved_publication:original_sha256")


def no_change(day=D):
    return replace(intent(day), kind=IntentKind.NO_CHANGE, weights=(), reason="unknown_candidate_membership")


def policy(callback=lambda value: value, policy_id=POLICY_ID):
    callback.policy_id = policy_id
    return callback


def step(state, base, day, **options):
    return engine.advance(state, base, day, CONFIG, base.calendar.index(day), context=CONTEXT, **options)


def seeded_ledger():
    state = new_state(CONFIG.initial_cash)
    book = Ledger(state, CONFIG, CONTEXT)
    for symbol in (A, B):
        identifier = f"{O}:{symbol}:buy"
        book.submit(identifier, O, symbol, "buy", 200, 1000)
        book.fill(identifier + ":fill", identifier, 200, 1000, O, "2025-01-03")
        book.mark_price(symbol, 1000, O)
    return state


def test_reducer_sees_opening_fills_and_real_old_new_origins_then_effective_receipt_executes_E():
    state, base, seen = seeded_ledger(), feed(), []
    # A is sold at D open, B remains an old continuous holding, C is newly bought.
    state["pending_targets"] = {B: .2, C: .2}
    original = intent()
    fixed = fixed_exit_policy(base, state, CONFIG)
    def reduce(value):
        book = Ledger(state, CONFIG, CONTEXT)
        seen.append({s: (book.quantity(s), holding_origin(state, s)) for s in (A, B, C)})
        return fixed(value)
    reducer = policy(reduce)
    ordering = []
    def risk(day, book, source):
        ordering.append(deepcopy(book.state["pending_targets"]))
        return None
    risk.policy_id = "test:independent_post_target_risk"
    result = step(state, base, D, published_close_intent=original, published_close_target_policy=reducer,
                  replay_risk_exit_policy=risk)
    assert seen[0][A] == (0, None) and seen[0][B][0] > 0 and seen[0][B][1] == O
    assert seen[0][C][0] > 0 and seen[0][C][1] == D
    # Pre-applying the policy would have removed old A too; post-opening permits
    # its fresh re-entry while reducing the genuinely surviving expired B.
    assert state["pending_targets"] == {A: .2, C: .2} and ordering == [{A: .2, C: .2}]
    receipt = state["pending_published_close_intent"]
    assert receipt["schema"] == engine.PUBLISHED_CLOSE_REDUCED_INTENT_SCHEMA
    assert receipt["original_intent"] == original.to_dict() and receipt["policy_id"] == POLICY_ID
    assert receipt["reduced_intent"]["weights"] == {A: .2, C: .2}
    assert receipt["decision_at"] == original.decided_at and receipt["execution_not_before"] == E
    assert result["published_close_intent_frontier"]["intent"] == original.to_dict()
    assert result["published_close_reduction"] == receipt
    assert result["events"][-1]["published_close_reduction"] == receipt
    assert state["published_close_target_policy"]["context"] == CONTEXT.to_dict()
    # Persist/restore the same state; callback closes over the restored Ledger.
    restored = json.loads(json.dumps(state))
    reducer = policy(fixed_exit_policy(base, restored, CONFIG))
    next_result = step(restored, base, E, published_close_intent=no_change(E),
                       published_close_target_policy=reducer, replay_risk_exit_policy=risk)
    book = Ledger(restored, CONFIG, CONTEXT)
    assert book.quantity(A) > 0 and book.quantity(B) == 0 and book.quantity(C) > 0
    assert next_result["consumed_published_close_intent"] == receipt
    assert "pending_published_close_intent" not in restored and restored["pending_targets"] is None
    assert all(order["day"] in {O, D, E} for order in restored["orders"].values())


def test_original_NO_CHANGE_never_calls_reducer_or_creates_empty_exit():
    state, base = seeded_ledger(), feed()
    def forbidden(value):
        raise AssertionError("Unknown original publication must never be reduced into cash")
    result = step(state, base, D, published_close_intent=no_change(),
                  published_close_target_policy=policy(forbidden))
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 200
    assert state["pending_targets"] is None and "pending_published_close_intent" not in state
    assert "published_close_reduction" not in result
    assert result["published_close_intent_frontier"]["intent"] == no_change().to_dict()


def test_derived_NO_CHANGE_keeps_weights_unknown_not_empty_and_retains_original_frontier():
    state, base = seeded_ledger(), feed()
    reducer = policy(lambda value: replace(value, kind=IntentKind.NO_CHANGE, weights=(), reason="exit_inputs_unknown"))
    result = step(state, base, D, published_close_intent=intent(), published_close_target_policy=reducer)
    assert state["pending_targets"] is None and "pending_published_close_intent" not in state
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 200
    assert result["published_close_reduction"]["reduced_intent"]["kind"] == "NO_CHANGE"
    assert result["published_close_reduction"]["original_intent"] == intent().to_dict()


@pytest.mark.parametrize("bad", [
    lambda value: replace(value, context=RunContext("wrong_account", TradingMode.REPLAY)),
    lambda value: replace(value, decided_at=D + "T19:00:00+08:00"),
    lambda value: replace(value, execution_not_before=F),
    lambda value: replace(value, source="different_publication"),
    lambda value: replace(value, weights=((A, .3),)),
    lambda value: replace(value, weights=(("600001.SSE", .1),)),
    lambda value: replace(value, kind=IntentKind.NO_CHANGE, weights=(), reason="unknown", source=None),
    lambda value: replace(value, kind=IntentKind.NO_CHANGE, weights=(), reason="unknown", execution_not_before=F),
])
def test_invalid_reduction_rejected_on_trial_without_committing_real_ledger(bad):
    state, base = seeded_ledger(), feed()
    state["pending_targets"] = {C: .2}  # Trial must perform real opening fills before invoking the reducer.
    saved = deepcopy(state)
    trial = deepcopy(state)
    with pytest.raises(ValueError):
        step(trial, base, D, published_close_intent=intent(), published_close_target_policy=policy(bad))
    assert state == saved
    assert Ledger(trial, CONFIG, CONTEXT).quantity(C) > 0  # Not a pre-opening fake check.


@pytest.mark.parametrize("change", ["drop", "change", "context"])
def test_bound_policy_identity_cannot_be_dropped_changed_or_rebound_before_mutation(change):
    state, base = new_state(10000), feed()
    reducer = policy(lambda value: value)
    step(state, base, D, published_close_intent=intent(), published_close_target_policy=reducer)
    options = {"published_close_intent": no_change(E), "published_close_target_policy": reducer}
    if change == "drop":
        options["published_close_target_policy"] = None
    elif change == "change":
        options["published_close_target_policy"] = policy(lambda value: value, "different_method")
    else:
        state["published_close_target_policy"]["context"] = RunContext("wrong_account", TradingMode.REPLAY).to_dict()
    saved = deepcopy(state)
    with pytest.raises(ValueError):
        step(state, base, E, **options)
    assert state == saved


def test_reducer_cannot_be_introduced_into_advanced_legacy_account():
    state, base = new_state(10000), feed()
    step(state, base, D, published_close_intent=intent())
    saved = deepcopy(state)
    with pytest.raises(ValueError, match="introduced"):
        step(state, base, E, published_close_intent=no_change(E),
             published_close_target_policy=policy(lambda value: value))
    assert state == saved and state["pending_published_close_intent"]["schema"] == engine.PUBLISHED_CLOSE_INTENT_SCHEMA


def test_bound_published_reducer_cannot_fall_back_to_legacy_signal_when_publication_missing():
    state, base = new_state(10000), feed()
    reducer = policy(lambda value: value)
    step(state, base, D, published_close_intent=intent(), published_close_target_policy=reducer)
    saved = deepcopy(state)
    def forbidden(*args):
        raise AssertionError("Missing real publication cannot fall back to an old SignalFrame")
    base.scores = forbidden
    with pytest.raises(ValueError, match="explicit publication on every session"):
        step(state, base, E, published_close_target_policy=reducer)
    assert state == saved and not state["orders"]


@pytest.mark.parametrize("tamper", [
    lambda state: state["pending_targets"].update({A: .1}),
    lambda state: state["pending_published_close_intent"]["reduced_intent"].update(source="wrong_source"),
    lambda state: state["pending_published_close_intent"]["reduced_intent"].update(execution_not_before=F),
    lambda state: state["pending_published_close_intent"]["reduced_intent"].update(weights={A: .3}),
    lambda state: state["pending_published_close_intent"].update(policy_id="changed_method"),
])
def test_reduced_checkpoint_tampering_rejected_before_opening(tamper):
    state, base = new_state(10000), feed()
    reducer = policy(lambda value: replace(value, weights=((A, .2),)))
    step(state, base, D, published_close_intent=intent(), published_close_target_policy=reducer)
    tamper(state)
    saved = deepcopy(state)
    with pytest.raises(ValueError):
        step(state, base, E, published_close_intent=no_change(E), published_close_target_policy=reducer)
    assert state == saved and not state["orders"]


@pytest.mark.parametrize("reducer", [lambda value: value, policy(lambda value: value, " ")])
def test_reducer_requires_stable_nonempty_identity_before_opening(reducer):
    state, base = seeded_ledger(), feed()
    saved = deepcopy(state)
    with pytest.raises(ValueError, match="identified"):
        step(state, base, D, published_close_intent=intent(), published_close_target_policy=reducer)
    assert state == saved
