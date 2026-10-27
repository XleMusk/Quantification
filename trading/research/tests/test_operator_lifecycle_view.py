from pathlib import Path

from trading import api
from trading.research.operator_lifecycle_view import build_view


def _patch_empty_package(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(api, "operator_package", lambda: {"execution_day": "2026-10-27", "strategy_id": "fixed"})
    monkeypatch.setattr(api, "_latest_structured_observation_package", lambda: {})
    monkeypatch.setattr(api, "_latest_complete_structured_observation_package", lambda: {})
    monkeypatch.setattr(api, "_research_evidence_consumer_summary", lambda strategy_id: {})
    monkeypatch.setattr(api, "_package_artifact", lambda package, name: None)
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {"ledger_mutated": False})
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: {})
    monkeypatch.setattr(api, "_observed_fill_tca_summary", lambda value: {})
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda bundle, journal: {})
    monkeypatch.setattr(api, "paper_preview_readiness", lambda: {"status": "not_ready", "release_ready": False})


def test_lifecycle_view_is_read_only_without_saved_artifacts(monkeypatch, tmp_path):
    _patch_empty_package(monkeypatch, tmp_path)

    result = build_view(api.__dict__)

    assert result["schema"] == "offline_paper_lifecycle_view_v1"
    assert result["execution_admitted"] is False
    assert result["account_observed"] is False
    assert result["client_interacted"] is False
    assert result["ledger_mutated"] is False
    assert result["lifecycle"]["event_count"] == 0


def test_lifecycle_route_delegates_to_application_view(monkeypatch):
    expected = {"schema": "offline_paper_lifecycle_view_v1", "status": "delegated"}
    monkeypatch.setattr("trading.research.operator_lifecycle_view.build_view", lambda namespace: expected)

    assert api.operator_package_lifecycle() == expected
