from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import leader_selection_control as m
from trading.research.tests.test_leader_eight_type_historical_native import (
    SESSIONS, PREVIOUS, bars, accounting, qualification,
)

A, B, C, E, F = "000001.SZSE", "000002.SZSE", "000003.SZSE", "600001.SSE", "000004.SZSE"


def opportunity(symbols=(A,)):
    return {"opportunity_id": "synthetic-one", "period": "synthetic-period", "decision_day": SESSIONS[1],
        "execution_day": SESSIONS[2], "ordered_candidate_symbols": list(symbols),
        "pre_exit_target_weights": dict.fromkeys(symbols, .16), "references": [{"case_id": "case"}]}


def test_matching_exact_D_known_negative_board_industry_amount_tie_and_no_replacement():
    op = opportunity((A, B))
    day = op["decision_day"]
    symbols = [A, B, C, E, F, "000005.SZSE", "000006.SZSE"]
    panel = {"day": day, "environment": {"source_complete": True}, "types": [{"id": m.TYPE_ID,
        "candidates": [{"symbol": A}, {"symbol": B}], "unresolved_symbols": ["000005.SZSE"],
        "known_exclusions": dict.fromkeys([C, E, F, "000005.SZSE", "000006.SZSE"], "not_matched")}],
        "risk_features": {s: {"date": day, "ipo_feature_support": True} for s in symbols}}
    rows = {s: {"amount": 100., "tradestatus": 1, "isST": 0} for s in symbols}
    industry = {"snapshot_date": SESSIONS[0], "membership": dict.fromkeys(symbols, "same")}
    industry["membership"]["000006.SZSE"] = "different"
    q = SimpleNamespace(assess=lambda *a, **kw: {"engine_block_reason": None, "ipo_window_feature_support": True})
    result = m.match_opportunity(op, panel, rows, symbols, q, industry)
    assert [t["control_symbol"] for t in result["tickets"]] == [C, F]
    assert all(t["future_bar_used_for_matching"] is False for t in result["tickets"])
    missing = deepcopy(industry)
    missing["membership"] = {}
    failed = m.match_opportunity(op, panel, rows, symbols, q, missing)
    assert all(t["control_symbol"] is None and t["match_failure"] for t in failed["tickets"])
    assert len(failed["tickets"]) == 2
    future = {**industry, "snapshot_date": SESSIONS[3]}
    with pytest.raises(ValueError, match="future broad-industry"):
        m.match_opportunity(op, panel, rows, symbols, q, future)


def execution_fixture(*, delayed_exit=False, unknown_control=False, high_price=False):
    days = SESSIONS[:15]
    raw = bars(days, (A, B))
    raw["amount"] = "100000000"
    if unknown_control:
        raw.loc[raw.date.eq(days[4]) & raw.code.eq("sz.000002"), "preclose"] = ""
    if high_price:
        for field, value in (("open", "10000"), ("high", "10001"), ("low", "9999"), ("close", "10000"), ("preclose", "10000")):
            raw.loc[raw.code.eq("sz.000002"), field] = value
    if delayed_exit:
        current = raw.date.eq(days[7]) & raw.code.eq("sz.000001")
        raw.loc[current, ["open", "high", "low", "close"]] = "9"
        later = raw.date.gt(days[7]) & raw.code.eq("sz.000001")
        raw.loc[later, ["open", "close", "preclose"]] = "9"
        raw.loc[later, "high"] = "9.1"
        raw.loc[later, "low"] = "8.9"
    base = m.ExactSourceRawFeed(raw, days, [A, B], context_anchor={"day": PREVIOUS[days[0]], "closes": {A: 10., B: 10.}})
    factory = m.HistoricalExecutionFactory(accounting((A, B), days, days))
    return factory(base, days), qualification((A, B)), opportunity()


def test_real_shared_engine_exact16k_budget_T1_five_session_exit_and_slot_local_unknown():
    execution, q, op = execution_fixture(unknown_control=True)
    leader = m._run_arm(execution, q, A, op, "selection-control-synthetic:leader")
    control = m._run_arm(execution, q, B, op, "selection-control-synthetic:control")
    assert leader["complete_PnL_available"] is True
    assert leader["config"]["initial_cash"] == 20000
    assert leader["config"]["gross_limit"] == leader["config"]["single_limit"] == .8
    targets = [e for e in leader["account"]["events"] if e["kind"] == "targets_generated"]
    assert targets[0]["targets"] == {A: .8}
    assert leader["entry_fill_day"] == SESSIONS[2]
    assert leader["expiry_decision_day"] == SESSIONS[6]
    assert leader["last_sell_fill_day"] == SESSIONS[7]
    assert leader["overdue_after_five_holding_sessions"] is False
    fills = list(leader["account"]["fills"].values())
    assert all(f["available_day"] > f["day"] for f in fills if f["side"] == "buy")
    assert control["status"] == "source_blocked" and control["metrics"] is None
    assert control["last_committed_day"] == SESSIONS[3]
    assert control["residual_quantities"] and control["blocking_evidence"]["session_not_committed"] is True
    assert leader["context"] != control["context"]


def test_blocked_expiry_remains_actual_overdue_and_minimum_lot_no_fill_is_null():
    execution, q, op = execution_fixture(delayed_exit=True, high_price=True)
    leader = m._run_arm(execution, q, A, op, "selection-control-synthetic:delay")
    control = m._run_arm(execution, q, B, op, "selection-control-synthetic:expensive")
    assert leader["complete_PnL_available"] is True
    assert leader["last_sell_fill_day"] > SESSIONS[7]
    assert leader["overdue_after_five_holding_sessions"] is True
    assert leader["order_blocked_events"]
    assert control["status"] == "no_fill" and control["metrics"] is None and control["net_PnL_cny"] is None
    pair = m._pair({"match_status": "matched"}, leader, control)
    assert pair["complete_pair"] is False and pair["net_PnL_difference_cny"] is None


