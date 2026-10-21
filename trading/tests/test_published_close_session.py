"""Known D targets settle on E even when E has no new close publication."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json

import pytest

from trading import engine
from trading.contracts import PortfolioIntent, TradingMode, RunContext
from trading.ledger import Ledger, new_state
from trading.published_close_session import METHOD_ID, attach_close
from trading.tests.test_published_close_intent import D, E, F, A, B, CONTEXT, CONFIG, feed, intent


class Target:
    policy_id = "synthetic_fixed_target"
    def __call__(self, value):
        return value


class Risk:
    policy_id = "synthetic_fixed_expiry"
    def __call__(self, day, book, base):
        return None


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(F + "T20:00:00+08:00"))


def step(state, base, day, number, publication=None, risk=None):
    return engine.advance(state, base, day, CONFIG, number, context=CONTEXT,
        published_close_intent=publication, published_close_target_policy=Target(),
        replay_risk_exit_policy=risk or Risk(), published_close_session_method=METHOD_ID)


def attach(state, base, value, risk=None):
    return attach_close(state, base, E, value, CONFIG, 1, context=CONTEXT,
        published_close_target_policy=Target(), replay_risk_exit_policy=risk or Risk())


def test_settle_missing_close_then_attach_changes_only_next_target_not_fills_cash_or_T1():
    state, base = new_state(10000), feed()
    step(state, base, D, 0, intent())
    assert state["day"] == D and not state["orders"]
    row = step(state, base, E, 1)
    book = Ledger(state, CONFIG, CONTEXT)
    assert row["new_orders"] == 1 and book.quantity(B) == 800
    assert book.available(B, E) == 0 and book.available(B, F) == 800
    assert base.calls == [] and not any(e["kind"] == "no_decision" for e in row["events"])
    assert state["last_published_close_session"]["status"] == "awaiting_publication"
    assert state["pending_targets"] is None and "pending_published_close_intent" not in state
    saved = json.loads(json.dumps(state))
    e_plan = intent({A: .8}, decided_at=E + "T18:45:00+08:00", execution_not_before=F)
    result = attach(state, base, e_plan)
    assert result["new_orders"] == result["new_fills"] == 0
    for field in ("cash", "positions", "prices", "orders", "fills", "receivables", "entitlements"):
        assert state[field] == saved[field]
    assert state["pending_targets"] == {A: .8} and state["last_published_close_session"]["status"] == "publication_attached"
    checkpoint = deepcopy(state)
    assert attach(state, base, e_plan)["already_attached"] is True and state == checkpoint
    assert step(state, base, F, 2)["new_orders"] == 2
    assert book.quantity(B) == 0 and book.quantity(A) > 0 and base.calls == []
    assert sum(e["kind"] == "published_close_intent_consumed" for e in state["events"]) == 2


def test_explicit_NO_CHANGE_is_recorded_but_missing_publication_is_never_fabricated():
    state, base = new_state(10000), feed()
    step(state, base, D, 0, intent())
    step(state, base, E, 1)
    nc = PortfolioIntent.no_change("actual_unknown", context=CONTEXT,
        decided_at=E + "T18:45:00+08:00", execution_not_before=F, source="saved_actual_E")
    attach(state, base, nc)
    assert state["pending_targets"] is None
    assert state["last_published_close_session"]["publication"]["intent"]["kind"] == "NO_CHANGE"
    assert step(state, base, F, 2)["new_orders"] == 0
    assert Ledger(state, CONFIG, CONTEXT).quantity(B) == 800


def test_independent_risk_exit_survives_missing_E_publication_and_later_attachment():
    class Expiry(Risk):
        def __call__(self, day, book, base):
            return (engine.ReplayRiskExitIntent(CONTEXT, E, F, (B,), "synthetic_expiry", "known_expiry")
                if day == E else None)
    state, base = new_state(10000), feed()
    step(state, base, D, 0, intent())
    step(state, base, E, 1, risk=Expiry())
    assert state["pending_risk_exits"]["symbols"] == [B]
    # Actual E signal may arrive after settlement. A missing structural result
    # cannot cancel the independently known expiry queued by settlement.
    attach(state, base, intent({B: .8}, decided_at=E+"T18:45:00+08:00", execution_not_before=F))
    assert state["pending_risk_exits"]["symbols"] == [B]
    step(state, base, F, 2)
    assert Ledger(state, CONFIG, CONTEXT).quantity(B) == 0
    assert all(o["side"] == "sell" for o in state["orders"].values() if o["day"] == F)


def test_original_five_session_expiry_runs_across_multiple_missing_publications(monkeypatch):
    import pandas as pd
    from trading.research.leader_published_risk_exit import PublishedCloseLeaderTypeRiskExitPolicy
    days = ["2025-01-03", D, E, F, "2025-01-09", "2025-01-10", "2025-01-13", "2025-01-14", "2025-01-15"]
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(days[-1]+"T20:00:00+08:00"))
    base = feed()
    row = {**base.rows[D][B], "high": 10.1, "low": 9.9, "volume": 1000000.,
           "amount": 10000000., "adjustflag": "3"}
    base.calendar = days
    base.rows = {day: {s: deepcopy(row) for s in (A, B)} for day in days}
    base.volumes = pd.DataFrame(1000000., index=days, columns=[A, B])
    base.next_day = lambda d: days[days.index(d)+1]
    risk = PublishedCloseLeaderTypeRiskExitPolicy("relay_continuation_leader",
        candidate_by_day={D: [{"symbol": B}]}, feature_by_day={})
    state = new_state(10000)
    step(state, base, D, 0, intent(), risk)
    for number, day in enumerate(days[2:-2], 1):
        result = step(state, base, day, number, risk=risk)
        assert any(e["kind"] == "leader_type_risk_deferred" for e in result["events"])
        assert Ledger(state, CONFIG, CONTEXT).quantity(B) == 800
    assert state["pending_risk_exits"]["execution_on"] == "2025-01-14"
    result = step(state, base, "2025-01-14", 6, risk=risk)
    assert result["new_orders"] == 1 and Ledger(state, CONFIG, CONTEXT).quantity(B) == 0
    assert base.calls == [] and not any(e["kind"] == "no_decision" for e in state["events"])


def test_unpriceable_held_session_preserves_pending_and_cash_before_execution():
    state, base = new_state(10000), feed()
    step(state, base, D, 0, intent())
    step(state, base, E, 1)
    base.rows[F].pop(B)
    before = deepcopy(state)
    with pytest.raises(ValueError, match="Unpriceable holding"):
        step(state, base, F, 2)
    assert state == before


@pytest.mark.parametrize("fault", ["legacy_resume", "legacy_upgrade", "skip", "changed_config", "late_attach", "replaced_pub"])
def test_method_migration_skip_and_replacement_are_rejected(fault):
    state, base = new_state(10000), feed()
    if fault == "legacy_upgrade":
        engine.advance(state, base, D, CONFIG, 0, context=CONTEXT, published_close_intent=intent())
    else:
        step(state, base, D, 0, intent())
    if fault in {"late_attach", "replaced_pub"}:
        step(state, base, E, 1)
    value = intent({A: .8}, decided_at=E+"T18:45:00+08:00", execution_not_before=F)
    if fault == "late_attach":
        step(state, base, F, 2)
    if fault == "replaced_pub":
        attach(state, base, value)
    before = deepcopy(state)
    with pytest.raises(ValueError):
        if fault == "legacy_resume":
            engine.advance(state, base, E, CONFIG, 1, context=CONTEXT)
        elif fault == "skip":
            step(state, base, F, 2)
        elif fault == "changed_config":
            engine.advance(state, base, E, replace(CONFIG, initial_cash=9999), 1, context=CONTEXT,
                published_close_session_method=METHOD_ID)
        elif fault in {"late_attach", "replaced_pub"}:
            attach(state, base, replace(value, source="replaced_source"))
        else:
            step(state, base, E, 1)
    assert state == before and base.calls == []
