from __future__ import annotations

from trading.research.f2_historical_evidence import (
    BLOCKED,
    READY,
    build_historical_gate_receipt,
    evaluate_historical_evidence,
)


def _row(**changes):
    row = {
        "publication_date": "2023-04-29",
        "retrieved_at": "2024-04-30T14:00:00+08:00",
        "asof_at": "2024-04-30T14:01:00+08:00",
        "revision_id": "cninfo-rev-1",
        "source_file": "000002.SZSE_2023Q1.pdf",
        "source_sha256": "a" * 64,
        "evidence_kind": "historical_archive",
    }
    row.update(changes)
    return row


def test_current_fetch_cannot_pass_historical_gate():
    result = evaluate_historical_evidence(
        _row(
            evidence_kind="current_network_fetch",
            retrieved_at="2026-10-26T02:22:03+08:00",
            asof_at=None,
            revision_id=None,
        ),
        "2024-05-01",
    )

    assert result["status"] == BLOCKED
    assert result["pit_asof_safe"] is False
    assert "historical_evidence_kind_missing_or_current" in result["reason_codes"]
    assert "historical_retrieval_asof_unproven" in result["reason_codes"]
    assert "revision_history_unproven" in result["reason_codes"]


def test_historical_archive_with_revision_is_ready_for_pit_gate():
    result = evaluate_historical_evidence(_row(), "2024-05-01")

    assert result["status"] == READY
    assert result["pit_asof_safe"] is True
    assert result["reason_codes"] == []


def test_asof_after_decision_stays_blocked():
    result = evaluate_historical_evidence(
        _row(asof_at="2024-05-02T09:00:00+08:00"),
        "2024-05-01",
    )

    assert result["status"] == BLOCKED
    assert "historical_retrieval_asof_unproven" in result["reason_codes"]


def test_batch_receipt_preserves_mixed_gate_results():
    receipt = build_historical_gate_receipt(
        [_row(), _row(evidence_kind="current_network_fetch", asof_at=None, revision_id=None)],
        decision_day="2024-05-01",
    )

    assert receipt["status"] == BLOCKED
    assert receipt["summary"] == {"rows": 2, "ready_rows": 1, "blocked_rows": 1}
    assert receipt["execution_admitted"] is False
