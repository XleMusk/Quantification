"""Synthetic saved-reference upgrades; no market scan, replay or network."""
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.market_dataset import write_json
from trading.research.action_events import amend_distribution_reference


def setup_pair(tmp_path, *, fault=None, legacy_listing_absent=False):
    event, reference, reviewed = [tmp_path / name for name in ("events", "reference", "reviewed")]
    for path in (event, reference, reviewed / "supplements"):
        path.mkdir(parents=True)
    pdf = b"%PDF-synthetic-identical-issuer-terms"
    (reference / "original.pdf").write_bytes(pdf)
    (reviewed / "issuer-renamed.pdf").write_bytes(pdf)
    (reviewed / "exchange-rule.pdf").write_bytes(b"%PDF-synthetic-rule-4.4.2")
    original = {"code": "sz.000538", "symbol": "000538.SZSE", "dividRegistDate": "2026-06-23",
        "dividOperateDate": "2026-06-24", "dividPayDate": "2026-06-24", "dividStockMarketDate": "",
        "dividCashPsBeforeTax": "0.10", "dividStocksPs": "0", "dividReserveToStockPs": "0",
        "source_file": "original.pdf", "source_sha256": sha256(pdf).hexdigest(),
        "status": "added_missing_cash_dividend"}
    canonical = {key: value for key, value in original.items() if key not in {"source_file", "source_sha256", "status"}}
    if legacy_listing_absent:
        original.pop("dividStockMarketDate")
    pd.DataFrame([canonical]).to_parquet(event / "canonical_actions.parquet", index=False)
    write_json(event / "result.json", {"status": "research_action_events_projected", "execution_admitted": False,
        "start": "2026-01-05", "end": "2026-10-09", "files": {
            "canonical_actions.parquet": sha256((event / "canonical_actions.parquet").read_bytes()).hexdigest()}})
    if fault == "terms":
        original["dividCashPsBeforeTax"] = "0.11"
    elif fault == "formula":
        original["reference_adjustment_cash_ps"] = "0.05"
    other = {**original, "code": "sz.000539", "symbol": "000539.SZSE"}
    write_json(reference / "cash_reference_evidence.json", [original, other])
    write_json(reference / "distribution_reference_evidence.json", [])
    write_json(reference / "reviewed_special_references.json", [])
    pd.DataFrame([{**canonical, "date": canonical["dividOperateDate"], "previous_observed_close": 10., "preclose": 9.95,
        "has_reported_distribution": True, "reference_formula_status": "unresolved_blank_terms",
        "reference_formula_basis": "", "expected_reference_rmb": "", "reference_error_rmb": None}]).to_parquet(
            reference / "raw_reference_changes.parquet", index=False)
    write_json(reference / "result.json", {"status": "research_reference_presence_diagnostic", "execution_admitted": False,
        "event_view": str(event), "files": {p.name: sha256(p.read_bytes()).hexdigest() for p in reference.iterdir()}})
    row = {**canonical, "source_file": "issuer-renamed.pdf", "source_sha256": sha256(pdf).hexdigest(),
        "source_date": "2026-06-19", "source_url": "https://example.test/synthetic-issuer.pdf",
        "reference_adjustment_cash_ps": "0.05", "reference_adjustment_share_ratio": "0",
        "reference_source_file": "exchange-rule.pdf", "reference_source_sha256": sha256((reviewed / "exchange-rule.pdf").read_bytes()).hexdigest(),
        "reference_source_date": "2026-01-01", "reference_source_url": "https://example.test/synthetic-rule.pdf",
        "reference_source_pages": [1]}
    write_json(reviewed / "supplements/distribution_references.json", [row])
    return event, reference, reviewed, original, other


def test_explicit_upgrade_archives_original_cash_and_pins_review_without_changing_parent(tmp_path):
    event, reference, reviewed, original, other = setup_pair(tmp_path, legacy_listing_absent=True)
    saved = {path: path.read_bytes() for folder in (event, reference) for path in folder.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="Duplicate reference evidence"):
        amend_distribution_reference(event, reference, reviewed, tmp_path / "default")
    assert not (tmp_path / "default").exists()
    folder, result = amend_distribution_reference(event, reference, reviewed, tmp_path / "upgrade",
        upgrade_unformulated_cash=True)
    active = json.loads((folder / "cash_reference_evidence.json").read_text(encoding="utf-8"))
    assert active == [other]
    receipt = result["distribution_amendment"]["unformulated_cash_upgrade"]
    assert receipt["exact_upgraded_keys"] == [("sz.000538", "2026-06-24")]
    assert receipt["legacy_absent_fields_not_applicable"] == [{"code": "sz.000538", "dividOperateDate": "2026-06-24",
        "field": "dividStockMarketDate", "legacy_field_present": False,
        "reason": "explicit_zero_bonus_and_reserve_no_stock_listing_applicable"}]
    archive = folder / receipt["archive"]["file"]
    assert archive.read_bytes() == saved[reference / "cash_reference_evidence.json"]
    assert json.loads(archive.read_text(encoding="utf-8"))[0] == original
    assert "dividStockMarketDate" not in original
    assert result["files"][archive.name] == receipt["archive"]["sha256"]
    for name in (archive.name, "cash_reference_evidence.json", "distribution_reference_evidence.json", "issuer-renamed.pdf", "exchange-rule.pdf"):
        assert result["files"][name] == sha256((folder / name).read_bytes()).hexdigest()
    assert (folder / "original.pdf").read_bytes() == (folder / "issuer-renamed.pdf").read_bytes()
    assert pd.read_parquet(folder / "raw_reference_changes.parquet").reference_formula_status.tolist() == ["reference_match"]
    assert result["distribution_amendment"]["entitlements_changed"] is False
    assert all(path.read_bytes() == value for path, value in saved.items())


@pytest.mark.parametrize("fault,message", [("terms", "identical canonical amounts"), ("formula", "Existing reviewed cash reference")])
def test_upgrade_rejects_changed_cash_terms_or_existing_formula_before_copy(tmp_path, fault, message):
    event, reference, reviewed, _, _ = setup_pair(tmp_path, fault=fault)
    before = (reference / "cash_reference_evidence.json").read_bytes()
    with pytest.raises(ValueError, match=message):
        amend_distribution_reference(event, reference, reviewed, tmp_path / "forbidden", upgrade_unformulated_cash=True)
    assert not (tmp_path / "forbidden").exists()
    assert (reference / "cash_reference_evidence.json").read_bytes() == before
