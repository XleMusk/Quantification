"""Audit the local paper plan and its target/THS evidence boundary.

The audit binds four already-produced artifacts: the paper-plan receipt, the
StockDB close reconciliation, the fixed-daily continuity receipt and the THS
read-only request.  It is deliberately read-only and treats a historical
StockDB mark as a paper reference, never as a current quote or prefill input.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "quantification_paper_plan_reconciliation_audit_v1"
VERIFIED = "VERIFIED_LOCAL_PAPER_PLAN_RECONCILIATION"
BLOCKED = "RECONCILIATION_AUDIT_BLOCKED"
DEFAULT_PLAN = Path("outputs/paper_plans/20261024_lowvol_current_paper_plan_receipt.json")
DEFAULT_RECON = Path("outputs/paper_plans/20261024_lowvol_current_paper_reconciliation/receipt.json")
DEFAULT_CONTINUITY = Path("outputs/paper_plans/20261024_lowvol_current_continuity_receipt.json")
DEFAULT_THS = Path("outputs/paper_plans/20261024_lowvol_current_ths_readonly_request/receipt.json")
DEFAULT_THS_CSV = Path("outputs/paper_plans/20261024_lowvol_current_ths_readonly_request/target_reconciliation.csv")
DEFAULT_OUTPUT = Path("outputs/paper_plans/20261024_lowvol_current_plan_reconciliation_audit")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def audit_payloads(
    plan: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    continuity: Mapping[str, Any],
    ths: Mapping[str, Any],
    ths_rows: Sequence[Mapping[str, Any]],
    *,
    source_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return an evidence-bound audit without writing or mutating any input."""
    issues: list[str] = []
    expected_false = {
        "execution_admitted": False,
        "prefill_allowed": False,
    }
    for name, payload in (("plan", plan), ("reconciliation", reconciliation), ("continuity", continuity), ("ths", ths)):
        if payload.get("execution_admitted") is not False:
            issues.append(f"{name}.execution_admitted")
    for key, expected in expected_false.items():
        if plan.get(key) is not expected:
            issues.append(f"plan.{key}")
    if plan.get("broker_connected") is not False or plan.get("ths_client_interacted") is not False:
        issues.append("plan.live_channel_flags")
    if reconciliation.get("status") != "PAPER_CLOSE_REFERENCE_READY":
        issues.append("reconciliation.status")
    if reconciliation.get("orders_created") is not False or reconciliation.get("ledger_mutated") is not False:
        issues.append("reconciliation.mutation_flags")
    if reconciliation.get("missing_symbols") or reconciliation.get("inactive_symbols"):
        issues.append("reconciliation.coverage")
    if continuity.get("status") != "VERIFIED_READONLY_CURRENT_CUTOFF":
        issues.append("continuity.status")
    if continuity.get("pending_targets") is not False or continuity.get("client_interacted") is not False:
        issues.append("continuity.pending_or_client_flags")
    ths_flags = {
        "client_interacted": False,
        "prefill_allowed": False,
        "submitted": False,
        "cancelled": False,
        "readback_observed": False,
    }
    for key, expected in ths_flags.items():
        if ths.get(key) is not expected:
            issues.append(f"ths.{key}")

    target_weights = dict(plan.get("historical_target", {}).get("weights") or {})
    rows = list(reconciliation.get("rows") or [])
    recon_weights = {row.get("symbol"): row.get("target_weight") for row in rows}
    if not target_weights or set(target_weights) != set(recon_weights):
        issues.append("target.symbol_set")
    else:
        for symbol, weight in target_weights.items():
            if recon_weights.get(symbol) != weight:
                issues.append(f"target.weight.{symbol}")
    if reconciliation.get("target_count") != len(target_weights):
        issues.append("target.count")
    if len(rows) != len(target_weights):
        issues.append("target.row_count")
    if reconciliation.get("day") != plan.get("seed_last_completed_day"):
        issues.append("target.day_vs_plan")
    if reconciliation.get("day") != continuity.get("last_completed_day"):
        issues.append("target.day_vs_continuity")
    if any(row.get("ths_prefill_status") != "reference_only_not_prefillable" for row in rows):
        issues.append("target.prefill_status")

    # The original THS CSV must remain an unobserved blank template.  This is
    # intentionally stricter than merely trusting readback_observed=false.
    for index, row in enumerate(ths_rows, start=1):
        if row.get("prefill_status") != "reference_only_not_prefillable":
            issues.append(f"ths_csv.{index}.prefill_status")
        for key in ("same_session_price_rmb", "estimated_target_qty", "delta_qty"):
            if str(row.get(key, "")).strip():
                issues.append(f"ths_csv.{index}.{key}_populated")
    if len(ths_rows) != len(target_weights):
        issues.append("ths_csv.row_count")

    # Reconcile the producer's own hash bindings with the files audited here.
    # Without this cross-check a receipt could be structurally valid while
    # referring to an older paper plan or continuity checkpoint.
    if source_hashes:
        bound = reconciliation.get("source_hashes") or {}
        if bound.get("paper_plan_sha256") and bound["paper_plan_sha256"] != source_hashes.get("paper_plan"):
            issues.append("binding.paper_plan_hash")
        if bound.get("paper_account_sha256") and continuity.get("source_hashes", {}).get("account"):
            if bound["paper_account_sha256"] != continuity["source_hashes"]["account"]:
                issues.append("binding.paper_account_hash")
        if bound.get("source_run_sha256") and continuity.get("source_hashes", {}).get("run"):
            if bound["source_run_sha256"] != continuity["source_hashes"]["run"]:
                issues.append("binding.source_run_hash")

    status = VERIFIED if not issues else BLOCKED
    return {
        "schema": SCHEMA,
        "status": status,
        "source_day": reconciliation.get("day"),
        "target_count": len(target_weights),
        "paper_reconciliation_status": reconciliation.get("status"),
        "continuity_status": continuity.get("status"),
        "ths_status": ths.get("status"),
        "issues": issues,
        "execution_admitted": False,
        "client_interacted": False,
        "orders_created": False,
        "account_advanced": False,
        "readback_observed": False,
        "source_hashes": dict(source_hashes or {}),
        "limitations": [
            "This audit binds local paper and read-only reference artifacts only.",
            "StockDB close marks do not prove a current broker quote, account balance or THS readback.",
            "No login, prefill, submit, cancel, transfer or Ledger mutation is attempted.",
        ],
    }


