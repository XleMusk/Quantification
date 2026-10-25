import json

from trading.research.f01_scoped_field_coverage import validate_receipt


def test_scoped_receipt_validates_source_hashes_lines_and_pit_boundary():
    result = validate_receipt()
    assert result["ok"] is True
    assert result["status"] == "PARTIAL_FIELD_EVIDENCE_PIT_PENDING"
    assert result["required_field_coverage"] == {
        "parent_net_profit": 3,
        "total_assets": 3,
        "joint": 3,
    }
    assert result["pit_verified"] is False
    assert result["factor_ready"] is False
    assert result["execution_admitted"] is False


def test_scoped_receipt_rejects_hash_or_pit_boundary_change(tmp_path):
    source = json.loads(open(validate_receipt()["receipt"], encoding="utf-8").read())
    source["rows_detail"][0]["report_source"]["text_sha256"] = "0" * 64
    source["pit_verified"] = True
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    result = validate_receipt(path)
    assert result["ok"] is False
    assert "pit_verified_must_remain_false" in result["errors"]
    assert "600004.SSE:text_sha256_mismatch" in result["errors"]

