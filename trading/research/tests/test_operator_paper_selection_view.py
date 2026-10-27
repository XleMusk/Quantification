from pathlib import Path

from trading import api
from trading.research.operator_paper_selection_view import build_view


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
