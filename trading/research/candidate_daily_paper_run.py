"""Run the registered candidate input fan-in and shared paper projection.

This is a local orchestration entry point for one closed-session research run:
replay sources are converted into a catalog-bound input bundle, then the same
targets and quotes are projected by ``daily-candidate-paper-batch``.  No data
provider fetch, persistent account mutation, broker/THS client or submission
is performed here.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Sequence

from .candidate_shadow_paper import _target_payload
from .candidate_daily_input_bundle import GATE_MODES, build_input_bundle
from .daily_candidate_paper_batch import build_batch
from . import stockdb_fixed_daily
from .stockdb_paper_target_package import build_paper_target_package


SCHEMA = "candidate_daily_paper_pipeline_v1"
STATUS = "COMPLETED_RESEARCH_ONLY_CANDIDATE_DAILY_PIPELINE"
DEFAULT_CATALOG = Path("docs/architecture_v3/candidate_strategy_catalog_20261024.json")


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_candidates(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    candidates = raw if isinstance(raw, list) else raw.get("candidates") if isinstance(raw, dict) else None
    if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
        raise ValueError("Source manifest must be a list or an object containing candidate objects")
    return candidates


def _input_identity(source_manifest: Path, catalog_path: Path | None,
                    fixed_source: Path | None, fixed_strategy_id: str | None, *,
                    initial_cash: float, execution_day: str, settlement_day: str,
                    gate_mode: str) -> tuple[str, dict[str, Any]]:
    """Return the stable identity for one closed-session projection.

    The pipeline writes only paper artifacts, but an interrupted or repeated
    invocation must still resolve to one result.  Hashing the exact source,
    catalog and optional fixed leg keeps a later input change from silently
    replacing the earlier projection.
    """
    source_ref = {"path": str(source_manifest), "sha256": _sha256(source_manifest)}
    catalog_ref = None
    if catalog_path is not None:
        if not catalog_path.is_file():
            raise ValueError(f"Candidate catalog does not exist: {catalog_path}")
        catalog_ref = {"path": str(catalog_path), "sha256": _sha256(catalog_path)}
    fixed_ref = None
    if fixed_source is not None:
        fixed_ref = {"path": str(fixed_source), "sha256": _sha256(fixed_source),
                     "strategy_id": fixed_strategy_id}
    body = {"source_manifest": source_ref, "catalog": catalog_ref,
            "fixed_terminal_account": fixed_ref, "initial_cash": initial_cash,
            "execution_day": execution_day, "settlement_day": settlement_day,
            "gate_mode": gate_mode}
    digest = sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")).hexdigest()
    return digest, body


def run_pipeline(source_manifest: str | Path, output_dir: str | Path, *,
                 catalog_path: str | Path | None, initial_cash: float,
                 execution_day: str, settlement_day: str,
                 gate_mode: str = "research_followup",
                 fixed_terminal_account: str | Path | None = None,
                 fixed_run_dir: str | Path | None = None,
                 fixed_strategy_id: str | None = None) -> dict[str, Any]:
    try:
        initial_cash = float(initial_cash)
    except (TypeError, ValueError) as error:
        raise ValueError("initial_cash must be numeric") from error
    source_manifest = Path(source_manifest).resolve()
    if not source_manifest.is_file():
        raise ValueError(f"Source manifest does not exist: {source_manifest}")
    candidates = _read_candidates(source_manifest)
    output = Path(output_dir).resolve()
    if output == source_manifest or output in source_manifest.parents:
        raise ValueError("Pipeline output must remain separate from its source manifest")
    if gate_mode not in GATE_MODES:
        raise ValueError(f"Unsupported candidate gate mode: {gate_mode}")
    if (fixed_terminal_account is None) == (fixed_run_dir is None) and fixed_strategy_id is not None:
        raise ValueError("fixed_strategy_id requires one fixed source")
    if fixed_run_dir is not None and fixed_terminal_account is not None:
        raise ValueError("Provide exactly one of fixed_terminal_account or fixed_run_dir")
    resolved_signal = None
    if fixed_run_dir is not None:
        fixed_source, resolved_signal = stockdb_fixed_daily.resolve_terminal_account(fixed_run_dir)
    else:
        fixed_source = Path(fixed_terminal_account).resolve() if fixed_terminal_account is not None else None
    if resolved_signal is not None:
        fixed_strategy_id = fixed_strategy_id or resolved_signal
    if fixed_source is not None and not fixed_source.is_file():
        raise ValueError(f"Fixed terminal account does not exist: {fixed_source}")
    if fixed_source is not None and (not isinstance(fixed_strategy_id, str) or not fixed_strategy_id):
        raise ValueError("fixed_strategy_id is required with fixed_terminal_account")
    input_fingerprint, fingerprint_inputs = _input_identity(
        source_manifest, Path(catalog_path).resolve() if catalog_path is not None else None,
        fixed_source, fixed_strategy_id, initial_cash=initial_cash,
        execution_day=execution_day, settlement_day=settlement_day, gate_mode=gate_mode)
    receipt_path = output / "pipeline_receipt.json"
    if receipt_path.is_file():
        existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (existing.get("schema") != SCHEMA
                or existing.get("status") != STATUS
                or existing.get("input_fingerprint") != input_fingerprint):
            raise ValueError("Pipeline output already exists for different inputs; use a new output directory")
        if any(existing.get(key) is not False for key in
               ("execution_admitted", "broker_interacted", "ths_interacted",
                "persistent_ledger_mutated")):
            raise ValueError("Existing pipeline receipt crosses the paper-only boundary")
        return {**existing, "reused_existing_run": True}
    input_dir = output / "inputs"
    paper_dir = output / "paper_batch"
    fixed_result = None
    if fixed_source is not None:
        fixed_target, fixed_quote = _target_payload(
            json.loads(fixed_source.read_text(encoding="utf-8")),
            legacy_account=True,
            strategy_id=fixed_strategy_id,
        )
        fixed_input_dir = output / "fixed_input"
        fixed_input_dir.mkdir(parents=True, exist_ok=True)
        fixed_target_path = fixed_input_dir / "target_payload.json"
        fixed_quote_path = fixed_input_dir / "quote_payload.json"
        fixed_target_path.write_text(json.dumps(fixed_target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        fixed_quote_path.write_text(json.dumps(fixed_quote, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        fixed_dir = output / "fixed"
        fixed_result = build_paper_target_package(
            fixed_target, fixed_quote, initial_cash,
            execution_day=execution_day, settlement_day=settlement_day,
            output_dir=fixed_dir,
        )["result"]
        fixed_result = {
            "source": {"path": str(fixed_source), "sha256": _sha256(fixed_source)},
            "target": {"path": str(fixed_target_path), "sha256": _sha256(fixed_target_path)},
            "quotes": {"path": str(fixed_quote_path), "sha256": _sha256(fixed_quote_path)},
            "strategy_id": fixed_result["strategy_id"],
            "decision_day": fixed_result["decision_day"],
            "target_count": fixed_result["target_count"],
            "paper_order_count": fixed_result["paper_order_count"],
            "projected_final_cash_rmb": fixed_result["projected_final_cash_rmb"],
            "projected_final_equity_rmb": fixed_result["projected_final_equity_rmb"],
            "cost_model_id": (fixed_result.get("cost_model") or {}).get("id"),
            "benchmark_binding_id": (fixed_result.get("benchmark_binding") or {}).get("id"),
            "package_result": str(fixed_dir / "result.json"),
        }
    bundle = build_input_bundle(candidates, input_dir, catalog_path=catalog_path,
                                gate_mode=gate_mode)
    manifest_path = input_dir / "manifest.json"
    batch = build_batch(
        json.loads(manifest_path.read_text(encoding="utf-8"))["candidates"],
        paper_dir,
        initial_cash=initial_cash,
        execution_day=execution_day,
        settlement_day=settlement_day,
        catalog_path=catalog_path,
        manifest_path=manifest_path,
        gate_mode=gate_mode,
    )
    binding_ids = []
    benchmark_ids = []
    if fixed_result is not None:
        binding_ids.append(fixed_result["cost_model_id"])
        benchmark_ids.append(fixed_result["benchmark_binding_id"])
    binding_ids.extend(item.get("cost_model_id") for item in batch["candidates"])
    benchmark_ids.extend(item.get("benchmark_binding_id") for item in batch["candidates"])
    if not binding_ids or any(value is None for value in binding_ids) or len(set(binding_ids)) != 1:
        raise ValueError("Fixed and candidate paper packages must share one cost model version")
    if not benchmark_ids or any(value is None for value in benchmark_ids) or len(set(benchmark_ids)) != 1:
        raise ValueError("Fixed and candidate paper packages must share one benchmark binding")
    receipt = {
        "schema": SCHEMA,
        "status": STATUS,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
        "input_fingerprint": input_fingerprint,
        "fingerprint_inputs": fingerprint_inputs,
        "reused_existing_run": False,
        "fixed_strategy": fixed_result,
        "source_manifest": {"path": str(source_manifest), "sha256": _sha256(source_manifest),
                            "candidate_count": len(candidates)},
        "input_bundle": {"receipt_path": bundle["receipt_path"],
                         "manifest_path": bundle["manifest"]["path"],
                         "sha256": bundle["manifest"]["sha256"]},
        "paper_batch": {"receipt_path": batch["receipt_path"],
                        "candidate_count": batch["candidate_count"],
                        "execution_day": batch["execution_day"],
                        "settlement_day": batch["settlement_day"]},
        "cost_benchmark_binding": {"cost_model_id": binding_ids[0],
                                    "benchmark_binding_id": benchmark_ids[0]},
        "gate_mode": gate_mode,
        "candidate_count": batch["candidate_count"],
        "limitations": [
            "The source manifest must point to completed research-only replay results.",
            "The pipeline does not fetch market data or create a current-day signal.",
            "Projected fills are local paper projections and are not broker fills.",
        ],
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt["receipt_path"] = str(receipt_path)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--settlement-day", required=True)
    parser.add_argument("--gate-mode", choices=sorted(GATE_MODES), default="research_followup")
    parser.add_argument("--fixed-terminal-account", type=Path,
                        help="optional saved research terminal account for the fixed strategy leg")
    parser.add_argument("--fixed-run-dir", type=Path,
                        help="registered stockdb-fixed-daily directory; uses its current head")
    parser.add_argument("--fixed-strategy-id",
                        help="required with --fixed-terminal-account; remains research-only")
    args = parser.parse_args(argv)
    receipt = run_pipeline(
        args.source_manifest, args.output_dir, catalog_path=args.catalog,
        initial_cash=args.initial_cash, execution_day=args.execution_day,
        settlement_day=args.settlement_day, gate_mode=args.gate_mode,
        fixed_terminal_account=args.fixed_terminal_account,
        fixed_run_dir=args.fixed_run_dir,
        fixed_strategy_id=args.fixed_strategy_id,
    )
    print(json.dumps({"status": receipt["status"], "candidate_count": receipt["candidate_count"],
                      "execution_admitted": receipt["execution_admitted"],
                      "receipt": receipt["receipt_path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
