from trading.research.paper_preview_readiness_view import build_view


def _inputs(**overrides):
    values = {
        "preview": {"status": "observed", "execution_day": "2026-10-12", "release_blockers": []},
        "observation": {
            "missing_evidence": [],
            "account_summary": {"status": "read_background"},
            "client_session": {},
        },
        "structured": {"status": "partial"},
        "structured_package": {"order_reconciliation": {"status": "blocked"}},
        "structured_reconciliation": {"status": "blocked"},
        "journal_reconciliation": {"ledger_mutated": False},
        "same_ledger_bundle_reconciliation": {"status": "isolated_rehearsal_only"},
        "quotes": {"status": "missing", "coverage_status": "unknown"},
        "quote_input": {"status": "missing"},
        "compatibility_fields": {
            "same_ledger_reconciliation_status": "isolated_rehearsal_only",
            "same_ledger_imported": False,
        },
    }
    values.update(overrides)
    return values


def test_view_keeps_paper_and_channel_boundaries_explicit():
    result = build_view(**_inputs())

    assert result["schema"] == "paper_preview_release_readiness_v1"
    assert result["status"] == "blocked"
    assert "ths_client_identity_unverified" in result["blockers"]
    assert "persistent_ths_export_required_for_same_ledger_reconciliation" in result["blockers"]
    assert result["release_ready"] is False
    assert result["execution_admitted"] is False
    assert result["prefill_admitted"] is False
    assert result["ledger_mutated"] is False


def test_same_day_complete_quotes_remove_only_their_blocker():
    result = build_view(**_inputs(
        preview={
            "status": "observed",
            "execution_day": "2026-10-12",
            "release_blockers": ["same_session_raw_rmb_quotes_missing", "other_blocker"],
        },
        quotes={"status": "complete", "coverage_status": "complete", "trading_date": "2026-10-12"},
    ))

    assert "same_session_raw_rmb_quotes_missing" not in result["blockers"]
    assert "other_blocker" in result["blockers"]
    assert result["quote_observation"]["trading_date"] == "2026-10-12"


def test_complete_structured_bundle_resolves_only_its_evidence_classes():
    result = build_view(**_inputs(
        structured={"status": "complete"},
        preview={
            "status": "observed",
            "execution_day": "2026-10-12",
            "release_blockers": [
                "current_user_account_observation_missing",
                "sellable_quantity_and_t1_status_missing",
                "unrelated_blocker",
            ],
        },
    ))

    assert "current_user_account_observation_missing" not in result["blockers"]
    assert "sellable_quantity_and_t1_status_missing" not in result["blockers"]
    assert "unrelated_blocker" in result["blockers"]
    assert result["account_evidence_available"] is True
    assert result["account_observation_complete"] is True
