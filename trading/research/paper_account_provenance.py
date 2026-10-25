"""Validate the fixed-strategy paper account's complete local pipeline.

The saved account already contains the Engine/Ledger event stream.  This
read-only validator binds that stream to the pinned StockDB view, run/replay
receipts and daily account file, proving the seven local stages without
opening a broker or THS channel.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "quantification_paper_account_provenance_v1"
VERIFIED = "VERIFIED_LOCAL_PAPER_ACCOUNT_PROVENANCE"
BLOCKED = "PAPER_ACCOUNT_PROVENANCE_BLOCKED"
STAGES = ("Data", "Signal", "Target Portfolio", "Orders", "BrokerAdapter", "Fill", "Cash/Position")
DEFAULT_HEAD = Path(r"D:\QuantificationData\trading\fixed_daily\20261024_lowvol_current\head.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_lowvol_paper_account_provenance.json")


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


def validate_payloads(
    head: Mapping[str, Any], run: Mapping[str, Any], replay: Mapping[str, Any],
    account: Mapping[str, Any], daily_rows: Sequence[Mapping[str, Any]],
    *, references: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    events = [event for event in account.get("events", []) if isinstance(event, Mapping)]
    kinds = {event.get("kind") for event in events}
    sequences = [event.get("sequence") for event in events]
    if any(type(value) is not int for value in sequences):
        issues.append("event_sequence_missing")
    elif sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        issues.append("event_sequence_not_strict")
    if head.get("execution_admitted") is not False:
        issues.append("head.execution_admitted")
    if run.get("execution_admitted") is not False:
        issues.append("run.execution_admitted")
    if replay.get("status") != "completed":
        issues.append("replay.status")
    if not isinstance(account.get("targets"), dict) or not account["targets"]:
        issues.append("targets_missing")
    if not isinstance(account.get("orders"), dict) or not account["orders"]:
        issues.append("orders_missing")
    if not isinstance(account.get("fills"), dict) or not account["fills"]:
        issues.append("fills_missing")
    if not isinstance(account.get("positions"), dict) or "cash" not in account:
        issues.append("cash_position_missing")
    if not daily_rows:
        issues.append("daily_rows_missing")
    dates = [str(row.get("date", "")) for row in daily_rows]
    if len(dates) != len(set(dates)):
        issues.append("daily_dates_duplicate")
    expected_day = head.get("last_completed_day")
    if not dates or dates[-1] != expected_day or account.get("day") != expected_day:
        issues.append("last_completed_day_mismatch")
    if references:
        for role, expected in (("raw_snapshot", True), ("paper_account", True), ("source_run", True), ("daily", True)):
            ref = references.get(role, {})
            if ref.get("exists") is not expected:
                issues.append(f"missing_reference:{role}")
        expected_hashes = {
            "raw_snapshot": head.get("supplement", {}).get("sha256"),
            "paper_account": head.get("source", {}).get("account", {}).get("sha256"),
            "source_run": head.get("source", {}).get("run", {}).get("sha256"),
            "daily": head.get("source", {}).get("daily", {}).get("sha256"),
        }
        for role, expected_hash in expected_hashes.items():
            actual = references.get(role, {}).get("sha256")
            if expected_hash and actual and expected_hash != actual:
                issues.append(f"hash_mismatch:{role}")
    stage_evidence = {
        "Data": bool(references and references.get("raw_snapshot", {}).get("exists"))
        and bool(head.get("supplement", {}).get("sha256")),
        "Signal": "targets_generated" in kinds and bool(account.get("candidates") is not None),
        "Target Portfolio": "targets_generated" in kinds and bool(account.get("targets")),
        "Orders": bool(account.get("orders")) and "submitted" in kinds,
        "BrokerAdapter": "submitted" in kinds,
        "Fill": bool(account.get("fills")) and bool(kinds.intersection({"partial", "filled"})),
        "Cash/Position": bool(account.get("cash") is not None and isinstance(account.get("positions"), dict)
                           and daily_rows),
    }
    for stage, present in stage_evidence.items():
        if not present:
            issues.append(f"missing_stage:{stage}")
    receipt = {
        "schema": SCHEMA,
        "status": VERIFIED if not issues else BLOCKED,
        "strategy_id": str(head.get("registration", {}).get("signal", "low_volatility_252_20")),
        "last_completed_day": expected_day,
        "daily_rows": len(daily_rows),
        "event_count": len(events),
        "event_kinds": sorted(kind for kind in kinds if isinstance(kind, str)),
        "order_count": len(account.get("orders", {})),
        "fill_count": len(account.get("fills", {})),
        "position_count": len(account.get("positions", {})),
        "stages": stage_evidence,
        "issues": issues,
        "references": dict(references or {}),
        "execution_admitted": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "network_accessed": False,
        "limitations": [
            "This validates a saved local paper account and its event stream; it does not prove PIT completeness or live readiness.",
            "The account is research-only and its simulated BrokerAdapter events are not real broker fills.",
        ],
    }
    return receipt


def run(head_path: Path = DEFAULT_HEAD, output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    head_path = head_path.resolve()
    head = _json(head_path)
    source = head["source"]
    paths = {
        "source_run": Path(source["run"]["path"]).resolve(),
        "paper_account": Path(source["account"]["path"]).resolve(),
        "replay": Path(source["replay"]["path"]).resolve(),
        "daily": Path(source["daily"]["path"]).resolve(),
        "raw_snapshot": Path(head["supplement"]["path"]).resolve(),
    }
    if paths["raw_snapshot"].is_dir():
        paths["raw_snapshot"] = paths["raw_snapshot"] / "raw_daily.parquet"
    run_payload = _json(paths["source_run"])
    replay = _json(paths["replay"])
    account = _json(paths["paper_account"])
    with paths["daily"].open("r", encoding="utf-8", newline="") as handle:
        daily_rows = list(csv.DictReader(handle))
    refs = {
        role: {"path": str(path), "exists": path.is_file(), "sha256": _sha256(path) if path.is_file() else None}
        for role, path in paths.items()
    }
    receipt = validate_payloads(head, run_payload, replay, account, daily_rows, references=refs)
    receipt["observed_at"] = datetime.now(timezone.utc).isoformat()
    receipt["head"] = {"path": str(head_path), "sha256": _sha256(head_path)}
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    receipt["receipt_path"] = str(output_path)
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", type=Path, default=DEFAULT_HEAD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    receipt = run(args.head, args.output)
    print(json.dumps({"status": receipt["status"], "last_completed_day": receipt["last_completed_day"],
                      "stages": receipt["stages"], "issues": receipt["issues"],
                      "execution_admitted": receipt["execution_admitted"]}, ensure_ascii=False))
    return 0 if receipt["status"] == VERIFIED else 2


__all__ = ["SCHEMA", "VERIFIED", "BLOCKED", "STAGES", "validate_payloads", "run", "main"]

