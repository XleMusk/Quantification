"""Explicit new-issuer metadata with actual retrieval/curation clocks.

This adds missing issuers to the existing typed qualification. It does not edit
the old evidence, replace master records, or certify historical publication.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from pypdf import PdfReader

from .ipo_qualification import (
    IpoQualification, _canonical_day, _digest, _load_legacy_ipo_qualification, _time as _legacy_time,
)


BUNDLE_SCHEMA = "research_ipo_listing_bundle_v1"
SUPPLEMENT_SCHEMA = "research_ipo_listing_supplement_v1"
SOURCE_SCHEMA = "official_listing_supplement_source_receipt_v1"
FACTS = {"issuer", "name", "symbol", "board", "ipo_date"}
FALSE_FLAGS = ("execution_admitted", "pit_verified", "corporate_actions_verified",
               "historical_publication_archive_verified")


def _time(value):
    # Python 3.10's legacy reader accepts explicit offsets, but not ISO UTC Z.
    return _legacy_time(value[:-1] + "+00:00" if isinstance(value, str) and value.endswith("Z") else value)


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _boundary(record, schema):
    if (record.get("schema") != schema or any(record.get(key) is not False for key in FALSE_FLAGS)):
        raise ValueError("Explicit unadmitted listing metadata boundaries required")


def _official(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and any(
        host == domain or host.endswith("." + domain) for domain in ("szse.cn", "cninfo.com.cn"))


def _compact(value):
    return re.sub(r"\s+", "", value)


def _fact_tokens(record):
    day = _canonical_day(record["ipo_date"])
    parsed = datetime.strptime(day, "%Y-%m-%d")
    return {"issuer": [record["issuer"]], "name": [record["name"]],
        "symbol": [record["symbol"].split(".")[0]], "board": ["创业板", "ChiNext"],
        "ipo_date": [day, f"{parsed.year}年{parsed.month}月{parsed.day}日",
                     f"{parsed.year}年{parsed.month:02d}月{parsed.day:02d}日"]}


def _source(evidence, instrument):
    receipt_path = Path(evidence["source_receipt"]).resolve()
    if _digest(receipt_path) != evidence["source_receipt_sha256"]:
        raise ValueError("Listing supplement receipt changed")
    receipt = _json(receipt_path)
    if (receipt.get("schema") != SOURCE_SCHEMA or receipt.get("execution_admitted") is not False
            or receipt.get("pit_verified") is not False
            or receipt.get("status") not in ("downloaded", "downloaded_and_fact_reviewed")
            or receipt.get("http_status") != 200 or receipt.get("media_type", "").split(";")[0] != "application/pdf"
            or receipt.get("historical_publication_archive_verified") is not False
            or not _official(receipt["url"]) or not _official(receipt["final_url"])
            or receipt.get("publication_time_known") is not False):
        raise ValueError("Official downloaded listing source and unknown publication time required")
    retrieved = _time(receipt["retrieved_at"])
    publication = _canonical_day(receipt["publication_date"])
    if retrieved.date().isoformat() < publication:
        raise ValueError("Listing source retrieval precedes its stated publication date")
    raw = Path(receipt["raw_file"]).resolve()
    if (raw.stat().st_size != receipt["byte_count"] or _digest(raw) != receipt["sha256"]
            or raw.read_bytes()[:5] != b"%PDF-"):
        raise ValueError("Listing source PDF bytes changed or invalid")
    assertions = evidence["source_assertions"]
    if (not isinstance(assertions, dict) or set(assertions) != FACTS
            or evidence.get("source_id") != receipt["source_id"]):
        raise ValueError("Each listing identity fact must bind its official source")
    reader, pages = PdfReader(raw), {}
    for fact, tokens in _fact_tokens(instrument).items():
        assertion = assertions[fact]
        page, quote = assertion.get("page_1based"), assertion.get("quote")
        if (type(page) is not int or page < 1 or page > len(reader.pages)
                or not isinstance(quote, str) or not 1 <= len(quote) <= 300):
            raise ValueError("Listing fact requires an exact PDF page and short literal excerpt")
        if page not in pages:
            pages[page] = _compact(reader.pages[page - 1].extract_text() or "")
        literal = _compact(quote)
        if literal not in pages[page] or not any(_compact(token) in literal for token in tokens):
            raise ValueError(f"Curated {fact} differs from the saved official PDF excerpt")
    reviewed = _time(receipt["fact_reviewed_at"]) if receipt.get("status") == "downloaded_and_fact_reviewed" else retrieved
    if reviewed < retrieved:
        raise ValueError("Listing fact review precedes its actual raw retrieval")
    return receipt, receipt_path, max(retrieved, reviewed), sorted(pages)


def load_listing_bundle(evidence_path, calendar_path, *, master_run=None):
    """Load a pinned legacy base plus distinct new Chinext issuers, without IO writes."""
    path = Path(evidence_path).resolve()
    bundle = _json(path)
    _boundary(bundle, BUNDLE_SCHEMA)
    created = _time(bundle["created_at"])
    if created > datetime.now(timezone.utc):
        raise ValueError("Listing bundle actual creation time is in the future")
    base_path = Path(bundle["base_evidence"]).resolve()
    if base_path == path or _digest(base_path) != bundle["base_evidence_sha256"]:
        raise ValueError("Listing bundle base changed or is recursive")
    base_evidence = _json(base_path)
    if base_evidence.get("schema") != "research_ipo_qualification_v1":
        raise ValueError("Listing bundle requires the original legacy base, not nested bundles")
    base = _load_legacy_ipo_qualification(base_path, calendar_path, master_run=master_run)
    rules = base_evidence["instrument"]
    references = bundle["supplements"]
    if not isinstance(references, list) or not references:
        raise ValueError("Listing bundle needs explicit new issuer supplements")
    records, proofs, seen_paths = deepcopy(base.records), [], set()
    for reference in references:
        supplement_path = Path(reference["path"]).resolve()
        if supplement_path in seen_paths or _digest(supplement_path) != reference["sha256"]:
            raise ValueError("Listing supplement repeated or changed")
        seen_paths.add(supplement_path)
        supplement = _json(supplement_path)
        _boundary(supplement, SUPPLEMENT_SCHEMA)
        if supplement.get("base_evidence_sha256") != bundle["base_evidence_sha256"]:
            raise ValueError("Listing supplement must pin the identical original rule evidence")
        record = deepcopy(supplement["instrument"])
        symbol = record.get("symbol")
        if (not isinstance(symbol, str) or not re.fullmatch(r"30[01]\d{3}\.SZSE", symbol)
                or symbol in records or record.get("board") != "chinext"
                or not isinstance(record.get("issuer"), str) or not record["issuer"].strip()
                or not isinstance(record.get("name"), str) or not record["name"].strip()):
            raise ValueError("Only distinct missing Chinext issuer identities may be added")
        for field in ("rule_effective_from", "first_no_limit_sessions", "following_price_limit_pct"):
            if record.get(field) != rules[field]:
                raise ValueError("New listing must reuse the original verified effective board rule")
        ipo = _canonical_day(record["ipo_date"])
        if ipo not in base.calendar or ipo < record["rule_effective_from"]:
            raise ValueError("New listing date must fall inside the pinned effective rule calendar")
        receipt, receipt_path, retrieved, pages = _source(supplement, record)
        curated = _time(supplement["created_at"])
        if not retrieved <= curated <= created or _time(record["available_at"]) != curated:
            raise ValueError("Listing knowledge must start at actual curation, never publication-date backfill")
        record["available_at"] = curated.isoformat()
        record["source_basis"] = "official_listing_pdf_actual_retrieval_and_curation_not_historical_PIT"
        records[symbol] = record
        proofs.append({"symbol": symbol, "evidence_file": str(supplement_path),
            "evidence_sha256": reference["sha256"], "source_receipt": str(receipt_path),
            "source_receipt_sha256": supplement["source_receipt_sha256"], "raw_sha256": receipt["sha256"],
            "source_url": receipt["url"], "retrieved_at": receipt["retrieved_at"],
            "available_at": record["available_at"], "publication_date": receipt["publication_date"],
            "publication_time_known": False, "source_pages_verified_1based": pages})
    verification = {**deepcopy(base.verification), "evidence_file": str(path),
        "evidence_sha256": _digest(path), "base_evidence": deepcopy(base.verification),
        "listing_supplements": proofs, "official_source_count": base.verification["official_source_count"] + len(proofs),
        "knowledge_time_policy": "new_issuers_actual_retrieval_then_curation_no_old_record_replacement",
        "bundle_created_at": bundle["created_at"]}
    return IpoQualification(records, base.calendar, verification)


def validate_listing_bundle_append(previous, current):
    """Compare two already source-validated bundles; never replace an old fact.

    Reusing the identical bundle is valid. A different bundle must add at least
    one distinct issuer while retaining every original source and record.
    """
    old, new = previous.verification, current.verification
    required = {"base_evidence", "listing_supplements", "bundle_created_at"}
    if not required <= old.keys() or not required <= new.keys():
        raise ValueError("IPO append requires explicit original-base listing bundles")
    mutable = {"evidence_file", "evidence_sha256", "listing_supplements",
               "official_source_count", "bundle_created_at"}
    if (previous.calendar != current.calendar
            or {k: v for k, v in old.items() if k not in mutable}
            != {k: v for k, v in new.items() if k not in mutable}
            or any(symbol not in current.records or current.records[symbol] != record
                   for symbol, record in previous.records.items())):
        raise ValueError("IPO append cannot change the base, master, calendar or any previous issuer fact")
    if old == new:
        if previous.records != current.records:
            raise ValueError("Identical IPO evidence cannot describe different issuer records")
        return []
    before, after = old["listing_supplements"], new["listing_supplements"]
    if (len(after) <= len(before) or after[:len(before)] != before
            or _time(new["bundle_created_at"]) < _time(old["bundle_created_at"])):
        raise ValueError("IPO bundle changes must append new issuer sources without reordering or backdating")
    added = [proof["symbol"] for proof in after[len(before):]]
    if (len(added) != len(set(added))
            or set(current.records) - set(previous.records) != set(added)
            or new["official_source_count"] - old["official_source_count"] != len(added)):
        raise ValueError("IPO append must add only the distinct new issuer records named by its new sources")
    return added
