"""Bounded source repair and complete-prefix checks; no Engine or market data."""
from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research import leader_environment_exit_ablation as m
from trading.research.action_events import reference_concordance
from trading.research.action_reviewed_amendment import patch_reference_rows


DAYS = ["2026-04-10", "2026-04-13", "2026-04-14"]
A, B = "600367.SSE", "603738.SSE"


def context(name):
    return {"schema_version": 1, "contract_type": "RunContext", "account_id": name, "mode": "historical_replay"}


def source_fixture():
    config = m._config(100000, 5).to_dict()
    original = {"inputs": "synthetic_manifest.json", "inputs_sha256": "a" * 64,
        "study_plan": "synthetic_study.json", "study_plan_sha256": "b" * 64,
        "binding": {"start": DAYS[0], "end": DAYS[1], "source_days": DAYS,
            "scope_members": [A, B], "config": config}, "execution_optimization": deepcopy(m.READINESS_OPTIMIZATION)}
    cases = []
    for i, case_id in enumerate(m.CASE_IDS):
        kind, guard, cap = case_id.split("__")
        symbol = A if i < 2 else B
        gap = {"symbol": symbol, "code": "sh." + symbol[:6], "date": DAYS[1], "previous_day": DAYS[0],
            "previous_observed_close": "10", "preclose": "9.9", "has_reported_distribution": True,
            "dividCashPsBeforeTax": ".1", "dividStocksPs": "0", "dividReserveToStockPs": ""}
        cases.append({"case_id": case_id, "type": kind, "guard": guard, "capacity": cap,
            "config": deepcopy(config), "engine_sessions_committed": 1, "daily": [{"date": DAYS[0]}],
            "complete_PnL_available": False, "metrics": None if i < 7 else {"net_return": -.2},
            "account": {"day": DAYS[0], "replay_risk_exit_policy": {"context": context(case_id)}},
            "blocking_evidence": {"day": DAYS[1], "reason": "held_or_pending_raw_or_action_reference_unknown",
                "session_not_committed": True, "raw_issues": [], "reference_evidence": [
                    {"symbol": symbol, "day": DAYS[1], "status": "unresolved_blank_terms", "reference_evidence": gap}]} if i < 7 else None,
            "residual_positions": [symbol], "orders": [], "fills": [], "decisions": []})
    saved = {**deepcopy(original["binding"]), "schema": m.RESULT_SCHEMA, "method_id": m.METHOD_ID,
        "spec": deepcopy(m.SPEC), "parent_fixed_protocol": deepcopy(m.PROTOCOL), "case_count": 48, "cases": cases,
        "study_plan": original["study_plan"], "study_plan_sha256": original["study_plan_sha256"],
        "source": {"manifest": original["inputs"], "manifest_sha256": original["inputs_sha256"]},
        "execution_optimization": deepcopy(m.READINESS_OPTIMIZATION)}
    return original, saved


def test_only_newly_resolved_original_source_subset_is_selected_not_negative_or_unknown_returns():
    original, saved = source_fixture()
    before = deepcopy(saved)
    blocked = m._source_blocked_cases(original, saved)
    actions = pd.DataFrame([{"symbol": symbol, "dividOperateDate": DAYS[1],
        "dividCashPsBeforeTax": ".1", "dividStocksPs": "0", "dividReserveToStockPs": "0" if symbol == A else ""}
        for symbol in (A, B)])
    resolved = m._resolved_source_cases(blocked, actions, {"special": [], "distribution": []})
    binding = m._supplement_binding(original, saved, {"changed_events": [[A, DAYS[1]]]}, resolved)
    assert binding["replayed_case_ids"] == list(m.CASE_IDS[:2])
    assert len(binding["reused_case_ids"]) == 46 and len(binding["unresolved_source_case_ids"]) == 5
    assert resolved[m.CASE_IDS[0]]["references"][0]["status"] == "reference_match"
    assert resolved[m.CASE_IDS[2]]["references"][0]["status"] == "unresolved_blank_terms"
    assert saved == before and saved["cases"][20]["metrics"] == {"net_return": -.2}
    none = {case_id: {**row, "resolved": False} for case_id, row in resolved.items()}
    with pytest.raises(ValueError, match="No original source block"):
        m._supplement_binding(original, saved, {}, none)
    bad = deepcopy(saved)
    bad["config"]["commission"] = .01
    with pytest.raises(ValueError, match="exact original"):
        m._source_blocked_cases(original, bad)