def run(plan_path: Path = DEFAULT_PLAN, reconciliation_path: Path = DEFAULT_RECON,
        continuity_path: Path = DEFAULT_CONTINUITY, ths_path: Path = DEFAULT_THS,
        ths_csv_path: Path = DEFAULT_THS_CSV, output_dir: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    paths = {
        "paper_plan": plan_path.resolve(), "reconciliation": reconciliation_path.resolve(),
        "continuity": continuity_path.resolve(), "ths_request": ths_path.resolve(),
        "ths_target_reconciliation": ths_csv_path.resolve(),
    }
    payloads = {
        "paper_plan": _read_json(paths["paper_plan"]),
        "reconciliation": _read_json(paths["reconciliation"]),
        "continuity": _read_json(paths["continuity"]),
        "ths_request": _read_json(paths["ths_request"]),
    }
    source_hashes = {name: _sha256(path) for name, path in paths.items()}
    receipt = audit_payloads(
        payloads["paper_plan"], payloads["reconciliation"], payloads["continuity"],
        payloads["ths_request"], _read_csv(paths["ths_target_reconciliation"]),
        source_hashes=source_hashes,
    )
    receipt["observed_at"] = datetime.now(timezone.utc).isoformat()
    receipt["source_paths"] = {name: str(path) for name, path in paths.items()}
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "receipt.json"
    receipt["artifact"] = {"receipt_json": str(output_path)}
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--reconciliation", type=Path, default=DEFAULT_RECON)
    parser.add_argument("--continuity", type=Path, default=DEFAULT_CONTINUITY)
    parser.add_argument("--ths-request", type=Path, default=DEFAULT_THS)
    parser.add_argument("--ths-csv", type=Path, default=DEFAULT_THS_CSV)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = run(args.paper_plan, args.reconciliation, args.continuity, args.ths_request,
                 args.ths_csv, args.output_dir)
    print(json.dumps({"status": result["status"], "source_day": result["source_day"],
                      "target_count": result["target_count"], "issues": result["issues"],
                      "execution_admitted": result["execution_admitted"]}, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "VERIFIED", "BLOCKED", "audit_payloads", "run", "main"]

