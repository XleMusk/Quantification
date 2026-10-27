from pathlib import Path

from trading import api
from trading.research.operator_blotter_view import build_view


def _namespace(monkeypatch, tmp_path):
    blotter = tmp_path / "blotter.csv"
    blotter.write_text(
        "preflight_status,route_status,lifecycle,side\n"
        "accepted,route_available,paper_staged_only,buy\n"
        "rejected,route_unavailable,paper_hold,sell\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "operator_package", lambda: {})
    monkeypatch.setattr(api, "_package_artifact", lambda _package, name: blotter if name == "blotter" else None)
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {
        "status": "unobserved",
        "ledger_mutated": False,
    })
    bundle = {"schema": "ths_same_ledger_bundle_reconciliation_v1", "status": "unobserved"}
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: bundle)
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda *_args: {
        "same_ledger_reconciliation_status": "unobserved",
    })


def test_blotter_view_projects_frozen_orders_without_execution(monkeypatch, tmp_path: Path):
    _namespace(monkeypatch, tmp_path)

    result = build_view(api.__dict__)

    assert result["schema"] == "offline_blotter_view_v1"
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False
    assert result["blotter"]["row_count"] == 2
    assert result["blotter"]["route_available_count"] == 1
    assert result["blotter"]["side_counts"] == {"buy": 1, "sell": 1}


def test_blotter_route_delegates_to_view(monkeypatch):
    expected = {"schema": "offline_blotter_view_v1", "status": "delegated"}
    monkeypatch.setattr("trading.research.operator_blotter_view.build_view", lambda namespace: expected)

    assert api.operator_package_blotter() == expected
