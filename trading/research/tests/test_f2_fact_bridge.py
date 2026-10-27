import hashlib
import json

from trading.research.f2_fact_bridge import BLOCKED, READY, build_fact_bridge
from trading.research.f2_source_snapshot import build_source_snapshot_receipt


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _historical(tmp_path, symbol="000001.SZSE", period="2024Q1"):
    path = tmp_path / "historical.json"
    path.write_text(json.dumps({
        "schema": "f2_historical_evidence_contract_v1",
        "status": "HISTORICAL_EVIDENCE_READY",
        "decision_day": "2024-05-01",
        "rows": [{
            "schema": "f2_historical_evidence_contract_v1",
            "status": "HISTORICAL_EVIDENCE_READY",
            "symbol": symbol,
            "period": period,
            "evidence_kind": "historical_archive",
            "pit_asof_safe": True,
            "reason_codes": [],
        }],
    }), encoding="utf-8")
    return path


def _source_snapshot_receipt(tmp_path, *, pit=True):
    source = tmp_path / "issuer.pdf"
    source.write_bytes(b"synthetic issuer source")
    row = {
        "symbol": "000001.SZSE",
        "period": "2024Q1",
        "publication_date": "2024-04-27",
        "revision_id": "issuer-000001-2024Q1-v1",
        "source_file": source.name,
        "source_sha256": _sha(source),
    }
    if pit:
        row.update({
            "retrieved_at": "2024-04-30T12:00:00+08:00",
            "asof_at": "2024-04-30T12:00:00+08:00",
        })
    snapshot = tmp_path / "source_snapshot.json"
    snapshot.write_text(json.dumps({
        "schema": "f2_source_snapshot_v1",
        "snapshot_id": "bridge-fixture-v1",
        "captured_at": "2024-04-30T12:01:00+08:00",
        "rows": [row],
    }), encoding="utf-8")
    receipt = tmp_path / "source_snapshot_receipt.json"
    receipt.write_text(json.dumps(build_source_snapshot_receipt(
        snapshot, decision_day="2024-05-01")), encoding="utf-8")
    return receipt


def _sidecar(tmp_path):
    source = tmp_path / "sidecar-report.pdf"
    source.write_bytes(b"synthetic sidecar source")
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(json.dumps({
        "schema": "f01_publication_sidecar_v1",
        "status": "REAL_SOURCE_MAPPED_PARTIAL_PIT",
        "execution_admitted": False,
        "decision_day": "2024-05-01",
        "rows": [{
            "symbol": "000001.SZSE", "period": "2024Q1", "stat_date": "2024-03-31",
            "publication_date": "2024-04-27", "parent_net_profit": "1000000",
            "total_assets": "50000000", "unit": "yuan", "currency": "CNY",
            "scope_group": "consolidated_parent",
            "retrieved_at": "2024-04-30T12:00:00+08:00",
            "asof_at": "2024-04-30T12:00:00+08:00",
            "revision_id": "issuer-000001-2024Q1-v1",
            "contract_evaluation": {
                "pit_asof_safe": True, "publication_evidence_reasons": [],
            },
            "source_file": str(source), "source_sha256": _sha(source),
        }],
    }), encoding="utf-8")
    return sidecar


def test_source_snapshot_adapter_receipt_feeds_fact_bridge(tmp_path):
    result = build_fact_bridge(
        _sidecar(tmp_path), historical_evidence=_source_snapshot_receipt(tmp_path))

    assert result["status"] == READY
    assert result["summary"]["fact_rows"] == 2
    assert result["summary"]["reason_counts"] == {}
    assert result["historical_evidence_schema"] == "f2_source_snapshot_adapter_v1"
    assert result["source_snapshot_id"] == "bridge-fixture-v1"
    assert result["rows"][0]["source_provenance"]["source_snapshot_id"] == "bridge-fixture-v1"
    assert result["rows"][0]["source_provenance"]["source_hash_verified"] is True
    assert all(fact["source_provenance"]["source_hash_verified"] is True
               for fact in result["rows"][0]["facts"])


