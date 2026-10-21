"""Saved restoration contract only; existing payload loader is isolated here."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from trading.research import leader_eight_type_historical_native as native


def fixture(tmp_path, monkeypatch):
    prefix = pd.DataFrame([{"date": "2025-09-29", "code": "sz.000001", "symbol": "000001.SZSE",
        "tradestatus": "0", "volume": "", "open": "0"}])
    raw, diagnostic_path = tmp_path / "restore_history.parquet", tmp_path / "restore_diagnostic.json"
    prefix.to_parquet(raw, index=False)
    diagnostic = {"created_at": "2026-10-17T12:22:00+08:00", "fixture": "already_validated_saved_payload"}
    diagnostic_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    prior = {"path": "fixed2025.json", "sha256": "calendar25", "actual_read_at": "2026-10-17T12:21:00+08:00",
        "source_file": "notice.html", "source_sha256": "official25", "source_receipt_sha256": "receipt25"}
    old = {"schema": native.INPUT_SCHEMA, "method_id": native.METHOD_ID,
        "created_at": "2026-10-17T12:26:00+08:00", "start": "2026-01-05", "end": "2026-01-08", "through": "2026-01-16",
        "scope_members": ["000001.SZSE"], "warmup_source_days": ["2025-09-30", "2025-12-31"],
        "source_proofs": [{"source_view": "base", "raw_sha256": "raw1", "opened_at": "2026-10-17T12:21:00+08:00"},
            {"source_view": "extension", "raw_sha256": "raw2", "opened_at": "2026-10-17T12:21:00+08:00"}],
        "master_run": "master_run.json", "calendar": "fixed2026.json", "calendar_sha256": "calendar26",
        "prior_calendars": [prior], "IPO_qualification": {"method_id": native.LISTING_METHOD,
            "master_run": "master_run.json", "master_run_sha256": "mr", "master_file": "master.csv", "master_sha256": "master1",
            "original_available_at": "2026-10-08T12:00:00+08:00", "posthoc_observed_at": "2026-10-17T12:21:00+08:00",
            "calendar": "fixed2026.json", "calendar_sha256": "calendar26", "prior_calendars": [prior]},
        "turnover_unit_evidence": {"path": "units.json", "sha256": "units"},
        "warmup_restoration": {"enabled": True, "method_id": "saved_original_method", "required_valid_observations": 61,
            "first_decision_day": "2026-01-05", "raw_file": str(raw), "raw_sha256": native.digest(raw),
            "diagnostic_file": str(diagnostic_path), "diagnostic_sha256": native.digest(diagnostic_path)}}
    anchor = tmp_path / "manifest.json"
    anchor.write_text(json.dumps(old), encoding="utf-8")
    calls = []
    def already_tested_payload_loader(path):
        calls.append(path)
        assert path == anchor.resolve()
        return {}, deepcopy(old)
    monkeypatch.setattr(native, "load_inputs", already_tested_payload_loader)
    monkeypatch.setattr(native, "NativeHistorySource", lambda *a, **k: pytest.fail("saved reuse must not open another source reader"))
    expected = deepcopy(old)
    expected.update(end="2026-01-30", through="2026-02-09", created_at="2026-10-17T13:00:00+08:00")
    expected["IPO_qualification"]["posthoc_observed_at"] = "2026-10-17T12:59:00+08:00"
    expected["prior_calendars"][0]["actual_read_at"] = "2026-10-17T12:59:00+08:00"
    for proof in expected["source_proofs"]:
        proof["opened_at"] = "2026-10-17T12:59:00+08:00"
    return anchor, old, expected, prefix, diagnostic, calls


def test_reuse_keeps_original_rows_hashes_and_created_clock_with_larger_window_and_new_read_clocks(tmp_path, monkeypatch):
    anchor, old, expected, prefix, diagnostic, calls = fixture(tmp_path, monkeypatch)
    before = {name: native.digest(old["warmup_restoration"][name]) for name in ("raw_file", "diagnostic_file")}
    actual, saved_diagnostic, reuse = native._saved_warmup_for_reuse(anchor, expected)
    pd.testing.assert_frame_equal(actual, prefix)
    assert saved_diagnostic == diagnostic and len(calls) == 1
    assert reuse["raw_sha256"] == old["warmup_restoration"]["raw_sha256"]
    assert reuse["reuse_from"]["restoration_created_at"] == diagnostic["created_at"]
    assert reuse["reuse_from"]["manifest_sha256"] == native.digest(anchor)
    assert {name: native.digest(reuse[name]) for name in ("raw_file", "diagnostic_file")} == before
    assert actual.iloc[0].volume == "" and actual.iloc[0].tradestatus == "0"
    saved_source = old["source_proofs"][0]
    assert not native._restoration_source_matches(saved_source, expected["source_proofs"], reused=False)
    assert native._restoration_source_matches(saved_source, expected["source_proofs"], reused=True)
    changed_source = deepcopy(expected["source_proofs"])
    changed_source[0]["source_available_at"] = "changed_source_terminal_clock"
    assert not native._restoration_source_matches(saved_source, changed_source, reused=True)


def test_reuse_rejects_later_first_day_scope_source_order_and_changed_pin_contracts(tmp_path, monkeypatch):
    anchor, old, expected, *_ = fixture(tmp_path, monkeypatch)
    bad = []
    changed = deepcopy(expected); changed["start"] = "2026-01-06"; bad.append(changed)
    changed = deepcopy(expected); changed["scope_members"].append("000002.SZSE"); bad.append(changed)
    changed = deepcopy(expected); changed["warmup_source_days"] = ["2025-10-09", "2025-12-31"]; bad.append(changed)
    changed = deepcopy(expected); changed["source_proofs"].reverse(); bad.append(changed)
    changed = deepcopy(expected); changed["source_proofs"][0]["raw_sha256"] = "changed"; bad.append(changed)
    changed = deepcopy(expected); changed["IPO_qualification"]["master_sha256"] = "changed"; bad.append(changed)
    changed = deepcopy(expected); changed["calendar_sha256"] = "changed"; bad.append(changed)
    changed = deepcopy(expected); changed["prior_calendars"][0]["sha256"] = "changed"; bad.append(changed)
    changed = deepcopy(expected); changed["turnover_unit_evidence"]["sha256"] = "changed"; bad.append(changed)
    for changed in bad:
        with pytest.raises(ValueError, match="exact original first-day"):
            native._saved_warmup_for_reuse(anchor, changed)
