import json
from pathlib import Path

from trading import api
from trading.research.operator_execution_evidence_view import build_view


def _namespace(monkeypatch, tmp_path: Path):
    paths = {}
    contents = {
        "paper_execution_cost_stress": "strategy_id,additional_slippage_bps,estimated_total_execution_cost_rmb,synthetic_filled_notional_rmb\ns1,0,1.2,1000\ns1,50,2.0,1000\n",
        "tca_pretrade": "metric,value\nestimated_all_in_cost_rmb,1.2\n",
        "risk_checks": "strategy_id,check,status\ns1,execution_scope,review\n",
        "child_order_schedule": "strategy_id,parent_sequence,child_sequence\ns1,1,1\n",
        "paper_child_order_reconciliation": "strategy_id,reconciliation_status,parent_execution_status\ns1,pass_synthetic_complete,synthetic_full_fill\n",
    }
    for name, content in contents.items():
        path = tmp_path / f"{name}.csv"
        path.write_text(content, encoding="utf-8")
        paths[name] = path
    summary = tmp_path / "paper_child_order_reconciliation_summary.json"
    summary.write_text(json.dumps({
        "schema": "paper_child_order_reconciliation_v1",
        "status": "completed_synthetic",
        "parent_count": 1,
        "scheduled_parent_count": 1,
        "complete_synthetic_parent_count": 1,
        "rejected_parent_count": 0,
        "mismatch_count": 0,
        "real_broker_state": False,
        "execution_admitted": False,
    }), encoding="utf-8")
    paths["paper_child_order_reconciliation_summary"] = summary
    monkeypatch.setattr(api, "operator_package", lambda: {
        "strategy_candidates": [{"strategy_id": "s1", "net_return": 0.01}],
    })
    monkeypatch.setattr(api, "_package_artifact", lambda _package, name: paths.get(name))
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {
        "status": "paper_only", "ledger_mutated": False,
    })
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: {
        "schema": "ths_same_ledger_bundle_reconciliation_v1", "status": "unobserved",
    })
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda _bundle, _journal: {
        "same_ledger_reconciliation_status": "unobserved",
    })
    monkeypatch.setattr(api, "_broker_observation_alerts", lambda _bundle: {
        "status": "no_observation", "alerts": [],
    })
    monkeypatch.setattr(api, "_observed_fill_tca_summary", lambda _bundle: {
        "status": "no_observed_fill",
    })
    return api.__dict__


def test_execution_evidence_view_preserves_paper_diagnostics(monkeypatch, tmp_path: Path):
    result = build_view(_namespace(monkeypatch, tmp_path))

    assert result["schema"] == "offline_execution_evidence_view_v2"
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False
    assert result["risk_checks"]["attention_row_count"] == 1
    assert result["child_order_reconciliation"]["complete_synthetic_parent_count"] == 1
    assert result["strategy_execution_summary"][0]["strategy_id"] == "s1"


def test_execution_evidence_route_delegates_to_view(monkeypatch):
    expected = {"schema": "offline_execution_evidence_view_v2", "status": "delegated"}
    monkeypatch.setattr(
        "trading.research.operator_execution_evidence_view.build_view",
        lambda namespace: expected,
    )

    assert api.operator_package_execution_evidence() == expected
