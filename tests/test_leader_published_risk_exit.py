"""Forward queue availability only; no source collection or profitability claim."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine
from trading.contracts import PortfolioIntent, RunContext, SignalUnavailable, TradingMode
from trading.engine import ReplayRiskExitIntent
from trading.ledger import Ledger, new_state
from trading.research.leader_published_risk_exit import PublishedCloseLeaderTypeRiskExitPolicy
from trading.research.leader_type_risk_exit import LeaderTypeRiskExitPolicy
from trading.research.shortline_decision_v2 import holding_origin
from trading.research.tests.test_leader_type_risk_exit import (
    A, CONFIG, DAYS, candidate, feature, fixture, raw,
)


class PastRowsOnly(dict):
    """An attempted successor-bar read is a test failure, not an unknown value."""
    def __init__(self, values, allowed):
        super().__init__(values)
        self.allowed, self.reads = set(allowed), []

    def get(self, day, default=None):
        self.reads.append(day)
        assert day in self.allowed, f"Future raw unexpectedly inspected: {day}"
        return super().get(day, default)


def published_policy(identity, old):
    return PublishedCloseLeaderTypeRiskExitPolicy(identity,
        candidate_by_day={day: list(rows.values()) for day, rows in old.candidates.items()},
        feature_by_day=old.features, complete_feature_days=old.complete_feature_days)


def test_known_D_structure_queues_exact_calendar_E_without_reading_any_E_bar_and_old_policy_still_defers():
    book, feed, old = fixture("trend_leader", sma20=11.)
    day, following = DAYS[2:4]
    feed.rows = PastRowsOnly({d: rows for d, rows in feed.rows.items() if d <= day}, DAYS[:3])
    result = published_policy("trend_leader", old)(day, book, feed)
    assert isinstance(result, ReplayRiskExitIntent) and result.execution_on == following and result.symbols == (A,)
    assert following not in feed.rows and following not in feed.rows.reads
    assessed = [event for event in book.state["events"] if event["kind"] == "leader_type_risk_assessed"][-1]
    queued = book.state["events"][-1]
    assert assessed["status"] == "invalid"
    assert queued["successor_raw_inspected"] is False and queued["successor_execution_quality_pending"] is True
    assert not [fill for fill in book.state["fills"].values() if fill["side"] == "sell"]
    # The old default remains unchanged and explicitly defers without E raw.
    feed.rows = dict(feed.rows)
    legacy = old(day, book, feed)
    assert isinstance(legacy, SignalUnavailable) and "raw_or_status" in legacy.reason


def test_unknown_structure_cannot_disable_fifth_continuous_session_expiry_after_later_increment():
    book, feed, old = fixture("cross_regime_candidate")
    book.submit("increment", DAYS[4], A, "buy", 100, 1000)
    book.fill("increment:fill", "increment", 100, 1000, DAYS[4], DAYS[5])
    day, following = DAYS[5:7]
    feed.rows = PastRowsOnly({d: rows for d, rows in feed.rows.items() if d <= day}, DAYS[:6])
    assert holding_origin(book.state, A) == DAYS[1]
    result = published_policy("cross_regime_candidate", old)(day, book, feed)
    assert result.symbols == (A,) and result.execution_on == following
    assert following not in feed.rows.reads
    assessed = [event for event in book.state["events"] if event["kind"] == "leader_type_risk_assessed"][-1]
    assert assessed["status"] == "unknown"
    assert "required_fixed_entry_structure_unavailable" in assessed["reasons"]
    assert any(event["kind"] == "leader_type_risk_deferred" and event["independent_expiry_not_suppressed"]
               for event in book.state["events"])
    assert not [fill for fill in book.state["fills"].values() if fill["side"] == "sell"]


def test_missing_current_owned_raw_is_rejected_by_inherited_validation_before_Engine_mutation():
    book, feed, old = fixture("trend_leader", sma20=11.)
    del feed.rows[DAYS[2]][A]
    policy = published_policy("trend_leader", old)
    saved = deepcopy(book.state)
    with pytest.raises(ValueError, match="Unpriceable risk-exit holding"):
        engine.advance(book.state, feed, DAYS[2], CONFIG, 0, context=book.context,
                       replay_risk_exit_policy=policy)
    assert book.state == saved


def test_same_Engine_queues_before_E_exists_then_T1_block_partial_and_remaining_retry(monkeypatch):
    # Only the fixed calendar exists ahead of time. Add each raw day immediately
    # before advancing it; all queue calls are guarded against next-bar reads.
    sessions = ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10", "2025-01-13"]
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat("2025-01-13T20:00:00+08:00"))
    context = RunContext("published_risk_same_Engine", TradingMode.REPLAY)
    state = new_state(CONFIG.initial_cash)
    observed = PastRowsOnly({}, [])
    volumes = pd.DataFrame(float("nan"), index=sessions, columns=[A])
    base = SimpleNamespace(calendar=sessions, rows=observed, volumes=volumes,
        instruments={A: {"ipoDate": "2000-01-01", "outDate": ""}}, actions=[],
        next_day=lambda day: sessions[sessions.index(day) + 1],
        scores=lambda *args: (_ for _ in ()).throw(AssertionError("No old signal fallback")))
    risk = PublishedCloseLeaderTypeRiskExitPolicy("trend_leader",
        candidate_by_day={sessions[0]: [candidate(sessions[0])]},
        feature_by_day={day: {A: feature(day, sma20=11.)} for day in sessions})
    saved_quantities, results = [], []
    for index, day in enumerate(sessions[:5]):
        changes = {}
        if index == 2:
            changes = {"open": 9., "high": 9., "low": 9., "close": 9., "volume": 10_000., "amount": 90_000.}
        elif index == 3:
            changes = {"open": 9.1, "high": 9.3, "low": 9., "close": 9.1, "preclose": 9.}
        elif index == 4:
            changes = {"open": 9.2, "high": 9.4, "low": 9.1, "close": 9.2, "preclose": 9.1}
        observed[day] = {A: raw(**changes)}
        observed.allowed.add(day)
        volumes.at[day, A] = observed[day][A]["volume"]
        metadata = {"context": context, "decided_at": day + "T18:45:00+08:00",
                    "execution_not_before": sessions[index + 1], "source": "saved:synthetic_publication"}
        intent = (PortfolioIntent.full_target({A: .16}, **metadata) if index == 0 else
                  PortfolioIntent.no_change("unknown_current_candidates", **metadata))
        results.append(engine.advance(state, base, day, CONFIG, index, context=context,
            published_close_intent=intent, replay_risk_exit_policy=risk))
        book = Ledger(state, CONFIG, context)
        saved_quantities.append(book.quantity(A))
        if index == 1:
            assert book.quantity(A) > 0 and book.available(A, day) == 0
            assert book.available(A, sessions[2]) == book.quantity(A)
        if index in (1, 2, 3):
            assert state["pending_risk_exits"]["execution_on"] == sessions[index + 1]
            assert sessions[index + 1] not in observed
    bought = saved_quantities[1]
    assert bought > 100 and saved_quantities[2] == bought
    assert saved_quantities[3] == bought - 100 and saved_quantities[4] == 0
    blocks = [event for event in state["events"] if event["kind"] == "order_blocked"]
    assert [(event["day"], event["reason"]) for event in blocks] == [(sessions[2], "limit_down_queue_unknown")]
    executions = [event for event in state["events"] if event["kind"] == "risk_exit_execution"]
    assert [(event["day"], event["remaining_quantities"]) for event in executions] == [
        (sessions[2], {A: bought}), (sessions[3], {A: bought - 100}), (sessions[4], {A: 0})]
    sells = [fill for fill in state["fills"].values() if fill["side"] == "sell"]
    assert sum(fill["qty"] for fill in sells) == bought
    assert {fill["day"] for fill in sells} == {sessions[3], sessions[4]}
    assert state["pending_risk_exits"] is None and state["pending_targets"] is None
    assert "pending_published_close_intent" not in state
    assert all(event.get("successor_raw_inspected") is False for event in state["events"]
               if event["kind"] == "published_risk_reduction_queued_from_current_close")
    saved = deepcopy(state)
    with pytest.raises(ValueError, match="twice or backwards"):
        engine.advance(state, base, sessions[4], CONFIG, 4, context=context, replay_risk_exit_policy=risk)
    assert state == saved
