import hashlib
import json

from trading.research.f01_publication_sidecar import build_sidecar


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sidecar_maps_pdf_provenance_but_keeps_provider_retrieval_separate(tmp_path):
    root = tmp_path / "financial_run"
    checks = root / "field_checks"
    checks.mkdir(parents=True)
    pdf = checks / "000001.SZSE_2024Q1.pdf"
    text = checks / "000001.SZSE_2024Q1.txt"
    pdf.write_bytes(b"pdf-source")
    text.write_text("parent profit 10\ntotal assets 100\n", encoding="utf-8")
    scoped = tmp_path / "scoped.json"
    row = {
        "symbol": "000001.SZSE",
        "period": "2024Q1",
        "stat_date": "2024-03-31",
        "pub_date": "2024-04-27",
        "parent_net_profit": "10",
        "total_assets": "100",
        "parent_net_profit_source": {"text": str(text), "line_no": 1, "snippet": "parent profit 10"},
        "total_assets_source": {"text": str(text), "line_no": 2, "snippet": "total assets 100"},
        "report_source": {
            "url": "https://example.test/report.pdf",
            "pdf": str(pdf),
            "pdf_sha256": _sha(pdf),
            "text_sha256": _sha(text),
        },
        "source_checks": {
            "unit_explicit_as_rmb_yuan": True,
            "parent_field_explicit": True,
            "total_assets_explicit": True,
        },
        "pit": {"revision_safe_pit_verified": False},
    }
    scoped.write_text(json.dumps({
        "schema": "f01_scoped_field_coverage_receipt_v1",
        "status": "PARTIAL_FIELD_EVIDENCE_PIT_PENDING",
        "pit_verified": False,
        "revision_safe_pit_verified": False,
        "factor_ready": False,
        "execution_admitted": False,
        "single_quarter_input": True,
        "rows": 1,
        "required_field_coverage": {"parent_net_profit": 1, "total_assets": 1, "joint": 1},
        "rows_detail": [row],
    }), encoding="utf-8")
    (root / "quarterly").mkdir()
    (root / "quarterly" / "000001.SZSE_2024Q1.csv").write_text(
        "code,pubDate,statDate,netProfit\nsz.000001,2024-04-27,2024-03-31,12\n", encoding="utf-8")
    (root / "receipts.json").write_text(json.dumps({"tasks": {
        "000001.SZSE:2024Q1": {
            "file": "quarterly/000001.SZSE_2024Q1.csv",
            "retrieved_at": "2026-09-30T16:00:00+08:00",
        },
    }}), encoding="utf-8")

    result = build_sidecar(scoped, financial_run_root=root, decision_day="2024-05-01")
    assert result["status"] == "REAL_SOURCE_MAPPED_PIT_BLOCKED"
    assert result["summary"]["source_files_present"] == 1
    assert result["summary"]["source_text_files_present"] == 1
    assert result["summary"]["provider_retrieved_at_rows"] == 1
    assert result["summary"]["issuer_retrieved_at_rows"] == 0
    assert result["summary"]["asof_at_rows"] == 0
    assert result["summary"]["revision_id_rows"] == 0
    assert result["summary"]["provider_scope_mismatch_rows"] == 1
    assert result["summary"]["pit_safe_rows"] == 0
    row_out = result["rows"][0]
    assert row_out["contract_evaluation"]["pit_asof_safe"] is False
    assert row_out["provider_evidence"]["retrieved_at"] == "2026-09-30T16:00:00+08:00"
    assert row_out["retrieved_at"] == ""

    retrieval = tmp_path / "retrieval.json"
    retrieval.write_text(json.dumps({"rows": [{
        "symbol": "000001.SZSE", "period": "2024Q1",
        "retrieved_source_hash_matches_saved": True,
        "revision_id": "abc123",
        "retrieval": {"retrieved_at": "2026-10-24T12:00:01+08:00"},
    }]}), encoding="utf-8")
    merged = build_sidecar(scoped, financial_run_root=root, decision_day="2024-05-01",
                           retrieval_receipt=retrieval)
    merged_row = merged["rows"][0]
    assert merged["summary"]["retrieval_receipt_rows"] == 1
    assert merged_row["retrieved_at"] == "2026-10-24T12:00:01+08:00"
    assert merged_row["asof_at"] == "2026-10-24T12:00:01+08:00"
    assert merged_row["revision_id"] == "abc123"
    evaluation = merged_row["contract_evaluation"]
    assert evaluation["revision_identity_proven"] is True
    assert evaluation["retrieved_asof_proven"] is False
    assert "retrieval_or_asof_after_decision" in evaluation["publication_evidence_reasons"]
    assert "revision_id_missing" not in evaluation["publication_evidence_reasons"]
    assert merged_row["contract_evaluation"]["pit_asof_safe"] is False

