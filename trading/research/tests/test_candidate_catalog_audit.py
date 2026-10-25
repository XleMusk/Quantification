from pathlib import Path

from trading.research.candidate_catalog_audit import BLOCKED, VERIFIED, audit_catalog


def _catalog():
    return {
        "execution_admitted": False,
        "selection_policy": "mixed snapshots are not ranked",
        "entries": [{
            "id": "candidate_a", "family": "trend_momentum", "state": "DIAGNOSTIC",
            "comparability_group": "snapshot_a", "metrics": {"net_return": .1, "orders": 2},
            "evidence": ["evidence.json"], "limits": ["not PIT"],
        }],
    }


def test_catalog_audit_checks_real_evidence_and_preserves_closed_gate(tmp_path):
    (tmp_path / "evidence.json").write_text("{}", encoding="utf-8")
    result = audit_catalog(_catalog(), root=tmp_path, catalog_hash="abc")
    assert result["status"] == VERIFIED
    assert result["catalog_entries"] == 1
    assert result["evidence_references"] == 1
    assert result["execution_admitted"] is False
    assert result["selection_enabled"] is False


def test_catalog_audit_reports_missing_evidence_or_invalid_metrics(tmp_path):
    catalog = _catalog()
    catalog["entries"][0]["metrics"]["bad"] = float("nan")
    catalog["entries"][0]["evidence"] = ["missing.json"]
    result = audit_catalog(catalog, root=tmp_path)
    assert result["status"] == BLOCKED
    assert result["missing_evidence"] == ["candidate_a:missing.json"]
    assert "entry.1.metrics.bad" in result["issues"]
