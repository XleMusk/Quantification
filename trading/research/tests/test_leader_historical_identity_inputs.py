from copy import deepcopy
import json

import pandas as pd
import pytest

from trading.research import leader_historical_identity_inputs as inputs
from trading.research.baostock_daily_slice import digest
from trading.research.leader_eight_type_panel_replay import TYPE_IDS


def test_identity_view_preserves_qualified_unknown_and_exact_native_reference():
    day, symbol = "2026-01-08", "001369.SZSE"
    raw = pd.DataFrame([{"date": day, "symbol": symbol, "close": "12.00", "high": "12.50",
        "low": "11.50", "amount": "32000000.0", "pctChg": "9.990000", "preclose": "10.91"}])
    panel = {"available_at": "2026-10-17T12:00:00+08:00", "risk_features": {symbol: {
        "date": day, "close": 12., "qualified_stage": None, "qualified_limit_up": True,
        "ipo_feature_support": True}}}
    view = inputs._view(day, panel, raw, None)
    assert view["features"][symbol]["stage"] is None
    assert view["features"][symbol]["limit_up"] is True
    assert view["features"][symbol]["preclose_for_limits"] == 10.91
    assert view["features"][symbol]["pct_chg"] == 9.99
    assert view["available_at"] == panel["available_at"]
    future = raw.copy()
    future["date"] = "2026-01-09"
    with pytest.raises(ValueError, match="exact own-date"):
        inputs._view(day, panel, future, None)


@pytest.mark.parametrize("mutation", ["old_type", "risk"])
def test_overlay_rejects_changes_outside_the_two_registered_identity_types(tmp_path, monkeypatch, mutation):
    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)
    original = {"2026-01-08": {"day": "2026-01-08", "available_at": "2026-10-17T12:00:00+08:00",
        "risk_features": {"A": {"sma20": 10.}}, "environment": {"breadth": .5},
        "types": [{"id": identity, "candidates": [], "unresolved_symbols": []} for identity in TYPE_IDS]}}
    parent = {"raw_sha256": "original_raw", "panels_file": write("original.json", original),
        "panels_sha256": None, "created_at": "2026-10-17T12:00:00+08:00"}
    parent["panels_sha256"] = digest(parent["panels_file"])
    parent_path = write("parent.json", parent)
    registration_path = write("registration.json", {"registered_at": "2026-10-17T12:01:00+08:00"})
    observations_path = write("observations.json", {})
    descriptor = {"method_id": inputs.METHOD_ID, "changed_types": list(inputs.TYPES),
        "historical_publication_backdated": False, "registration": registration_path,
        "registration_sha256": digest(registration_path), "parent_manifest": parent_path,
        "parent_manifest_sha256": digest(parent_path), "observations_file": observations_path,
        "observations_sha256": digest(observations_path), "actual_derived_at": "2026-10-17T12:02:00+08:00"}
    child = {**parent, "identity_research": descriptor, "panels_file": "child.json"}
    monkeypatch.setattr(inputs, "load_registration", lambda _: ({"base_manifest": parent_path,
        "registered_at": "2026-10-17T12:01:00+08:00"}, None))
    panels = deepcopy(original)
    panel = panels["2026-01-08"]
    panel["available_at"] = descriptor["actual_derived_at"]
    panel["identity_research_method"] = inputs.METHOD_ID
    inputs.validate_overlay(child, panels)
    if mutation == "old_type":
        panel["types"][0]["candidates"] = [{"symbol": "A"}]
    else:
        panel["risk_features"]["A"]["sma20"] = 99.
    with pytest.raises(ValueError, match="Unchanged six|source/risk/environment"):
        inputs.validate_overlay(child, panels)


def test_registration_accepts_its_exact_parent_without_old_window_reuse_proof(tmp_path, monkeypatch):
    parent = tmp_path / "parent.json"
    parent.write_text("{}", encoding="utf-8")
    manifest = {"start": "2026-01-05", "end": "2026-01-30", "scope_members": ["000001.SZSE"]}
    baseline = tmp_path / "result.json"
    baseline.write_text(json.dumps({"case_count": 32, "protocol": inputs.PROTOCOL,
        "config": inputs._config(100000, 5).to_dict(),
        **manifest, "source": {"manifest": str(parent.resolve()), "manifest_sha256": digest(parent)}}), encoding="utf-8")
    class Industry:
        provenance = {"method_id": "fixture_industry"}
    monkeypatch.setattr(inputs, "load_inputs", lambda _: ({}, manifest))
    monkeypatch.setattr(inputs, "_components", lambda: ({"id": "fixed_fixture_rule"}, None,
        lambda *args, **kwargs: Industry()))
    saved = inputs.register(parent, tmp_path, baseline, None, tmp_path / "registered")
    assert saved["baseline_input_relation"] == "exact_same_parent"
    assert saved["baseline_reuse_evidence"] is None
    loaded, _ = inputs.load_registration(tmp_path / "registered" / "registration.json")
    assert loaded["base_manifest_sha256"] == digest(parent)
    parent.write_text("{\"changed\": true}", encoding="utf-8")
    with pytest.raises(ValueError, match="input changed"):
        inputs.load_registration(tmp_path / "registered" / "registration.json")
