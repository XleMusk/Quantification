"""Explicit-mode and single-dispatch checks with tiny saved synthetic panels."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import leader_eight_type_historical_native as native
from trading.research import leader_historical_identity_inputs as m
from trading.research.baostock_daily_slice import digest


DAY = "2026-02-02"
CLOCK = "2026-10-17T15:00:00+08:00"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture_inputs(tmp_path, monkeypatch):
    symbol = "600000.SSE"
    panel = {"day": DAY, "available_at": "2026-10-17T14:00:00+08:00",
        "environment": {"phase": "weak", "source_complete": True},
        "risk_features": {symbol: {"date": DAY, "close": 12., "qualified_stage": 1,
            "qualified_limit_up": True, "ipo_feature_support": True}},
        "types": [{"id": kind, "candidates": [{"symbol": symbol, "score": i}],
            "unresolved_symbols": []} for i, kind in enumerate(m.TYPE_IDS)]}
    panels = {DAY: panel}
    original_panels = tmp_path / "original" / "panels.json"
    write(original_panels, panels)
    manifest = {"created_at": "2026-10-17T14:00:00+08:00", "start": DAY, "end": DAY,
        "source_days": [DAY], "scope_members": [symbol], "raw_sha256": "saved_original_native_raw",
        "panels_file": str(original_panels), "panels_sha256": digest(original_panels)}
    path = tmp_path / "original" / "manifest.json"
    write(path, manifest)
    loaded = {"raw": pd.DataFrame([{"date": DAY, "symbol": symbol, "close": "12", "high": "12",
        "low": "11", "amount": "30000000", "pctChg": "10", "preclose": "10.91"}]),
        "panel_by_day": panels, "qualification": SimpleNamespace(calendar=[DAY])}
    calls = []

    class Tracker:
        def __init__(self, scope, sessions, industry, actual, *, spec):
            calls.append((scope, sessions, actual, spec))

        def observe(self, day, original, view):
            return {"types": [{"id": kind, "candidates": [{"symbol": symbol, "score": 99}],
                "unresolved_symbols": []} for kind in m.TYPES],
                "identity_evidence": {"actual_created_at": CLOCK, "posthoc_only": True}}

    industry = SimpleNamespace(provenance={"posthoc_only": True, "actual_available_at": "2026-10-04T10:00:00+08:00"})
    monkeypatch.setattr(m, "_components", lambda: ({"id": "unchanged_synthetic_spec"}, Tracker,
        lambda *args, **kwargs: industry))
    monkeypatch.setattr(m, "_now", lambda: CLOCK)
    monkeypatch.setattr(m, "load_inputs", lambda _: (loaded, manifest))
    return path, manifest, loaded, calls


def standalone(tmp_path, monkeypatch):
    path, manifest, loaded, calls = fixture_inputs(tmp_path, monkeypatch)
    folder = tmp_path / "registered"
    saved = m.register(path, tmp_path / "industry", None, None, folder, standalone=True)
    return path, manifest, loaded, calls, folder / "registration.json", saved


def test_standalone_is_explicit_rejects_baselines_and_preserves_old_registration_mode(tmp_path, monkeypatch):
    path, manifest, _, _ = fixture_inputs(tmp_path, monkeypatch)
    baseline = tmp_path / "baseline.json"
    write(baseline, {"case_count": 32, "protocol": m.PROTOCOL, "config": m._config(100000, 5).to_dict(),
        "start": DAY, "end": DAY, "scope_members": manifest["scope_members"],
        "source": {"manifest": str(path), "manifest_sha256": digest(path)}})
    for baseline_arg, reuse_arg in ((baseline, None), (None, baseline)):
        with pytest.raises(ValueError, match="cannot attach baseline"):
            m.register(path, tmp_path, baseline_arg, reuse_arg, tmp_path / "forbidden", standalone=True)
    assert not (tmp_path / "forbidden").exists()
    old = m.register(path, tmp_path, baseline, None, tmp_path / "old")
    assert "all32" not in old and "standalone" not in old
    assert m._execution_mode(m.load_registration(tmp_path / "old" / "registration.json")[0]) == "reused_eight"
    new = m.register(path, tmp_path, None, None, tmp_path / "new", standalone=True)
    assert new["all32"] is True and new["standalone"] is True
    assert new["base_input_created_at"] == manifest["created_at"] and new["baseline_result"] is None
    assert new["fixed_protocol"] == m.PROTOCOL
    tampered = deepcopy(new)
    tampered["baseline_result"], tampered["baseline_result_sha256"] = str(baseline), digest(baseline)
    write(tmp_path / "bad_registration.json", tampered)
    with pytest.raises(ValueError, match="cannot reuse"):
        m.load_registration(tmp_path / "bad_registration.json")


def test_standalone_derive_changes_only_two_identity_slots_and_preserves_sources(tmp_path, monkeypatch):
    path, manifest, loaded, calls, registration, _ = standalone(tmp_path, monkeypatch)
    before = path.read_bytes(), Path(manifest["panels_file"]).read_bytes()
    original = deepcopy(loaded["panel_by_day"])
    child = m.derive(registration, tmp_path / "derived")
    panels = json.loads(Path(child["panels_file"]).read_text())
    m.validate_overlay(child, panels)
    for old, new in zip(original[DAY]["types"], panels[DAY]["types"]):
        assert (old != new) is (old["id"] in m.TYPES)
    assert len(calls) == 1 and panels[DAY]["available_at"] == CLOCK
    assert panels[DAY]["environment"] == original[DAY]["environment"]
    assert panels[DAY]["risk_features"] == original[DAY]["risk_features"]
    assert (path.read_bytes(), Path(manifest["panels_file"]).read_bytes()) == before
    assert child["raw_sha256"] == manifest["raw_sha256"]


def test_all32_calls_original_shared_runner_once_and_old_replay_cannot_duplicate_it(tmp_path, monkeypatch):
    _, _, loaded, _, registration, _ = standalone(tmp_path, monkeypatch)
    child = m.derive(registration, tmp_path / "derived")
    manifest = tmp_path / "derived" / "manifest.json"
    calls = []

    def shared(path, out, **kwargs):
        calls.append((path, out, kwargs))
        result = {"case_count": 32, "cases": [{"fills": []} for _ in range(32)]}
        write(Path(out) / "result.json", result)
        return result

    monkeypatch.setattr(native, "run", shared)
    result = m.run_all(manifest, tmp_path / "all32", action_event_view="event", action_reference_view="reference")
    assert result["case_count"] == 32 and len(calls) == 1
    sidecar = json.loads((tmp_path / "all32" / "identity_method.json").read_text())
    assert sidecar["all32"] is True and sidecar["shared_runner_calls"] == 1
    assert sidecar["baseline_replayed"] is False and sidecar["cases_reused_from_prior_replay"] == 0
    assert sidecar["fixed_protocol"] == m.PROTOCOL
    assert sidecar["execution_admitted"] is False
    monkeypatch.setattr(m, "load_inputs", lambda _: (loaded, child))
    with pytest.raises(ValueError, match="requires replay-all"):
        m.run(manifest, tmp_path / "wrong8", action_event_view="event", action_reference_view="reference")
    assert len(calls) == 1 and not (tmp_path / "wrong8").exists()