def test_raw_source_snapshot_manifest_feeds_fact_bridge(tmp_path):
    _source_snapshot_receipt(tmp_path)
    manifest = tmp_path / "raw_source_snapshot.json"
    payload = json.loads((tmp_path / "source_snapshot.json").read_text(encoding="utf-8"))
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = build_fact_bridge(_sidecar(tmp_path), historical_evidence=manifest)

    assert result["status"] == READY
    assert result["summary"]["fact_rows"] == 2
    assert result["historical_evidence_schema"] == "f2_source_snapshot_v1"
    assert result["source_snapshot_id"] == "bridge-fixture-v1"


def test_source_snapshot_without_pit_fields_stays_blocked_in_fact_bridge(tmp_path):
    result = build_fact_bridge(
        _sidecar(tmp_path), historical_evidence=_source_snapshot_receipt(tmp_path, pit=False))

    assert result["status"] == BLOCKED
    assert result["summary"]["fact_rows"] == 0
    assert result["summary"]["reason_counts"]["historical:historical_retrieval_asof_unproven"] == 1


def test_wayback_probe_receipt_feeds_fact_bridge_and_stays_blocked(tmp_path):
    wayback = tmp_path / "wayback.json"
    wayback.write_text(json.dumps({
        "schema": "f2_wayback_probe_v1",
        "status": "BLOCKED_DATA",
        "execution_admitted": False,
        "historical_evidence": {
            "schema": "f2_historical_evidence_contract_v1",
            "status": "BLOCKED_DATA",
            "symbol": "000001.SZSE",
            "period": "2024Q1",
            "pit_asof_safe": False,
            "reason_codes": ["archive_capture_after_decision_only"],
        },
    }), encoding="utf-8")
    result = build_fact_bridge(_sidecar(tmp_path), historical_evidence=wayback)
    assert result["status"] == BLOCKED
    assert result["historical_evidence_schema"] == "f2_wayback_probe_v1"
    assert result["summary"]["fact_rows"] == 0


def test_real_sidecar_remains_blocked_with_machine_readable_pit_reasons():
    result = build_fact_bridge(
        "outputs/pipeline_receipts/20261025_f01_publication_sidecar_contract_v3.json",
        scoped_receipt="outputs/financials/f01_scoped_field_coverage_20261024.json",
    )

    assert result["status"] == BLOCKED
    assert result["execution_admitted"] is False
    assert result["summary"]["rows"] == 3
    assert result["summary"]["ready_rows"] == 0
    assert result["summary"]["fact_rows"] == 0
    assert result["summary"]["reason_counts"]["publication:retrieval_or_asof_after_decision"] == 3
    assert result["summary"]["reason_counts"]["scope_missing"] == 3


def test_pit_safe_explicit_metadata_materializes_two_standard_facts(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"synthetic source only")
    sidecar = tmp_path / "sidecar.json"
    row = {
        "symbol": "000001.SZSE",
        "period": "2024Q1",
        "stat_date": "2024-03-31",
        "publication_date": "2024-04-27",
        "parent_net_profit": "1000000",
        "total_assets": "50000000",
        "unit": "thousand_yuan",
        "currency": "CNY",
        "scope_group": "consolidated_parent",
        "retrieved_at": "2024-04-30T12:00:00+08:00",
        "asof_at": "2024-04-30T12:00:00+08:00",
        "revision_id": "issuer-000001-2024Q1-v1",
        "contract_evaluation": {
            "pit_asof_safe": True,
            "publication_evidence_reasons": [],
        },
        "source_file": str(source),
        "source_sha256": _sha(source),
    }
    sidecar.write_text(json.dumps({
        "schema": "f01_publication_sidecar_v1",
        "status": "REAL_SOURCE_MAPPED_PARTIAL_PIT",
        "execution_admitted": False,
        "decision_day": "2024-05-01",
        "rows": [row],
    }), encoding="utf-8")

    result = build_fact_bridge(sidecar, historical_evidence=_historical(tmp_path))

    assert result["status"] == READY
    assert result["summary"] == {
        "rows": 1,
        "ready_rows": 1,
        "blocked_rows": 0,
        "fact_rows": 2,
        "reason_counts": {},
    }
    facts = result["rows"][0]["facts"]
    assert {fact["metric_id"] for fact in facts} == {"parent_net_profit", "total_assets"}
    assert {fact["value"] for fact in facts} == {"1000000000", "50000000000"}
    assert all(fact["period_kind"] == "single_quarter" for fact in facts)
    assert all(fact["available_at"]["start"] == "2024-04-30T12:00:00+08:00" for fact in facts)
    assert result["factor_computed"] is False
    assert result["account_replay"] is False


