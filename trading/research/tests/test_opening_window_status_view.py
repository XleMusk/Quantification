from __future__ import annotations

import json
from pathlib import Path

from trading import api
from trading.research import opening_window_status_view as view


DAY = "2026-10-27"


def test_missing_receipt_is_explicit_and_read_only(tmp_path: Path) -> None:
    result = view.build_view(tmp_path, trading_date=DAY)
    assert result["schema"] == view.SCHEMA
    assert result["status"] == "not_started"
    assert result["execution_admitted"] is False
    assert result["client_interacted"] is False
    assert result["ledger_mutated"] is False


def test_saved_receipt_projects_payload_without_reauthorizing_execution(tmp_path: Path) -> None:
    path = tmp_path / "research" / "opening_session_runs" / DAY / "opening_window_coordinator.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema": "opening_window_coordinator_v1",
        "status": "paper_only",
        "execution_admitted": True,
        "client_interacted": True,
        "ledger_mutated": True,
        "submission_attempted": True,
        "manual_ths_prefill_only": True,
    }), encoding="utf-8")
    result = view.build_view(tmp_path, trading_date=DAY)
    assert result["schema"] == view.SCHEMA
    assert result["status"] == "paper_only"
    assert result["manual_ths_prefill_only"] is True
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False
    assert result["submission_attempted"] is False


def test_api_route_delegates_to_view(monkeypatch) -> None:
    expected = {"schema": view.SCHEMA, "status": "delegated"}
    monkeypatch.setattr(
        "trading.research.opening_window_status_view.build_view",
        lambda data_root: expected,
    )
    monkeypatch.setattr(api.settings, "DATA_ROOT", Path("C:/research"))
    assert api.opening_window_status() == expected
