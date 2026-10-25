"""Load a versioned F2 source snapshot and apply the historical gate.

The adapter only verifies that the snapshot and referenced source bytes are
the ones recorded by the producer.  It does not infer retrieval/as-of or
revision facts: those fields still have to be present in each row before the
existing PIT gate can return ``HISTORICAL_EVIDENCE_READY``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .f2_historical_evidence import (
    BLOCKED,
    SCHEMA as HISTORICAL_SCHEMA,
    build_historical_gate_receipt,
)


SCHEMA = "f2_source_snapshot_adapter_v1"
SNAPSHOT_SCHEMA = "f2_source_snapshot_v1"


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"source snapshot must be an object: {path}")
    return value


def _source_path(snapshot_path: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (snapshot_path.parent / path).resolve()


def load_source_snapshot(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read and verify one snapshot manifest without promoting any row."""

    snapshot_path = Path(path).expanduser().resolve()
    payload = _read(snapshot_path)
    if payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("unsupported F2 source snapshot schema")
    snapshot_id = payload.get("snapshot_id")
    captured_at = payload.get("captured_at")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("source snapshot requires snapshot_id")
    if not isinstance(captured_at, str) or "T" not in captured_at:
        raise ValueError("source snapshot requires offset-aware captured_at")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("source snapshot requires a non-empty rows list")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"source snapshot row {index} must be an object")
        row = dict(raw)
        source_file = row.get("source_file")
        expected_hash = row.get("source_sha256")
        if not isinstance(source_file, str) or not source_file.strip():
            row["snapshot_source_check"] = "missing_source_file"
            row["snapshot_source_check_passed"] = False
        elif not isinstance(expected_hash, str) or len(expected_hash) != 64:
            row["snapshot_source_check"] = "missing_source_sha256"
            row["snapshot_source_check_passed"] = False
        else:
            source_path = _source_path(snapshot_path, source_file)
            row["source_file"] = str(source_path)
            if not source_path.is_file():
                row["snapshot_source_check"] = "source_file_missing"
                row["snapshot_source_check_passed"] = False
            else:
                actual_hash = _sha256(source_path)
                row["snapshot_source_check"] = (
                    "source_hash_verified" if actual_hash.lower() == expected_hash.lower()
                    else "source_hash_mismatch"
                )
                row["snapshot_source_check_passed"] = actual_hash.lower() == expected_hash.lower()
                # Keep the producer's expected hash in the row.  On mismatch
                # the downstream gate must see provenance as unproven rather
                # than silently replacing evidence with the observed hash.
        row["evidence_kind"] = "source_snapshot"
        row["source_snapshot_id"] = snapshot_id
        rows.append(row)
    metadata = {
        "snapshot_path": str(snapshot_path),
        "snapshot_id": snapshot_id,
        "captured_at": captured_at,
        "row_count": len(rows),
    }
    return metadata, rows


def build_source_snapshot_receipt(
    path: str | Path,
    *,
    decision_day: str,
) -> dict[str, Any]:
    """Verify a source snapshot and run its rows through the existing gate."""

    metadata, rows = load_source_snapshot(path)
    gate_rows = [
        row if row.get("snapshot_source_check_passed") is True
        else {**row, "source_sha256": None}
        for row in rows
    ]
    gate = build_historical_gate_receipt(
        gate_rows,
        decision_day=decision_day,
        source_description=f"source_snapshot:{metadata['snapshot_id']}",
    )
    source_checks = {
        "verified": sum(row.get("snapshot_source_check_passed") is True for row in rows),
        "failed": sum(row.get("snapshot_source_check_passed") is not True for row in rows),
    }
    status = gate["status"] if source_checks["failed"] == 0 else BLOCKED
    return {
        "schema": SCHEMA,
        "status": status,
        "decision_day": decision_day,
        "historical_gate_schema": HISTORICAL_SCHEMA,
        "snapshot": metadata,
        "source_checks": source_checks,
        "historical_gate": gate,
        "execution_admitted": False,
        "factor_computed": False,
        "account_replay": False,
        "limitations": [
            "Hash verification proves snapshot bytes, not historical availability.",
            "Rows without retrieval/as-of and revision evidence remain BLOCKED_DATA.",
        ],
    }


def write_source_snapshot_receipt(
    output: str | Path,
    path: str | Path,
    *,
    decision_day: str,
) -> dict[str, Any]:
    receipt = build_source_snapshot_receipt(path, decision_day=decision_day)
    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


__all__ = [
    "BLOCKED",
    "SCHEMA",
    "SNAPSHOT_SCHEMA",
    "build_source_snapshot_receipt",
    "load_source_snapshot",
    "write_source_snapshot_receipt",
]
