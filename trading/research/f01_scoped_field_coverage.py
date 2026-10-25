"""Read-only validation for the scoped F01 issuer-report field receipt.

The receipt is deliberately a partial evidence package.  This module verifies
the files and field snippets that support it without fetching data, rewriting
the receipt, or changing F01/PIT readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "f01_scoped_field_coverage_receipt_v1"
EXPECTED_STATUS = "PARTIAL_FIELD_EVIDENCE_PIT_PENDING"
DEFAULT_RECEIPT = (
    Path(__file__).resolve().parents[2]
    / "outputs"
    / "financials"
    / "f01_scoped_field_coverage_20261024.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fail(errors: list[str], message: str) -> None:
    errors.append(message)


def validate_receipt(receipt_path: str | Path = DEFAULT_RECEIPT) -> dict[str, Any]:
    """Validate a scoped receipt and return a machine-readable audit.

    Validation is intentionally closed-world: missing files, changed hashes,
    changed evidence lines, or a claimed PIT/factor-ready state fail the audit.
    No network or data-provider calls are made.
    """

    path = Path(receipt_path).expanduser().resolve()
    errors: list[str] = []
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"schema": SCHEMA, "ok": False, "receipt": str(path), "errors": [str(exc)]}

    if receipt.get("schema") != SCHEMA:
        _fail(errors, "schema_mismatch")
    if receipt.get("status") != EXPECTED_STATUS:
        _fail(errors, "status_must_remain_partial_field_evidence")
    if receipt.get("pit_verified") is not False:
        _fail(errors, "pit_verified_must_remain_false")
    if receipt.get("revision_safe_pit_verified") is not False:
        _fail(errors, "revision_safe_pit_verified_must_remain_false")
    if receipt.get("factor_ready") is not False:
        _fail(errors, "factor_ready_must_remain_false")
    if receipt.get("execution_admitted") is not False:
        _fail(errors, "execution_admitted_must_remain_false")
    if receipt.get("single_quarter_input") is not True:
        _fail(errors, "single_quarter_input_marker_missing")

    rows = receipt.get("rows_detail")
    if not isinstance(rows, list) or not rows:
        _fail(errors, "rows_detail_missing")
        rows = []
    counts = {"parent_net_profit": 0, "total_assets": 0, "joint": 0}
    for index, row in enumerate(rows):
        prefix = f"rows_detail[{index}]"
        if not isinstance(row, Mapping):
            _fail(errors, f"{prefix}:not_mapping")
            continue
        symbol = row.get("symbol", "<missing-symbol>")
        for field in ("parent_net_profit", "total_assets"):
            if row.get(field) in (None, ""):
                _fail(errors, f"{symbol}:{field}_missing")
            else:
                counts[field] += 1
        if row.get("parent_net_profit") not in (None, "") and row.get("total_assets") not in (None, ""):
            counts["joint"] += 1
        source = row.get("report_source")
        if not isinstance(source, Mapping):
            _fail(errors, f"{symbol}:report_source_missing")
            continue
        for kind in ("pdf",):
            source_path = Path(str(source.get(kind, "")))
            if not source_path.is_file():
                _fail(errors, f"{symbol}:{kind}_missing")
            else:
                expected_hash = source.get(f"{kind}_sha256")
                if not expected_hash or _sha256(source_path) != expected_hash:
                    _fail(errors, f"{symbol}:{kind}_sha256_mismatch")
        for field, source_key in (("parent_net_profit", "parent_net_profit_source"), ("total_assets", "total_assets_source")):
            evidence = row.get(source_key)
            if not isinstance(evidence, Mapping):
                _fail(errors, f"{symbol}:{source_key}_missing")
                continue
            text_path = Path(str(evidence.get("text", "")))
            line_no = evidence.get("line_no")
            snippet = evidence.get("snippet")
            if not text_path.is_file():
                _fail(errors, f"{symbol}:{field}_evidence_text_missing")
                continue
            expected_text_hash = source.get("text_sha256")
            if not expected_text_hash or _sha256(text_path) != expected_text_hash:
                _fail(errors, f"{symbol}:text_sha256_mismatch")
            if not isinstance(line_no, int) or line_no < 1 or not isinstance(snippet, str):
                _fail(errors, f"{symbol}:{field}_evidence_locator_invalid")
                continue
            lines = text_path.read_text(encoding="utf-8").splitlines()
            if line_no > len(lines) or snippet not in lines[line_no - 1]:
                _fail(errors, f"{symbol}:{field}_evidence_line_changed")

    expected_counts = receipt.get("required_field_coverage")
    if counts != expected_counts:
        _fail(errors, "required_field_coverage_mismatch")
    if receipt.get("rows") != len(rows):
        _fail(errors, "row_count_mismatch")
    return {
        "schema": SCHEMA,
        "ok": not errors,
        "receipt": str(path),
        "status": receipt.get("status"),
        "rows": len(rows),
        "required_field_coverage": counts,
        "pit_verified": receipt.get("pit_verified"),
        "factor_ready": receipt.get("factor_ready"),
        "execution_admitted": receipt.get("execution_admitted"),
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    result = validate_receipt(args.receipt)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("PASS" if result["ok"] else "FAIL", result["receipt"])
        if result["errors"]:
            print("; ".join(result["errors"]))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

