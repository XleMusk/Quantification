"""The fixed-cohort endpoint reads evidence only and cannot start collection."""
import pytest
from fastapi import HTTPException

from trading import api
from trading.research import leader_cohort_observation as collector
from trading.research import leader_cohort_quote_view as view
from trading.research.tests.test_leader_cohort_quote_view import cohort_fixture, READ_NOW, dump


def snapshot(root):
    return {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}


def test_endpoint_reads_same_registry_without_network_or_mutation(cohort_fixture, monkeypatch):
    root = cohort_fixture["root"]
    monkeypatch.setattr(api.settings, "DATA_ROOT", root)
    original = view.leader_cohort_intraday_view
    monkeypatch.setattr(view, "leader_cohort_intraday_view", lambda data_root: original(data_root, now=READ_NOW))
    monkeypatch.setattr(collector, "run", lambda *a, **k: pytest.fail("Read endpoint cannot start a collector"))
    monkeypatch.setattr(collector.requests, "get", lambda *a, **k: pytest.fail("Read endpoint cannot contact quote provider"))
    before = snapshot(root)
    result = api.leader_cohort_intraday()
    assert result["registry"]["member_count"] == 3 and result["current_summary"]["unknown_count"] == 1
    assert result["provider_contacted"] is result["execution_admitted"] is result["registry_mutated"] is False
    assert snapshot(root) == before


def test_invalid_newest_cohort_returns_409_without_repair_or_fallback(cohort_fixture, monkeypatch):
    root = cohort_fixture["root"]
    monkeypatch.setattr(api.settings, "DATA_ROOT", root)
    pointer = cohort_fixture["pointer"]
    pointer["registry_sha256"] = "wrong registry"
    dump(cohort_fixture["pointer_path"], pointer)
    before = snapshot(root)
    with pytest.raises(HTTPException) as error:
        api.leader_cohort_intraday()
    assert error.value.status_code == 409
    assert snapshot(root) == before
