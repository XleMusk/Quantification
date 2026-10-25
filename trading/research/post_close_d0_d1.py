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
from typing import Any, Sequence

from . import next_close_d0_d1, stockdb_fixed_daily


SCHEMA = "stockdb_post_close_d0_d1_orchestration_v1"
STATUS = "COMPLETED_RESEARCH_ONLY_POST_CLOSE_D0_D1"
WAITING_STATUS = "WAITING_FIXED_DAILY_SOURCES"
FAILED_STATUS = "FAILED_POST_CLOSE_D0_D1"


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


def _input_fingerprint(*, source_manifest: Path, fixed_run_dir: Path,
                       packages: Sequence[Path], close_date: str,
                       execution_day: str, settlement_day: str,
                       initial_cash: float, stockdb_root: Path,
                       catalog_path: Path | None, calendar_path: Path | None,
                       decision_receipt: Path | None,
                       supplemental_raw_view: Path | None,
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
        "source_manifest": _ref(source_manifest),
        "fixed_run_dir": str(fixed_run_dir.resolve()),
        "packages": package_refs,
        "stockdb_root": str(stockdb_root.resolve()),
        "catalog": _ref(catalog_path) if catalog_path is not None else None,
        "calendar": _ref(calendar_path) if calendar_path is not None else None,
        "research_decision_receipt": _ref(decision_receipt) if decision_receipt is not None else None,
        "supplemental_raw_view": _ref(supplemental_raw_view) if supplemental_raw_view is not None else None,
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


def run_pipeline(source_manifest: str | Path, output_dir: str | Path, *,
                 fixed_run_dir: str | Path, packages: Sequence[str | Path],
                 close_date: str, execution_day: str, settlement_day: str,
                 initial_cash: float, stockdb_root: str | Path,
                 catalog_path: str | Path | None = None,
                 calendar_path: str | Path | None = None,
                 research_decision_receipt: str | Path | None = None,
                 supplemental_raw_view: str | Path | None = None,
                 minute_time: str = "", gate_mode: str = "research_followup",
                 joint_capacity_policy: str | None = None,
                 joint_max_target_rows: int = 22) -> dict[str, Any]:
    source = Path(source_manifest).resolve()
    output = Path(output_dir).resolve()
    fixed_dir = Path(fixed_run_dir).resolve()
    packages_resolved = [Path(item).resolve() for item in packages]
    stockdb = Path(stockdb_root).resolve()
    catalog = Path(catalog_path).resolve() if catalog_path is not None else None
    calendar = Path(calendar_path).resolve() if calendar_path is not None else None
    decision = Path(research_decision_receipt).resolve() if research_decision_receipt is not None else None
    supplement = Path(supplemental_raw_view).resolve() if supplemental_raw_view is not None else None
    for path, label in ((source, "source manifest"), (fixed_dir, "fixed daily run directory")):
        if not path.exists():
            raise ValueError(f"{label} does not exist: {path}")
    for package in packages_resolved:
        if not (package / "plan.json").is_file() or not (package / "receipt.json").is_file():
            raise ValueError(f"Complete source package is missing plan/receipt: {package}")
    for path, label in ((catalog, "catalog"), (calendar, "calendar"),
                        (decision, "research decision receipt"),
                        (supplement, "supplemental raw view")):
        if path is not None and not path.is_file():
            raise ValueError(f"{label} does not exist: {path}")
    fingerprint, fingerprint_inputs = _input_fingerprint(
        source_manifest=source, fixed_run_dir=fixed_dir, packages=packages_resolved,
        close_date=close_date, execution_day=execution_day,
        settlement_day=settlement_day, initial_cash=initial_cash,
        stockdb_root=stockdb, catalog_path=catalog, calendar_path=calendar,
        decision_receipt=decision, supplemental_raw_view=supplement,
        minute_time=minute_time, gate_mode=gate_mode,
        joint_capacity_policy=joint_capacity_policy,
        joint_max_target_rows=joint_max_target_rows)
    receipt_path = output / "post_close_receipt.json"
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
    }
    try:
        fixed_result = stockdb_fixed_daily.run(
            fixed_dir, close_date, packages=packages_resolved,
            supplemental_raw_view=supplement)
        _assert_unadmitted(fixed_result, "fixed daily result")
        base["fixed_daily"] = fixed_result
        fixed_status = fixed_result.get("status")
        if fixed_status == "waiting_for_complete_sources":
            receipt = {**base, "status": WAITING_STATUS,
                       "reason": "Fixed daily has no complete source package for every requested session",
                       "missing_days": fixed_result.get("missing_days", []),
                       "receipt_path": str(receipt_path)}
            receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return receipt
        if fixed_result.get("last_completed_day") != close_date:
            raise ValueError("Fixed daily did not reach the requested close date")
        d0_result = next_close_d0_d1.run_contract(
            source, output / "next_close", fixed_run_dir=fixed_dir,
            stockdb_root=stockdb, catalog_path=catalog,
            close_date=close_date, execution_day=execution_day,
            settlement_day=settlement_day, initial_cash=initial_cash,
            minute_time=minute_time, gate_mode=gate_mode,
            research_decision_receipt=decision, calendar_path=calendar,
            joint_capacity_policy=joint_capacity_policy,
            joint_max_target_rows=joint_max_target_rows)
        _assert_unadmitted(d0_result, "D0/D1 result")
        receipt = {**base, "status": STATUS, "fixed_daily": fixed_result,
                   "d0_d1": d0_result, "receipt_path": str(receipt_path),
                   "artifacts": {"d0_d1": str(output / "next_close")}}
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt
    except Exception as error:
        failure = {**base, "status": FAILED_STATUS,
                   "error_type": type(error).__name__, "error": str(error),
                   "failure_evidence": {"output_dir": str(output),
                                        "receipt_path": str(receipt_path)}}
        receipt_path.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixed-run-dir", type=Path, required=True)
    parser.add_argument("--package", type=Path, action="append", default=[],
                        help="complete close-source package; repeat for each missing fixed_daily day")
    parser.add_argument("--close-date", required=True)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--settlement-day", required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--stockdb-root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--research-decision-receipt", type=Path)
    parser.add_argument("--supplemental-raw-view", type=Path)
    parser.add_argument("--minute-time", default="")
    parser.add_argument("--gate-mode", choices=("research_followup", "separate_gate"), default="research_followup")
    parser.add_argument("--joint-capacity-policy", choices=("strict", "budgeted"))
    parser.add_argument("--joint-max-target-rows", type=int, default=22)
    args = parser.parse_args(argv)
    receipt = run_pipeline(
        args.source_manifest, args.output_dir,
        fixed_run_dir=args.fixed_run_dir, packages=args.package,
        close_date=args.close_date, execution_day=args.execution_day,
        settlement_day=args.settlement_day, initial_cash=args.initial_cash,
        stockdb_root=args.stockdb_root, catalog_path=args.catalog,
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
