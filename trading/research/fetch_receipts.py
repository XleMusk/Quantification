"""Small, source-agnostic FetchRequest/FetchReceipt contracts for F2.

The contract records coverage and failure semantics around an adapter.  It does
not fetch data, retry a provider, or decide whether a financial fact is usable.
Adapters can use it before handing a payload to the existing financial and
snapshot consumers.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Iterable, Mapping


COMPLETION_STATES = (
    "COMPLETE", "COMPLETE_EMPTY", "PARTIAL", "UNKNOWN_COVERAGE",
    "SCHEMA_CHANGED", "DENIED", "RETRYABLE_ERROR",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def request_hash(request: Mapping[str, Any]) -> str:
    """Hash the normalized request identity, excluding volatile receipt fields."""
    if not isinstance(request, Mapping) or not request:
        raise ValueError("FetchRequest must be a non-empty mapping")
    return sha256(_canonical(dict(request)).encode("utf-8")).hexdigest()


def _names(values: Iterable[Any], field: str) -> list[str]:
    result = [value if isinstance(value, str) else "" for value in values]
    if any(not value for value in result) or len(result) != len(set(result)):
        raise ValueError(f"{field} must contain unique non-empty strings")
    return result


def validate_receipt(receipt: Mapping[str, Any], request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate a receipt without treating a partial payload as complete."""
    if not isinstance(receipt, Mapping):
        raise ValueError("FetchReceipt must be a mapping")
    required = {
        "request_hash", "source", "upstream_source", "adapter_version", "requested_scope",
        "fetched_at", "raw_content_reference", "schema_fingerprint", "expected_partitions",
        "completed_partitions", "page_receipts", "completion_state", "error_code", "retryable",
    }
    missing = sorted(required - set(receipt))
    if missing:
        raise ValueError(f"FetchReceipt missing fields: {', '.join(missing)}")
    if request is not None and receipt["request_hash"] != request_hash(request):
        raise ValueError("FetchReceipt request hash does not match FetchRequest")
    if not isinstance(receipt["request_hash"], str) or len(receipt["request_hash"]) != 64:
        raise ValueError("FetchReceipt request_hash must be a SHA-256 hex digest")
    expected = _names(receipt["expected_partitions"], "expected_partitions")
    completed = _names(receipt["completed_partitions"], "completed_partitions")
    if not set(completed) <= set(expected):
        raise ValueError("completed_partitions contains an unexpected partition")
    pages = receipt["page_receipts"]
    if not isinstance(pages, list):
        raise ValueError("page_receipts must be a list")
    page_ids = []
    rows = 0
    for page in pages:
        if not isinstance(page, Mapping) or not isinstance(page.get("partition"), str):
            raise ValueError("Each page receipt needs a partition")
        page_ids.append(page["partition"])
        if type(page.get("rows", 0)) is not int or page["rows"] < 0:
            raise ValueError("Page rows must be a non-negative integer")
        rows += page["rows"]
    if len(page_ids) != len(set(page_ids)) or set(page_ids) != set(completed):
        raise ValueError("page_receipts must exactly cover completed_partitions")
    state = receipt["completion_state"]
    if state not in COMPLETION_STATES:
        raise ValueError(f"Unknown completion_state: {state}")
    complete = set(completed) == set(expected)
    if state in {"COMPLETE", "COMPLETE_EMPTY"} and not complete:
        raise ValueError("A complete receipt must cover every expected partition")
    if state == "COMPLETE_EMPTY" and rows != 0:
        raise ValueError("COMPLETE_EMPTY cannot contain rows")
    if state == "COMPLETE" and rows == 0:
        raise ValueError("Zero-row complete fetch must use COMPLETE_EMPTY")
    if state == "PARTIAL" and complete:
        raise ValueError("A fully covered fetch cannot be PARTIAL")
    if state == "UNKNOWN_COVERAGE" and complete:
        raise ValueError("Unknown coverage cannot claim all expected partitions")
    if state in {"SCHEMA_CHANGED", "DENIED", "RETRYABLE_ERROR"} and not receipt.get("error_code"):
        raise ValueError(f"{state} requires error_code")
    if type(receipt["retryable"]) is not bool:
        raise ValueError("retryable must be boolean")
    if state == "RETRYABLE_ERROR" and not receipt["retryable"]:
        raise ValueError("RETRYABLE_ERROR must be marked retryable")
    return {"valid": True, "rows": rows, "coverage_complete": complete, "completion_state": state}


def make_receipt(request: Mapping[str, Any], *, source: str, upstream_source: str,
                 adapter_version: str, requested_scope: Mapping[str, Any], fetched_at: str,
                 raw_content_reference: Mapping[str, Any] | None, schema_fingerprint: str,
                 expected_partitions: Iterable[str], completed_partitions: Iterable[str],
                 page_receipts: list[Mapping[str, Any]], completion_state: str,
                 error_code: str | None = None, retryable: bool = False) -> dict[str, Any]:
    """Build and validate one immutable receipt for a fetch attempt."""
    receipt = {
        "request_hash": request_hash(request), "source": source, "upstream_source": upstream_source,
        "adapter_version": adapter_version, "requested_scope": dict(requested_scope), "fetched_at": fetched_at,
        "raw_content_reference": dict(raw_content_reference) if raw_content_reference is not None else None,
        "schema_fingerprint": schema_fingerprint, "expected_partitions": list(expected_partitions),
        "completed_partitions": list(completed_partitions), "page_receipts": [dict(page) for page in page_receipts],
        "completion_state": completion_state, "error_code": error_code, "retryable": retryable,
    }
    validate_receipt(receipt, request)
    return receipt
