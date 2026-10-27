import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading import api
from trading.research.operator_paper_selection_view import apply_selection, build_view


def _namespace(monkeypatch, tmp_path, package):
    monkeypatch.setattr(api, "operator_package", lambda: package)
    monkeypatch.setattr(api, "_operator_package_session_root", lambda _package: Path(tmp_path))
    monkeypatch.setattr(api, "_package_artifact", lambda _package, _name: None)


def test_no_selection_is_explicit_and_read_only(monkeypatch, tmp_path):
    _namespace(monkeypatch, tmp_path, {"execution_day": "2026-10-27"})

    result = build_view(api.__dict__)

    assert result == {
        "schema": "offline_paper_selection_view_v1",
        "status": "no_paper_strategy_selected",
        "strategy_id": None,
        "receipt": None,
        "capacity": None,
    }


def test_stale_selection_is_not_projected(monkeypatch, tmp_path):
    _namespace(monkeypatch, tmp_path, {"execution_day": "2026-10-27"})
    (tmp_path / "paper_strategy_application_receipt_v1.json").write_text(
        '{"schema":"paper_strategy_application_receipt_v1",'
        '"execution_day":"2026-10-26","strategy_id":"candidate_a"}',
        encoding="utf-8",
    )

    result = build_view(api.__dict__)

    assert result["status"] == "stale_paper_strategy_selection"
    assert result["strategy_id"] is None
    assert result["receipt"] is None


def test_route_delegates_to_read_only_view(monkeypatch):
    expected = {"schema": "offline_paper_selection_view_v1", "status": "delegated"}
    monkeypatch.setattr(
        "trading.research.operator_paper_selection_view.build_view",
        lambda namespace: expected,
    )

    assert api.operator_package_paper_selection() == expected


def test_apply_selection_runs_only_frozen_candidate_script(monkeypatch, tmp_path):
    opening_pack = tmp_path / "opening_pack.json"
    opening_pack.write_text("{}", encoding="utf-8")
    session = tmp_path / "session"
    package = {
        "package_freshness": "fresh",
        "strategy_candidates": [{"strategy_id": "candidate_a"}],
        "opening_pack": str(opening_pack),
    }
    calls = []
    monkeypatch.setattr(api, "operator_package", lambda: package)
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(api, "_operator_package_session_root", lambda _package: session)

    class Result:
        returncode = 0
        stdout = "paper-only"
        stderr = ""

    monkeypatch.setattr(
        "trading.research.operator_paper_selection_view.subprocess.run",
        lambda command, **kwargs: (calls.append((command, kwargs)) or Result()),
    )
    receipt = {"schema": "paper_strategy_application_receipt_v1", "strategy_id": "candidate_a"}
    session.mkdir()
    (session / "paper_strategy_application_receipt_v1.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    result = apply_selection(api.__dict__, SimpleNamespace(strategy_id="candidate_a"))
    assert result["status"] == "offline_paper_strategy_applied"
    assert result["execution_admitted"] is False
    assert result["submission_enabled"] is False
    assert calls and "apply_offline_strategy_selection.ps1" in calls[0][0][5]


def test_apply_selection_rejects_unknown_candidate(monkeypatch, tmp_path):
    package = {"package_freshness": "fresh", "strategy_candidates": []}
    monkeypatch.setattr(api, "operator_package", lambda: package)
    with pytest.raises(Exception) as error:
        apply_selection(api.__dict__, SimpleNamespace(strategy_id="unknown"))
    assert getattr(error.value, "status_code", None) == 400
