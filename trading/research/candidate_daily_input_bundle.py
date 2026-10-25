"""Build a reproducible D1 input manifest from registered replay results.

Strategy runners remain responsible for producing a terminal StockDB replay
result.  This adapter turns those explicit research-only results into target
and quote payloads for ``daily-candidate-paper-batch``.  It never fetches
data, mutates an account, or contacts a broker/THS client.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .candidate_shadow_paper import _target_payload


SCHEMA = "candidate_daily_input_bundle_v1"
STATUS = "COMPLETED_RESEARCH_ONLY_CANDIDATE_INPUT_BUNDLE"
DEFAULT_CATALOG = Path("docs/architecture_v3/candidate_strategy_catalog_20261024.json")
GATE_MODES = {"research_followup", "separate_gate", "historical_shadow"}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _candidate_id(item: Mapping[str, Any]) -> str:
    value = item.get("candidate_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Each candidate requires a non-empty candidate_id")
    return value.strip()


def _source(item: Mapping[str, Any]) -> tuple[Path, bool]:
    replay = item.get("replay_result")
    account = item.get("terminal_account")
    if bool(replay) == bool(account):
        raise ValueError("Each candidate requires exactly one replay_result or terminal_account")
    path = Path(replay or account).resolve()
    if not path.is_file():
        raise ValueError(f"Replay source does not exist: {path}")
    return path, account is not None


def _catalog(path: str | Path | None) -> tuple[dict[str, Mapping[str, Any]], dict[str, Any] | None]:
    if path is None:
        return {}, None
    catalog_file = Path(path).resolve()
    payload = _read(catalog_file)
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Candidate catalog entries are missing")
    by_id = {str(entry.get("id")): entry for entry in entries if isinstance(entry, Mapping)}
    return by_id, {"path": str(catalog_file), "sha256": _sha256(catalog_file)}


def _catalog_gate(entry: Mapping[str, Any], candidate_id: str, gate_mode: str) -> dict[str, Any]:
    if gate_mode not in GATE_MODES:
        raise ValueError(f"Unsupported candidate gate mode: {gate_mode}")
    selection = entry.get("research_selection")
    if not isinstance(selection, Mapping):
        raise ValueError(f"Candidate catalog selection is missing: {candidate_id}")
    followup_allowed = selection.get("research_followup_allowed") is True
    if gate_mode == "research_followup" and not followup_allowed:
        raise ValueError(f"Candidate is not enabled for research follow-up: {candidate_id}")
    if gate_mode == "separate_gate":
        if followup_allowed:
            raise ValueError(f"Candidate is not a separate-gate candidate: {candidate_id}")
        decision = selection.get("decision") or selection.get("action")
        if decision != "UNASSESSED_SEPARATE_GATE":
            raise ValueError(f"Separate-gate candidate lacks explicit unassessed decision: {candidate_id}")
    if gate_mode == "historical_shadow":
        decision = selection.get("decision") or selection.get("action")
        if decision not in {"STOP_RESEARCH_ONLY", "BLOCKED_COVERAGE"}:
            raise ValueError(f"Historical shadow requires a stopped or coverage-blocked candidate: {candidate_id}")
    return dict(selection)


def build_input_bundle(candidates: Sequence[Mapping[str, Any]], output_dir: str | Path, *,
                       catalog_path: str | Path | None = None,
                       gate_mode: str = "research_followup") -> dict[str, Any]:
    if not candidates:
        raise ValueError("At least one candidate replay source is required")
    identifiers = [_candidate_id(item) for item in candidates]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Candidate identifiers must be unique")
    if gate_mode not in GATE_MODES:
        raise ValueError(f"Unsupported candidate gate mode: {gate_mode}")
    catalog_entries, catalog_ref = _catalog(catalog_path)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_candidates: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for item, candidate_id in zip(candidates, identifiers):
        decision_id = item.get("decision_id")
        if decision_id is not None and (not isinstance(decision_id, str) or not decision_id.strip()):
            raise ValueError(f"Candidate decision_id must be a non-empty string: {candidate_id}")
        catalog_entry = catalog_entries.get(candidate_id)
        catalog_selection = None
        if catalog_path is not None:
            if catalog_entry is None:
                raise ValueError(f"Candidate is absent from the registered catalog: {candidate_id}")
            catalog_selection = _catalog_gate(catalog_entry, candidate_id, gate_mode)
        source_path, legacy_account = _source(item)
        source_payload = _read(source_path)
        source_strategy = source_payload.get("strategy_id")
        if source_strategy is None and isinstance(source_payload.get("config"), Mapping):
            source_strategy = source_payload["config"].get("strategy_id")
        requested_strategy = item.get("strategy_id")
        if (isinstance(requested_strategy, str) and isinstance(source_strategy, str)
                and requested_strategy != source_strategy):
            raise ValueError(f"Candidate strategy_id does not match replay source: {candidate_id}")
        target, quote = _target_payload(
            source_payload,
            legacy_account=legacy_account,
            strategy_id=item.get("strategy_id"),
        )
        strategy_id = target.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id:
            raise ValueError(f"Candidate source has no strategy_id: {candidate_id}")
        candidate_output = output / candidate_id
        candidate_output.mkdir(parents=True, exist_ok=True)
        target_path = candidate_output / "target_payload.json"
        quote_path = candidate_output / "quote_payload.json"
        target_path.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        quote_path.write_text(json.dumps(quote, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        target_ref = {"path": str(target_path), "sha256": _sha256(target_path)}
        quote_ref = {"path": str(quote_path), "sha256": _sha256(quote_path)}
        source_ref = {"path": str(source_path), "sha256": _sha256(source_path)}
        manifest_candidate = {"candidate_id": candidate_id, "strategy_id": strategy_id,
                              "target": str(target_path), "quotes": str(quote_path)}
        if decision_id is not None:
            manifest_candidate["decision_id"] = decision_id.strip()
        manifest_candidates.append(manifest_candidate)
        record = {
            "candidate_id": candidate_id,
            "strategy_id": strategy_id,
            "source": source_ref,
            "source_kind": "legacy_terminal_account" if legacy_account else "nested_replay_result",
            "decision_day": target["decision_day"],
            "target": target_ref,
            "quotes": quote_ref,
            "catalog_selection": catalog_selection,
            "gate_mode": gate_mode,
        }
        if decision_id is not None:
            record["decision_id"] = decision_id.strip()
        records.append(record)
    manifest = {
        "schema": SCHEMA,
        "status": STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "catalog": catalog_ref,
        "gate_mode": gate_mode,
        "candidates": manifest_candidates,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "schema": SCHEMA,
        "status": STATUS,
        "created_at": manifest["created_at"],
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "manifest": {"path": str(manifest_path), "sha256": _sha256(manifest_path),
                     "candidate_count": len(records)},
        "catalog": catalog_ref,
        "gate_mode": gate_mode,
        "candidate_count": len(records),
        "candidates": records,
        "limitations": [
            "Replay results are explicit inputs; this bundle does not fetch StockDB data or create signals.",
            "Payloads are research-only inputs for local paper projection, not broker instructions.",
            "The candidate batch must apply execution-day and quote-day validation before projection.",
        ],
    }
    receipt_path = output / "bundle_receipt.json"
    receipt["receipt_path"] = str(receipt_path)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="JSON list or object with candidates and replay_result/terminal_account paths")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--gate-mode", choices=sorted(GATE_MODES), default="research_followup")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    raw = json.loads(args.manifest.read_text(encoding="utf-8"))
    candidates = raw if isinstance(raw, list) else raw.get("candidates") if isinstance(raw, dict) else None
    if not isinstance(candidates, list):
        raise ValueError("Manifest must be a list or an object containing candidates")
    receipt = build_input_bundle(candidates, args.output_dir, catalog_path=args.catalog,
                                 gate_mode=args.gate_mode)
    print(json.dumps({"status": receipt["status"], "candidate_count": receipt["candidate_count"],
                      "execution_admitted": receipt["execution_admitted"],
                      "manifest": receipt["manifest"]["path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
