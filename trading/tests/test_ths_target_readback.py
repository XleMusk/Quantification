from hashlib import sha256
from pathlib import Path

import pytest

from trading.ths.target_readback import compare_target_reference


def _reference():
    return {
        "schema": "quantification_ths_readonly_target_reference_v1",
        "reference_day": "2026-05-29",
        "execution_admitted": False,
        "prefill_allowed": False,
        "rows": [
            {"symbol": "000001.SZSE", "target_quantity": 100},
            {"symbol": "600000.SSE", "target_quantity": 200},
        ],
    }


def test_target_readback_verified_without_execution():
    result = compare_target_reference(_reference(), {
        "observed_at": "2026-10-25T18:00:00+08:00",
        "rows": [{"symbol": "000001.SZSE", "qty": 100}, {"symbol": "600000.SSE", "qty": 200}],
    })
    assert result["status"] == "readback_verified"
    assert result["execution_admitted"] is False
    assert result["submitted"] is False


def test_target_readback_mismatch_stops():
    result = compare_target_reference(_reference(), {
        "rows": [{"symbol": "000001.SZSE", "qty": 300}, {"symbol": "600000.SSE", "qty": 200}],
    })
    assert result["status"] == "readback_mismatch_stop"
    assert result["quantity_mismatches"][0]["symbol"] == "000001.SZSE"


def test_target_readback_missing_row_stops():
    result = compare_target_reference(_reference(), {"rows": [{"symbol": "000001.SZSE", "qty": 100}]})
    assert result["status"] == "readback_incomplete_stop"
    assert result["missing_symbols"] == ["600000.SSE"]


def test_target_readback_duplicate_export_row_stops_without_last_row_selection():
    result = compare_target_reference(_reference(), {
        "rows": [
            {"symbol": "000001.SZSE", "qty": 100},
            {"symbol": "000001.SZSE", "qty": 999},
            {"symbol": "600000.SSE", "qty": 200},
        ],
    })
    assert result["status"] == "readback_mismatch_stop"
    assert result["duplicate_readback_symbols"] == ["000001.SZSE"]


def test_target_readback_duplicate_reference_is_rejected():
    reference = _reference()
    reference["rows"].append({"symbol": "000001.SZSE", "target_quantity": 100})
    with pytest.raises(ValueError, match="duplicate symbols"):
        compare_target_reference(reference, {"rows": []})


def test_synthetic_readback_is_explicitly_unobserved():
    result = compare_target_reference(_reference(), {
        "synthetic_fixture": True,
        "observed_at": "2026-10-27T01:20:00+08:00",
        "account_observed": False,
        "quote_observed": False,
        "rows": [{"symbol": "000001.SZSE", "qty": 100}, {"symbol": "600000.SSE", "qty": 200}],
    })
    assert result["readback_provenance"]["kind"] == "synthetic_fixture"
    assert result["authorized_readonly_observation"] is False


def test_claimed_observation_requires_hash_bound_source():
    with pytest.raises(ValueError, match="authorized_read_only_export"):
        compare_target_reference(_reference(), {
            "account_observed": True,
            "quote_observed": False,
            "client_interacted": True,
            "rows": [{"symbol": "000001.SZSE", "qty": 100}, {"symbol": "600000.SSE", "qty": 200}],
        })


def test_authorized_readonly_observation_requires_no_submission():
    result = compare_target_reference(_reference(), {
        "capture_mode": "authorized_read_only_export",
        "client_interacted": True,
        "account_observed": True,
        "quote_observed": True,
        "observed_at": "2026-10-27T01:20:00+08:00",
        "source_file": "D:/evidence/ths_readback.json",
        "source_sha256": "a" * 64,
        "rows": [{"symbol": "000001.SZSE", "qty": 100}, {"symbol": "600000.SSE", "qty": 200}],
    })
    assert result["status"] == "readback_verified"
    assert result["authorized_readonly_observation"] is True
    assert result["submitted"] is False
    assert result["execution_admitted"] is False


def test_authorized_observation_rehashes_export_when_source_root_is_supplied(tmp_path):
    export = tmp_path / "ths_readback.json"
    export.write_text("authorized export bytes", encoding="utf-8")
    readback = {
        "capture_mode": "authorized_read_only_export",
        "client_interacted": True,
        "account_observed": True,
        "quote_observed": True,
        "observed_at": "2026-10-27T01:20:00+08:00",
        "source_file": export.name,
        "source_sha256": sha256(export.read_bytes()).hexdigest(),
        "rows": [{"symbol": "000001.SZSE", "qty": 100}, {"symbol": "600000.SSE", "qty": 200}],
    }
    result = compare_target_reference(_reference(), readback, source_root=tmp_path)
    assert result["status"] == "readback_verified"
    assert result["readback_provenance"]["source_file_verified"] is True
    assert Path(result["readback_provenance"]["source_file"]) == export.resolve()


def test_authorized_observation_rejects_changed_export_hash(tmp_path):
    export = tmp_path / "ths_readback.json"
    export.write_text("changed bytes", encoding="utf-8")
    with pytest.raises(ValueError, match="source_sha256"):
        compare_target_reference(_reference(), {
            "capture_mode": "authorized_read_only_export",
            "client_interacted": True,
            "account_observed": True,
            "quote_observed": True,
            "observed_at": "2026-10-27T01:20:00+08:00",
            "source_file": export.name,
            "source_sha256": "a" * 64,
            "rows": [{"symbol": "000001.SZSE", "qty": 100}, {"symbol": "600000.SSE", "qty": 200}],
        }, source_root=tmp_path)
