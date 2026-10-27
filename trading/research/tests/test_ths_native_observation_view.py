from __future__ import annotations

import json
from pathlib import Path

from trading.research.ths_native_observation_view import build_view


def _namespace(summary, grids, evidence, structured):
    return {
        "ths_native_summary": lambda: summary,
        "ths_native_grids": lambda: grids,
        "ths_readonly_evidence": lambda: evidence,
        "_latest_structured_observation_package": lambda: structured,
        "_ths_client_session_status": lambda: {
            "status": "not_attempted",
            "client_interacted": False,
            "execution_admitted": False,
        },
    }


def test_view_keeps_missing_native_observation_explicit():
    result = build_view(_namespace(
        {"status": "not_observed"},
        {"grids": {}},
        {"status": "not_observed"},
        {"order_reconciliation": {}},
    ))

    assert result["schema"] == "ths_native_observation_candidate_v1"
    assert result["status"] == "incomplete"
    assert "account_summary" in result["missing_evidence"]
    assert result["execution_admitted"] is False
    assert result["client_interacted"] is False
    assert result["ledger_mutated"] is False


def test_view_prefers_indexed_readonly_summary_and_candidate(tmp_path: Path):
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps({
        "candidate_state": "symbol_rows_detected",
        "row_count": 1,
        "rows": [{"symbol": "000001.SZ"}],
        "checks": {"summary_cross_checks_pass": True},
    }), encoding="utf-8")
    result = build_view(_namespace(
        {"status": "not_observed"},
        {"grids": {}},
        {
            "status": "partial_readonly_evidence",
            "account_alias": "paper-reference",
            "summary": {
                "status": "read",
                "path": "summary.json",
                "observed_at": "2026-10-27T04:00:00+00:00",
                "positions_read": True,
                "full_account_observation": False,
                "metrics_raw": {"cash": "unknown"},
            },
            "holdings": {"candidate": {"path": str(candidate_path)}},
        },
        {"order_reconciliation": {}, "ready_for_ledger_reconciliation": False},
    ))

    assert result["status"] == "candidate_only"
    assert result["account_summary"]["source"] == "readonly_evidence_index"
    assert result["position_candidate"]["row_count"] == 1
    assert "verified_structured_holdings" not in result["missing_evidence"]
    assert result["full_account_observation"] is False
    assert result["execution_admitted"] is False

