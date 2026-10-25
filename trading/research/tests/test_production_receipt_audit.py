import json

from trading.research.production_receipt_audit import audit_receipts


def test_audit_classifies_saved_alpha_split_as_historical_readonly(tmp_path):
    alpha = tmp_path / "alpha.json"
    alpha.write_text(json.dumps({
        "actual_status": "completed_alpha158_source_context",
        "observed_at": "2026-10-21T18:24:49+08:00",
        "source_day": "2026-10-20",
        "publication_source_day": "2026-10-19",
        "score_rows": 5024,
        "source_days": ["2026-10-19", "2026-10-20"],
        "score_dates": ["2026-10-20"],
        "execution_admitted": False,
        "publication_generated": False,
        "engine_advanced": False,
        "original_accounts_advanced": False,
        "model_refit": False,
        "selection_enabled": False,
        "strategy_validated": False,
        "candidate_panel_only": True,
    }), encoding="utf-8")
    route = tmp_path / "route.json"
    route.write_text(json.dumps({
        "publication_status": "close_research_target_publication_only",
        "execution_admitted": False,
        "orders_generated": False,
        "ths_payload_generated": False,
        "client_interacted": False,
        "cases": [{"case": "uncapped"}],
        "unique_symbols": ["605376.SSE"],
    }), encoding="utf-8")
    result = audit_receipts(alpha, route, observed_at="2026-10-24T00:00:00+00:00")
    assert result["status"] == "VERIFIED_LOCAL_READONLY_RECEIPT"
    assert result["temporal_class"] == "HISTORICAL_RECEIPT"
    assert result["execution_admitted"] is False and result["account_advanced"] is False


def test_audit_blocks_a_receipt_with_execution_flags(tmp_path):
    alpha = tmp_path / "alpha.json"
    alpha.write_text(json.dumps({
        "actual_status": "completed_alpha158_source_context", "observed_at": "2026-10-21T00:00:00+00:00",
        "source_day": "2026-10-20", "score_rows": 1, "source_days": ["2026-10-20"], "score_dates": ["2026-10-20"],
        "execution_admitted": True, "publication_generated": False, "engine_advanced": False,
        "original_accounts_advanced": False, "model_refit": False, "selection_enabled": False,
        "strategy_validated": False, "candidate_panel_only": True,
    }), encoding="utf-8")
    result = audit_receipts(alpha)
    assert result["status"] == "RECEIPT_REVIEW_BLOCKED"
    assert "execution_admitted" in result["issues"]
