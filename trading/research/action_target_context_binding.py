"""Bind a paper target to dated corporate-action context without adjustments."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from ..settings import ROOT

SCHEMA = "stockdb_action_target_context_binding_v1"
DEFAULT_TARGET = ROOT / "outputs/pipeline_receipts/20261025_continue_candidates_joint_paper_v3/joint_paper_receipt.json"
DEFAULT_ACTION = ROOT / "outputs/pipeline_receipts/20261024_stockdb_dated_action_context_sidecar_v4_receipt.json"
DEFAULT_OUTPUT = ROOT / "outputs/pipeline_receipts/20261026_action_target_context_binding_v1.json"
DEFAULT_REPORT = ROOT / "outputs/research_reports/20261026_action_target_context_binding_v1.md"


def _read_json(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _target_rows(payload: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    joint = payload.get("joint")
    if not isinstance(joint, Mapping):
        raise ValueError("target receipt has no joint payload")
    day = str(joint.get("common_terminal_day") or "")
    rows = joint.get("target_rows")
    if not day or not isinstance(rows, list):
        raise ValueError("target receipt has no decision day or target rows")
    return day, [dict(row) for row in rows if isinstance(row, Mapping) and row.get("symbol")]


def bind_target_context(target: Mapping[str, Any], action_receipt: Mapping[str, Any], context: pd.DataFrame) -> dict[str, Any]:
    decision_day, targets = _target_rows(target)
    required = {"date", "symbol", "action_event_present", "terms_complete", "pit_asof_safe", "company_action_applied"}
    missing = sorted(required - set(context.columns))
    if missing:
        raise ValueError(f"action context missing columns: {missing}")
    frame = context.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame = frame.loc[frame["date"].eq(decision_day)].copy()
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("action context has duplicate date/symbol rows")
    indexed = frame.set_index("symbol")
    bound: list[dict[str, Any]] = []
    for target_row in targets:
        symbol = str(target_row["symbol"]).upper()
        observed = indexed.loc[symbol] if symbol in indexed.index else None
        if observed is None:
            bound.append({"symbol": symbol, "contributors": target_row.get("contributors", []), "target_weight": target_row.get("target_weight"), "action_context_present": False, "action_event_present": False, "terms_complete": False, "pit_asof_safe": False, "company_action_applied": False, "reason": "target_symbol_missing_from_action_context"})
            continue
        reasons = observed.get("publication_evidence_reasons", [])
        if hasattr(reasons, "tolist"):
            reasons = reasons.tolist()
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        bound.append({
            "symbol": symbol, "contributors": target_row.get("contributors", []), "target_weight": target_row.get("target_weight"),
            "action_context_present": True, "action_event_present": bool(observed.get("action_event_present", False)),
            "publication_known_by_date": bool(observed.get("publication_known_by_date", False)),
            "terms_complete": bool(observed.get("terms_complete", False)), "pit_asof_safe": bool(observed.get("pit_asof_safe", False)),
            "company_action_applied": bool(observed.get("company_action_applied", False)),
            "publication_evidence_reasons": reasons,
            "reason": "pit_or_action_application_unproven" if not bool(observed.get("pit_asof_safe", False)) else "bound_without_adjustment",
        })
    pit_safe = sum(bool(row["pit_asof_safe"]) for row in bound)
    applied = sum(bool(row["company_action_applied"]) for row in bound)
    return {
        "schema": SCHEMA, "status": "BOUND_ACTION_CONTEXT_PIT_BLOCKED" if pit_safe < len(bound) else "BOUND_ACTION_CONTEXT_RESEARCH_ONLY",
        "created_at": datetime.now().astimezone().isoformat(), "decision_day": decision_day,
        "target_receipt": str(target.get("schema", "")), "action_context_receipt": str(action_receipt.get("schema", "")),
        "target_count": len(bound), "action_context_bound_rows": sum(bool(row["action_context_present"]) for row in bound),
        "pit_asof_safe_rows": pit_safe, "company_action_applied_rows": applied, "rows": bound,
        "execution_admitted": False, "target_mutated": False, "account_replay": False,
        "limitations": ["The binding joins dated action context to target symbols but does not apply cash, share or price adjustments.", "PIT/as-of unsafe rows remain visible and cannot authorize factor, order or account replay."],
    }


def build_binding(target_path: str | Path = DEFAULT_TARGET, action_receipt_path: str | Path = DEFAULT_ACTION) -> dict[str, Any]:
    target = _read_json(target_path); action_receipt = _read_json(action_receipt_path)
    context_path = Path(str(action_receipt.get("metrics", {}).get("context_path", ""))).expanduser()
    if not context_path.is_file():
        raise FileNotFoundError(f"action context parquet missing: {context_path}")
    return bind_target_context(target, action_receipt, pd.read_parquet(context_path))


def write_outputs(target_path: str | Path = DEFAULT_TARGET, action_receipt_path: str | Path = DEFAULT_ACTION, output_path: str | Path = DEFAULT_OUTPUT, report_path: str | Path = DEFAULT_REPORT) -> tuple[Path, Path]:
    receipt = build_binding(target_path, action_receipt_path)
    output = Path(output_path).expanduser().resolve(); report = Path(report_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True); report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(f"# Action context bound to retained target\n\nDecision day: `{receipt['decision_day']}`; target rows: `{receipt['target_count']}`; bound rows: `{receipt['action_context_bound_rows']}`.\nPIT/as-of safe rows: `{receipt['pit_asof_safe_rows']}`; company-action applications: `{receipt['company_action_applied_rows']}`.\n\nNo cash, share, price or target adjustment was applied; unsafe rows remain blocked.\n", encoding="utf-8")
    return output, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--target", type=Path, default=DEFAULT_TARGET); parser.add_argument("--action-receipt", type=Path, default=DEFAULT_ACTION); parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT); parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv); output, report = write_outputs(args.target, args.action_receipt, args.output, args.report)
    print(json.dumps({"output": str(output), "report": str(report)}, ensure_ascii=False)); return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "bind_target_context", "build_binding", "main", "write_outputs"]
