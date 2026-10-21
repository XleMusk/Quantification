"""Focused synthetic missing-cash amendments; no market scan, Engine or network."""
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.market_dataset import write_json
from trading.research import action_reviewed_amendment as module
from trading.research.price_boundaries import EFFECTIVE, NEW, OLD, RENAME_SHA256


KEY = ("000538.SZSE", "2025-09-24")


def fixture_pair(tmp_path, monkeypatch):
    event, reference, supplement = [tmp_path / name for name in ("event", "reference", "supplement")]
    for path in (event, reference, supplement / "supplements"):
        path.mkdir(parents=True)
    monkeypatch.setattr(module, "DATA_ROOT", tmp_path / "derived")
    old = {"code": "sh.600000", "symbol": "600000.SSE", "dividOperateDate": "2025-07-01",
           "dividRegistDate": "2025-06-30", "dividPayDate": "2025-07-01", "dividStockMarketDate": "",
           "dividCashPsBeforeTax": "0.1", "dividStocksPs": "0", "dividReserveToStockPs": "0",
           "source_file": "", "source_url": "", "source_date": "", "source_code": "sh.600000",
           "source_symbol": "600000.SSE", "security_id": "sh.600000", "identity_origin": "unresolved_provider_code",
           "tradable_code": "sh.600000", "registration_code": "sh.600000", "payment_code": "sh.600000",
           "stock_listing_code": ""}
    pd.DataFrame([old]).to_parquet(event / "canonical_actions.parquet", index=False)
    pd.DataFrame([{"code": "sh.600000", "symbol": "600000.SSE"},
                  {"code": "sz.000538", "symbol": KEY[0]}]).to_csv(event / "source_input_universe.csv", index=False)
    write_json(event / "price_identity.json", {"rename": {"old": OLD, "new": NEW,
        "effective_date": EFFECTIVE, "published_date": "2025-02-14", "disclosure_sha256": RENAME_SHA256,
        "share_count_unchanged_on_rename": True, "pre_rename_forward_scale": "0.986265"}})
    write_json(event / "cash_supplements.json", [])
    (event / "input_coverage.csv").write_text("unaltered source coverage", encoding="utf-8")
    write_json(event / "result.json", {"status": "research_action_events_projected", "execution_admitted": False,
        "start": "2025-01-01", "end": "2025-12-31", "canonical_events": 1, "supplements_added": 0,
        "source_rows": 1, "queried": 2, "unqueried": 0,
        "files": {p.name: sha256(p.read_bytes()).hexdigest() for p in event.iterdir()}})
    shared = {"tradestatus": 1, "source_year_queried": True, "has_reviewed_special_reference": False,
              "reference_formula_basis": "", "expected_reference_rmb": "", "reference_error_rmb": None}
    existing = {**old, **shared, "date": old["dividOperateDate"], "previous_observed_close": 10., "preclose": 9.9,
                "close": 10., "reference_delta": -.1, "has_reported_distribution": True,
                "reference_formula_status": "reference_match"}
    missing = {**{field: None for field in old}, **shared, "code": "sz.000538", "date": KEY[1],
               "previous_observed_close": 57.93, "preclose": 56.91, "close": 57.69, "reference_delta": -1.02,
               "has_reported_distribution": False, "reference_formula_status": "missing_source_event"}
    pd.DataFrame([existing, missing]).to_parquet(reference / "raw_reference_changes.parquet", index=False)
    for name in ("cash_reference_evidence.json", "distribution_reference_evidence.json", "reviewed_special_references.json"):
        write_json(reference / name, [])
    (reference / "input_prices.json").write_text("unaltered raw-price pin", encoding="utf-8")
    write_json(reference / "result.json", {"status": "research_reference_presence_diagnostic", "execution_admitted": False,
        "event_view": str(event), "raw_reference_changes": 2,
        "files": {p.name: sha256(p.read_bytes()).hexdigest() for p in reference.iterdir()}})
    pdf = supplement / "supplements/notice.pdf"
    pdf.write_bytes(b"%PDF-synthetic-official-notice-no-reference-formula")
    row = {"symbol": KEY[0], "dividRegistDate": "2025-09-23", "dividOperateDate": KEY[1],
           "dividPayDate": KEY[1], "dividStockMarketDate": "", "dividCashPsBeforeTax": "1.019",
           "dividStocksPs": "0", "dividReserveToStockPs": "0", "source_date": "2025-09-17",
           "source_url": "https://static.cninfo.com.cn/finalpage/2025-09-17/1224680000.PDF",
           "source_file": "supplements/notice.pdf", "source_sha256": sha256(pdf.read_bytes()).hexdigest()}
    pd.DataFrame([row]).to_csv(supplement / "supplements/corporate_actions.csv", index=False)
    return event, reference, supplement, row


