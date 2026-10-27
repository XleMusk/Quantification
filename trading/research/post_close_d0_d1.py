"""Advance fixed daily research and project one closed session through D0/D1.

This is the post-close coordinator for the existing research-only paths.  It
does not create an account or a signal: the fixed daily checkpoint is advanced
first, then ``next_close_d0_d1`` consumes same-day candidate target sources and
the committed fixed head.  Missing complete source packages stop the
coordinator before target/quote projection.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any, Sequence

from . import next_close_d0_d1, stockdb_fixed_daily
from .candidate_daily_input_bundle import build_input_bundle
from .exchange_calendar import adjacent_d0_d1_sessions
from .stockdb_paper_target_package import TARGET_PREVIEW


SCHEMA = "stockdb_post_close_d0_d1_orchestration_v1"
STATUS = "COMPLETED_RESEARCH_ONLY_POST_CLOSE_D0_D1"
WAITING_STATUS = "WAITING_FIXED_DAILY_SOURCES"
FAILED_STATUS = "FAILED_POST_CLOSE_D0_D1"
BLOCKED_D0_STATUS = "BLOCKED_D0_D1_INPUT"


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ref(path: Path) -> dict[str, str]:
    path = path.resolve()
    return {"path": str(path), "sha256": _sha256(path)}


def _input_fingerprint(*, source_manifest: Path | None,
                       candidate_replay_manifest: Path | None,
                       fixed_run_dir: Path,
                       packages: Sequence[Path], close_date: str,
                       execution_day: str, settlement_day: str,
                       initial_cash: float, stockdb_root: Path,
                       catalog_path: Path | None, calendar_path: Path | None,
                       decision_receipt: Path | None,
                       supplemental_raw_view: Path | None,
                       stockdb_snapshot: Path | None,
                       minute_time: str, gate_mode: str,
                       joint_capacity_policy: str | None,
                       joint_max_target_rows: int) -> tuple[str, dict[str, Any]]:
    package_refs = []
    for package in packages:
        package_refs.append({
            "path": str(package.resolve()),
            "plan": _ref(package / "plan.json"),
            "receipt": _ref(package / "receipt.json"),
        })
    refs: dict[str, Any] = {
        "source_manifest": _ref(source_manifest) if source_manifest is not None else None,
        "candidate_replay_manifest": (_ref(candidate_replay_manifest)
                                      if candidate_replay_manifest is not None else None),
        "fixed_run_dir": str(fixed_run_dir.resolve()),
        "packages": package_refs,
        "stockdb_root": str(stockdb_root.resolve()),
        "catalog": _ref(catalog_path) if catalog_path is not None else None,
        "calendar": _ref(calendar_path) if calendar_path is not None else None,
        "research_decision_receipt": _ref(decision_receipt) if decision_receipt is not None else None,
        "supplemental_raw_view": _ref(supplemental_raw_view) if supplemental_raw_view is not None else None,
        "stockdb_snapshot": (_ref(stockdb_snapshot / "snapshot.json")
                             if stockdb_snapshot is not None else None),
    }
    body = {
        "close_date": close_date, "execution_day": execution_day,
        "settlement_day": settlement_day, "initial_cash": float(initial_cash),
        "minute_time": minute_time, "gate_mode": gate_mode,
        "joint_capacity_policy": joint_capacity_policy,
        "joint_max_target_rows": joint_max_target_rows, "inputs": refs,
    }
    fingerprint = sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()
    return fingerprint, body


def _assert_unadmitted(payload: dict[str, Any], label: str) -> None:
    if payload.get("execution_admitted") is not False:
        raise ValueError(f"{label} crossed execution boundary")


def _write_receipt(receipt_path: Path, payload: dict[str, Any], *, started_monotonic: float | None = None) -> dict[str, Any]:
    """Persist one terminal receipt with bounded local timing evidence."""

    finished_at = datetime.now(timezone.utc).isoformat()
    timing = {"finished_at": finished_at}
    if started_monotonic is not None:
        timing["elapsed_seconds"] = round(max(0.0, time.monotonic() - started_monotonic), 3)
    payload = {**payload, "timing": timing}
    receipt_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _run_fixed_daily_stage(*, fixed_dir: Path, close_date: str,
                           packages: Sequence[Path], supplement: Path | None,
                           base: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Advance the persistent fixed account before any candidate projection."""

    fixed_result = stockdb_fixed_daily.run(
        fixed_dir, close_date, packages=packages, supplemental_raw_view=supplement
    )
    _assert_unadmitted(fixed_result, "fixed daily result")
    base["fixed_daily"] = fixed_result
    fixed_status = fixed_result.get("status")
    if fixed_status == "waiting_for_complete_sources":
        base["stage_trace"].append({
            "stage": "fixed_daily",
            "status": WAITING_STATUS,
            "missing_days": fixed_result.get("missing_days", []),
        })
        return fixed_result, {
            **base,
            "status": WAITING_STATUS,
            "reason": "Fixed daily has no complete source package for every requested session",
            "missing_days": fixed_result.get("missing_days", []),
        }
    if fixed_result.get("last_completed_day") != close_date:
        raise ValueError("Fixed daily did not reach the requested close date")
    base["stage_trace"].append({
        "stage": "fixed_daily",
        "status": str(fixed_status or "COMPLETED"),
        "last_completed_day": close_date,
    })
    return fixed_result, None


