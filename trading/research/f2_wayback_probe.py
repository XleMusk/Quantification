"""Probe Internet Archive CDX for historical F2 issuer evidence.

The probe records archive captures for one exact issuer URL.  A capture after
the decision cutoff is retained as a failed experiment; it cannot be treated
as historical retrieval/as-of evidence.  No factor, target, order or account
action is performed here.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote
from urllib.request import Request, urlopen

from .f2_historical_evidence import BLOCKED, evaluate_historical_evidence
from .f2_source_selection import select_latest_on_or_before
from ..contracts import decision_time


SCHEMA = "f2_wayback_probe_v1"
CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _hash_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _capture_timestamp_iso(timestamp: str | None) -> str | None:
    """Convert a Wayback 14-digit UTC capture time without losing precision."""

    if not isinstance(timestamp, str) or len(timestamp) < 14 or not timestamp[:14].isdigit():
        return None
    try:
        captured = datetime.strptime(timestamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return captured.isoformat()


def cdx_url(source_url: str) -> str:
    query = (
        f"{CDX_ENDPOINT}?url={quote(source_url, safe='')}"
        "&output=json&filter=statuscode:200&fl=timestamp,original,statuscode,digest"
        "&collapse=digest"
    )
    return query


def archive_url(source_url: str, timestamp: str) -> str:
    """Return a replayable Wayback URL for one selected CDX capture."""
    if not timestamp:
        raise ValueError("archive timestamp is required")
    return f"https://web.archive.org/web/{timestamp}id_/{source_url}"


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


def fetch_archive(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch selected archived bytes and retain response metadata only."""
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
                "payload": payload,
                "response_sha256": _hash_bytes(payload),
                "error": None,
            }
    except Exception as exc:  # network failures are evidence, not success
        return {
            "requested_at": requested_at,
            "retrieved_at": _now(),
            "http_status": None,
            "content_type": "",
            "payload": b"",
            "response_sha256": "",
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


def _select_capture(captures: list[dict[str, str]], decision_day: date) -> tuple[list[dict[str, str]], dict[str, str] | None]:
    """Select through the shared deterministic source-selection contract."""
    selection = select_latest_on_or_before(
        captures, decision_day, timestamp_key="timestamp", timestamp_format="%Y%m%d%H%M%S",
        cutoff_time=decision_time(decision_day.isoformat()))
    eligible = selection["eligible"]
    selected = selection["selected"]
    return eligible, selected


def probe_wayback(
    *,
    symbol: str,
    period: str,
    publication_date: str,
    decision_day: str,
    source_url: str,
    fetcher: Callable[[str], dict[str, Any]] = fetch_cdx,
    archive_fetcher: Callable[[str], dict[str, Any]] = fetch_archive,
    archive_payload_path: str | Path | None = None,
) -> dict[str, Any]:
    query = cdx_url(source_url)
    response = fetcher(query)
    captures = _captures(response.get("payload", b"")) if response.get("http_status") == 200 else []
    decision = date.fromisoformat(decision_day)
    before, selected_row = _select_capture(captures, decision)
    archive_timestamp = selected_row.get("timestamp") if selected_row else None
    archive_date = f"{archive_timestamp[:4]}-{archive_timestamp[4:6]}-{archive_timestamp[6:8]}" if archive_timestamp and len(archive_timestamp) >= 8 else None
    archive_timestamp_iso = _capture_timestamp_iso(archive_timestamp)
    archive_response: dict[str, Any] | None = None
    archive_source_file: str | None = None
    archive_source_sha256: str | None = None
    if selected_row and before:
        selected_archive_url = archive_url(selected_row.get("original") or source_url, archive_timestamp)
        archive_response = archive_fetcher(selected_archive_url)
        payload = archive_response.get("payload", b"")
        if archive_response.get("http_status") == 200 and isinstance(payload, bytes) and payload:
            archive_source_sha256 = _hash_bytes(payload)
            if archive_payload_path is not None:
                payload_path = Path(archive_payload_path).expanduser().resolve()
                payload_path.parent.mkdir(parents=True, exist_ok=True)
                payload_path.write_bytes(payload)
                archive_source_file = str(payload_path)
            else:
                archive_source_file = selected_archive_url
    evidence_row = {
        "symbol": symbol,
        "period": period,
        "publication_date": publication_date,
        # Wayback CDX timestamps are UTC.  Preserve the exact capture time;
        # reducing it to a date and midnight can erase a valid boundary.
        "retrieved_at": archive_timestamp_iso,
        "asof_at": archive_timestamp_iso,
        # A single CDX digest is content identity, not issuer revision history.
        # Keep it in ``selected_capture`` but do not promote it to revision_id.
        "revision_id": None,
        "source_file": archive_source_file,
        "source_sha256": archive_source_sha256,
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
    if before and (archive_response is None or archive_response.get("http_status") != 200
                   or not archive_source_sha256):
        reasons.insert(0, "archive_payload_not_retrieved")
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
        "archive_retrieval": ({key: value for key, value in archive_response.items() if key != "payload"}
                               if archive_response is not None else None),
        "archive_payload_path": archive_source_file,
        "captures": captures,
        "captures_on_or_before_decision_day": before,
        "selected_capture": selected_row,
        "capture_selection": {
            "policy": "latest_on_or_before_decision_day_v1",
            "eligible_count": len(before),
            "observed_count": len(captures),
            "selected_capture_at": archive_timestamp_iso,
        },
        "historical_evidence": {**gate, "reason_codes": reasons, "status": BLOCKED if reasons else gate["status"]},
        "factor_computed": False,
        "account_replay": False,
        "limitations": [
            "A capture after the decision cutoff does not prove historical retrieval/as-of availability.",
            "This probe never creates FinancialFact, factor, target, order, fill or account state.",
        ],
    }


def write_receipt(output: str | Path, **kwargs: Any) -> dict[str, Any]:
    output_path = Path(output).expanduser().resolve()
    kwargs.setdefault("archive_payload_path", output_path.with_name(output_path.stem + "_archive_payload.pdf"))
    receipt = probe_wayback(**kwargs)
    path = output_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--period", required=True)
    parser.add_argument("--publication-date", required=True)
    parser.add_argument("--decision-day", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = write_receipt(
        args.output,
        symbol=args.symbol,
        period=args.period,
        publication_date=args.publication_date,
        decision_day=args.decision_day,
        source_url=args.source_url,
    )
    print(json.dumps({
        "status": receipt["status"],
        "symbol": receipt["symbol"],
        "period": receipt["period"],
        "historical_evidence": receipt["historical_evidence"]["status"],
        "pit_asof_safe": receipt["historical_evidence"]["pit_asof_safe"],
        "execution_admitted": receipt["execution_admitted"],
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "archive_url", "cdx_url", "fetch_archive", "fetch_cdx", "probe_wayback", "write_receipt", "main"]
