import json

from trading.research import research_decision_matrix as matrix


def test_market_replay_blocker_is_visible_in_exception_register(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "ROOT", tmp_path)
    run = tmp_path / "outputs" / "market_replay" / "run-1"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({
        "id": "run-1",
        "scope": "full_source_sh_sz_research_only",
        "status": "failed_full_source_diagnostic",
        "start": "2025-01-02",
        "end": "2025-12-31",
        "results": [{
            "signal": "liquid_momentum",
            "status": "failed",
            "completed_sessions": 115,
            "blocker": {
                "code": "missing_corporate_action_terms",
                "message": "missing reserve term",
                "symbol": "603121.SSE",
                "decision_date": "2025-06-27",
                "missing_fields": ["dividReserveToStockPs"],
            },
        }],
    }), encoding="utf-8")

    result = matrix._research_exception_register()

    assert result["status"] == "open_blockers"
    assert result["latest_market_replay_attempt"]["signal"] == "liquid_momentum"
    assert result["open_items"] == [{
        "code": "missing_corporate_action_terms",
        "message": "missing reserve term",
        "symbol": "603121.SSE",
        "decision_date": "2025-06-27",
        "missing_fields": ["dividReserveToStockPs"],
        "source": str(run / "run.json"),
        "signal": "liquid_momentum",
        "window": {"start": "2025-01-02", "end": "2025-12-31"},
        "source_kind": "market_replay",
    }]
