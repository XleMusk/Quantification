"""Capture one public issuer-report artifact for the F2 PIT gate.

The capture separates what the public artifact proves now (identity, published
date, content hash and current retrieval) from what it cannot prove (historical
retrieval/as-of availability and revision history).  It never promotes a fact
to a factor or account replay.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCHEMA = "f2_issuer_source_evidence_v1"
BLOCKED = "BLOCKED_DATA"

# The report table uses different semantic scopes for flows and balance-sheet
# stocks.  Keep this mapping explicit so a later F2 consumer does not infer a
# common scope from the fact that both values appear on the same page.
_FIELD_SCOPE_CONTRACTS: dict[str, dict[str, Any]] = {
    "parent_net_profit": {
        "scope": "parent_attributable",
        "period_kind": "single_quarter",
        "statement_role": "flow",
        "evidence_terms": ("归属于上市公司股东的净利润",),
    },
    "total_assets": {
        "scope": "consolidated_group",
        "period_kind": "instant",
        "statement_role": "stock",
        "evidence_terms": ("本集团", "总资产", "报告期末"),
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _hash_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _parse_http_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_source(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch one exact URL and retain response metadata without mutation."""

    requested_at = _now()
    request = Request(url, headers={"User-Agent": "Quantification-F2/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return {
                "requested_at": requested_at,
                "retrieved_at": _now(),
                "http_status": int(response.status),
                "content_type": response.headers.get("Content-Type", ""),
                "content_length": len(payload),
                "response_sha256": _hash_bytes(payload),
                "server_last_modified_at": _parse_http_date(response.headers.get("Last-Modified")),
                "etag": response.headers.get("ETag", ""),
                "payload": payload,
                "error": None,
            }
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return {
            "requested_at": requested_at,
            "retrieved_at": _now(),
            "http_status": None,
            "content_type": "",
            "content_length": 0,
            "response_sha256": "",
            "server_last_modified_at": None,
            "etag": "",
            "payload": b"",
            "error": f"{type(exc).__name__}: {exc}",
        }


def extract_pdf_text(path: Path) -> tuple[str, str]:
    """Extract text with pypdf, then Poppler when font encoding needs it."""

    try:
        from pypdf import PdfReader

        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    except Exception:
        text = ""
    if "000002" in text and "2023" in text:
        return text, "pypdf"
    executable = shutil.which("pdftotext")
    if executable:
        result = subprocess.run(
            [executable, "-layout", str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return result.stdout, "pdftotext"
    return text, "pypdf"


def _identity_checks(text: str, symbol: str, period: str) -> dict[str, bool]:
    normalized = re.sub(r"\s+", "", text)
    year = period[:4]
    quarter = period[-1]
    return {
        "symbol_present": symbol.split(".", 1)[0] in normalized,
        "year_present": year in normalized,
        "quarter_report_title_present": f"{year}年第一季度报告" in normalized,
        "period_text_present": f"{year}年1-3月" in normalized or f"{year}年01-03月" in normalized,
        "currency_text_present": "人民币" in normalized,
        "quarter_identity_requested": period == f"{year}Q{quarter}",
    }


def map_field_scope(text: str, field_candidates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Map report fields to explicit scope/period semantics.

    This validates the report wording and the candidate metadata together.  It
    deliberately does not evaluate historical retrieval/as-of or revision
    history; callers must keep those independent PIT gates closed.
    """

    normalized = re.sub(r"\s+", "", text)
    unit_verified = "单位：元" in normalized or "人民币" in normalized
    fields: dict[str, dict[str, Any]] = {}
    for metric, contract in _FIELD_SCOPE_CONTRACTS.items():
        candidate = field_candidates.get(metric, {})
        expected_scope = contract["scope"]
        expected_period_kind = contract["period_kind"]
        expected_role = contract["statement_role"]
        terms = list(contract["evidence_terms"])
        evidence_terms = [term for term in terms if term in normalized]
        metadata_match = (
            candidate.get("scope") == expected_scope
            and candidate.get("period_kind", expected_period_kind) == expected_period_kind
            and candidate.get("statement_role", expected_role) == expected_role
            and candidate.get("unit", "yuan") == "yuan"
            and candidate.get("currency", "CNY") == "CNY"
        )
        fields[metric] = {
            "status": "verified" if unit_verified and metadata_match and len(evidence_terms) == len(terms) else "blocked",
            "scope": expected_scope,
            "period_kind": expected_period_kind,
            "statement_role": expected_role,
            "unit": "yuan" if unit_verified else None,
            "currency": "CNY" if unit_verified else None,
            "evidence_terms": evidence_terms,
            "required_evidence_terms": terms,
            "metadata_match": metadata_match,
        }
    return {
        "status": "verified" if fields and all(item["status"] == "verified" for item in fields.values()) else "blocked",
        "all_verified": bool(fields) and all(item["status"] == "verified" for item in fields.values()),
        "unit_currency_verified": unit_verified,
        "fields": fields,
    }


def capture_issuer_source(
    *,
    symbol: str,
    period: str,
    publication_date: str,
    decision_day: str,
    source_url: str,
    local_pdf: str | Path,
    fetcher: Callable[..., dict[str, Any]] = fetch_source,
) -> dict[str, Any]:
    """Build a blocked-or-ready evidence receipt for one issuer PDF."""

    pdf_path = Path(local_pdf).expanduser().resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    saved_bytes = pdf_path.read_bytes()
    saved_hash = _hash_bytes(saved_bytes)
    response = fetcher(source_url)
    payload = response.get("payload", b"")
    response_hash = str(response.get("response_sha256") or "")
    hash_matches = bool(response_hash and response_hash == saved_hash)
    text, text_engine = extract_pdf_text(pdf_path)
    identity = _identity_checks(text, symbol, period)
    field_scope = map_field_scope(text, {
        "parent_net_profit": {
            "scope": "parent_attributable", "period_kind": "single_quarter",
            "statement_role": "flow", "unit": "yuan", "currency": "CNY",
        },
        "total_assets": {
            "scope": "consolidated_group", "period_kind": "instant",
            "statement_role": "stock", "unit": "yuan", "currency": "CNY",
        },
    })
    publication = date.fromisoformat(publication_date)
    decision = date.fromisoformat(decision_day)
    reasons: list[str] = []
    if response.get("http_status") != 200:
        reasons.append("issuer_source_http_not_200")
    if not hash_matches:
        reasons.append("retrieved_source_hash_does_not_match_saved_artifact")
    if not all(identity.values()):
        reasons.append("issuer_pdf_identity_probe_incomplete")
    if publication >= decision:
        reasons.append("publication_not_before_decision_cutoff")
    # A current fetch is deliberately never treated as historical as-of proof.
    reasons.extend(["historical_retrieval_asof_unproven", "revision_history_unproven"])
    if not field_scope["all_verified"]:
        reasons.append("issuer_scope_semantics_require_mapping")
    metadata = response.get("server_last_modified_at")
    metadata_before_publication = bool(metadata and str(metadata)[:10] <= publication_date)
    return {
        "schema": SCHEMA,
        "status": BLOCKED,
        "execution_admitted": False,
        "symbol": symbol,
        "period": period,
        "publication_date": publication_date,
        "decision_day": decision_day,
        "source_url": source_url,
        "local_pdf": str(pdf_path),
        "saved_sha256": saved_hash,
        "saved_bytes": len(saved_bytes),
        "text_engine": text_engine,
        "identity_checks": identity,
        "retrieval": {key: value for key, value in response.items() if key != "payload"},
        "retrieved_source_hash_matches_saved": hash_matches,
        "server_metadata_before_publication": metadata_before_publication,
        "historical_retrieval_asof_verified": False,
        "revision_history_verified": False,
        "scope_verified": field_scope["all_verified"],
        "field_scope_mapping": field_scope,
        "reason_codes": reasons,
        "field_candidates": {
            "parent_net_profit": {"value": "1445811341.87", "unit": "yuan", "scope": "parent_attributable", "page": 2},
            "total_assets": {"value": "1743078594535.06", "unit": "yuan", "scope": "consolidated_group", "page": 2},
        },
        "limitations": [
            "The current retrieval proves source identity and content hash, not historical retrieval/as-of availability.",
            "Server Last-Modified is transport metadata and does not prove an issuer revision history.",
            "Field candidates are not admitted to F2 until scope and historical PIT evidence pass.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--period", required=True)
    parser.add_argument("--publication-date", required=True)
    parser.add_argument("--decision-day", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--local-pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = capture_issuer_source(
        symbol=args.symbol, period=args.period, publication_date=args.publication_date,
        decision_day=args.decision_day, source_url=args.source_url, local_pdf=args.local_pdf,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "reason_codes": receipt["reason_codes"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BLOCKED", "SCHEMA", "capture_issuer_source", "extract_pdf_text", "fetch_source", "map_field_scope"]