def test_add_preserves_parents_all_old_rows_raw_prices_and_absent_reference_formula(tmp_path, monkeypatch):
    event, reference, supplement, row = fixture_pair(tmp_path, monkeypatch)
    saved = {p: p.read_bytes() for root in (event, reference) for p in root.rglob("*") if p.is_file()}
    old_actions = pd.read_parquet(event / "canonical_actions.parquet")
    old_gaps = pd.read_parquet(reference / "raw_reference_changes.parquet")
    folder, refs, receipt = module.amend_missing_cash(event, reference, supplement, [KEY])
    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    actions = pd.read_parquet(folder / "canonical_actions.parquet")
    assert actions.iloc[:1][old_actions.columns].equals(old_actions)
    assert actions.iloc[1].source_code == actions.iloc[1].registration_code == "sz.000538"
    assert actions.iloc[1].dividCashPsBeforeTax == "1.019"
    assert (result["canonical_events"], result["supplements_added"]) == (2, 1)
    assert (result["source_rows"], result["queried"], result["unqueried"]) == (1, 2, 0)
    gaps = pd.read_parquet(refs / "raw_reference_changes.parquet")
    assert gaps.iloc[:1].equals(old_gaps.iloc[:1])
    raw = ["code", "date", "close", "preclose", "previous_observed_close", "reference_delta", "source_year_queried"]
    assert gaps[raw].equals(old_gaps[raw])
    assert gaps.iloc[1].has_reported_distribution and gaps.iloc[1].reference_formula_status == "unresolved_blank_terms"
    assert not receipt["raw_prices_rescanned"] and not receipt["pit_verified"]
    cash = json.loads((refs / "cash_reference_evidence.json").read_text(encoding="utf-8"))[0]
    assert "reference_adjustment_cash_ps" not in cash
    for root in (folder, refs):
        manifest = json.loads((root / "result.json").read_text(encoding="utf-8"))
        assert manifest["files"][cash["source_file"]] == row["source_sha256"]
        assert (root / cash["source_file"]).read_bytes() == (supplement / row["source_file"]).read_bytes()
    assert json.loads((refs / "input_events.json").read_text(encoding="utf-8")) == result
    assert all(p.read_bytes() == value for p, value in saved.items())
    calls = []
    monkeypatch.setattr(module, "amend_missing_cash", lambda *args: calls.append(args) or (folder, refs, receipt))
    assert module.main(["--event-view", str(event), "--reference-view", str(reference),
                        "--cash-supplement-root", str(supplement), "--only-event", ":".join(KEY)]) == 0
    assert len(calls) == 1 and calls[0][-1] == [list(KEY)]


def test_duplicate_or_existing_request_cannot_replace_a_frozen_event(tmp_path, monkeypatch):
    event, reference, supplement, _ = fixture_pair(tmp_path, monkeypatch)
    for keys, message in (([KEY, KEY], "Unique explicit"), ([("600000.SSE", "2025-07-01")], "existing event")):
        with pytest.raises(ValueError, match=message):
            module.amend_missing_cash(event, reference, supplement, keys)
    assert not (tmp_path / "derived").exists()


