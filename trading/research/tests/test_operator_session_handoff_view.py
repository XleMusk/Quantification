from pathlib import Path

import pytest

from trading import api
from trading.research.operator_session_handoff_view import build_view


def test_session_handoff_requires_saved_paper_artifacts(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(api, "operator_package", lambda: {"strategy_id": "momentum"})
    monkeypatch.setattr(api, "_research_evidence_consumer_summary", lambda _: {"status": "research_only"})
    monkeypatch.setattr(api, "_latest_structured_observation_package", lambda: {})
    monkeypatch.setattr(api, "_package_artifact", lambda *_args: None)
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {"ledger_mutated": False})
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: {})
    monkeypatch.setattr(api, "paper_preview_readiness", lambda: {"status": "blocked"})

    with pytest.raises(api.HTTPException) as error:
        build_view(api.__dict__)
    assert error.value.status_code == 404
    assert "会话交接" in str(error.value.detail)


def test_session_handoff_route_delegates_to_application_view(monkeypatch):
    expected = {"schema": "offline_session_handoff_view_v1", "status": "delegated"}
    monkeypatch.setattr(
        "trading.research.operator_session_handoff_view.build_view",
        lambda namespace: expected,
    )

    assert api.operator_package_session_handoff() == expected
