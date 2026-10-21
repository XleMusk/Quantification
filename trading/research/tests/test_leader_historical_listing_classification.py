"""Tiny posthoc-listing fixtures; no native inventory, Engine or provider call."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import leader_historical_listing_classification as classification
from trading.research.baostock_daily_slice import digest
from trading.research.leader_eight_type_panel_replay import TYPE_IDS
from trading.research.leader_historical_identity_inputs import _view
from trading.research.leader_historical_identity_rules import HistoricalIdentityRules
from trading.research.tests.test_leader_historical_identity_rules import A, B, C, DAYS, CLOCK, industry


OUTSIDE = "600999.SSE"
SCOPE = [A, B, C]
MISSING = "fixed_member_missing_from_current_own_roster"


def qualification(*, out_day=DAYS[1]):
    return SimpleNamespace(records={A: {"available_at": "2026-10-08T10:00:00+08:00"},
        B: {"reported_out_date": out_day, "available_at": "2026-10-08T10:01:00+08:00"},
        C: {"reported_out_date": DAYS[0], "available_at": "2026-10-08T10:02:00+08:00"}},
        verification={"method_id": classification.LISTING_METHOD,
            "posthoc_observed_at": "2026-10-17T15:00:00+08:00",
            "original_availability_clocks_backdated": False})


def raw(day, *, active_b=False, c_status="0"):
    rows = []
    for symbol, status in [(A, "1"), *(([(B, "1")] if active_b else [])), (C, c_status)]:
        rows.append({"date": day, "symbol": symbol, "code": ("sh." if symbol.endswith("SSE") else "sz.") + symbol[:6],
            "tradestatus": status, "isST": "0", "open": "10", "high": "10.2", "low": "9.8",
            "close": "10", "preclose": "10", "amount": "50000000", "pctChg": "0"})
    return pd.DataFrame(rows)


def panel(day, *, candidates=(), unresolved=(B,)):
    return {"day": day, "available_at": CLOCK,
        "environment": {"source_complete": True, "breadth": .5, "median_return_pct": 1.},
        "risk_features": {A: {"date": day, "close": 10., "ipo_feature_support": True,
            "qualified_stage": 3, "qualified_limit_up": True}},
        "types": [{"id": kind, "status": "unknown", "candidates": deepcopy(list(candidates)),
            "unresolved_symbols": list(unresolved), "scope_complete": False,
            "missing_evidence": {MISSING: len(unresolved), "stage_reference_chain_unknown": 7},
            "saved_stage_audit": {"stage": 3, "known_at": CLOCK}}
            for kind in TYPE_IDS]}


def test_exact_out_date_missing_only_preserves_other_unknowns_status_and_reason_counts():
    evidence = qualification()
    before = classification.classify_day(DAYS[0], raw(DAYS[0]), SCOPE, evidence)
    assert before["known_exclusions"] == before["conflicts"] == {}
    at_out = classification.classify_day(DAYS[1], raw(DAYS[1]), SCOPE, evidence)
    assert set(at_out["known_exclusions"]) == {B} and at_out["conflicts"] == {}
    assert C not in at_out["known_exclusions"]  # an actual halted row is retained, never a missing-row exclusion
    assert at_out["known_exclusions"][B]["original_record_available_at"] == evidence.records[B]["available_at"]
    assert at_out["known_exclusions"][B]["historical_PIT_knowledge_claimed"] is False
    candidates = [{"symbol": A, "source_day": DAYS[1], "stage": 3, "score": 100.}]
    saved = panel(DAYS[1], candidates=candidates, unresolved=(B, C, OUTSIDE))
    saved["types"][-1].update(candidates=[], unresolved_symbols=[B, OUTSIDE], missing_evidence={MISSING: 1, "other_unknown": 9})
    original = deepcopy(saved)
    changed = classification.apply_day(saved, at_out, available_at=CLOCK, scope_members=SCOPE)
    assert saved == original and changed["risk_features"] == original["risk_features"]
    for item in changed["types"][:3]:
        assert item["candidates"] == candidates and item["saved_stage_audit"] == original["types"][0]["saved_stage_audit"]
        assert item["unresolved_symbols"] == sorted([C, OUTSIDE])
        assert item["missing_evidence"] == {MISSING: 2, "stage_reference_chain_unknown": 7}
        assert item["scope_complete"] is False and item["status"] == "matched"
    outside = changed["types"][-1]
    assert outside["unresolved_symbols"] == [OUTSIDE] and outside["scope_complete"] is True
    assert outside["status"] == "unknown" and outside["missing_evidence"] == {"other_unknown": 9}
    assert outside["known_exclusions"][B] == classification.EXCLUSION

    # No newly classified symbol means no reinterpretation of the saved status/reasons.
    untouched = panel(DAYS[0], candidates=[{"symbol": A, "score": 1.}], unresolved=(B,))
    same = classification.apply_day(untouched, before, available_at=CLOCK, scope_members=SCOPE)
    assert [item["status"] for item in same["types"]] == ["unknown"] * 8
    assert all(item["missing_evidence"] == untouched["types"][i]["missing_evidence"]
        and "historical_listing_membership_changed" not in item for i, item in enumerate(same["types"]))


def test_active_raw_conflict_retains_candidate_and_adds_explicit_unknown():
    evidence = qualification(out_day=DAYS[0])
    bars = raw(DAYS[1], active_b=True)
    found = classification.classify_day(DAYS[1], bars, SCOPE, evidence)
    assert found["known_exclusions"] == {} and set(found["conflicts"]) == {B}
    candidates = [{"symbol": B, "source_day": DAYS[1], "score": 50., "stage": 2}]
    saved = panel(DAYS[1], candidates=candidates, unresolved=())
    for item in saved["types"]:
        item.update(status="matched", scope_complete=True)
    original = deepcopy(saved)
    changed = classification.apply_day(saved, found, available_at=CLOCK, scope_members=SCOPE)
    assert saved == original and bars.equals(raw(DAYS[1], active_b=True))
    for item in changed["types"]:
        assert item["candidates"] == candidates and item["status"] == "matched"
        assert item["unresolved_symbols"] == [B] and item["scope_complete"] is False
        assert item["historical_listing_active_raw_conflicts"] == [B]
        assert B not in item["known_exclusions"]
        assert item["missing_evidence"] == original["types"][0]["missing_evidence"]


def test_verified_view_identity_inherits_known_exclusion_and_rejects_maps_or_sidecar_drift(tmp_path):
    day, evidence = DAYS[0], qualification(out_day=DAYS[0])
    bars, saved = raw(day), panel(day)
    found = classification.classify_day(day, bars, SCOPE, evidence)
    classified = classification.apply_day(saved, found, available_at=CLOCK, scope_members=SCOPE)
    hook = classification.verify_day(found, day, bars, SCOPE, evidence)
    verified_view = _view(day, classified, bars, evidence, historical_listing_classification=hook)
    result = HistoricalIdentityRules(SCOPE, DAYS, industry, CLOCK).observe(day, classified, verified_view)
    for item in result["types"]:
        assert item["known_exclusions"][B] == classification.EXCLUSION and B not in item["unresolved_symbols"]
    legacy_view = _view(day, classified, bars, evidence)
    legacy = HistoricalIdentityRules(SCOPE, DAYS, industry, CLOCK).observe(day, classified, legacy_view)
    assert classification.DESCRIPTOR not in legacy_view
    assert all(B in item["unresolved_symbols"] and B not in item["known_exclusions"] for item in legacy["types"])
    with pytest.raises(ValueError, match="verified listing sidecar"):
        _view(day, classified, bars, evidence, historical_listing_classification=found)
    bad_view = {**legacy_view, classification.DESCRIPTOR: found}
    with pytest.raises(ValueError, match="verified own-day frozen scope"):
        HistoricalIdentityRules(SCOPE, DAYS, industry, CLOCK).observe(day, classified, bad_view)
    wrong = deepcopy(found)
    wrong["known_exclusions"].pop(B)
    with pytest.raises(ValueError, match="differs from exact"):
        classification.verify_day(wrong, day, bars, SCOPE, evidence)

    # The small sidecar keeps its parent/source/clock pins, including after the two identity types are derived.
    parent_file, sidecar_file = tmp_path / "parent.json", tmp_path / "sidecar.json"
    parent = {"created_at": "2026-10-17T15:00:00+08:00", "scope_members": SCOPE,
        "source_days": [day], "panels_file": "original_panels.json", "panels_sha256": "a" * 64}
    parent_file.write_text(json.dumps(parent), encoding="utf-8")
    sidecar = {"schema": classification.SCHEMA, "method_id": classification.METHOD_ID,
        "created_at": "2026-10-17T15:30:00+08:00", "parent_manifest": str(parent_file),
        "parent_manifest_sha256": digest(parent_file), "scope_members": SCOPE, "source_days": [day],
        "qualification_verification": evidence.verification, "days": {day: found}, **classification.FLAGS}
    sidecar_file.write_text(json.dumps(sidecar), encoding="utf-8")
    manifest = {**deepcopy(parent), "created_at": CLOCK, "panels_file": "classified_panels.json",
        "panels_sha256": "b" * 64, "identity_research": {"synthetic": True},
        classification.DESCRIPTOR: {"method_id": classification.METHOD_ID, "classified_at": sidecar["created_at"],
            "parent_manifest": str(parent_file), "parent_manifest_sha256": digest(parent_file),
            "sidecar_file": str(sidecar_file), "sidecar_sha256": digest(sidecar_file)}}
    kinds = {item["id"]: item for item in result["types"]}
    derived = deepcopy(classified)
    derived["types"] = [kinds.get(item["id"], item) for item in derived["types"]]
    assert classification.validate(manifest, {day: derived}, bars, evidence) == sidecar
    sidecar_file.write_text(json.dumps({**sidecar, "created_at": "2026-10-17T15:31:00+08:00"}), encoding="utf-8")
    with pytest.raises(ValueError, match="sidecar or original parent pin changed"):
        classification.load_classification(manifest, evidence)
