"""Project multiple registered strategy targets through one paper-only route.

This is the D1 fan-in used after a closed-session signal run.  Strategy
adapters produce explicit research-only target and quote payloads; this module
keeps each candidate in its own output directory while projecting every one
through the existing shared Ledger package.  It never fetches data, contacts a
broker/THS client, or mutates the persistent paper account.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .stockdb_paper_target_package import TARGET_PREVIEW, build_paper_target_package


SCHEMA = "daily_candidate_paper_batch_v2"
STATUS = "COMPLETED_RESEARCH_ONLY_CANDIDATE_BATCH"
DEFAULT_CATALOG = Path("docs/architecture_v3/candidate_strategy_catalog_20261024.json")
GATE_MODES = {"research_followup", "separate_gate", "historical_shadow"}


def build_stage_trace(*, candidate_id: str, target_count: int, paper_order_count: int,
                      route_available_count: int, fill_count: int | None = None) -> list[dict[str, Any]]:
    """Describe one candidate's shared paper path without claiming live fills."""

    return [
        {"stage": "Data", "status": "BOUND_SAVED_INPUTS", "candidate_id": candidate_id},
        {"stage": "Signal", "status": "BOUND_SAVED_RESEARCH_RESULT", "candidate_id": candidate_id},
        {"stage": "Target Portfolio", "status": "PROJECTED", "candidate_id": candidate_id,
         "target_count": target_count},
        {"stage": "Orders", "status": "PROJECTED", "candidate_id": candidate_id,
         "order_count": paper_order_count},
        {"stage": "BrokerAdapter", "status": "NOT_ATTEMPTED_RESEARCH_ONLY", "candidate_id": candidate_id,
         "route_available_count": route_available_count, "execution_admitted": False},
        {"stage": "Fill", "status": "PROJECTED_PAPER_ONLY", "candidate_id": candidate_id,
         "fill_count": fill_count if fill_count is not None else paper_order_count,
         "broker_fill": False},
        {"stage": "Cash/Position", "status": "PROJECTED_EPHEMERAL_LEDGER", "candidate_id": candidate_id,
         "persistent_ledger_mutated": False},
    ]


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
    value = item.get("candidate_id") or item.get("strategy_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Each candidate requires a non-empty candidate_id")
    return value.strip()


def _load_payload(item: Mapping[str, Any], key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path_value = item.get(key)
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"Candidate {key} path is required")
    path = Path(path_value).resolve()
    payload = _read(path)
    return payload, {"path": str(path), "sha256": _sha256(path)}


def _manifest_candidates(path: Path) -> list[Mapping[str, Any]]:
    raw = _read(path)
    candidates = raw if isinstance(raw, list) else raw.get("candidates")
    if not isinstance(candidates, list) or not all(isinstance(item, Mapping) for item in candidates):
        raise ValueError("Manifest must be a list or an object containing candidate objects")
    return list(candidates)


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