def test_prefix_includes_last_close_pending_order_all_fill_fees_and_risk_events():
    _, saved = source_fixture()
    old = saved["cases"][0]
    old["daily"] = [{"date": DAYS[0], "cash": 99990., "equity": 100000., "fees": 10.,
        "events": [{"kind": "risk_exit_scheduled", "qty": 100, "reason": "fixed_structure",
            "intent": {"context": context("original")}}]}]
    old["decisions"] = [{"day": DAYS[0], "status": "FULL_TARGET", "target": {A: .16}}]
    old["account"]["replay_risk_exit_policy"]["context"] = context("original")
    old["orders"] = [{"id": "filled", "day": DAYS[0], "qty": 100, "limit_price": 1000, "fees": 1000},
        {"id": "pending-at-close", "day": DAYS[0], "qty": 200, "limit_price": 900, "fees": 0, "status": "submitted"}]
    old["fills"] = [{"order_id": "filled", "day": DAYS[0], "qty": 40, "price": 1000, "fees": 500},
        {"order_id": "filled", "day": DAYS[0], "qty": 60, "price": 1000, "fees": 500}]
    new = deepcopy(old)
    new["account"]["replay_risk_exit_policy"]["context"] = context("supplement")
    new["committed_prefix_snapshot"] = deepcopy(new["account"])
    new["daily"][0]["events"][0]["intent"]["context"] = context("supplement")
    new["daily"].append({"date": DAYS[1], "cash": 0.})
    new["decisions"].append({"day": DAYS[1], "status": "NO_CHANGE"})
    new["orders"].append({"id": "new-future", "day": DAYS[1], "qty": 100})
    new["fills"].append({"order_id": "new-future", "day": DAYS[1], "qty": 100, "fees": 5})
    report = m._committed_prefix_check(old, new, DAYS)
    assert report["sessions"] == 1 and report["pending_orders_included"] is True and report["risk_events_included"] is True
    assert report["compared_records"]["orders"]["record_count"] == 2
    assert report["compared_records"]["fills"]["record_count"] == 2
    for changed in ("cash", "pending_order", "fill_fees", "risk_reason"):
        bad = deepcopy(new)
        if changed == "cash": bad["daily"][0]["cash"] += 1
        elif changed == "pending_order": bad["orders"][1]["qty"] += 100
        elif changed == "fill_fees": bad["fills"][0]["fees"] += 1
        else: bad["daily"][0]["events"][0]["reason"] = "different_rule"
        with pytest.raises(ValueError, match="prefix changed"):
            m._committed_prefix_check(old, bad, DAYS)


