from pathlib import Path

from trading import api
from trading.research.selected_plan_view import build_selected_plan_view


def _patch_no_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "operator_package", lambda: {"execution_day": "2026-10-27"})
    monkeypatch.setattr(api, "_latest_structured_observation_package", lambda: {})
    monkeypatch.setattr(api, "_latest_complete_structured_observation_package", lambda: {})
    monkeypatch.setattr(api, "_operator_package_session_root", lambda package: Path(tmp_path))
    monkeypatch.setattr(api, "_broker_capability_snapshot", lambda: {"ths": {"execution_admitted": False}})


def test_selected_plan_view_no_selection_is_read_only(monkeypatch, tmp_path):
    _patch_no_selection(monkeypatch, tmp_path)

    result = build_selected_plan_view(api.__dict__)

    assert result["schema"] == "offline_selected_plan_view_v1"
    assert result["status"] == "no_paper_strategy_selected"
    assert result["execution_admitted"] is False
    assert result["client_interacted"] is False
    assert result["target_orders"] == []


def test_selected_plan_route_delegates_to_view(monkeypatch):
    expected = {"schema": "offline_selected_plan_view_v1", "status": "delegated"}

    monkeypatch.setattr(
        "trading.research.selected_plan_view.build_selected_plan_view",
        lambda namespace: expected,
    )

    assert api.operator_package_selected_plan() == expected