def _resolve_candidate_stage(*, source: Path | None,
                             replay_manifest: Path | None,
                             decision: Path | None,
                             catalog: Path | None,
                             output: Path,
                             gate_mode: str,
                             base: dict[str, Any]) -> Path:
    """Resolve a direct candidate manifest or build one from replay outputs."""

    if replay_manifest is None:
        if source is None:
            raise ValueError("Candidate source manifest was not resolved")
        base["stage_trace"].append({"stage": "candidate_inputs", "status": "DIRECT_SOURCE"})
        return source
    raw = json.loads(replay_manifest.read_text(encoding="utf-8"))
    candidates = raw if isinstance(raw, list) else raw.get("candidates") if isinstance(raw, dict) else None
    if not isinstance(candidates, list):
        raise ValueError("Candidate replay manifest must contain a candidates list")
    decision_selection = None
    if decision is not None:
        candidates, decision_selection = next_close_d0_d1._decision_bound_candidates(
            candidates, decision
        )
        base["research_decision_selection"] = decision_selection
    candidate_bundle = build_input_bundle(
        candidates, output / "candidate_inputs", catalog_path=catalog, gate_mode=gate_mode
    )
    resolved = Path(candidate_bundle["manifest"]["path"]).resolve()
    base["candidate_input_bundle"] = {
        "receipt": candidate_bundle["receipt_path"],
        "manifest": candidate_bundle["manifest"],
    }
    base["stage_trace"].append({
        "stage": "candidate_inputs",
        "status": "BUILT_FROM_REPLAY",
        "candidate_count": candidate_bundle["manifest"].get("candidate_count"),
    })
    return resolved


def _run_d0_d1_stage(*, source: Path, output: Path, fixed_dir: Path,
                     stockdb: Path, catalog: Path | None, snapshot_dir: Path | None,
                     close_date: str, execution_day: str, settlement_day: str,
                     initial_cash: float, minute_time: str, gate_mode: str,
                     decision: Path | None, calendar: Path | None,
                     joint_capacity_policy: str | None,
                     joint_max_target_rows: int, base: dict[str, Any]) -> dict[str, Any]:
    """Run the candidate Target -> paper D0/D1 projection stage."""

    d0_result = next_close_d0_d1.run_contract(
        source, output / "next_close", fixed_run_dir=fixed_dir,
        stockdb_root=stockdb, catalog_path=catalog,
        stockdb_snapshot=snapshot_dir,
        close_date=close_date, execution_day=execution_day,
        settlement_day=settlement_day, initial_cash=initial_cash,
        minute_time=minute_time, gate_mode=gate_mode,
        research_decision_receipt=decision, calendar_path=calendar,
        joint_capacity_policy=joint_capacity_policy,
        joint_max_target_rows=joint_max_target_rows,
    )
    _assert_unadmitted(d0_result, "D0/D1 result")
    base["stage_trace"].append({
        "stage": "d0_d1",
        "status": d0_result.get("status"),
        "snapshot_coverage": d0_result.get("snapshot_coverage"),
    })
    return d0_result


