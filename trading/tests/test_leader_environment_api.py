import json

import pytest
from fastapi import HTTPException

from trading import api


def save(root, date="20261014", **changes):
    boundary = {"candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
                "execution_admitted": False, "orders_generated": False, "ledger_mutated": False}
    payload = {"schema": "leader_environment_analysis_run_v1", "day": "2026-10-14", **boundary,
               "analysis": {"day": "2026-10-14", **boundary, "archetypes": [
                   {"id": "trend_leader", "candidate_count": 5, "candidates": [{"symbol": str(i)} for i in range(5)]}]}}
    payload.update(changes)
    path = root / "outputs/leader_environment" / (date + "_v1") / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_display_limit_is_not_portfolio_selection_and_never_changes_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path / "data")
    path = save(tmp_path)
    before = path.read_bytes()
    view = api.leader_environment_view(display_limit=2)
    row = view["analysis"]["archetypes"][0]
    assert row["candidate_count"] == row["stored_candidate_count"] == 5 and len(row["candidates"]) == 2
    assert view["display_is_not_portfolio_selection"] is True
    assert view["intraday_observation_verified"] is view["execution_admitted"] is False
    assert path.read_bytes() == before


def test_missing_and_unsafe_newest_do_not_fall_back_to_old_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path / "data")
    assert api.leader_environment_view(display_limit=10)["analysis"] is None
    save(tmp_path)
    save(tmp_path, "20261015", execution_admitted=True)
    with pytest.raises(HTTPException) as error:
        api.leader_environment_view(display_limit=10)
    assert error.value.status_code == 409
