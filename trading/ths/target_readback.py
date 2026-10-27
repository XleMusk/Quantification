"""Compare an explicit THS read-only target reference with captured rows.

The comparator consumes a local reference and a user-provided read-only export.
It never opens a client, submits an order, or treats a row match as a fill.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "ths_target_readback_comparison_v1"
AUTHORIZED_CAPTURE_MODE = "authorized_read_only_export"


def _load(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _readback_provenance(readback: Mapping[str, Any], *, source_root: str | Path | None = None) -> dict[str, Any]:
    """Validate provenance only when a readback claims client observation.

    Offline and synthetic fixtures remain usable without an external source.
    A claim that an account or quote was observed must carry an explicit capture
    mode, an offset-aware observation time, and a hash-bound source artifact;
    otherwise the comparison would be a row match with an unsupported channel
    claim.
    """

    synthetic = readback.get("synthetic_fixture") is True
    account_observed = readback.get("account_observed") is True
    quote_observed = readback.get("quote_observed") is True
    client_interacted = readback.get("client_interacted") is True
    claims_channel = account_observed or quote_observed or client_interacted
    if synthetic and claims_channel:
        raise ValueError("Synthetic readback cannot claim client/account/quote observation")
    if claims_channel:
        if readback.get("capture_mode") != AUTHORIZED_CAPTURE_MODE:
            raise ValueError("Observed readback requires authorized_read_only_export capture_mode")
        observed_at = readback.get("observed_at")
        if not isinstance(observed_at, str):
            raise ValueError("Observed readback requires observed_at")
        try:
            parsed = datetime.fromisoformat(observed_at)
        except ValueError as error:
            raise ValueError("Observed readback observed_at must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise ValueError("Observed readback observed_at requires an explicit timezone")
        source_file = readback.get("source_file")
        source_sha256 = readback.get("source_sha256")
        if not isinstance(source_file, str) or not source_file.strip():
            raise ValueError("Observed readback requires source_file")
        if (not isinstance(source_sha256, str) or len(source_sha256) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in source_sha256)):
            raise ValueError("Observed readback requires a 64-character source_sha256")
        source_path = Path(source_file).expanduser()
        if source_root is not None and not source_path.is_absolute():
            source_path = Path(source_root).expanduser().resolve() / source_path
        source_path = source_path.resolve()
        if source_root is not None:
            if not source_path.is_file():
                raise ValueError("Observed readback source_file is missing")
            actual = sha256(source_path.read_bytes()).hexdigest()
            if actual.lower() != source_sha256.lower():
                raise ValueError("Observed readback source_sha256 does not match source_file")
            source_file = str(source_path)
        kind = AUTHORIZED_CAPTURE_MODE
    elif synthetic:
        kind = "synthetic_fixture"
    else:
        kind = "offline_reference"
    return {
        "kind": kind,
        "capture_mode": readback.get("capture_mode"),
        "source_file": source_file if claims_channel else readback.get("source_file"),
        "source_sha256": readback.get("source_sha256"),
        "source_file_verified": bool(claims_channel and source_root is not None),
        "client_interacted": client_interacted,
    }


def compare_target_reference(reference: Mapping[str, Any], readback: Mapping[str, Any], *, source_root: str | Path | None = None) -> dict[str, Any]:
    if reference.get("schema") != "quantification_ths_readonly_target_reference_v1":
        raise ValueError("Unexpected target reference schema")
    if reference.get("execution_admitted") is not False or reference.get("prefill_allowed") is not False:
        raise ValueError("Target reference crossed the execution boundary")
    provenance = _readback_provenance(readback, source_root=source_root)
    expected_rows = reference.get("rows")
    received_rows = readback.get("rows")
    if not isinstance(expected_rows, list) or not isinstance(received_rows, list):
        raise ValueError("Reference and readback must contain rows lists")

    expected_symbols = [str(row.get("symbol")) for row in expected_rows
                        if isinstance(row, Mapping) and row.get("symbol")]
    received_symbols = [str(row.get("symbol")) for row in received_rows
                        if isinstance(row, Mapping) and row.get("symbol")]
    duplicate_expected = sorted({symbol for symbol in expected_symbols
                                 if expected_symbols.count(symbol) > 1})
    duplicate_received = sorted({symbol for symbol in received_symbols
                                 if received_symbols.count(symbol) > 1})
    if duplicate_expected:
        raise ValueError(f"Target reference contains duplicate symbols: {duplicate_expected}")
    expected = {
        str(row.get("symbol")): int(row.get("target_quantity", 0))
        for row in expected_rows if isinstance(row, Mapping) and row.get("symbol")
    }
    received = {
        str(row.get("symbol")): int(row.get("qty", row.get("target_quantity", 0)))
        for row in received_rows if isinstance(row, Mapping) and row.get("symbol")
    }
    missing = sorted(set(expected) - set(received))
    unexpected = sorted(set(received) - set(expected))
    mismatches = [
        {"symbol": symbol, "expected_qty": expected[symbol], "received_qty": received.get(symbol)}
        for symbol in sorted(set(expected) & set(received))
        if expected[symbol] != received[symbol]
    ]
    if duplicate_received:
        status = "readback_mismatch_stop"
    elif missing:
        status = "readback_incomplete_stop"
    elif unexpected or mismatches:
        status = "readback_mismatch_stop"
    else:
        status = "readback_verified"
    return {
        "schema": SCHEMA,
        "status": status,
        "reference_day": reference.get("reference_day"),
        "observed_at": readback.get("observed_at"),
        "expected_rows": len(expected),
        "received_rows": len(received),
        "missing_symbols": missing,
        "unexpected_symbols": unexpected,
        "quantity_mismatches": mismatches,
        "duplicate_readback_symbols": duplicate_received,
        "account_observed": readback.get("account_observed") is True,
        "quote_observed": readback.get("quote_observed") is True,
        "readback_provenance": provenance,
        "authorized_readonly_observation": provenance["kind"] == AUTHORIZED_CAPTURE_MODE,
        "submitted": False,
        "execution_admitted": False,
        "ledger_mutated": False,
        "limitations": [
            "A row match verifies the captured read-only export against the target reference; it is not a fill or account settlement.",
            "Current quotes and account balances remain unknown unless explicitly present in the readback export.",
            "Mismatch or incomplete rows must stop the route and must not be resent automatically.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--readback", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path,
                        help="optional root for resolving and rehashing an authorized read-only export")
    args = parser.parse_args(argv)
    result = compare_target_reference(
        _load(args.reference), _load(args.readback),
        source_root=args.source_root if args.source_root is not None else args.readback.parent,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "execution_admitted": False,
                      "output": str(output)}, ensure_ascii=False))
    return 0 if result["status"] == "readback_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "compare_target_reference", "main"]
