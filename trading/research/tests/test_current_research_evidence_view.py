import json

import pytest

from trading.research import current_research_evidence_view as view


def test_missing_current_research_inputs_are_explicit(tmp_path):
    assert view.leader_comparison(tmp_path)["status"] == "missing"
    assert view.market_scoreboard(tmp_path)["status"] == "missing"


def test_leader_comparison_keeps_saved_boundary_and_newest_result(tmp_path):
    path = tmp_path / "outputs" / "leader_daily_replay" / "run" / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema": "leader_daily_comparison_v2",
        "candidate_panel_only": True,
        "execution_admitted": False,
        "strategy_validated": False,
        "selection_enabled": False,
        "cases": [],
    }), encoding="utf-8")

    result = view.leader_comparison(tmp_path)

    assert result["source_file"] == str(path.resolve())
    assert result["research_only"] is True
    assert result["automatic_promotion"] is False


def test_invalid_leader_boundary_is_rejected(tmp_path):
    path = tmp_path / "outputs" / "leader_daily_replay" / "run" / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": "leader_daily_comparison_v2", "execution_admitted": True}), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate-panel"):
        view.leader_comparison(tmp_path)


def test_market_scoreboard_preserves_frozen_model_boundary(tmp_path):
    path = tmp_path / "outputs" / "market_regime_scoreboard" / "scoreboard.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema": "market_regime_scoreboard_v1",
        "execution_admitted": False,
        "model_refit": False,
        "summary_by_horizon": {"5": {"registered_targets": 1}},
    }), encoding="utf-8")

    result = view.market_scoreboard(tmp_path)

    assert result["status"] == "saved_forecast_outcome_audit"
    assert result["selection_enabled"] is False
    assert result["execution_admitted"] is False
