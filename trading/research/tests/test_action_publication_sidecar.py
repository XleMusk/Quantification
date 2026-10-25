import json

import pandas as pd

from trading.research.action_publication_sidecar import build_sidecar


def test_sidecar_scans_saved_source_metadata_and_keeps_missing_retrieval_unknown(tmp_path):
    root = tmp_path / "actions"
    root.mkdir()
    source = root / "notice.pdf"
    source.write_bytes(b"saved-source")
    sha = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    pd.DataFrame([{
        "symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_file": "notice.pdf",
    }]).to_parquet(root / "canonical_actions.parquet", index=False)
    (root / "metadata.json").write_text(json.dumps({"rows": [{
        "symbol": "000001.SZSE", "dividOperateDate": "2026-01-30",
        "source_date": "2026-01-29", "source_file": "notice.pdf", "source_sha256": sha,
        "actual_cache_read_at": "2026-02-01T10:00:00+08:00",
    }]}), encoding="utf-8")
    result = build_sidecar(root)
    assert result["status"] == "REAL_SOURCE_MAPPED_PIT_BLOCKED"
    assert result["summary"]["rows_mapped"] == 1
    assert result["summary"]["source_files_present"] == 1
    assert result["summary"]["retrieved_at_rows"] == 0
    assert result["summary"]["artifact_retrieved_at_rows"] == 0
    assert result["summary"]["pit_safe_rows"] == 0
    assert result["rows"][0]["contract_evaluation"]["pit_asof_safe"] is False


def test_sidecar_deduplicates_root_and_audit_records(tmp_path):
    root = tmp_path / "actions"
    root.mkdir()
    source = root / "notice.pdf"
    source.write_bytes(b"saved-source")
    sha = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    pd.DataFrame([{"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_file": "notice.pdf"}]).to_parquet(root / "canonical_actions.parquet", index=False)
    row = {"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_date": "2026-01-29", "source_file": "notice.pdf", "source_sha256": sha}
    (root / "one.json").write_text(json.dumps(row), encoding="utf-8")
    (root / "two.json").write_text(json.dumps({"evidence": [row]}), encoding="utf-8")
    result = build_sidecar(root)
    assert result["summary"]["rows_mapped"] == 1


def test_sidecar_collapses_same_event_with_two_distinct_artifacts(tmp_path):
    root = tmp_path / "actions"
    root.mkdir()
    first = root / "notice-a.pdf"
    second = root / "notice-b.pdf"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    sha_a = __import__("hashlib").sha256(first.read_bytes()).hexdigest()
    sha_b = __import__("hashlib").sha256(second.read_bytes()).hexdigest()
    pd.DataFrame([{"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_file": "notice-a.pdf"}]).to_parquet(root / "canonical_actions.parquet", index=False)
    rows = [
        {"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_date": "2026-01-29", "source_file": "notice-a.pdf", "source_sha256": sha_a},
        {"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_date": "2026-01-29", "source_file": "notice-b.pdf", "source_sha256": sha_b},
    ]
    (root / "metadata.json").write_text(json.dumps(rows), encoding="utf-8")
    result = build_sidecar(root)
    assert result["summary"]["raw_rows_mapped"] == 2
    assert result["summary"]["rows_mapped"] == 1
    assert result["rows"][0]["source_artifact_count"] == 2
    assert len(result["rows"][0]["source_artifacts"]) == 2
    assert "contract_evaluation" in result["rows"][0]["source_artifacts"][0]


def test_sidecar_retains_artifact_timing_metadata_after_event_collapse(tmp_path):
    root = tmp_path / "actions"
    root.mkdir()
    source = root / "notice.pdf"
    source.write_bytes(b"saved-source")
    sha = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    pd.DataFrame([{"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_file": "notice.pdf"}]).to_parquet(root / "canonical_actions.parquet", index=False)
    row = {
        "symbol": "000001.SZSE", "dividOperateDate": "2026-01-30",
        "source_date": "2026-01-29", "source_file": "notice.pdf", "source_sha256": sha,
        "actual_retrieved_at": "2026-01-29T12:00:00+08:00",
        "revision_id": "rev-1",
    }
    (root / "metadata.json").write_text(json.dumps(row), encoding="utf-8")
    result = build_sidecar(root)
    artifact = result["rows"][0]["source_artifacts"][0]
    assert artifact["retrieved_at"] == "2026-01-29T12:00:00+08:00"
    assert artifact["retrieved_at_field"] == "actual_retrieved_at"
    assert artifact["revision_id"] == "rev-1"
    assert artifact["contract_evaluation"]["pit_asof_safe"] is False


def test_sidecar_uses_source_retrieved_at_only_as_explicit_source_metadata(tmp_path):
    root = tmp_path / "actions"
    root.mkdir()
    source = root / "notice.pdf"
    source.write_bytes(b"saved-source")
    sha = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    pd.DataFrame([{"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_file": "notice.pdf"}]).to_parquet(root / "canonical_actions.parquet", index=False)
    (root / "metadata.json").write_text(json.dumps({
        "symbol": "000001.SZSE", "dividOperateDate": "2026-01-30",
        "source_date": "2026-01-29", "source_file": "notice.pdf", "source_sha256": sha,
        "source_retrieved_at": "2026-01-29T12:00:00+08:00",
        "source_notice_id": "notice-1",
    }), encoding="utf-8")
    result = build_sidecar(root)
    row = result["rows"][0]
    assert row["retrieved_at"] == "2026-01-29T12:00:00+08:00"
    assert row["retrieved_at_field"] == "source_retrieved_at"
    assert result["summary"]["retrieved_at_rows"] == 1
    assert result["summary"]["pit_safe_rows"] == 0


def test_sidecar_maps_annual_input_receipt_as_lineage_only(tmp_path):
    root = tmp_path / "actions"
    root.mkdir()
    source = root / "notice.pdf"
    source.write_bytes(b"saved-source")
    sha = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    pd.DataFrame([{"symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_file": "notice.pdf"}]).to_parquet(
        root / "canonical_actions.parquet", index=False)
    (root / "input_input_receipts.json").write_text(json.dumps({
        "000001.SZSE": {"symbol": "000001.SZSE", "tasks": {
            "actions_2026": {"path": "actions/000001.SZSE/2026.csv", "retrieved_at": "2026-02-01T10:00:00+08:00",
                              "sha256": "bulk-sha", "origin": "closed_year_legacy_cache"}
        }}
    }), encoding="utf-8")
    (root / "metadata.json").write_text(json.dumps({
        "symbol": "000001.SZSE", "dividOperateDate": "2026-01-30", "source_date": "2026-01-29",
        "source_file": "notice.pdf", "source_sha256": sha,
    }), encoding="utf-8")
    result = build_sidecar(root)
    row = result["rows"][0]
    assert row["upstream_input_retrieved_at"] == "2026-02-01T10:00:00+08:00"
    assert row["upstream_input_origin"] == "closed_year_legacy_cache"
    assert result["summary"]["upstream_input_receipt_rows"] == 1
    assert result["summary"]["upstream_input_retrieved_at_rows"] == 1
    assert row["retrieved_at"] == ""
    assert row["contract_evaluation"]["pit_asof_safe"] is False
