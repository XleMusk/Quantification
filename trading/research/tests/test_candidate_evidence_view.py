from datetime import datetime

from trading.research import candidate_evidence_view as view
from trading.research.tests.test_research_evidence_catalog import fixture


def test_view_delegates_saved_catalog_without_mutating_inputs(tmp_path):
    fixture_data = fixture(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = view.build_view(
        fixture_data["project"],
        fixture_data["data"],
        now=datetime.fromisoformat("2026-10-19T10:00:00+08:00"),
    )

    assert result["schema"] == view.SCHEMA
    assert result["candidate_count"] == 19
    assert result["execution_admitted"] is False
    assert result["selection_enabled"] is False
    assert result["producer_called"] is False
    assert result["files_written"] is False
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_view_preserves_catalog_evidence_failure_as_unknown(tmp_path):
    fixture_data = fixture(tmp_path)
    fixture_data["summary"].write_text("{}", encoding="utf-8")

    result = view.build_view(fixture_data["project"], fixture_data["data"])

    candidate = result["candidates"][0]
    assert candidate["ranking_diagnostic"]["status"] == "unknown"
    assert candidate["account_comparison"]["status"] == "saved"
    assert result["candidate_count"] == 19
