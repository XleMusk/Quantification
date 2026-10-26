"""Provider-neutral historical retrieval/as-of/revision gate for F2.

Current network retrieval proves that an artifact can be fetched now. It does
not prove that the same content was available at a historical decision cutoff.
This contract gives future archive or provider-revision adapters one narrow
input shape and keeps the current-fetch case explicitly blocked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import argparse
import json
from pathlib import Path
from typing import Any

from .publication_event_contract import evaluate_publication_evidence


SCHEMA = "f2_historical_evidence_contract_v1"
READY = "HISTORICAL_EVIDENCE_READY"
BLOCKED = "BLOCKED_DATA"
HISTORICAL_EVIDENCE_KINDS = frozenset({
    "historical_archive",
    "provider_revision",
    "source_snapshot",
})


def evaluate_historical_evidence(row: Mapping[str, Any], decision_day: str) -> dict[str, Any]:
    """Evaluate one historical evidence row without inferring missing fields."""

    if not isinstance(row, Mapping):
        raise TypeError("historical evidence row must be a mapping")
    reasons: list[str] = []
    evidence_kind = row.get("evidence_kind")
    if evidence_kind not in HISTORICAL_EVIDENCE_KINDS:
        reasons.append("historical_evidence_kind_missing_or_current")

    publication = evaluate_publication_evidence(row, decision_day)
    if not publication["publication_event_proven"]:
        reasons.append("publication_event_not_proven")
    if not publication["retrieved_asof_proven"]:
        reasons.append("historical_retrieval_asof_unproven")
    if not publication["revision_identity_proven"]:
        reasons.append("revision_history_unproven")
    if not publication["source_provenance_proven"]:
        reasons.append("historical_source_provenance_unproven")

    detail_reasons = list(publication["publication_evidence_reasons"])
    reasons.extend(f"contract:{value}" for value in detail_reasons)
    unique_reasons = list(dict.fromkeys(reasons))
    ready = not unique_reasons
    result = {
        "schema": SCHEMA,
        "status": READY if ready else BLOCKED,
        "symbol": row.get("symbol"),
        "period": row.get("period"),
        "evidence_kind": evidence_kind,
        "historical_retrieval_asof_verified": publication["retrieved_asof_proven"],
        "revision_history_verified": publication["revision_identity_proven"],
        "source_provenance_verified": publication["source_provenance_proven"],
        "pit_asof_safe": ready,
        "reason_codes": unique_reasons,
        "publication_contract": publication,
        "execution_admitted": False,
    }
    # Preserve source identity for downstream fact bridges.  The gate still
    # decides PIT eligibility from the contract above; these fields are only
    # provenance and are never used to relax a blocked row.
    for key in (
        "source_snapshot_id", "source_file", "source_sha256",
        "snapshot_source_check", "snapshot_source_check_passed",
    ):
        if key in row:
            result[key] = row[key]
    return result


def build_historical_gate_receipt(
    rows: Sequence[Mapping[str, Any]],
    *,
    decision_day: str,
    source_description: str = "provider-neutral historical evidence rows",
) -> dict[str, Any]:
    """Build a deterministic blocked/ready receipt for a batch of evidence."""

    evaluated = [evaluate_historical_evidence(row, decision_day) for row in rows]
    ready_rows = sum(item["status"] == READY for item in evaluated)
    return {
        "schema": SCHEMA,
        "status": READY if evaluated and ready_rows == len(evaluated) else BLOCKED,
        "decision_day": decision_day,
        "source_description": source_description,
        "rows": evaluated,
        "summary": {
            "rows": len(evaluated),
            "ready_rows": ready_rows,
            "blocked_rows": len(evaluated) - ready_rows,
        },
        "execution_admitted": False,
        "factor_computed": False,
        "account_replay": False,
        "limitations": [
            "A current network fetch is not historical retrieval/as-of evidence.",
            "A ready gate still requires the downstream F2 period, scope and unit contract.",
        ],
    }


def write_receipt(
    output: str | Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    decision_day: str,
    source_description: str = "provider-neutral historical evidence rows",
) -> dict[str, Any]:
    """Write the gate receipt for CLI and batch adapters."""

    receipt = build_historical_gate_receipt(
        rows,
        decision_day=decision_day,
        source_description=source_description,
    )
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def load_input_rows(path: str | Path) -> tuple[list[dict[str, Any]], str | None]:
    """Load raw F2 evidence rows from a saved source snapshot or row list.

    The loader accepts only explicit envelopes already written by a source
    adapter: a JSON list, an object with ``rows``, or an object containing a
    ``historical_gate.rows`` list.  A source snapshot's ``capture_mode`` is
    mapped to the contract's evidence kind; no timing, revision or source
    fields are filled in.
    """

    input_path = Path(path).expanduser().resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    source_description: str | None = None
    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, Mapping):
        historical_gate = payload.get("historical_gate")
        if isinstance(historical_gate, Mapping) and isinstance(historical_gate.get("rows"), list):
            raw_rows = historical_gate["rows"]
            source_description = historical_gate.get("source_description")
        elif isinstance(payload.get("rows"), list):
            raw_rows = payload["rows"]
            source_description = payload.get("source_description") or payload.get("snapshot_id")
        else:
            raise ValueError("input must contain an explicit rows list")
    else:
        raise ValueError("input must be a JSON list or object")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(raw_rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"input row {index} must be an object")
        normalized = dict(row)
        if "evidence_kind" not in normalized:
            capture_mode = normalized.get("capture_mode")
            normalized["evidence_kind"] = (
                "source_snapshot" if capture_mode == "current_fetch" else capture_mode
            )
        rows.append(normalized)
    return rows, source_description


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="saved source snapshot or JSON rows envelope")
    parser.add_argument("--decision-day", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-description", default=None)
    args = parser.parse_args(argv)
    rows, input_description = load_input_rows(args.input)
    receipt = write_receipt(
        args.output,
        rows,
        decision_day=args.decision_day,
        source_description=args.source_description or input_description
        or f"historical evidence input: {args.input.name}",
    )
    print(json.dumps({
        "status": receipt["status"],
        "rows": receipt["summary"]["rows"],
        "ready_rows": receipt["summary"]["ready_rows"],
        "blocked_rows": receipt["summary"]["blocked_rows"],
        "execution_admitted": receipt["execution_admitted"],
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False))
    return 0


__all__ = [
    "BLOCKED",
    "HISTORICAL_EVIDENCE_KINDS",
    "READY",
    "SCHEMA",
    "build_historical_gate_receipt",
    "evaluate_historical_evidence",
    "load_input_rows",
    "main",
    "write_receipt",
]


if __name__ == "__main__":
    raise SystemExit(main())
