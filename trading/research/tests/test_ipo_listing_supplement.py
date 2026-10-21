"""Synthetic PDF facts test metadata timing; these are never market evidence."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from trading.contracts import RunContext, TradingMode
from trading.engine import _execution_listing_reason
from trading.research.ipo_qualification import IpoQualifiedResearchFeed, load_ipo_qualification
from trading.research.tests.fixtures.ipo_qualification import write_ipo_fixture


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash(path):
    return sha256(path.read_bytes()).hexdigest()


def _pdf(path, code="301569"):
    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(("BT /F1 12 Tf 20 700 Td (Synthetic Issuer Ltd; SYN; " + code + "; ChiNext; 2026-10-15) Tj ET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


@pytest.fixture
def bundle(tmp_path):
    legacy, master = write_ipo_fixture(tmp_path)
    calendar = tmp_path / "calendar.json"
    _write(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": [], "open_sessions": []})
    raw, receipt, supplement, bundle_path = [tmp_path / name for name in
        ("synthetic.pdf", "new_source.json", "new_issuer.json", "bundle.json")]
    _pdf(raw)
    _write(receipt, {"schema": "official_listing_supplement_source_receipt_v1", "status": "downloaded",
        "source_id": "issuer_listing_notice_301569", "url": "https://static.cninfo.com.cn/fixture.pdf",
        "final_url": "https://static.cninfo.com.cn/fixture.pdf", "raw_file": str(raw),
        "http_status": 200, "media_type": "application/pdf",
        "sha256": _hash(raw), "byte_count": raw.stat().st_size,
        "retrieved_at": "2026-10-15T12:00:00+08:00", "publication_date": "2026-10-14",
        "publication_time_known": False, "execution_admitted": False, "pit_verified": False,
        "historical_publication_archive_verified": False})
    flags = {"execution_admitted": False, "pit_verified": False,
        "corporate_actions_verified": False, "historical_publication_archive_verified": False}
    _write(supplement, {"schema": "research_ipo_listing_supplement_v1", "created_at": "2026-10-15T19:00:00+08:00",
        "base_evidence_sha256": _hash(legacy), "source_receipt": str(receipt),
        "source_receipt_sha256": _hash(receipt), "source_id": "issuer_listing_notice_301569",
        "instrument": {"symbol": "301569.SZSE", "issuer": "Synthetic Issuer Ltd", "name": "SYN",
            "board": "chinext", "ipo_date": "2026-10-15", "available_at": "2026-10-15T19:00:00+08:00",
            "rule_effective_from": "2026-07-06", "first_no_limit_sessions": 5, "following_price_limit_pct": 20},
        "source_assertions": {key: {"page_1based": 1, "quote": quote} for key, quote in
            {"issuer": "Synthetic Issuer Ltd", "name": "SYN", "symbol": "301569", "board": "ChiNext",
             "ipo_date": "2026-10-15"}.items()}, **flags})
    _write(bundle_path, {"schema": "research_ipo_listing_bundle_v1", "created_at": "2026-10-15T19:01:00+08:00",
        "base_evidence": str(legacy), "base_evidence_sha256": _hash(legacy),
        "supplements": [{"path": str(supplement), "sha256": _hash(supplement)}], **flags})
    return SimpleNamespace(path=bundle_path, legacy=legacy, master=master, calendar=calendar,
        raw=raw, receipt=receipt, supplement=supplement)


def _load(fixture):
    return load_ipo_qualification(fixture.path, fixture.calendar, master_run=fixture.master)


def _edit_supplement(fixture, modify):
    record = json.loads(fixture.supplement.read_text())
    modify(record)
    _write(fixture.supplement, record)
    root = json.loads(fixture.path.read_text())
    root["supplements"][0]["sha256"] = _hash(fixture.supplement)
    _write(fixture.path, root)


def test_actual_knowledge_clock_and_exact_listing_sessions_preserve_legacy(bundle):
    original_bytes = bundle.legacy.read_bytes()
    base = load_ipo_qualification(bundle.legacy, bundle.calendar, master_run=bundle.master)
    proof = _load(bundle)
    assert {key: value for key, value in proof.records.items() if key != "301569.SZSE"} == base.records
    assert proof.assess("301569.SZSE", "2026-10-15")["knowledge_status"] == "unknown"
    first = proof.assess("301569.SZSE", "2026-10-15", decision_as_of="2026-10-15T19:00:00+08:00")
    assert first["listing_session_ordinal"] == 1 and first["no_price_limit_first_five"] is True
    second = proof.assess("301569.SZSE", "2026-10-16", decision_as_of="2026-10-16T09:30:00+08:00")
    assert second["listing_session_ordinal"] == 2 and second["ipo_window_feature_support"] is False
    sixth = proof.assess("301569.SZSE", "2026-10-22")
    assert sixth["listing_session_ordinal"] == 6 and sixth["price_limit_pct"] == 20
    assert sixth["engine_block_reason"] is None
    assert sixth["pit_verified"] is sixth["execution_admitted"] is sixth["paper_admitted"] is False
    assert bundle.legacy.read_bytes() == original_bytes
    assert proof.verification["base_evidence"] == base.verification


def test_original_engine_replay_hook_blocks_new_listing_without_mutating_base(bundle):
    proof = _load(bundle)
    base = SimpleNamespace(instruments={"301569.SZSE": {"ipoDate": None}}, manifest={})
    before = deepcopy(base.instruments)
    feed = IpoQualifiedResearchFeed(base, proof)
    reason = _execution_listing_reason(feed, "301569.SZSE", "2026-10-16", RunContext("test", TradingMode.REPLAY))
    assert reason == "unsupported_first_five_listing_sessions" and base.instruments == before
    with pytest.raises(ValueError, match="replay-only"):
        _execution_listing_reason(feed, "301569.SZSE", "2026-10-16", RunContext("test", TradingMode.PAPER))


def test_iso_utc_Z_supplement_is_normalized_for_the_existing_qualification(bundle):
    _edit_supplement(bundle, lambda record: record.update(created_at="2026-10-15T11:00:00Z"))
    _edit_supplement(bundle, lambda record: record["instrument"].update(available_at="2026-10-15T11:00:00Z"))
    root = json.loads(bundle.path.read_text())
    root["created_at"] = "2026-10-15T11:01:00Z"
    _write(bundle.path, root)
    proof = _load(bundle)
    assert proof.records["301569.SZSE"]["available_at"] == "2026-10-15T19:00:00+08:00"
    assert proof.assess("301569.SZSE", "2026-10-15")["knowledge_status"] == "unknown"
    assert proof.assess("301569.SZSE", "2026-10-16")["listing_session_ordinal"] == 2


@pytest.mark.parametrize("field,value", [("ipo_date", "2026-10-16"), ("symbol", "301568.SZSE"),
    ("issuer", "A Different Issuer"), ("name", "OTHER")])
def test_metadata_change_cannot_relabel_saved_pdf(bundle, field, value):
    _edit_supplement(bundle, lambda record: record["instrument"].update({field: value}))
    with pytest.raises(ValueError, match="differs from the saved official PDF"):
        _load(bundle)


def test_publication_date_cannot_backfill_actual_knowledge(bundle):
    _edit_supplement(bundle, lambda record: record["instrument"].update(available_at="2026-10-14T23:59:59+08:00"))
    with pytest.raises(ValueError, match="actual curation"):
        _load(bundle)


def test_raw_source_tampering_and_unofficial_redirect_are_rejected(bundle):
    original = bundle.raw.read_bytes()
    bundle.raw.write_bytes(original[:-1] + b"x")
    with pytest.raises(ValueError, match="PDF bytes changed"):
        _load(bundle)
    bundle.raw.write_bytes(original)
    receipt = json.loads(bundle.receipt.read_text())
    receipt["final_url"] = "https://unofficial.example/redirect.pdf"
    _write(bundle.receipt, receipt)
    _edit_supplement(bundle, lambda record: record.update(source_receipt_sha256=_hash(bundle.receipt)))
    with pytest.raises(ValueError, match="Official downloaded"):
        _load(bundle)


def test_original_record_cannot_be_replaced_or_rule_relaxed(bundle):
    _edit_supplement(bundle, lambda record: record["instrument"].update(symbol="301660.SZSE"))
    with pytest.raises(ValueError, match="distinct missing"):
        _load(bundle)


def test_missing_pdf_assertion_cannot_be_admitted(bundle):
    _edit_supplement(bundle, lambda record: record["source_assertions"].pop("ipo_date"))
    with pytest.raises(ValueError, match="Each listing identity fact"):
        _load(bundle)


def test_multiple_new_issuers_keep_individual_knowledge_clocks(bundle):
    second_raw = bundle.raw.with_name("second.pdf")
    _pdf(second_raw, "301570")
    receipt = json.loads(bundle.receipt.read_text())
    receipt.update(source_id="issuer_listing_notice_301570", raw_file=str(second_raw),
        sha256=_hash(second_raw), byte_count=second_raw.stat().st_size)
    second_receipt = bundle.receipt.with_name("second_receipt.json")
    _write(second_receipt, receipt)
    supplement = json.loads(bundle.supplement.read_text())
    supplement.update(created_at="2026-10-15T19:00:30+08:00", source_receipt=str(second_receipt),
        source_receipt_sha256=_hash(second_receipt), source_id="issuer_listing_notice_301570")
    supplement["instrument"].update(symbol="301570.SZSE", available_at=supplement["created_at"])
    supplement["source_assertions"]["symbol"]["quote"] = "301570"
    second_supplement = bundle.supplement.with_name("second_supplement.json")
    _write(second_supplement, supplement)
    root = json.loads(bundle.path.read_text())
    root["supplements"].append({"path": str(second_supplement), "sha256": _hash(second_supplement)})
    _write(bundle.path, root)
    proof = _load(bundle)
    clock = "2026-10-15T19:00:15+08:00"
    assert proof.assess("301569.SZSE", "2026-10-15", decision_as_of=clock)["listing_session_ordinal"] == 1
    assert proof.assess("301570.SZSE", "2026-10-15", decision_as_of=clock)["knowledge_status"] == "unknown"
    assert proof.assess("301570.SZSE", "2026-10-16")["listing_session_ordinal"] == 2
    assert len(proof.verification["listing_supplements"]) == 2
