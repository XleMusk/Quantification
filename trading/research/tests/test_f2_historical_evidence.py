from __future__ import annotations

import json

from trading.research.f2_historical_evidence import (
    BLOCKED,
    READY,
    build_historical_gate_receipt,
    evaluate_historical_evidence,
    load_input_rows,
    main,
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


def test_cli_loads_source_snapshot_and_preserves_missing_pit_fields(tmp_path):
    source = tmp_path / "source_snapshot.json"
    source.write_text(
        json.dumps({
            "schema": "f2_source_snapshot_v1",
            "snapshot_id": "real-source-fixture-v1",
            "capture_mode": "current_fetch",
            "rows": [{
                "symbol": "000002.SZSE",
                "period": "2023Q1",
                "publication_date": "2023-04-29",
                "retrieved_at": "2026-10-26T02:22:03+08:00",
                "asof_at": None,
                "revision_id": None,
                "source_file": "issuer.pdf",
                "source_sha256": "a" * 64,
                "capture_mode": "current_fetch",
            }],
        }), encoding="utf-8",
    )
    rows, description = load_input_rows(source)
    assert rows[0]["evidence_kind"] == "source_snapshot"
    assert description == "real-source-fixture-v1"
    output = tmp_path / "gate.json"
    assert main(["--input", str(source), "--decision-day", "2024-05-01", "--output", str(output)]) == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["status"] == BLOCKED
    assert receipt["summary"] == {"rows": 1, "ready_rows": 0, "blocked_rows": 1}
    assert "historical_retrieval_asof_unproven" in receipt["rows"][0]["reason_codes"]
    assert receipt["execution_admitted"] is False
