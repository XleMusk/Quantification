"""Fixed confirmation logic and a small real shared-Engine lifecycle."""
from copy import deepcopy

import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.research import leader_reported_turnover_confirmation as m
from trading.research.shortline_decision_v2 import BATCH_SCHEMA

A, B, C = "600001.SSE", "600002.SSE", "600003.SSE"


def fixture(days=None, symbols=(A, B)):
    days = days or ["2026-01-05", "2026-01-06", "2026-01-07"]
    batches = {d: {"schema": BATCH_SCHEMA, "day": d, "source": "synthetic_saved_rank",
        "status": "complete", "candidates": [{"symbol": s, "score": 10-i} for i, s in enumerate(symbols)]} for d in days}
    raw = pd.DataFrame([{"date": d, "symbol": s, "open": 10., "high": 10.1, "low": 9.9,
        "close": 10., "preclose_for_limits": 10., "volume": 10000000., "amount": 100000000.,
        "pct_chg": 0., "is_st": False, "source_row_valid": True, "is_st_unknown": False}
        for d in ["2024-01-02", *days] for s in symbols])
    turn = {"rows": pd.DataFrame([{"date": d, "symbol": s, "turn_percent": float(i+1),
        "tradestatus": "1", "isST": "0"} for i,d in enumerate(days) for s in symbols])}
    return days, batches, raw, turn


def test_exact_previous_missing_overrides_known_false_and_never_cashes_unknown():
    days, batches, raw, turn = fixture()
    # Even a known one-price day does not turn a missing previous turn into a
    # known rejection; whole original selected set remains NO_CHANGE.
    raw.loc[raw.date.eq(days[0]), ["high", "low"]] = 10.
    turn["rows"].loc[turn["rows"].date.eq(days[1]) & turn["rows"].symbol.eq(B), "turn_percent"] = None
    original = deepcopy(batches)
    modified, audit = m.confirm_batches(batches, raw, turn, sessions=days, symbols=[A,B], family="synthetic")
    assert batches == original
    assert modified[days[0]]["status"] == modified[days[1]]["status"] == "incomplete"
    assert modified[days[0]]["candidates"] == modified[days[1]]["candidates"] == []
    assert audit[0]["previous_exact_session"] is None and audit[0]["condition_status"] == "unknown"
    assert audit[0]["current_turn_observation"]["reported_turn_percent"] == 1.
    assert not any(r["retained_in_effective_complete_batch"] for r in audit if r["day"] in days[:2])


def test_known_false_complete_empty_rank_preservation_and_no_refill():
    days, batches, raw, turn = fixture(symbols=(A,B,C))
    turn["rows"].loc[turn["rows"].date.eq(days[1]) & turn["rows"].symbol.isin([A,C]), "turn_percent"] = .5
    # Source ST/status are observations, not a new eligibility filter.
    turn["rows"].loc[turn["rows"].date.eq(days[1]) & turn["rows"].symbol.eq(B), "isST"] = "1"
    modified, audit = m.confirm_batches(batches, raw, turn, sessions=days, symbols=[A,B,C], family="synthetic")
    assert modified[days[1]]["candidates"] == [batches[days[1]]["candidates"][1]]
    assert [r["symbol"] for r in audit if r["day"] == days[1] and r["retained_in_effective_complete_batch"]] == [B]
    assert [r["original_list_position"] for r in audit if r["day"] == days[1]] == [1,2,3]
    raw.loc[raw.date.eq(days[1]) & raw.symbol.eq(B), ["high", "low"]] = 10.
    modified, _ = m.confirm_batches(batches, raw, turn, sessions=days, symbols=[A,B,C], family="synthetic")
    assert modified[days[1]]["status"] == "complete" and modified[days[1]]["candidates"] == []
    batches[days[1]]["status"] = "incomplete"
    original = deepcopy(batches[days[1]])
    modified, audit = m.confirm_batches(batches, raw, turn, sessions=days, symbols=[A,B,C], family="synthetic")
    assert modified[days[1]] == original
    assert not any(r["selected_for_confirmation"] for r in audit if r["day"] == days[1])


def test_complete_over_cap_rejected_rather_than_topup_or_unscoped_selection():
    days, batches, raw, turn = fixture()
    batches[days[1]]["candidates"] *= 3
    with pytest.raises(ValueError, match="at-most-five"):
        m.confirm_batches(batches, raw, turn, sessions=days, symbols=[A,B], family="synthetic")


def test_real_engine_unknown_holds_until_same_independent_expiry_and_unique_context():
    days = pd.bdate_range("2026-01-05", periods=9).strftime("%Y-%m-%d").tolist()
    days, batches, raw, turn = fixture(days=days, symbols=(A,))
    # First known comparison Jan6 creates the original target; later missing
    # reported turn stays NO_CHANGE, while the original risk hook still exits.
    turn["rows"].loc[turn["rows"].date.gt(days[1]), "turn_percent"] = None
    modified, _ = m.confirm_batches(batches, raw, turn, sessions=days, symbols=[A], family="synthetic")
    base = m.CommonCohortRawFeed(raw, days, [A])
    context = RunContext("synthetic-turnover-confirmation:registered-run:family", TradingMode.REPLAY)
    summary, evidence = m._case(base, modified, "synthetic", m.POOLS[0], m.EXIT_POLICIES[0], days[0], days[-1],
        replay_risk_exit_policy=m.FiveSessionExpiryPolicy(), context=context)
    assert summary["error"] is None
    fills = list(evidence["state"]["fills"].values())
    assert {f["day"] for f in fills if f["side"] == "buy"} == {days[2]}
    assert {f["day"] for f in fills if f["side"] == "sell"} == {days[7]}
    assert not any(evidence["state"]["positions"].values())
    assert evidence["state"]["replay_risk_exit_policy"]["context"] == context.to_dict()
    assert all(evidence["decisions"][i]["no_change"] for i in range(2, len(days)))


def test_zero_fill_or_incomplete_metrics_are_null_not_zero_strategy_returns():
    summary = {"metrics": {"net_return": 0.}, "tail_included_diagnostic_metrics": {"net_return": 0.},
        "fills": 0, "error": None, "status": "completed_already_inspected_engine_diagnostic"}
    evidence = {"daily": [{"date": "2026-01-05"}], "state": {"pending_targets": None}}
    value = m._normalize_summary(summary, evidence, ["2026-01-05"])
    assert value["metrics"] is value["tail_included_diagnostic_metrics"] is None
    assert value["status"] == "unavailable_no_simulated_fills"
    summary["fills"] = 1
    value = m._normalize_summary(summary, evidence, ["2026-01-05", "2026-01-06"])
    assert value["metrics"] is value["tail_included_diagnostic_metrics"] is None
    summary["fills"] = 0
    original = deepcopy(summary)
    baseline = m._baseline_summary(summary)
    assert baseline["metrics"] is baseline["tail_included_diagnostic_metrics"] is None
    assert baseline["status"] == "unavailable_no_simulated_fills" and summary == original
