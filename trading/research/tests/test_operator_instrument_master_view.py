import pytest

from trading.research import operator_instrument_master_view


def _values():
    return {
        "package": {"source_cutoff": "2026-10-26"},
        "coverage": {
            "schema": "instrument_master_coverage_v1",
            "execution_admitted": False,
            "status": "complete",
            "source_only": ["S1"],
            "master_only": [],
        },
        "snapshot": {
            "instrument_master": {"status": "matched", "master": "master.csv"},
            "instrument_master_coverage": {"status": "complete", "common_count": 2},
        },
        "snapshot_path": "snapshot.json",
        "coverage_path": "coverage.json",
    }


def test_view_preserves_cutoff_identity_and_research_boundary():
    result = operator_instrument_master_view.build_view(**_values())
    assert result["status"] == "matched"
    assert result["source_cutoff"] == "2026-10-26"
    assert result["coverage"]["common_count"] == 2
    assert result["execution_admitted"] is False
    assert result["client_interacted"] is False


def test_view_rejects_execution_enabled_or_wrong_schema():
    values = _values()
    values["coverage"]["execution_admitted"] = True
    with pytest.raises(operator_instrument_master_view.UnsupportedInstrumentMaster):
        operator_instrument_master_view.build_view(**values)
