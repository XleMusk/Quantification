from pathlib import Path

from trading import api
from trading.research import operator_portfolio_views


def _namespace(monkeypatch, tmp_path):
    paths = {}
    for name, content in {
        "scenario_analysis": "scenario\nwhat_if\n",
        "allocation_settlement_projection": "row_type,settlement_state,t1_dependency\norder,ready,none\ntotal,,\n",
    }.items():
        path = tmp_path / f"{name}.csv"
        path.write_text(content, encoding="utf-8")
        paths[name] = path
    monkeypatch.setattr(api, "operator_package", lambda: {})
    monkeypatch.setattr(api, "_package_artifact", lambda _package, name: paths.get(name))
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {
        "status": "paper_only", "ledger_mutated": False,
    })
    bundle = {"schema": "ths_same_ledger_bundle_reconciliation_v1", "status": "no_complete_bundle"}
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: bundle)
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda _bundle, journal: {
        "same_ledger_reconciliation_status": _bundle["status"],
        "same_ledger_imported": False,
    })
    return api.__dict__


def test_controls_view_projects_frozen_paper_inputs(monkeypatch, tmp_path):
    result = operator_portfolio_views.build_controls_view(_namespace(monkeypatch, tmp_path))
    assert result["schema"] == "offline_portfolio_controls_view_v1"
    assert result["allocation"]["order_count"] == 1
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False


def test_risk_view_projects_saved_payload(monkeypatch, tmp_path):
    risk = tmp_path / "risk.json"
    risk.write_text(
        '{"schema":"offline_portfolio_risk_summary_v1","candidate_count":1,"rows":[{"strategy_id":"s1"}]}',
        encoding="utf-8",
    )
    namespace = _namespace(monkeypatch, tmp_path)
    namespace["_package_artifact"] = lambda _package, name: risk if name == "offline_portfolio_risk_summary" else None
    result = operator_portfolio_views.build_risk_view(namespace)
    assert result["schema"] == "offline_portfolio_risk_view_v1"
    assert result["candidate_count"] == 1
    assert result["rows"] == [{"strategy_id": "s1"}]
    assert result["execution_admitted"] is False


def test_api_routes_delegate_to_portfolio_views(monkeypatch):
    expected = {"schema": "delegated"}
    monkeypatch.setattr(operator_portfolio_views, "build_controls_view", lambda namespace: expected)
    monkeypatch.setattr(operator_portfolio_views, "build_risk_view", lambda namespace: expected)
    assert api.operator_package_portfolio_controls() == expected
    assert api.operator_package_portfolio_risk() == expected
