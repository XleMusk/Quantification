from __future__ import annotations

import hashlib
import json

import pytest

from trading.research.f2_source_snapshot import (
    BLOCKED,
    build_source_snapshot_receipt,
    load_source_snapshot,
)


def _snapshot(tmp_path, *, row_changes=None):
    source = tmp_path / "issuer.pdf"
    source.write_bytes(b"issuer source bytes")
    row = {
        "symbol": "000002.SZSE",
        "period": "2023Q1",
        "publication_date": "2023-04-29",
        "retrieved_at": "2024-04-30T14:00:00+08:00",
        "asof_at": "2024-04-30T14:01:00+08:00",
        "revision_id": "issuer-rev-1",
        "source_file": "issuer.pdf",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    row.update(row_changes or {})
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({
        "schema": "f2_source_snapshot_v1",
        "snapshot_id": "snapshot-2024-04-30-v1",
        "captured_at": "2024-04-30T14:02:00+08:00",
        "rows": [row],
    }), encoding="utf-8")
    return path


def test_source_snapshot_hash_and_historical_gate_pass(tmp_path):
    receipt = build_source_snapshot_receipt(_snapshot(tmp_path), decision_day="2024-05-01")
    assert receipt["status"] == "HISTORICAL_EVIDENCE_READY"
    assert receipt["source_checks"] == {"verified": 1, "failed": 0}
    assert receipt["historical_gate"]["summary"]["ready_rows"] == 1
    assert receipt["execution_admitted"] is False


def test_source_snapshot_hash_mismatch_blocks_without_inference(tmp_path):
    snapshot = _snapshot(tmp_path, row_changes={"source_sha256": "0" * 64})
    receipt = build_source_snapshot_receipt(snapshot, decision_day="2024-05-01")
    assert receipt["status"] == BLOCKED
    assert receipt["source_checks"] == {"verified": 0, "failed": 1}
    assert "historical_source_provenance_unproven" in receipt["historical_gate"]["rows"][0]["reason_codes"]


def test_current_issuer_snapshot_stays_blocked_when_pit_fields_missing(tmp_path):
    snapshot = _snapshot(tmp_path, row_changes={"retrieved_at": None, "asof_at": None, "revision_id": None})
    receipt = build_source_snapshot_receipt(snapshot, decision_day="2024-05-01")
    assert receipt["status"] == BLOCKED
    assert receipt["source_checks"] == {"verified": 1, "failed": 0}
    reasons = receipt["historical_gate"]["rows"][0]["reason_codes"]
    assert "historical_retrieval_asof_unproven" in reasons
    assert "revision_history_unproven" in reasons


def test_invalid_snapshot_schema_is_rejected(tmp_path):
    path = _snapshot(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = "current_fetch"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported F2 source snapshot schema"):
        load_source_snapshot(path)
