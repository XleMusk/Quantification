from trading import api


def test_export_template_view_returns_without_mutating_or_reading_the_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "operator_package", lambda: {
        "execution_day": "2026-10-15", "source_cutoff": "2026-10-13",
    })
    monkeypatch.setattr(api, "_operator_package_session_root", lambda package: tmp_path)
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(api, "_latest_structured_observation_package", lambda: {})
    def forbidden():
        raise AssertionError("Export templates do not read or import the account journal")
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", forbidden)
    result = api.operator_package_manual_statements()
    assert result["status"] == "manual_export_templates_missing"
    assert result["source_cutoff"] == "2026-10-13"
    assert len(result["rows"]) == 5
    assert result["ledger_mutated"] is result["execution_admitted"] is False
    assert list(tmp_path.iterdir()) == []
