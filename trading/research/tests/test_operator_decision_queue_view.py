import json

import pytest

from trading.research.operator_decision_queue_view import (
    UnsupportedDecisionQueue,
    build_view,
)


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_queue_view_requires_supported_artifact(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_view({}, lambda _package, _name: None, {}, {}, lambda *_: {})


def test_queue_view_preserves_reconciliation_and_disables_retry(tmp_path):
    queue = _write(tmp_path / "queue.json", {
        "schema": "offline_operator_decision_queue_v1",
        "rows": [{"strategy_id": "fixed", "execution_admitted": "true"}],
    })
    journal = {"status": "paper_only", "ledger_mutated": False}
    bundle = {"status": "isolated_rehearsal_only", "ledger_mutated": False}
    result = build_view(
        {}, lambda _package, _name: queue, journal, bundle,
        lambda _bundle, _journal: {"same_ledger_reconciliation_status": "paper_only"},
    )
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False
    assert result["same_ledger_reconciliation_status"] == "paper_only"
    assert result["rows"][0]["execution_admitted"] == "false"
    assert result["recovery_handoffs"] == []


def test_queue_view_rejects_unknown_schema(tmp_path):
    queue = _write(tmp_path / "queue.json", {"schema": "old"})
    with pytest.raises(UnsupportedDecisionQueue):
        build_view({}, lambda _package, _name: queue, {}, {}, lambda *_: {})
