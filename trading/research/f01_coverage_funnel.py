"""Read-only coverage funnel for the F01 financial-input admission gate.

The funnel turns the existing annual/quarterly evidence into explicit row
reasons.  It is deliberately a diagnostic: unknown publication/revision
history and an absent market join remain blocked, and no values are filled or
promoted to a point-in-time factor input.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .f01_scoped_field_coverage import DEFAULT_RECEIPT, validate_receipt


SCHEMA = "f01_coverage_funnel_v1"
REQUIRED_FIELDS = ("parent_net_profit", "total_assets")
BLOCKED = "BLOCKED_DATA"


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _bool(value: Any) -> bool:
    return value is True


def classify_scoped_row(row: Mapping[str, Any], *, market_symbols: set[str] | None = None,
                        as_of: str | None = None,
                        publication_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Classify one issuer-report row without changing its raw values."""

    reasons: list[str] = []
    required = all(row.get(field) not in (None, "") for field in REQUIRED_FIELDS)
    source_checks = row.get("source_checks") if isinstance(row.get("source_checks"), Mapping) else {}
    publication = bool(row.get("pub_date")) and bool(
        isinstance(row.get("report_source"), Mapping)
        and row["report_source"].get("url"))
    units_scope = all(_bool(source_checks.get(key)) for key in (
        "unit_explicit_as_rmb_yuan", "parent_field_explicit", "total_assets_explicit"))
    pit = row.get("pit") if isinstance(row.get("pit"), Mapping) else {}
    contract = (publication_evidence.get("contract_evaluation")
                if isinstance(publication_evidence, Mapping)
                and isinstance(publication_evidence.get("contract_evaluation"), Mapping)
                else {})
    revision_safe = _bool(pit.get("revision_safe_pit_verified")) or _bool(contract.get("pit_asof_safe"))
    # A publication sidecar can supply the missing retrieval/as-of contract;
    # absent or incomplete evidence remains unknown and therefore blocked.
    retrieval_asof = _bool(contract.get("retrieved_asof_proven"))
    market_join = market_symbols is not None and row.get("symbol") in market_symbols

    if not required:
        reasons.append("required_field_missing")
    if not publication:
        reasons.append("publication_evidence_missing")
    if not units_scope:
        reasons.append("units_or_scope_unverified")
    if not revision_safe:
        reasons.append("revision_safe_pit_unverified")
    if not retrieval_asof:
        reasons.append("retrieval_asof_evidence_missing")
    if market_symbols is None:
        reasons.append("market_join_not_supplied")
    elif not market_join:
        reasons.append("market_symbol_not_found")

    return {
        "symbol": row.get("symbol"),
        "period": row.get("period"),
        "required_fields_observed": required,
        "publication_date_observed": publication,
        "units_scope_verified": units_scope,
        "revision_safe_pit_verified": revision_safe,
        "retrieval_asof_verified": retrieval_asof,
        "publication_sidecar_attached": bool(publication_evidence),
        "market_join_verified": market_join,
        "eligible_for_f01": not reasons,
        "reasons": reasons,
        "as_of_requested": as_of,
        "execution_admitted": False,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def count(key: str) -> int:
        return sum(bool(row.get(key)) for row in rows)

    reasons: dict[str, int] = {}
    for row in rows:
        for reason in row.get("reasons", []):
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "event_master_rows": len(rows),
        "required_fields_rows": count("required_fields_observed"),
        "publication_date_rows": count("publication_date_observed"),
        "units_scope_rows": count("units_scope_verified"),
        "revision_safe_pit_rows": count("revision_safe_pit_verified"),
        "retrieval_asof_rows": count("retrieval_asof_verified"),
        "market_join_rows": count("market_join_verified"),
        "f01_eligible_rows": count("eligible_for_f01"),
        "reason_counts": dict(sorted(reasons.items())),
    }


def from_scoped_receipt(receipt_path: str | Path = DEFAULT_RECEIPT, *,
                        market_symbols: Iterable[str] | None = None,
                        as_of: str | None = None,
                        publication_sidecar: str | Path | None = None) -> dict[str, Any]:
    """Build a funnel from the existing three-issuer quarterly receipt."""

    path = Path(receipt_path).resolve()
    validation = validate_receipt(path)
    if not validation.get("ok"):
        raise ValueError("scoped field receipt failed validation: " + "; ".join(validation.get("errors", [])))
    source = json.loads(path.read_text(encoding="utf-8"))
    symbols = set(market_symbols) if market_symbols is not None else None
    sidecar_path = Path(publication_sidecar).resolve() if publication_sidecar else None
    sidecar_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    if sidecar_path:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if sidecar.get("schema") != "f01_publication_sidecar_v1":
            raise ValueError("publication sidecar schema mismatch")
        for evidence in sidecar.get("rows", []):
            if isinstance(evidence, Mapping):
                key = (str(evidence.get("symbol", "")).upper(), str(evidence.get("period", "")))
                sidecar_index[key] = evidence
    rows = [classify_scoped_row(row, market_symbols=symbols, as_of=as_of)
            for row in source.get("rows_detail", [])]
    if sidecar_index:
        rows = [classify_scoped_row(
            row,
            market_symbols=symbols,
            as_of=as_of,
            publication_evidence=sidecar_index.get((str(row.get("symbol", "")).upper(), str(row.get("period", "")))),
        ) for row in source.get("rows_detail", [])]
    summary = _summary(rows)
    return {
        "schema": SCHEMA,
        "status": BLOCKED if not summary["f01_eligible_rows"] else "PARTIAL_DATA",
        "source_kind": "scoped_quarterly_receipt_with_publication_sidecar" if sidecar_path else "scoped_quarterly_receipt",
        "source_receipt": str(path),
        "source_receipt_sha256": _sha256(path),
        "publication_sidecar": str(sidecar_path) if sidecar_path else None,
        "publication_sidecar_sha256": _sha256(sidecar_path) if sidecar_path else None,
        "rows": rows,
        "summary": summary,
        "pit_verified": False,
        "factor_ready": False,
        "execution_admitted": False,
        "zero_fill_applied": False,
        "limitations": [
            "Publication day is evidence of disclosure identity, not revision-safe availability.",
            "Retrieval/as-of history is accepted only from the explicit publication sidecar contract.",
            "No market-data join was supplied; no row is promoted to F01.",
            "This funnel is a coverage diagnostic, not a factor or account replay.",
        ],
    }


def write_receipt(result: Mapping[str, Any], output: str | Path) -> Path:
    path = Path(output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--as-of")
    parser.add_argument("--market-symbol", action="append", default=None)
    parser.add_argument("--publication-sidecar", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = from_scoped_receipt(args.receipt, market_symbols=args.market_symbol, as_of=args.as_of,
                                 publication_sidecar=args.publication_sidecar)
    output = write_receipt(result, args.output)
    print(json.dumps({"status": result["status"], "summary": result["summary"], "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

