"""Exact reviewed BOC document provenance; no network or historical replay."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from trading import market_snapshot as snapshot


URL = "https://pic.bankofchina.com/bocappd/report/202504/P020250420624888698809.pdf"
PIN = "cad43128d472f79061ec4a9318e5f49ed1c22827d1ddb5fe68f2e668ba61bc1e"


def source_row():
    return {"kind": "issuer_confirmed_cash_only_terms", "symbol": "601988.SSE",
        "source_url": URL, "source_date": "2025-04-20", "source_notice_id": "\u4e342025-031",
        "source_channel": "issuer_official_website", "source_sha256": PIN,
        "reserve_numeric_zero_explicit_in_notice": False, "complete_cash_only_scheme_projection": True,
        "pit_verified": False}


def test_exact_reviewed_issuer_document_is_accepted_without_changing_term_interpretation(tmp_path):
    row = source_row()
    before = deepcopy(row)
    assert snapshot.REVIEWED_ISSUER_DOCUMENTS[URL]["source_sha256"] == PIN
    assert snapshot.reviewed_cash_notice_source(row, tmp_path) is True
    assert row == before
    assert row["reserve_numeric_zero_explicit_in_notice"] is False and row["pit_verified"] is False


def test_wrong_document_issuer_date_hash_channel_or_explicit_zero_claim_is_rejected(tmp_path):
    for field, value in (
        ("source_url", URL.replace("624888698809", "624888698810")),
        ("source_url", URL + "?copy=1"),
        ("source_url", URL.replace("https://", "http://")),
        ("symbol", "601398.SSE"), ("source_date", "2025-04-21"),
        ("source_notice_id", "\u4e342025-032"), ("source_sha256", "0" * 64),
        ("source_channel", "issuer_mirrored_exchange_notice"),
        ("kind", "issuer_confirmed_reserve_zero_term"),
        ("reserve_numeric_zero_explicit_in_notice", True),
        ("complete_cash_only_scheme_projection", False), ("pit_verified", True),
    ):
        row = {**source_row(), field: value}
        assert snapshot.reviewed_cash_notice_source(row, tmp_path) is False, (field, value)


def test_existing_resolution_loader_still_binds_source_pdf_bytes(tmp_path, monkeypatch):
    # Synthetic bytes need a synthetic hash pin; the first test checks the
    # production exact-document registration. No real curation is read or changed.
    raw = b"%PDF-1.4\nSynthetic reviewed issuer document fixture\n"
    fixture_hash = sha256(raw).hexdigest()
    monkeypatch.setitem(snapshot.REVIEWED_ISSUER_DOCUMENTS, URL,
        {**snapshot.REVIEWED_ISSUER_DOCUMENTS[URL], "source_sha256": fixture_hash})
    source = tmp_path / "fixture.pdf"
    source.write_bytes(raw)
    expected = {field: "" for field in snapshot.RESERVE_CORRECTION_FIELDS}
    expected.update(code="sh.601988", dividRegistDate="2025-04-24", dividPayDate="2025-04-25",
        dividCashPsBeforeTax="0.1216", dividStocksPs="0.000000", dividCashStock="10\u6d3e1.216\u5143")
    row = {**source_row(), "source_file": source.name, "source_sha256": fixture_hash,
        "code": "sh.601988", "dividRegistDate": "2025-04-24", "dividOperateDate": "2025-04-25",
        "dividPayDate": "2025-04-25", "dividStockMarketDate": "", "dividCashPsBeforeTax": "0.1216",
        "dividStocksPs": "0", "dividReserveToStockPs": "0", "expected_provider_terms": expected,
        "review_status": "manual_fulltext_and_pages_reviewed"}
    supplements = tmp_path / "supplements"
    supplements.mkdir()
    (supplements / "provider_action_resolutions.json").write_text(json.dumps([row]), encoding="utf-8")
    result = snapshot.load_action_resolutions(tmp_path, ["601988.SSE"])
    assert len(result) == 1 and result[0]["reserve_numeric_zero_explicit_in_notice"] is False
    assert result[0]["expected_provider_terms"]["dividReserveToStockPs"] == ""
    source.write_bytes(raw + b"changed")
    with pytest.raises(ValueError, match="source hash/PDF changed"):
        snapshot.load_action_resolutions(tmp_path, ["601988.SSE"])


def test_reviewed_mirror_notice_catalog_binds_300642_cash_only_replay_source(tmp_path):
    url = "https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESZ_STOCK/2026/2026-8/2026-08-27/12560657.PDF"
    row = {
        "kind": "issuer_confirmed_reserve_zero_term",
        "symbol": "300642.SZSE",
        "source_url": url,
        "source_date": "2026-08-27",
        "source_channel": "issuer_mirrored_exchange_notice",
    }
    assert snapshot.reviewed_cash_notice_source(row, tmp_path) is True
    row["source_date"] = "2026-08-28"
    assert snapshot.reviewed_cash_notice_source(row, tmp_path) is False
