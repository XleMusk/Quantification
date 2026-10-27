import json
from pathlib import Path

from trading import api
from trading.research.operator_package_view import build_view


def _namespace(monkeypatch, tmp_path: Path):
    package_path = tmp_path / "operator_package.json"
    package_path.write_text(json.dumps({
        "schema": "offline_operator_package_v1",
        "execution_day": "2099-01-02",
        "strategy_candidates": [],
    }), encoding="utf-8")
    monkeypatch.setattr(api, "_latest_operator_package_path", lambda: package_path)
    monkeypatch.setattr(api, "_latest_structured_observation_package", lambda: {
        "ready_for_ledger_reconciliation": False,
        "order_reconciliation": {"status": "unobserved", "order_count": 0},
    })
    monkeypatch.setattr(api, "_latest_complete_structured_observation_package", lambda: {
        "ready_for_ledger_reconciliation": False,
    })
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {
        "status": "paper_only", "ledger_mutated": False,
    })
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: {
        "status": "unobserved",
    })
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda *_args: {
        "same_ledger_reconciliation_status": "unobserved",
    })


def test_operator_package_view_enriches_saved_paper_package(monkeypatch, tmp_path: Path):
    _namespace(monkeypatch, tmp_path)

    result = build_view(api.__dict__)

    assert result["schema"] == "offline_operator_package_v1"
    assert result["package_freshness"] == "current_or_future_execution_day"
    assert result["order_reconciliation_status"] == "unobserved"
    assert result["execution_admitted"] is False
    assert result["client_interacted"] is False
    assert result["ledger_mutated"] is False


def test_operator_package_route_delegates_to_view(monkeypatch):
    expected = {"schema": "offline_operator_package_v1", "status": "delegated"}
    monkeypatch.setattr("trading.research.operator_package_view.build_view", lambda namespace: expected)

    assert api.operator_package() == expected
