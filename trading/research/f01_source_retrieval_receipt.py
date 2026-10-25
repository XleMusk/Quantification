"""Capture network retrieval evidence for the scoped F01 issuer reports.

The resulting receipt records this run's offset-aware network retrieval time
and verifies the response hash against the saved issuer PDF.  It is provenance
evidence only: a current fetch does not prove historical as-of availability or
revision history for the 2024 decision event.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .f01_scoped_field_coverage import DEFAULT_RECEIPT, validate_receipt
from .stockdb_rolling_low_volatility import _read_snapshot


SCHEMA = "f01_source_retrieval_receipt_v1"
DEFAULT_MARKET_SNAPSHOT = Path(r"D:\QuantificationData\trading\research\full_market\stockdb_2024_0101_1013_current_cutoff_v1")
DEFAULT_MARKET_DAY = "2024-05-06"


def _hash_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _hash_file(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _http_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).astimezone().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def fetch_source(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch one exact URL and retain enough metadata to reproduce the check."""

    requested_at = _now()
    request = Request(url, headers={"User-Agent": "Quantification-F01/1.0"})
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
                "server_last_modified_at": _http_date(response.headers.get("Last-Modified")),
                "etag": response.headers.get("ETag", ""),
                "is_latest": response.headers.get("IsLatest", ""),
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
            "server_last_modified_at": "", "etag": "", "is_latest": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _row_url(row: Mapping[str, Any]) -> str:
    source = row.get("report_source")
    return str(source.get("url", "")) if isinstance(source, Mapping) else ""


def _market_join(snapshot: Path | None, symbols: list[str], market_day: str) -> dict[str, Any]:
    if snapshot is None:
        return {"verified": False, "requested_day": market_day, "rows": [], "reason": "snapshot_not_supplied"}
    manifest = snapshot.resolve() / "manifest.json"
    if not manifest.is_file():
        return {"verified": False, "requested_day": market_day, "rows": [], "reason": "snapshot_manifest_missing"}
    _, _, frame, _ = _read_snapshot(snapshot, None, end=market_day)
    codes = {symbol.split(".", 1)[0] for symbol in symbols}
    joined = frame[(frame["date"] == market_day) & frame["code"].isin(codes)].copy()
    rows = joined[["code", "date", "open", "high", "low", "close", "volume", "amount"]].to_dict("records")
    return {
        "verified": len(rows) == len(codes), "requested_day": market_day,
        "snapshot": str(snapshot.resolve()), "snapshot_manifest_sha256": _hash_file(manifest),
        "rows": rows, "reason": None if len(rows) == len(codes) else "market_rows_missing",
    }


def build_retrieval_receipt(scoped_receipt: str | Path = DEFAULT_RECEIPT, *,
                            timeout: float = 30.0,
                            fetcher=fetch_source,
                            market_snapshot: str | Path | None = DEFAULT_MARKET_SNAPSHOT,
                            market_day: str = DEFAULT_MARKET_DAY) -> dict[str, Any]:
    receipt_path = Path(scoped_receipt).resolve()
    validation = validate_receipt(receipt_path)
    if not validation.get("ok"):
        raise ValueError("scoped F01 receipt failed validation: " + "; ".join(validation.get("errors", [])))
    source = json.loads(receipt_path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for row in source.get("rows_detail", []):
        report_source = row.get("report_source", {})
        expected_sha = str(report_source.get("pdf_sha256", "")) if isinstance(report_source, Mapping) else ""
        url = _row_url(row)
        evidence = fetcher(url, timeout=timeout) if url else {
            "requested_at": _now(), "retrieved_at": _now(), "http_status": None,
            "content_type": "", "content_length": 0, "response_sha256": "",
            "server_last_modified_at": "", "etag": "", "is_latest": "",
            "error": "source URL missing",
        }
        local_file = Path(str(report_source.get("pdf", ""))).resolve() if isinstance(report_source, Mapping) else Path()
        local_sha = _hash_file(local_file) if local_file.is_file() else ""
        hash_matches_saved = bool(evidence.get("response_sha256") and expected_sha and
                                 evidence["response_sha256"] == expected_sha)
        last_modified = str(evidence.get("server_last_modified_at") or "")
        publication_date = str(row.get("pub_date") or "")
        metadata_before_publication = bool(last_modified and publication_date and
                                           last_modified[:10] <= publication_date)
        rows.append({
            "symbol": row.get("symbol"), "period": row.get("period"),
            "publication_date": row.get("pub_date"), "source_url": url,
            "saved_source_file": str(local_file), "saved_source_sha256": expected_sha,
            "saved_source_file_sha256": local_sha,
            "saved_file_hash_verified": bool(local_sha and local_sha == expected_sha),
            "retrieved_source_hash_matches_saved": hash_matches_saved,
            "retrieval": evidence, "revision_id": evidence.get("response_sha256", ""),
            "server_last_modified_at": last_modified,
            "server_revision_id": str(evidence.get("etag") or evidence.get("response_sha256") or ""),
            "historical_server_metadata_before_publication": metadata_before_publication,
            "historical_asof_verified": False, "revision_history_verified": False,
        })
    all_retrieved = bool(rows) and all(bool(r["retrieved_source_hash_matches_saved"]) for r in rows)
    market = _market_join(Path(market_snapshot) if market_snapshot is not None else None,
                          [str(row.get("symbol")) for row in source.get("rows_detail", [])], market_day)
    return {
        "schema": SCHEMA,
        "status": "NETWORK_RETRIEVED_HASH_MATCH_PIT_PENDING" if all_retrieved else "NETWORK_RETRIEVAL_INCOMPLETE",
        "execution_admitted": False, "scoped_receipt": str(receipt_path),
        "scoped_receipt_sha256": _hash_file(receipt_path), "rows": rows,
        "market_join": market,
        "summary": {
            "rows": len(rows),
            "http_success_rows": sum(r["retrieval"].get("http_status") == 200 for r in rows),
            "retrieved_source_hash_match_rows": sum(r["retrieved_source_hash_matches_saved"] for r in rows),
            "saved_file_hash_verified_rows": sum(r["saved_file_hash_verified"] for r in rows),
            "historical_asof_verified_rows": 0, "revision_history_verified_rows": 0,
            "market_join_verified": bool(market["verified"]),
            "historical_server_metadata_before_publication_rows": sum(
                r["historical_server_metadata_before_publication"] for r in rows),
        },
        "limitations": [
            "retrieved_at records this run's network retrieval time, not the issuer retrieval time before the historical decision cutoff",
            "response hash is content identity and does not prove revision history or historical as-of availability",
            "this receipt does not promote F01, run a factor, create targets or perform broker/THS actions",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scoped-receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--market-snapshot", type=Path, default=DEFAULT_MARKET_SNAPSHOT)
    parser.add_argument("--market-day", default=DEFAULT_MARKET_DAY)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_retrieval_receipt(args.scoped_receipt, timeout=args.timeout,
                                     market_snapshot=args.market_snapshot, market_day=args.market_day)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "summary": result["summary"], "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
