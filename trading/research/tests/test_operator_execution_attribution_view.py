import json
from pathlib import Path

from trading import api
from trading.research.operator_execution_attribution_view import build_view


def _namespace(monkeypatch, tmp_path: Path):
    attribution = tmp_path / "paper_execution_attribution.json"
    attribution.write_text(json.dumps({
        "status": "offline_reference_attribution",
        "parent_count": 2,
        "child_count": 3,
        "completed_parent_count": 1,
        "residual_parent_count": 1,
        "tca_metrics": {"mean_cost_bps": 4.0},
        "parents": [{"parent_id": "p1"}],
    }), encoding="utf-8")
    monkeypatch.setattr(api, "operator_package", lambda: {})
    monkeypatch.setattr(
        api, "_package_artifact",
        lambda _package, name: attribution if name == "paper_execution_attribution" else None,
    )
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {
        "status": "unobserved",
        "ledger_mutated": False,
    })
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "unobserved",
    })
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda *_args: {
        "same_ledger_reconciliation_status": "unobserved",
    })


def test_execution_attribution_view_preserves_frozen_tca_without_execution(monkeypatch, tmp_path: Path):
    _namespace(monkeypatch, tmp_path)

    result = build_view(api.__dict__)

    assert result["schema"] == "offline_execution_attribution_view_v1"
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False
    assert result["parent_count"] == 2
    assert result["child_count"] == 3
    assert result["tca_metrics"]["mean_cost_bps"] == 4.0


def test_execution_attribution_route_delegates_to_view(monkeypatch):
    expected = {"schema": "offline_execution_attribution_view_v1", "status": "delegated"}
    monkeypatch.setattr(
        "trading.research.operator_execution_attribution_view.build_view",
        lambda namespace: expected,
    )

    assert api.operator_package_execution_attribution() == expected