def test_aggregate_counts_unique_opportunities_dates_and_keeps_incomplete_denominator():
    rows = [{"period": "p", "decision_day": "2026-01-05", "references": [1, 2, 3, 4],
        "complete_basket": True, "net_return_difference": .01,
        "pairs": [{"match_status": "matched", "complete_pair": True, "net_PnL_difference_cny": 100.}]},
        {"period": "p", "decision_day": "2026-01-05", "references": [1, 2],
        "complete_basket": False, "net_return_difference": None,
        "pairs": [{"match_status": "unmatched", "complete_pair": False, "net_PnL_difference_cny": None}]}]
    result = m._aggregate(rows, "p")
    assert result["unique_decision_days"] == 1 and result["unique_opportunities"] == 2
    assert result["original_case_D_references"] == 6
    assert result["unique_ticket_pairs"] == 2 and result["incomplete_pairs"] == 1
    assert result["mean_unique_complete_basket_return_difference"] == .01
    assert result["independent_samples_claimed"] is False


def test_original_full_target_inventory_is_bijective_before_any_matching_or_engine(tmp_path):
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        return {"file": str(path), "sha256": m.digest(path)}
    op = opportunity()
    op["references"] = []
    sources = []
    for index, case_id in enumerate(m.CASE_IDS):
        file = tmp_path / (str(index) + ".json")
        proof = write(file, [{"day": op["decision_day"], "execution_day": op["execution_day"],
            "status": "FULL_TARGET", "cap_candidates": [{"symbol": A, "score": 1.}]}])
        sources.append({"path": proof["file"], "sha256": proof["sha256"]})
        op["references"].append({"period": "synthetic-period", "case_id": case_id,
            "decisions_file": str(file), "decision_day": op["decision_day"]})
    op.pop("period")
    inventory_file = tmp_path / "inventory.json"
    inventory = {"opportunities": [op]}
    dummy = write(tmp_path / "source.json", {})
    reg = {"schema": m.REG_SCHEMA, "method_id": m.METHOD_ID, "registered_at": "2026-01-01T00:00:00+08:00",
        "inventory": write(inventory_file, inventory), "decision_sources": sources,
        "expected_counts": {"case_D_references": 4, "candidate_slot_references": 4,
            "unique_opportunities": 1, "unique_ticket_pairs": 1, "unique_decision_days": 1},
        "periods": [{"inventory_period": "synthetic-period", **{field: dummy for field in
            ("input", "prior_method_registration", "calendar", "industry_receipt", "action_event", "action_reference")}}],
        "protocol": {"parent_budget_cny": 100000, "slots": 5, "slot_wallet_cny": 20000,
            "original_slot_weight": .16, "slot_target_weight": .8, "slot_target_cny": 16000,
            "type_specific_structural_exit": False, "same_source_scope": True,
            "signal_candidate_pool_identical": False, "no_original_accounts_replayed": True,
            "no_new_source": True, "no_tuning": True}}
    registration = tmp_path / "registration.json"
    write(registration, reg)
    _, opportunities, counts = m.load_registration(registration)
    assert len(opportunities) == 1 and counts["case_D_references"] == 4
    # Counts alone would accept one duplicated stratum and one omitted stratum.
    inventory["opportunities"][0]["references"][1] = deepcopy(op["references"][0])
    reg["inventory"] = write(inventory_file, inventory)
    write(registration, reg)
    with pytest.raises(ValueError, match="complete original eight-case opportunity"):
        m.load_registration(registration)


def test_seven_fraction_clock_keeps_original_and_normalizes_industry_comparison_only(tmp_path, monkeypatch):
    clock = "2026-10-18T20:39:12.3512176+08:00"
    parsed = m._stamp(clock)
    assert parsed.microsecond == 351217 and parsed.utcoffset().total_seconds() == 8 * 3600
    with pytest.raises(ValueError, match="explicit timezone"):
        m._stamp("2026-10-18T20:39:12.351217")
    registration = tmp_path / "registration.json"
    registration.write_text(json.dumps({"registered_at": clock}), encoding="utf-8")
    before = registration.read_bytes()
    reg = {"registered_at": clock, "periods": [{"input": {"file": "synthetic-input"},
        "industry_root": "synthetic-industry"}]}
    monkeypatch.setattr(m, "load_registration", lambda path: (reg, [], {}))
    monkeypatch.setattr(m, "load_inputs", lambda path: ({}, {}))
    class StopBeforeMatching(Exception):
        pass
    def industry(root, observed_at):
        assert root == "synthetic-industry"
        assert observed_at == "2026-10-18T20:39:12.351217+08:00"
        # The existing industry parser remains unchanged and accepts this view.
        assert datetime.fromisoformat(observed_at) == parsed
        raise StopBeforeMatching
    monkeypatch.setattr(m, "load_industry", industry)
    monkeypatch.setattr(m, "advance", lambda *a, **kw: pytest.fail("Clock regression must not reach Engine"))
    with pytest.raises(StopBeforeMatching):
        m.run(registration, tmp_path / "synthetic-out")
    assert registration.read_bytes() == before and reg["registered_at"] == clock
