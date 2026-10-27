from pathlib import Path

from trading import api
from trading.research.operator_manual_statements_view import build_view


def test_manual_statement_view_is_read_only_and_does_not_create_templates(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "operator_package", lambda: {
        "execution_day": "2026-10-15", "source_cutoff": "2026-10-13",
    })
    monkeypatch.setattr(api, "_operator_package_session_root", lambda package: Path(tmp_path))
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(api, "_latest_structured_observation_package", lambda: {})

    result = build_view(api.__dict__)

    assert result["schema"] == "offline_manual_statement_view_v1"
    assert result["status"] == "manual_export_templates_missing"
    assert len(result["rows"]) == 5
    assert result["ledger_mutated"] is result["execution_admitted"] is False
    assert result["account_observed"] is False
    assert list(tmp_path.iterdir()) == []


def test_manual_statement_route_delegates_to_view(monkeypatch):
    expected = {"schema": "offline_manual_statement_view_v1", "status": "delegated"}
    monkeypatch.setattr(
        "trading.research.operator_manual_statements_view.build_view",
        lambda namespace: expected,
    )

    assert api.operator_package_manual_statements() == expected
