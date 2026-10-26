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