def run_pipeline(source_manifest: str | Path | None, output_dir: str | Path, *,
                 fixed_run_dir: str | Path, packages: Sequence[str | Path],
                 close_date: str, execution_day: str | None = None,
                 settlement_day: str | None = None,
                 initial_cash: float, stockdb_root: str | Path,
                 stockdb_snapshot: str | Path | None = None,
                 catalog_path: str | Path | None = None,
                 candidate_replay_manifest: str | Path | None = None,
                 calendar_path: str | Path | None = None,
                 research_decision_receipt: str | Path | None = None,
                 supplemental_raw_view: str | Path | None = None,
                 minute_time: str = "", gate_mode: str = "research_followup",
                 joint_capacity_policy: str | None = "budgeted",
                 joint_max_target_rows: int = 22) -> dict[str, Any]:
    source = Path(source_manifest).resolve() if source_manifest is not None else None
    replay_manifest = (Path(candidate_replay_manifest).resolve()
                       if candidate_replay_manifest is not None else None)
    if (source is None) == (replay_manifest is None):
        raise ValueError("Provide exactly one of source_manifest or candidate_replay_manifest")
    output = Path(output_dir).resolve()
    fixed_dir = Path(fixed_run_dir).resolve()
    packages_resolved = [Path(item).resolve() for item in packages]
    stockdb = Path(stockdb_root).resolve()
    snapshot_dir = Path(stockdb_snapshot).resolve() if stockdb_snapshot is not None else None
    catalog = Path(catalog_path).resolve() if catalog_path is not None else None
    if replay_manifest is not None and catalog is None:
        raise ValueError("candidate_replay_manifest requires a registered candidate catalog")
    calendar = Path(calendar_path).resolve() if calendar_path is not None else None
    session_resolution: dict[str, Any] = {
        "mode": "explicit_arguments",
        "calendar": str(calendar) if calendar is not None else None,
    }
    if execution_day is None and settlement_day is None:
        if calendar is None:
            raise ValueError(
                "execution_day and settlement_day are required when calendar_path is omitted"
            )
        execution_day, settlement_day, metadata = adjacent_d0_d1_sessions(close_date, calendar)
        session_resolution = {
            "mode": "pinned_calendar",
            "calendar": str(calendar),
            "metadata": metadata,
        }
    elif execution_day is None or settlement_day is None:
        raise ValueError("Provide both execution_day and settlement_day, or neither")
    decision = Path(research_decision_receipt).resolve() if research_decision_receipt is not None else None
    supplement = Path(supplemental_raw_view).resolve() if supplemental_raw_view is not None else None
    for path, label in ((source, "source manifest"), (replay_manifest, "candidate replay manifest"),
                        (fixed_dir, "fixed daily run directory")):
        if path is None:
            continue
        if not path.exists():
            raise ValueError(f"{label} does not exist: {path}")
    for package in packages_resolved:
        if not (package / "plan.json").is_file() or not (package / "receipt.json").is_file():
            raise ValueError(f"Complete source package is missing plan/receipt: {package}")
    for path, label in ((catalog, "catalog"), (calendar, "calendar"),
                        (decision, "research decision receipt"),
                        (supplement, "supplemental raw view"),
                        (snapshot_dir, "StockDB snapshot directory")):
        if path is not None and not path.is_file():
            if label == "StockDB snapshot directory" and (path / "snapshot.json").is_file():
                continue
            raise ValueError(f"{label} does not exist: {path}")
    fingerprint, fingerprint_inputs = _input_fingerprint(
        source_manifest=source, candidate_replay_manifest=replay_manifest,
        fixed_run_dir=fixed_dir, packages=packages_resolved,
        close_date=close_date, execution_day=execution_day,
        settlement_day=settlement_day, initial_cash=initial_cash,
        stockdb_root=stockdb, catalog_path=catalog, calendar_path=calendar,
        decision_receipt=decision, supplemental_raw_view=supplement,
        stockdb_snapshot=snapshot_dir,
        minute_time=minute_time, gate_mode=gate_mode,
        joint_capacity_policy=joint_capacity_policy,
        joint_max_target_rows=joint_max_target_rows)
    receipt_path = output / "post_close_receipt.json"
    started_monotonic = time.monotonic()
    if receipt_path.is_file():
        existing = _read(receipt_path)
        if existing.get("input_fingerprint") != fingerprint:
            raise ValueError("Post-close output already exists for different inputs; use a new output directory")
        _assert_unadmitted(existing, "saved post-close receipt")
        return {**existing, "reused_existing_run": True}
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Post-close output exists without a receipt; preserve and inspect it first")
    output.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {
        "schema": SCHEMA, "status": "RUNNING_POST_CLOSE_D0_D1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False, "broker_interacted": False,
        "ths_interacted": False, "persistent_ledger_mutated": False,
        "input_fingerprint": fingerprint, "fingerprint_inputs": fingerprint_inputs,
        "fixed_run_dir": str(fixed_dir), "close_date": close_date,
        "execution_day": execution_day, "settlement_day": settlement_day,
        "session_resolution": session_resolution,
        "persistent_fixed_daily_account_scope": "persistent_registered_account",
        "target_preview_projection_scope": {
            "fixed": TARGET_PREVIEW,
            "candidate": TARGET_PREVIEW,
            "ledger_scope": "ephemeral_projection",
            "continuous_account_consumer": False,
        },
        "stage_trace": [{"stage": "inputs", "status": "READY"}],
    }
    try:
        _fixed_result, waiting = _run_fixed_daily_stage(
            fixed_dir=fixed_dir, close_date=close_date,
            packages=packages_resolved, supplement=supplement, base=base,
        )
        if waiting is not None:
            return _write_receipt(
                receipt_path,
                {**waiting, "receipt_path": str(receipt_path)},
                started_monotonic=started_monotonic,
            )
        source = _resolve_candidate_stage(
            source=source, replay_manifest=replay_manifest, decision=decision,
            catalog=catalog, output=output, gate_mode=gate_mode, base=base,
        )
        d0_result = _run_d0_d1_stage(
            source=source, output=output, fixed_dir=fixed_dir, stockdb=stockdb,
            catalog=catalog, snapshot_dir=snapshot_dir, close_date=close_date,
            execution_day=execution_day, settlement_day=settlement_day,
            initial_cash=initial_cash, minute_time=minute_time, gate_mode=gate_mode,
            decision=decision, calendar=calendar,
            joint_capacity_policy=joint_capacity_policy,
            joint_max_target_rows=joint_max_target_rows, base=base,
        )
        d0_status = d0_result.get("status")
        if d0_status != next_close_d0_d1.STATUS:
            status = (BLOCKED_D0_STATUS
                      if isinstance(d0_status, str) and d0_status.startswith("BLOCKED")
                      else FAILED_STATUS)
            receipt = {
                **base,
                "status": status,
                "reason": d0_result.get("reason") or "D0/D1 did not complete",
                "d0_d1": d0_result,
                "snapshot_coverage": d0_result.get("snapshot_coverage"),
                "receipt_path": str(receipt_path),
                "artifacts": {"d0_d1": str(output / "next_close")},
            }
            return _write_receipt(receipt_path, receipt, started_monotonic=started_monotonic)
        receipt = {**base, "status": STATUS, "fixed_daily": _fixed_result,
                   "d0_d1": d0_result, "receipt_path": str(receipt_path),
                   "snapshot_coverage": d0_result.get("snapshot_coverage"),
                   "artifacts": {"d0_d1": str(output / "next_close")}}
        return _write_receipt(receipt_path, receipt, started_monotonic=started_monotonic)
    except Exception as error:
        failure = {**base, "status": FAILED_STATUS,
                   "error_type": type(error).__name__, "error": str(error),
                   "failure_evidence": {"output_dir": str(output),
                                        "receipt_path": str(receipt_path)}}
        _write_receipt(receipt_path, failure, started_monotonic=started_monotonic)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source-manifest", type=Path)
    source_group.add_argument("--candidate-replay-manifest", type=Path,
                              help="replay-result manifest; generates Target inputs before D0/D1")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixed-run-dir", type=Path, required=True)
    parser.add_argument("--package", type=Path, action="append", default=[],
                        help="complete close-source package; repeat for each missing fixed_daily day")
    parser.add_argument("--close-date", required=True)
    parser.add_argument("--execution-day",
                        help="optional with --calendar; otherwise required")
    parser.add_argument("--settlement-day",
                        help="optional with --calendar; otherwise required")
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--stockdb-root", type=Path, required=True)
    parser.add_argument("--stockdb-snapshot", type=Path,
                        help="optional pinned local snapshot directory for offline D0/D1")
    parser.add_argument("--catalog", type=Path,
                        default=Path("docs/architecture_v3/candidate_strategy_catalog_20261024.json"))
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--research-decision-receipt", type=Path)
    parser.add_argument("--supplemental-raw-view", type=Path)
    parser.add_argument("--minute-time", default="")
    parser.add_argument("--gate-mode", choices=("research_followup", "separate_gate"), default="research_followup")
    parser.add_argument("--joint-capacity-policy", choices=("strict", "budgeted"), default="budgeted")
    parser.add_argument("--joint-max-target-rows", type=int, default=22)
    args = parser.parse_args(argv)
    receipt = run_pipeline(
        args.source_manifest, args.output_dir,
        fixed_run_dir=args.fixed_run_dir, packages=args.package,
        close_date=args.close_date, execution_day=args.execution_day,
        settlement_day=args.settlement_day, initial_cash=args.initial_cash,
        stockdb_root=args.stockdb_root, catalog_path=args.catalog,
        stockdb_snapshot=args.stockdb_snapshot,
        candidate_replay_manifest=args.candidate_replay_manifest,
        calendar_path=args.calendar, research_decision_receipt=args.research_decision_receipt,
        supplemental_raw_view=args.supplemental_raw_view, minute_time=args.minute_time,
        gate_mode=args.gate_mode, joint_capacity_policy=args.joint_capacity_policy,
        joint_max_target_rows=args.joint_max_target_rows)
    print(json.dumps({"status": receipt["status"],
                      "execution_admitted": receipt["execution_admitted"],
                      "reused_existing_run": receipt.get("reused_existing_run", False),
                      "receipt": receipt["receipt_path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
