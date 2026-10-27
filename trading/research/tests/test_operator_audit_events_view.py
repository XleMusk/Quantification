import json
from pathlib import Path

from trading import api
from trading.research.operator_audit_events_view import build_view


def _namespace(monkeypatch, tmp_path: Path):
    event_log = tmp_path / "audit.csv"
    event_log.write_text(
        "state,event_type\n" "planned,target\n" "filled,fill\n", encoding="utf-8"
    )
    monkeypatch.setattr(api, "operator_package", lambda: {})
    monkeypatch.setattr(
        api,
        "_package_artifact",
        lambda _package, name: event_log if name == "paper_audit_event_log" else None,
    )
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {
        "status": "paper_only", "ledger_mutated": False,
    })
    bundle = {"schema": "ths_same_ledger_bundle_reconciliation_v1", "status": "unobserved"}
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: bundle)
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda *_args: {
        "same_ledger_reconciliation_status": "unobserved",
    })


def test_audit_events_view_projects_paper_log_without_broker_state(monkeypatch, tmp_path: Path):
    _namespace(monkeypatch, tmp_path)

    result = build_view(api.__dict__)

    assert result["schema"] == "offline_audit_event_log_view_v1"
    assert result["event_count"] == 2
    assert result["state_counts"] == {"planned": 1, "filled": 1}
    assert result["event_type_counts"] == {"target": 1, "fill": 1}
    assert result["execution_admitted"] is False
    assert result["broker_state_observed"] is False
    assert result["ledger_mutated"] is False


def test_audit_events_view_reads_jsonl_fallback(monkeypatch, tmp_path: Path):
    jsonl = tmp_path / "audit.jsonl"
    jsonl.write_text(json.dumps({"state": "planned", "event_type": "target"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(api, "operator_package", lambda: {})
    monkeypatch.setattr(
        api,
        "_package_artifact",
        lambda _package, name: jsonl if name == "paper_audit_event_log_jsonl" else None,
    )
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {"ledger_mutated": False})
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: {})
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda *_args: {})

    result = build_view(api.__dict__)

    assert result["event_count"] == 1
    assert result["source_files"]["jsonl"] == str(jsonl)


def test_audit_events_route_delegates_to_view(monkeypatch):
    expected = {"schema": "offline_audit_event_log_view_v1", "status": "delegated"}
    monkeypatch.setattr("trading.research.operator_audit_events_view.build_view", lambda namespace: expected)

    assert api.operator_package_audit_events() == expected
