"""Prove that a copied paper checkpoint preserves the shared account replay.

The proof consumes one existing persistent paper checkpoint, publishes a
versioned recovery bundle, restores it into a separate directory, and compares
the complete Engine/Ledger account state and event stream.  It is an account
compatibility slice for research state only; it never opens a broker or THS
channel and never changes the source account.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .paper_account_provenance import validate_payloads
from .paper_account_recovery import rehearse_paper_account_recovery


SCHEMA = "quantification_copied_account_replay_proof_v1"
VERIFIED = "VERIFIED_LOCAL_COPIED_ACCOUNT_REPLAY"
DEFAULT_RUN = Path(r"D:\QuantificationData\trading\fixed_daily\20261024_lowvol_current")
DEFAULT_PROOF_ROOT = Path(r"D:\QuantificationData\trading\research\copied_account_replay_proofs\20261024_lowvol_v1")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_copied_account_replay_proof_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_copied_account_replay_proof_v1.md")


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _events_digest(account: Mapping[str, Any]) -> str:
    events = account.get("events", [])
    encoded = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _state_summary(account: Mapping[str, Any]) -> dict[str, Any]:
    events = [event for event in account.get("events", []) if isinstance(event, Mapping)]
    sequences = [event.get("sequence") for event in events]
    return {
        "event_count": len(events),
        "event_sequence_contiguous": sequences == list(range(1, len(events) + 1)),
        "event_digest": _events_digest(account),
        "account_sequence": account.get("sequence"),
        "day": account.get("day"),
        "cash": account.get("cash"),
        "position_count": len(account.get("positions", {}) or {}),
        "order_count": len(account.get("orders", {}) or {}),
        "fill_count": len(account.get("fills", {}) or {}),
        "positions": account.get("positions", {}),
        "orders": account.get("orders", {}),
        "fills": account.get("fills", {}),
    }


def _source_provenance(head_path: Path) -> dict[str, Any]:
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
        paths["raw_snapshot"] /= "raw_daily.parquet"
    run = _json(paths["source_run"])
    replay = _json(paths["replay"])
    account = _json(paths["paper_account"])
    with paths["daily"].open("r", encoding="utf-8", newline="") as handle:
        daily = list(csv.DictReader(handle))
    refs = {role: {"path": str(path), "exists": path.is_file(),
                   "sha256": _sha256(path) if path.is_file() else None}
            for role, path in paths.items()}
    return {"head": head, "account": account,
            "provenance": validate_payloads(head, run, replay, account, daily, references=refs)}


def prove_copied_account_replay(run_dir: str | Path, proof_root: str | Path) -> dict[str, Any]:
    """Create and verify one copied-account replay proof."""
    run_dir = Path(run_dir).resolve()
    proof_root = Path(proof_root).resolve()
    bundle = proof_root / "bundle"
    restored = proof_root / "restored"
    provenance = _source_provenance(run_dir / "head.json")
    if provenance["provenance"]["status"] != "VERIFIED_LOCAL_PAPER_ACCOUNT_PROVENANCE":
        raise ValueError(f"Source paper account is not provenance-verified: {provenance['provenance']['issues']}")
    recovery = rehearse_paper_account_recovery(run_dir, bundle, restored)
    source_account = provenance["account"]
    restored_account = _json(restored / "account" / "account.json")
    source_state = _state_summary(source_account)
    restored_state = _state_summary(restored_account)
    compatibility = {
        "account_json_equal": source_account == restored_account,
        "event_digest_equal": source_state["event_digest"] == restored_state["event_digest"],
        "event_sequence_contiguous": source_state["event_sequence_contiguous"] and restored_state["event_sequence_contiguous"],
        "cash_equal": source_state["cash"] == restored_state["cash"],
        "positions_equal": source_state["positions"] == restored_state["positions"],
        "orders_equal": source_state["orders"] == restored_state["orders"],
        "fills_equal": source_state["fills"] == restored_state["fills"],
        "terminal_day_equal": source_state["day"] == restored_state["day"],
    }
    if not all(compatibility.values()):
        raise ValueError(f"Copied account replay compatibility failed: {compatibility}")
    return {
        "schema": SCHEMA,
        "status": VERIFIED,
        "execution_admitted": False,
        "research_only": True,
        "source_run_dir": str(run_dir),
        "bundle": str(bundle),
        "restore_dir": str(restored),
        "source_state": source_state,
        "restored_state": restored_state,
        "compatibility": compatibility,
        "pipeline_provenance": provenance["provenance"],
        "recovery_status": recovery["status"],
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "network_accessed": False,
        "limitations": [
            "This proves local copied-account replay compatibility for one saved paper checkpoint.",
            "The simulated BrokerAdapter events are not broker fills and this does not prove PIT completeness or live readiness.",
        ],
    }


def build_report(result: Mapping[str, Any]) -> str:
    state = result["source_state"]
    lines = ["# Copied account replay proof", "",
             f"Status: `{result['status']}`; execution: `false`.", "",
             f"The restored account matches the source event stream and terminal state: {state['event_count']} events, {state['order_count']} orders, {state['fill_count']} fills, {state['position_count']} positions, terminal day `{state['day']}`.", "",
             "| Check | Result |", "|---|---|"]
    for key, value in result["compatibility"].items():
        lines.append(f"| {key} | `{str(value).lower()}` |")
    lines += ["", "The proof is research-only and does not create orders, fills, broker actions or THS actions.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--proof-root", type=Path, default=DEFAULT_PROOF_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    result = prove_copied_account_replay(args.run_dir, args.proof_root)
    output, report = args.output.resolve(), args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(build_report(result), encoding="utf-8")
    print(json.dumps({"status": result["status"], "event_count": result["source_state"]["event_count"], "compatibility": result["compatibility"], "execution_admitted": False}, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "VERIFIED", "build_report", "main", "prove_copied_account_replay"]


if __name__ == "__main__":
    raise SystemExit(main())
