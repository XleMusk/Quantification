import json

import pytest
from fastapi import HTTPException

from trading import api


def receipt(root, name, **changes):
    path = root / "outputs" / "leader_daily_replay" / name / "result.json"
    path.parent.mkdir(parents=True)
    payload = {"schema": "leader_daily_comparison_v2", "status": "completed_with_data_gaps",
               "candidate_panel_only": True, "strategy_validated": False,
               "selection_enabled": False, "execution_admitted": False,
               "cases": [{"family": "stage1", "status": "blocked_data_gap"}]}
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_latest_failure_stays_visible_without_using_an_older_winner(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    receipt(tmp_path, "older", cases=[{"status": "completed", "net_return": .2}])
    latest = receipt(tmp_path, "latest", status="failed")
    import os
    os.utime(latest, (2000000000, 2000000000))
    result = api.leader_research_comparison()
    assert result["status"] == "failed" and result["cases"][0]["status"] == "blocked_data_gap"
    assert result["source_file"] == str(latest.resolve())
    assert result["selection_enabled"] is result["automatic_promotion"] is False


def test_missing_and_unsafe_latest_receipt_cannot_admit_a_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    assert api.leader_research_comparison()["status"] == "missing"
    receipt(tmp_path, "bad", execution_admitted=True)
    with pytest.raises(HTTPException) as error:
        api.leader_research_comparison()
    assert error.value.status_code == 409


def test_pending_market_forecasts_stay_unscored_in_readonly_view(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    path = tmp_path / "outputs" / "market_regime_scoreboard" / "scoreboard.json"
    path.parent.mkdir(parents=True)
    payload = {"schema": "market_regime_scoreboard_v1", "model_refit": False,
               "execution_admitted": False, "summary_by_horizon": {
                   "1": {"prospective_scored": 0, "signal_accuracy": None}},
               "rows": [{"status": "pending", "actual_return": None}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = api.market_forecast_scoreboard()
    assert result["summary_by_horizon"]["1"]["signal_accuracy"] is None
    assert result["rows"][0]["actual_return"] is None
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert result["selection_enabled"] is result["execution_admitted"] is False
