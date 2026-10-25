"""Probe Internet Archive CDX for historical F2 issuer evidence.

The probe records archive captures for one exact issuer URL.  A capture after
the decision cutoff is retained as a failed experiment; it cannot be treated
as historical retrieval/as-of evidence.  No factor, target, order or account
action is performed here.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen

from .f2_historical_evidence import BLOCKED, evaluate_historical_evidence


SCHEMA = "f2_wayback_probe_v1"
CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _hash_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def cdx_url(source_url: str) -> str:
    query = (
        f"{CDX_ENDPOINT}?url={quote(source_url, safe='')}"
        "&output=json&filter=statuscode:200&fl=timestamp,original,statuscode,digest"
        "&collapse=digest"
    )
    return query


def fetch_cdx(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    requested_at = _now()
    request = Request(url, headers={"User-Agent": "Quantification-F2-Wayback/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return {
                "requested_at": requested_at,
                "retrieved_at": _now(),
                "http_status": int(response.status),
                "content_type": response.headers.get("Content-Type", ""),
                "response_sha256": _hash_bytes(payload),
                "payload": payload,
                "error": None,
            }
    except Exception as exc:  # network failures are evidence, not success
        return {
            "requested_at": requested_at,
            "retrieved_at": _now(),
            "http_status": None,
            "content_type": "",
            "response_sha256": "",
            "payload": b"",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _captures(payload: bytes) -> list[dict[str, str]]:
    if not payload:
        return []
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, list) or not decoded:
        return []
    header = [str(value) for value in decoded[0]] if isinstance(decoded[0], list) else []
    rows: list[dict[str, str]] = []
    for item in decoded[1:]:
        if not isinstance(item, list):
            continue
        row = {header[index]: str(item[index]) for index in range(min(len(header), len(item)))}
        if row.get("timestamp"):
            rows.append(row)
    return rows


def probe_wayback(
    *,
    symbol: str,
    period: str,
    publication_date: str,
    decision_day: str,
    source_url: str,
    fetcher: Callable[[str], dict[str, Any]] = fetch_cdx,
) -> dict[str, Any]:
    query = cdx_url(source_url)
    response = fetcher(query)
    captures = _captures(response.get("payload", b"")) if response.get("http_status") == 200 else []
    decision = date.fromisoformat(decision_day)
    before = [row for row in captures if row.get("timestamp", "")[:8] <= decision.isoformat().replace("-", "")]
    selected = sorted(before or captures, key=lambda row: row.get("timestamp", ""))
    selected_row = selected[0] if selected else None
    archive_timestamp = selected_row.get("timestamp") if selected_row else None
    archive_date = f"{archive_timestamp[:4]}-{archive_timestamp[4:6]}-{archive_timestamp[6:8]}" if archive_timestamp and len(archive_timestamp) >= 8 else None
    evidence_row = {
        "symbol": symbol,
        "period": period,
        "publication_date": publication_date,
        "retrieved_at": f"{archive_date}T00:00:00+00:00" if archive_date else None,
        "asof_at": f"{archive_date}T00:00:00+00:00" if archive_date else None,
        # A single CDX digest is content identity, not issuer revision history.
        # Keep it in ``selected_capture`` but do not promote it to revision_id.
        "revision_id": None,
        "source_file": selected_row.get("original") if selected_row else source_url,
        "source_sha256": selected_row.get("digest") if selected_row else None,
        "evidence_kind": "historical_archive",
    }
    gate = evaluate_historical_evidence(evidence_row, decision_day)
    reasons = list(gate.get("reason_codes") or [])
    if response.get("http_status") != 200:
        reasons.insert(0, "cdx_http_not_200")
    if not captures:
        reasons.insert(0, "no_cdx_capture")
    if not before and captures:
        reasons.insert(0, "archive_capture_after_decision_only")
    reasons = list(dict.fromkeys(reasons))
    return {
        "schema": SCHEMA,
        "status": BLOCKED if reasons else gate["status"],
        "execution_admitted": False,
        "symbol": symbol,
        "period": period,
        "publication_date": publication_date,
        "decision_day": decision_day,
        "source_url": source_url,
        "cdx_url": query,
        "retrieval": {key: value for key, value in response.items() if key != "payload"},
        "captures": captures,
        "captures_on_or_before_decision_day": before,
        "selected_capture": selected_row,
        "historical_evidence": {**gate, "reason_codes": reasons, "status": BLOCKED if reasons else gate["status"]},
        "factor_computed": False,
        "account_replay": False,
        "limitations": [
            "A capture after the decision cutoff does not prove historical retrieval/as-of availability.",
            "This probe never creates FinancialFact, factor, target, order, fill or account state.",
        ],
    }


def write_receipt(output: str | Path, **kwargs: Any) -> dict[str, Any]:
    receipt = probe_wayback(**kwargs)
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


__all__ = ["SCHEMA", "cdx_url", "fetch_cdx", "probe_wayback", "write_receipt"]
