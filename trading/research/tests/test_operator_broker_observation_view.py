from trading.research import operator_broker_observation_view


def _inputs(alerts=None, live=False):
    return {
        "alerts": {
            "status": "isolated_observation_alerts_only",
            "alerts": alerts or [],
            "live_alerts": live,
        },
        "journal_reconciliation": {"ledger_mutated": False},
        "same_ledger_bundle_reconciliation": {
            "source_scope": "isolated_rehearsal",
            "source_manifest_status": "verified",
        },
        "persistent_readiness": {
            "status": "templates_blank",
            "preview": {"status": "templates_blank"},
            "source_manifest_status": "not_ready",
            "source_manifest_issues": [],
            "profile": {"mapping_validation": {"status": "shape_valid", "issues": []}},
        },
        "same_ledger_compat_fields": lambda _bundle, _journal: {"same_ledger_imported": False},
    }


def test_synthetic_alerts_remain_research_only():
    result = operator_broker_observation_view.build_view(
        **_inputs([{"live_alert": False, "reason": "synthetic"}])
    )
    assert result["persistent_bundle_readiness"]["bundle_is_account_evidence"] is False
    assert result["alert_source_selection"]["synthetic_source_is_not_account_evidence"] is True
    assert result["ledger_mutated"] is False
    assert "synthetic rehearsal evidence" in result["next_action"]


def test_persistent_verified_alert_uses_recovery_action():
    values = _inputs([{"live_alert": True}], live=True)
    values["same_ledger_bundle_reconciliation"] = {
        "source_scope": "persistent_user_export",
        "source_manifest_status": "verified",
    }
    values["persistent_readiness"] = {
        "status": "bundle_ready_for_reconciliation",
        "preview": {"status": "bundle_ready_for_reconciliation"},
        "source_manifest_status": "verified",
        "profile": {"mapping_validation": {"status": "valid", "issues": []}},
    }
    result = operator_broker_observation_view.build_view(**values)
    assert result["persistent_bundle_readiness"]["bundle_is_account_evidence"] is True
    assert result["alert_source_selection"]["reconciliation_is_account_evidence"] is True
    assert result["next_action"] == "resolve the listed broker/Ledger differences before any recovery"


def test_empty_alerts_keep_explicit_wait_state():
    result = operator_broker_observation_view.build_view(**_inputs())
    assert result["alerts"] == []
    assert "retain the explicit wait state" in result["next_action"]
    assert result["client_interacted"] is False