def test_missing_historical_evidence_blocks_even_pit_flagged_row(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"synthetic source")
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(json.dumps({
        "schema": "f01_publication_sidecar_v1",
        "execution_admitted": False,
        "decision_day": "2024-05-01",
        "rows": [{
            "symbol": "000001.SZSE", "period": "2024Q1", "stat_date": "2024-03-31",
            "publication_date": "2024-04-27", "parent_net_profit": "10", "total_assets": "100",
            "unit": "yuan", "currency": "CNY", "scope_group": "consolidated_parent",
            "retrieved_at": "2024-04-30T12:00:00+08:00", "asof_at": "2024-04-30T12:00:00+08:00",
            "revision_id": "issuer-v1", "contract_evaluation": {"pit_asof_safe": True, "publication_evidence_reasons": []},
            "source_file": str(source), "source_sha256": _sha(source),
        }],
    }), encoding="utf-8")

    result = build_fact_bridge(sidecar)

    assert result["status"] == BLOCKED
    assert result["summary"]["fact_rows"] == 0
    assert "historical_evidence_missing" in result["rows"][0]["reason_codes"]


def test_cached_pit_flag_cannot_bypass_missing_row_level_evidence(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"synthetic source")
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(json.dumps({
        "schema": "f01_publication_sidecar_v1",
        "execution_admitted": False,
        "decision_day": "2024-05-01",
        "rows": [{
            "symbol": "000001.SZSE", "period": "2024Q1", "stat_date": "2024-03-31",
            "publication_date": "2024-04-27", "parent_net_profit": "10", "total_assets": "100",
            "unit": "yuan", "currency": "CNY", "scope_group": "consolidated_parent",
            "contract_evaluation": {"pit_asof_safe": True, "publication_evidence_reasons": []},
            "source_file": str(source), "source_sha256": _sha(source),
        }],
    }), encoding="utf-8")

    result = build_fact_bridge(sidecar)

    assert result["status"] == BLOCKED
    assert result["summary"]["fact_rows"] == 0
    reasons = result["rows"][0]["reason_codes"]
    assert "publication:retrieval_asof_invalid:retrieved_at must be an offset-aware ISO timestamp" in reasons
    assert "publication:revision_id_missing" in reasons


def test_unknown_unit_blocks_materialization_without_zero_fill(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"synthetic source")
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(json.dumps({
        "schema": "f01_publication_sidecar_v1",
        "execution_admitted": False,
        "rows": [{
            "symbol": "000001.SZSE", "period": "2024Q1", "stat_date": "2024-03-31",
            "publication_date": "2024-04-27", "parent_net_profit": "10", "total_assets": "100",
            "unit": "crore", "currency": "CNY", "scope_group": "consolidated_parent",
            "asof_at": "2024-04-30T12:00:00+08:00",
            "contract_evaluation": {"pit_asof_safe": True, "publication_evidence_reasons": []},
            "source_file": str(source), "source_sha256": _sha(source),
        }],
    }), encoding="utf-8")

    result = build_fact_bridge(sidecar, historical_evidence=_historical(tmp_path))

    assert result["status"] == BLOCKED
    assert result["summary"]["fact_rows"] == 0
    assert "parent_net_profit_invalid:Unknown financial unit; explicit source dictionary required" in result["rows"][0]["reason_codes"]
    assert "total_assets_invalid:Unknown financial unit; explicit source dictionary required" in result["rows"][0]["reason_codes"]