def test_pair_verification_rejects_unrelated_terms_and_changed_new_review_payload(tmp_path):
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    def pin(path): return sha256(path.read_bytes()).hexdigest()
    actions = pd.DataFrame([{"symbol": symbol, "code": "sh." + symbol[:6], "dividOperateDate": DAYS[1],
        "dividRegistDate": DAYS[0], "dividCashPsBeforeTax": ".1", "dividStocksPs": "0", "dividReserveToStockPs": ""}
        for symbol in (A, B)])
    gaps = pd.DataFrame([{"symbol": symbol, "code": "sh." + symbol[:6], "date": DAYS[1],
        "previous_observed_close": "10", "preclose": "9.9", "has_reported_distribution": True,
        "dividCashPsBeforeTax": ".1", "dividStocksPs": "0", "dividReserveToStockPs": ""} for symbol in (A, B)])
    def write_pair(name, action_rows, parents=None):
        event, ref = tmp_path / (name + "_event"), tmp_path / (name + "_ref")
        event.mkdir(); ref.mkdir()
        action_rows.to_parquet(event / "canonical_actions.parquet", index=False)
        terms = gaps.copy()
        terms["dividReserveToStockPs"] = action_rows.dividReserveToStockPs
        refreshed = reference_concordance(terms, [], [])
        if parents:
            previous = pd.read_parquet(parents[1] / "raw_reference_changes.parquet")
            mask = previous.symbol.eq(A)
            refreshed = patch_reference_rows(previous, mask, refreshed.loc[mask])
        refreshed.to_parquet(ref / "raw_reference_changes.parquet", index=False)
        for file in ("reviewed_special_references.json", "cash_reference_evidence.json", "distribution_reference_evidence.json"):
            save(ref / file, [])
        metadata = {"status": "research_action_events_projected", "execution_admitted": False,
            "start": DAYS[0], "end": DAYS[-1], "canonical_events": 2,
            "files": {"canonical_actions.parquet": pin(event / "canonical_actions.parquet")}}
        amendment = None
        if parents:
            amendment = {"parent_event_view": str(parents[0]), "parent_reference_view": str(parents[1]),
                "events": [[A, DAYS[1]]], "raw_prices_rescanned": False, "other_events_changed": False}
            metadata["reviewed_cash_amendment"] = amendment
            (event / "new_review.txt").write_text("synthetic pinned new review", encoding="utf-8")
            metadata["files"]["new_review.txt"] = pin(event / "new_review.txt")
        save(event / "result.json", metadata)
        (ref / "input_events.json").write_bytes((event / "result.json").read_bytes())
        refmeta = {"status": "research_reference_presence_diagnostic", "execution_admitted": False,
            "event_view": str(event), "files": {p.name: pin(p) for p in ref.iterdir()}}
        if amendment: refmeta["reviewed_cash_amendment"] = deepcopy(amendment)
        save(ref / "result.json", refmeta)
        return event, ref
    old = write_pair("old", actions)
    changed = actions.copy()
    changed.loc[changed.symbol.eq(A), "dividReserveToStockPs"] = "0"
    new = write_pair("new", changed, old)
    original = {"action_event_view": {"directory": str(old[0])}, "action_reference_view": {"directory": str(old[1])}}
    proof, actual, _ = m._checked_supplement_pair(original, *new, {(A, DAYS[1])})
    assert proof["changed_events"] == [[A, DAYS[1]]] and len(proof["chain"]) == 1
    assert actual.loc[actual.symbol.eq(B), "dividReserveToStockPs"].iloc[0] == ""
    (new[0] / "new_review.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        m._checked_supplement_pair(original, *new, {(A, DAYS[1])})
    unrelated = changed.copy()
    unrelated.loc[unrelated.symbol.eq(B), "dividCashPsBeforeTax"] = ".2"
    bad = write_pair("unrelated", unrelated, old)
    with pytest.raises(ValueError, match="selected canonical"):
        m._checked_supplement_pair(original, *bad, {(A, DAYS[1])})


def test_explicit_case_snapshot_captures_last_close_before_later_engine_mutation():
    from trading.research import leader_eight_type_panel_replay as replay
    from trading.research.tests import test_leader_eight_type_panel_replay as fixture

    panels, days = fixture.panels(), fixture.DAYS
    paths, _ = replay._rank_path(panels, {fixture.A, fixture.B}, days[0], days[-1])
    environment = replay._environment(panels, fixture.SESSIONS, max(panel["available_at"] for panel in panels.values()))
    identity = "relay_continuation_leader"
    result = replay._case(fixture.raw(), days, fixture.SESSIONS, fixture.PREVIOUS, panels,
        paths[identity]["uncapped"], environment, {fixture.A, fixture.B}, fixture.qualification(),
        {"day": "2026-10-09", "closes": {fixture.A: 10., fixture.B: 10.}},
        days[0], days[-1], identity, "uncapped", "unguarded", "synthetic_snapshot_hook",
        snapshot_day=days[0])
    snapshot = result["committed_prefix_snapshot"]
    assert snapshot["day"] == days[0] and result["account"]["day"] == days[-1]
    assert snapshot["pending_targets"] == {fixture.A: .16}
    assert snapshot["positions"] == {} and result["account"]["positions"][fixture.A]
    assert snapshot["cash"] > result["account"]["cash"]
    frozen = deepcopy(snapshot)
    result["account"]["pending_targets"] = {fixture.A: 0.}
    result["account"]["positions"][fixture.A][0]["available"] = "2099-01-01"
    assert snapshot == frozen


def test_complete_snapshot_rejects_pending_targets_lot_unlock_price_receivable_or_exit_queue_drift():
    _, saved = source_fixture()
    old = saved["cases"][0]
    old["decisions"] = [{"day": DAYS[0], "status": "FULL_TARGET"}]
    old["account"].update(cash=10000000, positions={A: [{"qty": 100, "available": DAYS[1], "cost": 1000}]},
        orders={}, fills={}, events=[], pending_targets={A: .16}, targets={A: 100}, candidates=[{"symbol": A, "score": 1}],
        prices={A: 1000}, entitlements={A + ":" + DAYS[1]: 100}, receivables={A: 300}, sequence=0,
        pending_risk_exits={"context": context(old["case_id"]), "symbols": [B], "execution_on": DAYS[1]})
    new = deepcopy(old)
    new["account"]["replay_risk_exit_policy"]["context"] = context("new_context")
    snapshot = deepcopy(old["account"])
    snapshot["replay_risk_exit_policy"]["context"] = context("new_context")
    snapshot["pending_risk_exits"]["context"] = context("new_context")
    new["committed_prefix_snapshot"] = snapshot
    new["account"]["day"] = DAYS[-1]  # final account is not used as a substitute for the saved prefix
    report = m._committed_prefix_check(old, new, DAYS)
    assert report["complete_account_snapshot_compared"] is True and report["pending_targets_included"] is True
    assert report["compared_records"]["full_account"]["original_sha256"] == report["compared_records"]["full_account"]["supplement_sha256"]
    for field in ("pending_targets", "lot_available", "prices", "receivables", "exit_queue"):
        bad = deepcopy(new)
        account = bad["committed_prefix_snapshot"]
        if field == "pending_targets": account["pending_targets"][A] = 0.
        elif field == "lot_available": account["positions"][A][0]["available"] = DAYS[-1]
        elif field == "prices": account["prices"][A] += 1
        elif field == "receivables": account["receivables"][A] += 1
        else: account["pending_risk_exits"]["symbols"] = [A]
        with pytest.raises(ValueError, match="full account prefix changed"):
            m._committed_prefix_check(old, bad, DAYS)
    missing = deepcopy(new)
    missing.pop("committed_prefix_snapshot")
    with pytest.raises(ValueError, match="complete supplementary account snapshot"):
        m._committed_prefix_check(old, missing, DAYS)
