import json

from trading.research.pipeline_provenance import validate_pipeline_provenance


def _result(tmp_path, *, admitted=False):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
    score = tmp_path / "scores.parquet"
    score.write_bytes(b"scores")
    batch = tmp_path / "batch.json"
    batch.write_text("{}", encoding="utf-8")
    events = [
        {"sequence": 1, "kind": "targets_generated", "day": "2026-10-01"},
        {"sequence": 2, "kind": "submitted", "day": "2026-10-02"},
        {"sequence": 3, "kind": "partial", "day": "2026-10-02"},
        {"sequence": 4, "kind": "filled", "day": "2026-10-02"},
    ]
    return {
        "schema": "test", "strategy_id": "test-v1", "raw_snapshot": str(snapshot),
        "score_path": str(score), "batch_run": str(batch), "selected_events": [{"day": "2026-10-01"}],
        "engine_ledger_replay_completed": True, "target_holdings_generated": True,
        "paper_orders_generated": True, "ledger_mutated": admitted, "execution_admitted": admitted,
        "strategy_validated": False, "ths_payload_generated": False,
        "daily": [{"equity": 101.0, "events": events}],
        "orders": [{"id": "o1"}], "fills": [{"order_id": "o1"}],
        "state": {"cash": 1.0, "positions": {"000001.SZSE": 10}, "orders": [{"id": "o1"}], "fills": [{"order_id": "o1"}]},
        "metrics": {"final_equity": 101.0},
    }


def test_saved_result_provenance_covers_all_shared_pipeline_stages(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_result(tmp_path)), encoding="utf-8")
    receipt = validate_pipeline_provenance(result_path)
    assert receipt["status"] == "VERIFIED_LOCAL_PIPELINE_PROVENANCE"
    assert all(receipt["stages"].values())
    assert receipt["execution_admitted"] is False
    assert receipt["event_count"] == 4


def test_provenance_blocks_admitted_or_tampered_stage_flags(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_result(tmp_path, admitted=True)), encoding="utf-8")
    receipt = validate_pipeline_provenance(result_path)
    assert receipt["status"] == "PIPELINE_PROVENANCE_BLOCKED"
    assert "execution_admitted_must_be_false" in receipt["issues"]
    assert "ledger_mutated_must_be_false" in receipt["issues"]


def test_comparison_binding_rejects_snapshot_manifest_hash_drift(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_result(tmp_path)), encoding="utf-8")
    comparison = tmp_path / "comparison.json"
    comparison.write_text(json.dumps({
        "execution_admitted": False,
        "comparison_contract": {"price_snapshot_manifest_sha256": "wrong"},
        "candidates": [{"result": str(result_path)}],
    }), encoding="utf-8")
    receipt = validate_pipeline_provenance(result_path, comparison)
    assert receipt["status"] == "PIPELINE_PROVENANCE_BLOCKED"
    assert "comparison_snapshot_manifest_hash_mismatch" in receipt["issues"]
