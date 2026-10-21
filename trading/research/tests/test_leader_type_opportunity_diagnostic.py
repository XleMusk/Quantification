"""Tiny saved projection/denominator checks, never market or Engine replay."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from trading.research import leader_type_opportunity_diagnostic as diagnostic
from trading.research import leader_type_opportunity_selection as selector


A, DAYS = "600001.SSE", ["2024-01-02", "2024-01-03"]


def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf8")
    return {"path": str(path.resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}


def panels():
    result = {}
    for day in DAYS:
        types = [{"id": t, "status": "not_matched", "candidates": [], "unresolved_symbols": []}
            for t in selector.TYPE_IDS]
        types[1].update(status="matched", candidates=[{"symbol": A, "score": 2.,
            "evidence": {"must_not_retain": "full_candidate_payload"}}])
        if day == DAYS[1]:
            types[1]["status"] = "unknown"
            types[2].update(status="matched", candidates=[{"symbol": A, "score": 3.}])
        result[day] = {"day": day, "available_at": day+"T18:00:00+08:00",
            "environment": {"source_complete": True, "breadth": .5, "median_return_pct": .5},
            "types": types, "risk_features": {"future_label_must_not_read": "unrelated_payload"}}
    return result


def test_one_pass_projection_keeps_ownD_fields_and_original_rank_only_with_original_SHA(tmp_path):
    path = tmp_path/"panels.json"; ref = save(path, panels())
    actual = diagnostic.read_panel_projection(path, ref["sha256"])
    assert list(actual) == DAYS
    assert all(set(row) == {"day", "available_at", "environment", "types"} for row in actual.values())
    assert actual[DAYS[0]]["types"][1]["candidates"] == [{"symbol": A, "score": 2.}]
    with pytest.raises(ValueError, match="Pinned panel bytes"):
        diagnostic.read_panel_projection(path, "0"*64)


def test_allD_assumed_flat_and_saved_preflat_are_separate_and_unknown_never_skipped(monkeypatch):
    projected = {d: {k:v for k,v in row.items() if k != "risk_features"} for d,row in panels().items()}
    monkeypatch.setattr(diagnostic, "_environment", lambda *a: {d: {"per_type": {
        t: {"mode": "ALLOW_FIXED_CANDIDATES"} for t in selector.TYPE_IDS}} for d in DAYS})
    period = {"period": "synthetic2024", "start": DAYS[0], "end": DAYS[-1],
        "source_days": DAYS, "scope_count": 1}
    def old(day, *, held=(), pending=()):
        offset = DAYS.index(day)
        return {"day": day, "cycle": [selector.TYPE_IDS[(offset+i) % 8] for i in range(8)],
            "pre_held_symbols": list(held), "pre_positive_pending_symbols": list(pending),
            "selected_type": selector.TYPE_IDS[offset], "selected_symbols": [],
            "reasons": ["type_empty_or_capacity_filtered_all_no_signal"],
            "close_rule": "selected_type_unknown_or_empty_NO_CHANGE", "explicit_exit": None, "status": "NO_CHANGE"}
    records = {key: None for key in diagnostic.CASE_IDS}
    records[diagnostic.CASE_IDS[0]] = [old(DAYS[0])]
    records[diagnostic.CASE_IDS[1]] = [old(DAYS[0], held=[A]), old(DAYS[1])]
    records[diagnostic.CASE_IDS[1]][1].pop("pre_held_symbols")  # No manufactured empty prestate.
    released = old(DAYS[0]); released["close_rule"] = "actual_flat_cancel_release"
    records[diagnostic.CASE_IDS[2]] = [released]
    explicit = old(DAYS[0]); explicit["explicit_exit"] = "environment_EXIT_ALL"
    records[diagnostic.CASE_IDS[3]] = [explicit]
    full, saved = diagnostic.project_period(period, projected, [A], DAYS, records)
    assert len(full) == len(saved) == 8 and sum(r["evaluated"] for r in saved) == 3
    assert sum(r["recovered_on_original_flat_path"] for r in saved) == 1
    first = next(r for r in full if r["day"] == DAYS[0] and r["case_id"] == diagnostic.CASE_IDS[0])
    assert first["initial_known_empty"] and first["new_selected"] == selector.TYPE_IDS[1]
    assert first["selected_symbols"] == [A]
    later = next(r for r in full if r["day"] == DAYS[1] and r["case_id"] == diagnostic.CASE_IDS[0])
    assert later["unknown_barrier"] and later["unknown_barrier_type"] == selector.TYPE_IDS[1]
    assert later["new_selected"] is None and later["selected_symbols"] is None
    assert any(r["original_coverage"] == "covered_prestate_unknown" and r["original_pre_flat"] is None for r in saved)
    assert all(r["new_selected"] is None for r in saved if not r["evaluated"])
    groups = diagnostic.aggregate(saved)
    assert sum(g["evaluated_D_count"] for g in groups) == 3
    assert sum(g["opportunity_after_known_empty_D_count"] for g in groups) == 3
    assert sum(g["recovered_on_original_flat_path_D_count"] for g in groups) == 1
    assert all(g["returns"] is None for g in groups)


def test_register_only_pins_small_sources_and_does_not_open_declared_panels(tmp_path):
    definitions, original_studies = [], []
    router = tmp_path/"original_router"
    for year in (2023, 2024, 2025):
        label = str(year); start, end = str(year)+"-04-06", str(year)+"-12-21"
        calendar = save(tmp_path/label/"calendar.json", {"year": year})
        # A declared absent large panel: registration must not try to open it.
        header = {"panels_file": str(tmp_path/label/"large_panels_not_opened.json"), "panels_sha256": "a"*64,
            "calendar": calendar["path"], "calendar_sha256": calendar["sha256"]}
        manifest = save(tmp_path/label/"manifest.json", header)
        definitions.append({"label": label, "inputs": manifest})
        original_studies.append({"label": label, "inputs": manifest, "source_payloads": header,
            "start": start, "end": end, "through": str(year)+"-12-29", "source_days": [start,end], "scope_count": 1})
        for case in diagnostic.CASE_IDS:
            save(router/label/case/"decisions.json", [])
    original = save(tmp_path/"old_registration/registration.json", {
        "method_id": "single_active_type_cyclic_environment_switch_shared_engine_v1", "studies": original_studies})
    save(router/"started.json", {"registration": original})
    mapping = save(tmp_path/"mapping/studies.json", {"studies": definitions})
    record = diagnostic.register(mapping["path"], router, tmp_path/"new_registration")
    assert len(record["periods"]) == 3 and all(len(p["cases"]) == 4 for p in record["periods"])
    assert record["engine_advanced"] is record["prices_read"] is record["future_outcomes_read"] is False
    assert record["spec"]["populations"] == ["all_D_assumed_flat", "original_saved_path_observed_pre_flat"]
