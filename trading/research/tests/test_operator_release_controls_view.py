from trading.research.operator_release_controls_view import build_view


def test_release_controls_view_preserves_paper_only_state():
    result = build_view(
        alerts=[{"state": "open", "severity": "P1"}, {"state": "closed", "severity": "P1"}],
        controls=[{"control": "no_submit", "value": "true"}],
        source_files={"alerts": "alerts.csv", "controls": "controls.csv"},
        journal_reconciliation={"ledger_mutated": False},
        same_ledger_bundle_reconciliation={"status": "isolated_rehearsal_only"},
        compatibility_fields={"same_ledger_reconciliation_status": "isolated_rehearsal_only",
                              "same_ledger_imported": False},
    )

    assert result["schema"] == "offline_release_controls_view_v1"
    assert result["alerts"]["open_count"] == 1
    assert result["alerts"]["severity_counts"] == {"P1": 2}
    assert result["controls"]["row_count"] == 1
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False


def test_release_controls_view_does_not_mutate_input_rows():
    alerts = [{"state": "open", "severity": "P0"}]
    controls = [{"control": "prefill", "value": "false"}]
    build_view(
        alerts=alerts,
        controls=controls,
        source_files={},
        journal_reconciliation={"ledger_mutated": True},
        same_ledger_bundle_reconciliation={},
        compatibility_fields={},
    )
    assert alerts == [{"state": "open", "severity": "P0"}]
    assert controls == [{"control": "prefill", "value": "false"}]
