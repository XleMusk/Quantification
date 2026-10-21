import json
from types import SimpleNamespace

import pandas as pd

from trading.research import issuer_annual_inputs as annual


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_reconciliation_preserves_conflict_unknowns_and_retrieval_time(tmp_path, monkeypatch):
    monkeypatch.setattr(annual, "ROOT", tmp_path / "repository")
    provider = tmp_path / "provider"
    write_json(provider / "result.json", {"source": annual.FINANCIAL_ENDPOINT, "execution_admitted": False})
    candidates = pd.DataFrame([
        {"symbol": "300750.SZSE", "report_day": "2025-12-31", "field": "total_assets",
         "status": "observed_numeric", "value_numeric_unscaled": 100, "publish_day": "2026-07-25"},
        {"symbol": "300750.SZSE", "report_day": "2025-12-31", "field": "revenue",
         "status": "observed_numeric", "value_numeric_unscaled": 99, "publish_day": "2026-03-10"},
        {"symbol": "300750.SZSE", "report_day": "2025-12-31", "field": "operating_cashflow",
         "status": "statement_unavailable", "value_numeric_unscaled": None, "publish_day": None},
    ])
    source_file = provider / "annual_denominator_fields.csv"
    candidates.to_csv(source_file, index=False)
    original = source_file.read_bytes()
    root = tmp_path / "primary"
    received_at = "2026-10-04T21:39:00+08:00"
    write_json(root / "result.json", {"id": "fixture", "source_pdf": annual.PDF_URL,
        "provider_cache": str(provider), "received_at": received_at})
    facts = [{"symbol": "300750.SZSE", "report_day": "2025-12-31", "field": field,
              "value_cny": value, "pdf_page_index": 0}
             for field, value in [("total_assets", 100), ("revenue", 101), ("operating_cashflow", 7)]]
    write_json(root / "extracted/facts.json", {"profile": "catl_2025_english_consolidated_v1", "facts": facts})

    result = annual.reconcile(root)
    inputs = pd.read_csv(root / "annual_inputs.csv")
    assert result["provider_exact_fields"] == result["provider_conflicts"] == result["primary_only_fields"] == 1
    assert inputs.reconciliation.tolist() == [
        "exact_same_value_as_cny", "provider_conflict", "primary_only_provider_unavailable"]
    assert inputs.provider_absolute_gap_cny.iloc[1] == 2
    assert pd.isna(inputs.provider_value_unscaled.iloc[2])
    assert inputs.available_to_system_at.eq(received_at).all()
    assert inputs.provider_publish_day.iloc[0] == "2026-07-25"
    assert not inputs.pit_verified.any() and not inputs.execution_admitted.any()
    assert source_file.read_bytes() == original


def test_collect_returns_completed_consumer_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(annual, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(annual.requests, "get", lambda *args, **kwargs: SimpleNamespace(
        content=b"%PDF-fixture", url=annual.PDF_URL, raise_for_status=lambda: None))
    monkeypatch.setattr(annual.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    completed = {"status": "completed_primary_input_consumer", "fields": 9}
    monkeypatch.setattr(annual, "reconcile", lambda root: completed)

    _, result = annual.collect(tmp_path / "provider", tmp_path / "pdf-python.exe")
    assert result is completed