def build_batch(candidates: Sequence[Mapping[str, Any]], output_dir: str | Path, *,
                initial_cash: float, execution_day: str, settlement_day: str,
                catalog_path: str | Path | None = None,
                manifest_path: str | Path | None = None,
                gate_mode: str = "research_followup") -> dict[str, Any]:
    if not candidates:
        raise ValueError("At least one candidate target is required")
    if gate_mode not in GATE_MODES:
        raise ValueError(f"Unsupported candidate gate mode: {gate_mode}")
    output = Path(output_dir).resolve()
    manifest_ref = None
    if manifest_path is not None:
        manifest_file = Path(manifest_path).resolve()
        manifest_candidates = _manifest_candidates(manifest_file)
        if [_candidate_id(item) for item in manifest_candidates] != [_candidate_id(item) for item in candidates]:
            raise ValueError("Manifest candidate set does not match supplied candidates")
        manifest_ref = {
            "path": str(manifest_file),
            "sha256": _sha256(manifest_file),
            "candidate_count": len(manifest_candidates),
        }
    catalog_ref = None
    catalog_entries: dict[str, Mapping[str, Any]] = {}
    if catalog_path is not None:
        catalog_file = Path(catalog_path).resolve()
        catalog = _read(catalog_file)
        entries = catalog.get("entries")
        if not isinstance(entries, list):
            raise ValueError("Candidate catalog entries are missing")
        catalog_entries = {str(entry.get("id")): entry for entry in entries if isinstance(entry, Mapping)}
        catalog_ref = {"path": str(catalog_file), "sha256": _sha256(catalog_file)}
    identifiers = [_candidate_id(item) for item in candidates]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Candidate identifiers must be unique")
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for candidate, candidate_id in zip(candidates, identifiers):
        target, target_ref = _load_payload(candidate, "target")
        quote, quote_ref = _load_payload(candidate, "quotes")
        catalog_entry = catalog_entries.get(candidate_id)
        catalog_selection = None
        if catalog_path is not None:
            if catalog_entry is None:
                raise ValueError(f"Candidate is absent from the registered catalog: {candidate_id}")
            catalog_selection = _catalog_gate(catalog_entry, candidate_id, gate_mode)
        strategy_id = candidate.get("strategy_id") or target.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id or target.get("strategy_id") != strategy_id:
            raise ValueError(f"Candidate target strategy_id mismatch: {candidate_id}")
        if target.get("execution_admitted") is not False or target.get("research_only") is not True:
            raise ValueError(f"Candidate target is not research-only/unadmitted: {candidate_id}")
        if quote.get("execution_admitted") is not False or quote.get("client_interacted") is not False:
            raise ValueError(f"Candidate quote is not read-only: {candidate_id}")
        decision_day = target.get("decision_day")
        if not isinstance(decision_day, str) or not decision_day or execution_day <= decision_day:
            raise ValueError(f"Candidate execution day must follow its decision day: {candidate_id}")
        observed_at = str(quote.get("observed_at") or "")
        if not observed_at.startswith(decision_day):
            raise ValueError(f"Candidate quote clock is not on its decision day: {candidate_id}")
        prices = quote.get("prices") or {}
        if any(str(item.get("observed_date") or "") != decision_day for item in prices.values()):
            raise ValueError(f"Candidate quote rows must use the target decision day: {candidate_id}")
        candidate_output = output / candidate_id
        package = build_paper_target_package(
            target, quote, initial_cash,
            execution_day=execution_day, settlement_day=settlement_day,
            output_dir=candidate_output,
        )
        result = package["result"]
        if (result.get("execution_admitted") is not False
                or result.get("submitted") is not False
                or result.get("projection_mode") != TARGET_PREVIEW
                or result.get("ledger_scope") != "ephemeral_projection"
                or result.get("continuous_account_consumer") is not False):
            raise ValueError(f"Paper package crossed execution boundary: {candidate_id}")
        records.append({
            "candidate_id": candidate_id,
            "strategy_id": strategy_id,
            "decision_day": result.get("decision_day"),
            "execution_day": result.get("execution_day"),
            "target_count": result.get("target_count"),
            "paper_order_count": result.get("paper_order_count"),
            "route_available_count": result.get("route_available_count"),
            "modeled_fees_rmb": result.get("modeled_fees_rmb"),
            "projected_final_cash_rmb": result.get("projected_final_cash_rmb"),
            "projected_final_equity_rmb": result.get("projected_final_equity_rmb"),
            "projection_mode": result.get("projection_mode"),
            "ledger_scope": result.get("ledger_scope"),
            "continuous_account_consumer": result.get("continuous_account_consumer"),
            "stage_trace": build_stage_trace(
                candidate_id=candidate_id,
                target_count=int(result.get("target_count") or 0),
                paper_order_count=int(result.get("paper_order_count") or 0),
                route_available_count=int(result.get("route_available_count") or 0),
            ),
            "cost_model_id": (result.get("cost_model") or {}).get("id"),
            "benchmark_binding_id": (result.get("benchmark_binding") or {}).get("id"),
            "target": target_ref, "quotes": quote_ref,
            "catalog_selection": catalog_selection,
            "gate_mode": gate_mode,
            "package_result": {"path": str(candidate_output / "result.json"),
                                "sha256": _sha256(candidate_output / "result.json")},
        })
    receipt = {
        "schema": SCHEMA, "status": STATUS,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False, "broker_interacted": False,
        "ths_interacted": False, "persistent_ledger_mutated": False,
        "execution_day": execution_day, "settlement_day": settlement_day,
        "manifest": manifest_ref,
        "catalog": catalog_ref,
        "gate_mode": gate_mode,
        "projection_mode": TARGET_PREVIEW,
        "ledger_scope": "ephemeral_projection",
        "continuous_account_consumer": False,
        "candidate_count": len(records), "candidates": records,
        "stage_trace": {
            "stages": ["Data", "Signal", "Target Portfolio", "Orders", "BrokerAdapter", "Fill", "Cash/Position"],
            "candidate_count": len(records),
            "broker_adapter_attempted": False,
            "broker_fills": False,
            "persistent_ledger_mutated": False,
        },
        "output_dir": str(output),
        "limitations": [
            "Targets and quotes are explicit strategy-adapter inputs; this batch does not create signals or fetch market data.",
            "Projected fills are local shared-Ledger paper projections and are not broker fills.",
            "The batch is separate from the persistent fixed_daily account and is not a THS prefill payload.",
            "Every candidate is TARGET_PREVIEW only; the batch cannot publish a frozen plan or roll a continuous paper account.",
        ],
    }
    receipt_path = output / "batch_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt["receipt_path"] = str(receipt_path)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="JSON list or object with a candidates list; each item has candidate_id, target and quotes paths")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--gate-mode", choices=sorted(GATE_MODES), default="research_followup")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--settlement-day", required=True)
    args = parser.parse_args(argv)
    raw = json.loads(args.manifest.read_text(encoding="utf-8"))
    candidates = raw if isinstance(raw, list) else raw.get("candidates") if isinstance(raw, dict) else None
    if not isinstance(candidates, list):
        raise ValueError("Manifest must be a list or an object containing candidates")
    receipt = build_batch(candidates, args.output_dir, initial_cash=args.initial_cash,
                          execution_day=args.execution_day, settlement_day=args.settlement_day,
                          catalog_path=args.catalog, manifest_path=args.manifest,
                          gate_mode=args.gate_mode)
    print(json.dumps({"status": receipt["status"], "candidate_count": receipt["candidate_count"],
                      "execution_admitted": receipt["execution_admitted"],
                      "receipt": receipt["receipt_path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
