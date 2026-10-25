"""Audit the candidate-strategy catalog and its evidence boundaries.

This read-only audit makes the candidate library reproducible without ranking
results from different snapshots.  It checks that every catalog entry has a
real evidence path, machine-readable metrics and an explicit limitation.  It
does not promote a diagnostic to a strategy or open any execution channel.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_candidate_catalog_audit_v1"
VERIFIED = "VERIFIED_CANDIDATE_CATALOG_READONLY"
BLOCKED = "CANDIDATE_CATALOG_AUDIT_BLOCKED"
DEFAULT_CATALOG = Path("docs/architecture_v3/candidate_strategy_catalog_20261024.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_candidate_catalog_audit.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_evidence(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def audit_catalog(catalog: Mapping[str, Any], *, root: Path, catalog_hash: str | None = None) -> dict[str, Any]:
    issues: list[str] = []
    if catalog.get("execution_admitted") is not False:
        issues.append("catalog.execution_admitted")
    if not isinstance(catalog.get("selection_policy"), str) or not catalog["selection_policy"].strip():
        issues.append("catalog.selection_policy")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        issues.append("catalog.entries")
        entries = []
    ids: set[str] = set()
    families: Counter[str] = Counter()
    states: Counter[str] = Counter()
    evidence_count = 0
    missing_evidence: list[str] = []
    for index, entry in enumerate(entries, start=1):
        prefix = f"entry.{index}"
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier:
            issues.append(f"{prefix}.id")
            identifier = f"#{index}"
        elif identifier in ids:
            issues.append(f"{prefix}.duplicate_id")
        ids.add(identifier)
        for key in ("family", "state", "comparability_group"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                issues.append(f"{prefix}.{key}")
        families[str(entry.get("family", ""))] += 1
        states[str(entry.get("state", ""))] += 1
        metrics = entry.get("metrics")
        if not isinstance(metrics, dict):
            issues.append(f"{prefix}.metrics")
        else:
            for key, value in metrics.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    issues.append(f"{prefix}.metrics.{key}")
        limits = entry.get("limits")
        if not isinstance(limits, list) or not limits or any(not isinstance(value, str) or not value.strip() for value in limits):
            issues.append(f"{prefix}.limits")
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            issues.append(f"{prefix}.evidence")
            continue
        for evidence_value in evidence:
            if not isinstance(evidence_value, str) or not evidence_value.strip():
                issues.append(f"{prefix}.evidence_value")
                continue
            evidence_count += 1
            path = _resolve_evidence(root, evidence_value)
            if not path.is_file():
                missing_evidence.append(f"{identifier}:{evidence_value}")
    issues.extend(f"missing_evidence.{value}" for value in missing_evidence)
    return {
        "schema": SCHEMA,
        "status": VERIFIED if not issues else BLOCKED,
        "catalog_entries": len(entries),
        "unique_entry_ids": len(ids),
        "families": dict(sorted(families.items())),
        "states": dict(sorted(states.items())),
        "evidence_references": evidence_count,
        "missing_evidence": missing_evidence,
        "issues": issues,
        "catalog_sha256": catalog_hash,
        "execution_admitted": False,
        "client_interacted": False,
        "orders_created": False,
        "selection_enabled": False,
        "limitations": [
            "This is a read-only catalog/evidence audit; it does not rerun strategies or rank mixed snapshots.",
            "Diagnostic and paper results remain separate from PIT validation and strategy adoption.",
            "No login, prefill, submit, cancel, transfer or paid data action is attempted.",
        ],
    }


def run(catalog_path: Path = DEFAULT_CATALOG, output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    catalog_path = catalog_path.resolve()
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict):
        raise ValueError("Candidate catalog must be a JSON object")
    receipt = audit_catalog(catalog, root=Path.cwd().resolve(), catalog_hash=_sha256(catalog_path))
    receipt["observed_at"] = datetime.now(timezone.utc).isoformat()
    receipt["catalog_path"] = str(catalog_path)
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    receipt["receipt_path"] = str(output_path)
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    receipt = run(args.catalog, args.output)
    print(json.dumps({"status": receipt["status"], "catalog_entries": receipt["catalog_entries"],
                      "missing_evidence": receipt["missing_evidence"],
                      "execution_admitted": receipt["execution_admitted"]}, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "VERIFIED", "BLOCKED", "audit_catalog", "run", "main"]

