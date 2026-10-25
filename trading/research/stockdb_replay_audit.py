"""Audit a completed StockDB rolling replay as a historical research run."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .paper_account_provenance import validate_payloads


SCHEMA = "quantification_stockdb_replay_audit_v1"
VERIFIED = "VERIFIED_STOCKDB_HISTORICAL_REPLAY"
BLOCKED = "STOCKDB_HISTORICAL_REPLAY_AUDIT_BLOCKED"
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_stockdb_historical_replay_audit.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def audit_run(run_path: Path) -> dict[str, Any]:
    run_path = run_path.resolve()
    run = _json(run_path)
    issues: list[str] = []
    if run.get("status") != "completed_research_only":
        issues.append("run.status")
    for key in ("research_only", "execution_admitted", "strategy_validated", "fresh_holdout_claimed"):
        expected = True if key == "research_only" else False
        if run.get(key) is not expected:
            issues.append(f"run.{key}")
    artifacts = Path(run["artifacts"]).resolve()
    protocol_path = artifacts / "protocol.json"
    protocol = _json(protocol_path)
    if protocol.get("calendar_verification", {}).get("entire_replay_calendar_verified") is not True:
        issues.append("protocol.calendar_not_verified")
    if protocol.get("execution_admitted") is not False or protocol.get("research_only") is not True:
        issues.append("protocol.gate")
    variant_receipts: list[dict[str, Any]] = []
    for result in run.get("results", []):
        variant = result.get("signal")
        variant_dir = artifacts / str(variant)
        account_path, replay_path, daily_path = variant_dir / "account.json", variant_dir / "replay.json", variant_dir / "daily.csv"
        account = _json(account_path)
        replay = _json(replay_path)
        with daily_path.open("r", encoding="utf-8", newline="") as handle:
            daily_rows = list(csv.DictReader(handle))
        raw_path = Path(protocol.get("supplemental_raw_view", {}).get("path", "")).resolve()
        source_snapshot = Path(protocol["snapshot"]).resolve()
        refs = {
            "raw_snapshot": {"path": str(raw_path), "exists": raw_path.is_file(), "sha256": _sha256(raw_path) if raw_path.is_file() else None},
            "paper_account": {"path": str(account_path), "exists": True, "sha256": _sha256(account_path)},
            "source_run": {"path": str(run_path), "exists": True, "sha256": _sha256(run_path)},
            "daily": {"path": str(daily_path), "exists": True, "sha256": _sha256(daily_path)},
        }
        head = {
            "execution_admitted": False, "last_completed_day": run.get("replay_end"),
            "registration": {"signal": variant},
            "supplement": {"sha256": protocol.get("supplemental_raw_view", {}).get("path") and refs["raw_snapshot"]["sha256"]},
            "source": {role: {"sha256": refs[key]["sha256"]} for role, key in (
                ("account", "paper_account"), ("run", "source_run"), ("daily", "daily"))},
        }
        local = validate_payloads(head, run, replay, account, daily_rows, references=refs)
        local_issues = list(local["issues"])
        if result.get("status") != "completed" or result.get("completed_sessions") != run.get("sessions"):
            local_issues.append(f"result.{variant}.session_completion")
        local["issues"] = local_issues
        local["status"] = "VERIFIED_LOCAL_PAPER_ACCOUNT_PROVENANCE" if not local_issues else "PAPER_ACCOUNT_PROVENANCE_BLOCKED"
        local["source_snapshot"] = {"path": str(source_snapshot), "manifest_sha256": hashlib.sha256((source_snapshot / "manifest.json").read_bytes()).hexdigest()}
        variant_receipts.append(local)
        issues.extend(f"{variant}:{issue}" for issue in local_issues)
    if not variant_receipts:
        issues.append("run.results_missing")
    return {
        "schema": SCHEMA,
        "status": VERIFIED if not issues else BLOCKED,
        "run": str(run_path), "replay_start": run.get("replay_start"), "replay_end": run.get("replay_end"),
        "sessions": run.get("sessions"), "symbols": run.get("symbols"),
        "variants": variant_receipts, "issues": issues,
        "execution_admitted": False, "strategy_validated": False,
        "network_accessed": False, "broker_or_ths_interacted": False,
        "limitations": [
            "This audits a completed local StockDB replay and its shared Ledger artifacts; it is not PIT validation or an adoption decision.",
            "Historical results use the declared StockDB/supplement source and cost configuration; missing provider rows remain excluded.",
        ],
    }


def run(run_path: Path, output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    receipt = audit_run(run_path)
    receipt["observed_at"] = datetime.now(timezone.utc).isoformat()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    receipt["receipt_path"] = str(output_path)
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    receipt = run(args.run, args.output)
    print(json.dumps({"status": receipt["status"], "replay_start": receipt["replay_start"],
                      "replay_end": receipt["replay_end"], "sessions": receipt["sessions"],
                      "variants": len(receipt["variants"]), "issues": receipt["issues"],
                      "execution_admitted": receipt["execution_admitted"]}, ensure_ascii=False))
    return 0 if receipt["status"] == VERIFIED else 2


__all__ = ["SCHEMA", "VERIFIED", "BLOCKED", "audit_run", "run", "main"]