def test_unofficial_or_changed_pdf_is_rejected_before_publication(tmp_path, monkeypatch):
    for fault in ("url", "date", "sha"):
        event, reference, supplement, row = fixture_pair(tmp_path / fault, monkeypatch)
        row[{"url": "source_url", "date": "source_date", "sha": "source_sha256"}[fault]] = {
            "url": "https://example.test/unreviewed.pdf", "date": "2025-09-16", "sha": "0" * 64}[fault]
        pd.DataFrame([row]).to_csv(supplement / "supplements/corporate_actions.csv", index=False)
        with pytest.raises(ValueError):
            module.amend_missing_cash(event, reference, supplement, [KEY])
        assert not (tmp_path / fault / "derived").exists()


def test_non_cash_unknown_scope_or_conflicting_reference_cannot_be_appended(tmp_path, monkeypatch):
    for fault in ("stock", "unknown", "scope", "evidence"):
        event, reference, supplement, row = fixture_pair(tmp_path / fault, monkeypatch)
        if fault in {"stock", "unknown"}:
            row["dividStocksPs"] = "1" if fault == "stock" else "NaN"
            pd.DataFrame([row]).to_csv(supplement / "supplements/corporate_actions.csv", index=False)
        elif fault == "evidence":
            name = "distribution_reference_evidence.json"
            write_json(reference / name, [{"symbol": KEY[0], "code": "sz.000538", "dividOperateDate": KEY[1]}])
            manifest = json.loads((reference / "result.json").read_text(encoding="utf-8"))
            manifest["files"][name] = sha256((reference / name).read_bytes()).hexdigest()
            write_json(reference / "result.json", manifest)
        keys = [("000539.SZSE", KEY[1])] if fault == "scope" else [KEY]
        with pytest.raises(ValueError):
            module.amend_missing_cash(event, reference, supplement, keys)
        assert not (tmp_path / fault / "derived").exists()


def test_review_formula_and_quote_metadata_stays_evidence_only(tmp_path, monkeypatch):
    event, reference, supplement, row = fixture_pair(tmp_path, monkeypatch)
    metadata = {"reference_formula_status": "unconfirmed_no_literal_issuer_formula", "close": "999"}
    pd.DataFrame([{**row, **metadata}]).to_csv(supplement / "supplements/corporate_actions.csv", index=False)
    original = pd.read_parquet(reference / "raw_reference_changes.parquet")
    original_actions = pd.read_parquet(event / "canonical_actions.parquet")
    folder, refs, _ = module.amend_missing_cash(event, reference, supplement, [KEY])
    changed = pd.read_parquet(refs / "raw_reference_changes.parquet")
    assert changed.close.equals(original.close)
    assert changed.iloc[1].reference_formula_status == "unresolved_blank_terms"
    assert pd.read_parquet(folder / "canonical_actions.parquet").columns.tolist() == original_actions.columns.tolist()
    evidence = json.loads((refs / "cash_reference_evidence.json").read_text(encoding="utf-8"))[-1]
    assert all(evidence[field] == value for field, value in metadata.items())


def test_parquet_float_reference_error_keeps_schema_when_new_formula_is_unknown(tmp_path, monkeypatch):
    event, reference, supplement, _ = fixture_pair(tmp_path, monkeypatch)
    panel = reference / "raw_reference_changes.parquet"
    original = pd.read_parquet(panel)
    original["reference_error_rmb"] = pd.Series([0.001, float("nan")], dtype="float64")
    original.to_parquet(panel, index=False)
    manifest = json.loads((reference / "result.json").read_text(encoding="utf-8"))
    manifest["files"][panel.name] = sha256(panel.read_bytes()).hexdigest()
    write_json(reference / "result.json", manifest)
    _, refs, _ = module.amend_missing_cash(event, reference, supplement, [KEY])
    updated = pd.read_parquet(refs / panel.name)
    assert updated.dtypes.equals(original.dtypes)
    pd.testing.assert_frame_equal(updated.iloc[:1], original.iloc[:1])
    assert pd.isna(updated.iloc[1].reference_error_rmb)
    assert updated.iloc[1].reference_formula_status == "unresolved_blank_terms"
