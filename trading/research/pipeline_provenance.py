"""Validate the saved Data-to-account provenance of a paper/replay result.

This is a read-only validator.  It does not rerun a producer, advance an
account, query a broker or mutate a ledger.  The validator only accepts an
already saved replay result and records the references that support each
pipeline stage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA = "quantification_pipeline_provenance_validation_v1"
STAGES = ["Data", "Signal", "Target Portfolio", "Orders", "BrokerAdapter", "Fill", "Cash/Position"]
_SAFE_FALSE_FLAGS = (
    "execution_admitted",
    "ledger_mutated",
    "strategy_validated",
    "ths_payload_generated",
)


def _read(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).resolve()
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Replay result must be a JSON object: {resolved}")
    return value


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reference(value: str | Path) -> dict[str, Any]:
    path = Path(value).resolve()
    record: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if path.is_file():
        record.update({"kind": "file", "bytes": path.stat().st_size, "sha256": _sha256(path)})
    elif path.is_dir():
        manifest = path / "manifest.json"
        if manifest.is_file():
            record.update({"kind": "directory", "manifest": str(manifest), "manifest_sha256": _sha256(manifest)})
        else:
            record["kind"] = "directory"
    else:
        record["kind"] = "missing"
    return record


def _events(result: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for day in result.get("daily", []):
        if not isinstance(day, dict):
            continue
        for event in day.get("events", []):
            if isinstance(event, dict):
                events.append(event)
    return events


def validate_pipeline_provenance(result_path: str | Path, comparison_receipt: str | Path | None = None,
                                 *, observed_at: str | None = None) -> dict[str, Any]:
    """Validate one saved replay result and return a machine-readable receipt."""

    result_file = Path(result_path).resolve()
    result = _read(result_file)
    issues: list[str] = []
    events = _events(result)
    kinds = {event.get("kind") for event in events}
    references: dict[str, dict[str, Any]] = {"result": _reference(result_file)}

    # A replay result must retain its source lineage.  A directory is accepted
    # only as a reference; its manifest hash is captured when one is present.
    source_fields = {
        "raw_snapshot": result.get("raw_snapshot"),
        "score_path": result.get("score_path"),
        "batch_run": result.get("batch_run"),
    }
    for role, value in source_fields.items():
        if not isinstance(value, str) or not value:
            issues.append(f"missing_{role}")
            continue
        reference = _reference(value)
        references[role] = reference
        if not reference["exists"]:
            issues.append(f"missing_reference:{role}")

    if not isinstance(result.get("strategy_id"), str) or not result["strategy_id"]:
        issues.append("missing_strategy_id")
    if not isinstance(result.get("selected_events"), list) or not result["selected_events"]:
        issues.append("missing_signal_events")
    if not result.get("engine_ledger_replay_completed"):
        issues.append("engine_ledger_replay_not_completed")
    if not result.get("target_holdings_generated"):
        issues.append("targets_not_generated")
    if not result.get("paper_orders_generated"):
        issues.append("paper_orders_not_generated")

    expected_false = list(_SAFE_FALSE_FLAGS)
    for flag in expected_false:
        if result.get(flag) is not False:
            issues.append(f"{flag}_must_be_false")

    # Explicit stage evidence.  The event names are the existing shared
    # Engine/Ledger vocabulary, so this check is reusable across candidates.
    stage_evidence = {
        "Data": bool(references.get("raw_snapshot", {}).get("exists")) and bool(result.get("selected_events")),
        "Signal": bool(result.get("strategy_id")) and "targets_generated" in kinds,
        "Target Portfolio": "targets_generated" in kinds,
        "Orders": bool(result.get("orders")) and "submitted" in kinds,
        "BrokerAdapter": "submitted" in kinds,
        "Fill": bool(result.get("fills")) and bool(kinds.intersection({"partial", "filled"})),
        "Cash/Position": isinstance(result.get("state"), dict)
        and "cash" in result["state"] and "positions" in result["state"]
        and bool(result.get("daily")),
    }
    for stage, present in stage_evidence.items():
        if not present:
            issues.append(f"missing_stage:{stage}")

    sequences = [event.get("sequence") for event in events]
    if any(type(seq) is not int for seq in sequences):
        issues.append("event_sequence_missing_or_non_integer")
    elif sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        issues.append("event_sequence_not_strictly_increasing")

    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    last_daily = result.get("daily", [])[-1] if result.get("daily") else {}
    if isinstance(last_daily, dict) and "equity" in last_daily and "final_equity" in metrics:
        if float(last_daily["equity"]) != float(metrics["final_equity"]):
            issues.append("final_equity_daily_mismatch")
    if isinstance(state.get("orders"), list) and isinstance(result.get("orders"), list):
        if len(state["orders"]) != len(result["orders"]):
            issues.append("state_orders_mismatch")
    if isinstance(state.get("fills"), list) and isinstance(result.get("fills"), list):
        if len(state["fills"]) != len(result["fills"]):
            issues.append("state_fills_mismatch")

    comparison_path = None
    comparison = None
    if comparison_receipt is not None:
        comparison_path = Path(comparison_receipt).resolve()
        comparison = _read(comparison_path)
        references["comparison_receipt"] = _reference(comparison_path)
        matches = [item for item in comparison.get("candidates", [])
                   if isinstance(item, dict) and Path(item.get("result", "")).resolve() == result_file]
        if not matches:
            issues.append("result_not_bound_to_comparison_receipt")
        if comparison.get("execution_admitted") is not False:
            issues.append("comparison_execution_gate")
        contract = comparison.get("comparison_contract", {})
        if contract.get("same_window") is False or contract.get("cost_config_equal") is False:
            issues.append("comparison_contract_not_comparable")
        snapshot_reference = references.get("raw_snapshot", {})
        expected_manifest_hash = contract.get("price_snapshot_manifest_sha256")
        actual_manifest_hash = snapshot_reference.get("manifest_sha256")
        if expected_manifest_hash and actual_manifest_hash != expected_manifest_hash:
            issues.append("comparison_snapshot_manifest_hash_mismatch")

    stamp = observed_at or datetime.now(timezone.utc).isoformat()
    return {
        "schema": SCHEMA,
        "status": "VERIFIED_LOCAL_PIPELINE_PROVENANCE" if not issues else "PIPELINE_PROVENANCE_BLOCKED",
        "observed_at": stamp,
        "result": str(result_file),
        "comparison_receipt": str(comparison_path) if comparison_path else None,
        "strategy_id": result.get("strategy_id"),
        "stages": {stage: stage_evidence[stage] for stage in STAGES},
        "event_kinds": sorted(kind for kind in kinds if isinstance(kind, str)),
        "event_count": len(events),
        "order_count": len(result.get("orders", [])) if isinstance(result.get("orders"), list) else 0,
        "fill_count": len(result.get("fills", [])) if isinstance(result.get("fills"), list) else 0,
        "final_cash": state.get("cash"),
        "final_positions": len(state.get("positions", {})) if isinstance(state.get("positions"), dict) else None,
        "references": references,
        "issues": issues,
        "execution_admitted": False,
        "ledger_mutated": False,
        "strategy_validated": False,
        "network_accessed": False,
        "broker_or_ths_interacted": False,
        "limitations": [
            "This receipt validates a saved local replay; it does not prove point-in-time source quality or live readiness.",
            "The validator does not rerun a strategy, advance a paper account or access a broker/THS client.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True)
    parser.add_argument("--comparison-receipt")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    receipt = validate_pipeline_provenance(args.result, args.comparison_receipt)
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "out": str(output), "issues": receipt["issues"]}, ensure_ascii=False))
    return 0 if receipt["status"] == "VERIFIED_LOCAL_PIPELINE_PROVENANCE" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "STAGES", "validate_pipeline_provenance", "main"]
