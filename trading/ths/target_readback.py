"""Compare an explicit THS read-only target reference with captured rows.

The comparator consumes a local reference and a user-provided read-only export.
It never opens a client, submits an order, or treats a row match as a fill.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "ths_target_readback_comparison_v1"


def _load(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def compare_target_reference(reference: Mapping[str, Any], readback: Mapping[str, Any]) -> dict[str, Any]:
    if reference.get("schema") != "quantification_ths_readonly_target_reference_v1":
        raise ValueError("Unexpected target reference schema")
    if reference.get("execution_admitted") is not False or reference.get("prefill_allowed") is not False:
        raise ValueError("Target reference crossed the execution boundary")
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
    args = parser.parse_args(argv)
    result = compare_target_reference(_load(args.reference), _load(args.readback))
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "execution_admitted": False,
                      "output": str(output)}, ensure_ascii=False))
    return 0 if result["status"] == "readback_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "compare_target_reference", "main"]
