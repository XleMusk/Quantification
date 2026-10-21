"""Synthetic reduction mechanics, not research-performance evidence."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.engine import ReplayRiskExitIntent, advance
from trading.ledger import Ledger, new_state
from trading.research.leader_listing_features import METHOD_ID
from trading.research.leader_type_risk_exit import LeaderTypeRiskExitPolicy, surviving_buy_anchor
from trading.research.shortline_decision_v2 import BATCH_SCHEMA, ShortlineDecisionFeedV2, fixed_exit_policy
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config


A, B = "600001.SSE", "600002.SSE"
DAYS = pd.bdate_range("2026-10-12", periods=10).strftime("%Y-%m-%d").tolist()
CONFIG = _config(100000, 5)


def raw(**changes):
    return {"open": 10., "high": 10.2, "low": 9.8, "close": 10., "preclose": 10.,
        "volume": 1_000_000., "amount": 10_000_000., "tradestatus": 1, "isST": 0,
        "adjustflag": "3", **changes}


def feature(day, **changes):
    return {"date": day, "symbol": A, "board": "main", "limit_up": True, "stage": 2,
        "stage_reference_known": True, "listing_qualification_status": "supported",
        "listing_feature_method_id": METHOD_ID, "feature_decision_as_of": day + "T15:05:00+08:00",
        "sma20": 9.5, "sma60": 9., "amount_rank_pct": .995, **changes}


def candidate(day, **changes):
    return {"symbol": A, "date": day, "source_day": day, "board": "main",
        "setup_low": 9.8, "structure_high": 10.2, "evidence": {}, **changes}


def fixture(identity, **current):
    state = new_state(CONFIG.initial_cash)
    book = Ledger(state, CONFIG, RunContext("synthetic_structural", TradingMode.REPLAY))
    book.submit("buy1", DAYS[1], A, "buy", 100, 1000)
    book.fill("fill1", "buy1", 100, 1000, DAYS[1], DAYS[2])
    feed = SimpleNamespace(calendar=DAYS, rows={day: {A: raw(), B: raw()} for day in DAYS})
    candidates = {DAYS[0]: [candidate(DAYS[0])]}
    features = {day: {A: feature(day, **current)} for day in DAYS}
    policy = LeaderTypeRiskExitPolicy(identity, candidate_by_day=candidates,
        feature_by_day=features, complete_feature_days=DAYS)
    return book, feed, policy


@pytest.mark.parametrize("identity,changes,evidence", [
    ("space_height_leader", {"limit_up": False, "stage": 0}, {}),
    ("relay_continuation_leader", {"limit_up": False, "stage": 0}, {}),
    ("trend_leader", {"sma20": 10.1, "sma60": 9.}, {}),
    ("liquidity_capacity_proxy", {"amount_rank_pct": .989}, {}),
    ("rebound_repair_candidate", {}, {"previous_open": 10.1}),
    ("cross_regime_candidate", {}, {"structure_close": 10.1}),
    ("sector_catchup_leader", {}, {}),
    ("turnover_leader", {}, {}),
])
def test_each_fixed_structure_schedules_only_same_engine_next_session(identity, changes, evidence):
    book, feed, policy = fixture(identity, **changes)
    policy.candidates[DAYS[0]][A]["evidence"] = evidence
    if identity == "rebound_repair_candidate":
        # The first decision in this tiny pinned calendar lacks D-1. The
        # original candidate's proven previous_open is still a fixed anchor.
        assert feed.calendar[0] == DAYS[0]
    if identity in {"sector_catchup_leader", "turnover_leader"}:
        feed.rows[DAYS[2]][A] = raw(open=9.7, close=9.7, low=9.6)
    result = policy(DAYS[2], book, feed)
    assert isinstance(result, ReplayRiskExitIntent)
    assert result.execution_on == DAYS[3] and result.symbols == (A,)
    assert not [fill for fill in book.state["fills"].values() if fill["side"] == "sell"]
    evaluated = [item for item in book.state["events"] if item["kind"] == "leader_type_risk_assessed"][-1]
    assert evaluated["status"] == "invalid" and evaluated["entry"]["fill_id"] == "fill1"
    assert evaluated["entry"]["decision_day"] == DAYS[0]


def test_unknown_structure_never_falls_back_to_setup_low_and_does_not_disable_original_expiry():
    book, feed, policy = fixture("cross_regime_candidate")
    # A completely different current candidate cannot refresh the actual entry.
    policy.candidates[DAYS[2]] = {A: candidate(DAYS[2], evidence={"structure_close": 20.})}
    assert policy(DAYS[2], book, feed) is None
    assert book.state["events"][-1]["kind"] == "leader_type_risk_deferred"
    expiry = policy(DAYS[5], book, feed)
    assert expiry.symbols == (A,) and expiry.execution_on == DAYS[6]
    assert [event for event in book.state["events"] if event["kind"] == "leader_type_risk_assessed"][-1]["status"] == "unknown"


def test_missing_entry_candidate_defers_even_when_current_type_is_known_invalid():
    book, feed, policy = fixture("trend_leader", sma20=11.)
    policy.candidates = {DAYS[2]: {A: candidate(DAYS[2])}}
    assert policy(DAYS[2], book, feed) is None
    assert book.state["events"][-1]["reasons"] == ["actual_buy_entry_candidate_evidence_unavailable"]


def test_actual_fifo_earliest_surviving_fill_changes_anchor_only_after_older_lot_is_sold():
    book, feed, policy = fixture("cross_regime_candidate")
    policy.candidates[DAYS[0]][A]["evidence"] = {"structure_close": 11.}
    policy.candidates[DAYS[1]] = {A: candidate(DAYS[1], evidence={"structure_close": 9.})}
    book.submit("buy2", DAYS[2], A, "buy", 100, 1000)
    book.fill("fill2", "buy2", 100, 1000, DAYS[2], DAYS[3])
    before = policy.evaluate(DAYS[2], book, feed, A)
    assert before["status"] == "invalid" and before["entry"]["fill_id"] == "fill1"
    book.submit("sell1", DAYS[3], A, "sell", 100, 1000)
    book.fill("sold1", "sell1", 100, 1000, DAYS[3], DAYS[4])
    after = policy.evaluate(DAYS[3], book, feed, A)
    assert after["status"] == "not_invalid"
    assert after["entry"]["fill_id"] == "fill2" and after["entry"]["decision_day"] == DAYS[1]
    assert after["facts"]["structure_price"] == 9.


def test_space_only_known_same_board_higher_peer_invalidates_unknown_peers_do_not_sell():
    book, feed, policy = fixture("space_height_leader")
    policy.features[DAYS[2]][B] = feature(DAYS[2], symbol=B, stage=3, board="chinext")
    assert policy.evaluate(DAYS[2], book, feed, A)["status"] == "not_invalid"
    policy.features[DAYS[2]][B]["board"] = "main"
    assert policy.evaluate(DAYS[2], book, feed, A)["status"] == "invalid"
    policy.features[DAYS[2]][B]["stage"] = None
    assert policy(DAYS[2], book, feed) is None
    assert book.state["events"][-1]["reasons"] == ["same_board_peer_height_unknown"]


def test_IPO_unknown_false_is_not_qualified_break_and_incomplete_trend_is_unknown():
    book, feed, policy = fixture("relay_continuation_leader", limit_up=False,
        listing_qualification_status="unknown")
    assert policy(DAYS[2], book, feed) is None
    assert book.state["events"][-1]["reasons"] == ["current_qualified_limit_unknown"]
    book, feed, policy = fixture("trend_leader", sma20=11., sma60=None)
    assert policy(DAYS[2], book, feed) is None


def test_empty_current_candidate_cannot_reanchor_fixed_entry_and_inputs_are_copied():
    book, feed, _ = fixture("cross_regime_candidate")
    candidates = {DAYS[0]: [candidate(DAYS[0], evidence={"structure_close": 9.})], DAYS[2]: []}
    policy = LeaderTypeRiskExitPolicy("cross_regime_candidate", candidate_by_day=candidates, feature_by_day={})
    candidates[DAYS[0]][0]["evidence"]["structure_close"] = 20.
    assert policy.evaluate(DAYS[2], book, feed, A)["facts"]["structure_price"] == 9.
    assert policy(DAYS[2], book, feed) is None


def test_missing_next_raw_defers_known_structural_exit_without_fabricated_order():
    book, feed, policy = fixture("trend_leader", sma20=11.)
    del feed.rows[DAYS[3]][A]
    result = policy(DAYS[2], book, feed)
    assert isinstance(result, SignalUnavailable) and "raw_or_status" in result.reason
    assert not any(fill["side"] == "sell" for fill in book.state["fills"].values())


def test_rebound_uses_exact_decision_previous_open_not_current_previous_open():
    book, feed, policy = fixture("rebound_repair_candidate")
    # Entry decision is session 1, actual buy is session 2.
    book.state["fills"]["fill1"]["day"] = DAYS[2]
    book.state["fills"]["fill1"]["available_day"] = DAYS[3]
    book.state["positions"][A][0]["available"] = DAYS[3]
    policy.candidates = {DAYS[1]: {A: candidate(DAYS[1])}}
    feed.rows[DAYS[0]][A] = raw(open=10.1)
    feed.rows[DAYS[2]][A] = raw(open=9.)
    evaluated = policy.evaluate(DAYS[3], book, feed, A)
    assert evaluated["status"] == "invalid" and evaluated["facts"]["structure_price"] == 10.1
    assert evaluated["facts"]["entry_reference_day"] == DAYS[0]


def test_shared_Engine_executes_structural_exit_on_next_open_with_T1_and_duplicate_rejection():
    bars = pd.DataFrame([{ "date": day, "symbol": A, "open": 10., "high": 10.2, "low": 9.8,
        "close": 10., "preclose_for_limits": 10., "volume": 10_000_000., "amount": 100_000_000.,
        "is_st": False} for day in DAYS])
    base = ShortlinePanelFeed(bars, {}, [A])
    base.instruments = {A: {"ipoDate": "2000-01-01", "outDate": ""}}
    batches = {DAYS[0]: {"schema": BATCH_SCHEMA, "day": DAYS[0], "status": "complete",
        "source": "synthetic_only", "candidates": [{"symbol": A, "score": 1.}]}}
    state = new_state(CONFIG.initial_cash)
    feed = ShortlineDecisionFeedV2(base, batches)
    target = fixed_exit_policy(feed, state, CONFIG)
    policy = LeaderTypeRiskExitPolicy("trend_leader", candidate_by_day={DAYS[0]: [candidate(DAYS[0])]},
        feature_by_day={DAYS[1]: {A: feature(DAYS[1], sma20=11.)}})
    for index in range(3):
        advance(state, feed, DAYS[index], CONFIG, index, target_policy=target, replay_risk_exit_policy=policy)
    buys = [fill for fill in state["fills"].values() if fill["side"] == "buy"]
    sells = [fill for fill in state["fills"].values() if fill["side"] == "sell"]
    assert {fill["day"] for fill in buys} == {DAYS[1]}
    assert {fill["day"] for fill in sells} == {DAYS[2]}
    assert sum(fill["qty"] for fill in buys) == sum(fill["qty"] for fill in sells)
    assert all(fill["available_day"] == DAYS[2] for fill in buys)
    assert not Ledger(state, CONFIG).quantity(A) and state["cash"] < 10_000_000
    saved = deepcopy(state)
    with pytest.raises(ValueError, match="twice or backwards"):
        advance(state, feed, DAYS[2], CONFIG, 2, target_policy=target, replay_risk_exit_policy=policy)
    assert state == saved


def test_fill_reconciliation_and_entry_setup_conflict_defer_instead_of_inventing_anchor():
    book, feed, policy = fixture("turnover_leader")
    policy.candidates[DAYS[0]][A]["setup_low"] = 1.
    assert policy.evaluate(DAYS[2], book, feed, A)["reasons"] == ["saved_entry_setup_disagrees_with_exact_raw"]
    book.state["positions"][A][0]["qty"] = 200
    assert surviving_buy_anchor(book.state, A, DAYS, DAYS[2])[1] == "actual_fill_position_reconciliation_unknown"
