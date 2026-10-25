"""Audit a saved public-seat/trend replay without opening an execution route."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any


SCHEMA = "quantification_public_seat_replay_audit_v1"
VERIFIED = "VERIFIED_PUBLIC_SEAT_REPLAY_AUDIT"
BLOCKED = "PUBLIC_SEAT_REPLAY_AUDIT_BLOCKED"
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_public_seat_replay_audit_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_public_seat_replay_audit_v1.md")
RESEARCH_FAMILY = "full_source_public_seat_donchian_v1"
VALID_STATUSES = {"completed_seat_bulk_diagnostic", "partial_seat_bulk_diagnostic"}


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


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _reference(path: Path) -> dict[str, Any]:
    path = path.resolve()
    return {"path": str(path), "exists": path.is_file(), "sha256": _sha256(path) if path.is_file() else None}


def audit_run(run_path: Path) -> dict[str, Any]:
    run_path = run_path.resolve()
    run = _json(run_path)
    issues: list[str] = []
    if run.get("research_family") != RESEARCH_FAMILY:
        issues.append("run.research_family")
    if run.get("status") not in VALID_STATUSES:
        issues.append("run.status")
    if run.get("execution_admitted") is not False:
        issues.append("run.execution_admitted")

    artifacts_value = run.get("artifacts")
    artifacts = Path(artifacts_value).resolve() if isinstance(artifacts_value, str) else None
    protocol_path = artifacts / "protocol.json" if artifacts else None
    protocol = _json(protocol_path) if protocol_path and protocol_path.is_file() else {}
    if not protocol_path or not protocol_path.is_file():
        issues.append("artifacts.protocol")
    if protocol.get("execution_admitted") is not False:
        issues.append("protocol.execution_admitted")
    if protocol.get("live_orders") is not False:
        issues.append("protocol.live_orders")
    if "unknown publication timestamp" not in str(protocol.get("availability", "")):
        issues.append("protocol.publication_timing_boundary")

    results = run.get("results")
    if not isinstance(results, list) or not results:
        issues.append("run.results")
        results = []
    cases: list[dict[str, Any]] = []
    stop_points: list[dict[str, Any]] = []
    completed = failed = 0
    completed_sessions = completed_orders = completed_fills = 0
    for index, case in enumerate(results, start=1):
        if not isinstance(case, dict):
            issues.append(f"result.{index}.object")
            continue
        signal = str(case.get("signal", f"case_{index}"))
        status = case.get("status")
        sessions = case.get("completed_sessions", 0)
        if not isinstance(sessions, int) or isinstance(sessions, bool) or sessions < 0:
            issues.append(f"result.{signal}.completed_sessions")
            sessions = 0
        item = {"signal": signal, "mode": case.get("mode"), "lag": case.get("lag"),
                "status": status, "completed_sessions": sessions}
        if status == "completed":
            completed += 1
            if case.get("sessions") != sessions:
                issues.append(f"result.{signal}.session_completion")
            for key in ("net_return", "max_drawdown", "fees", "orders", "fills"):
                if not _finite(case.get(key)):
                    issues.append(f"result.{signal}.{key}")
                else:
                    item[key] = case[key]
            completed_sessions += sessions
            completed_orders += int(case.get("orders", 0)) if _finite(case.get("orders")) else 0
            completed_fills += int(case.get("fills", 0)) if _finite(case.get("fills")) else 0
        elif status in {"failed", "failed_preparation", "incomplete_or_no_fills"}:
            failed += 1
            error = case.get("error")
            if not isinstance(error, str) or not error.strip():
                issues.append(f"result.{signal}.error")
            item.update({"last_completed_session": case.get("last_completed_session"),
                         "attempted_session": case.get("attempted_session"), "error": error})
            stop_points.append({"signal": signal, "completed_sessions": sessions,
                                "last_completed_session": case.get("last_completed_session"),
                                "attempted_session": case.get("attempted_session"),
                                "error": error})
        else:
            issues.append(f"result.{signal}.status")
        cases.append(item)

    refs = {"run": _reference(run_path)}
    if protocol_path and protocol_path.is_file():
        refs["protocol"] = _reference(protocol_path)
    report_path = run_path.with_name("REPORT.md")
    if report_path.is_file():
        refs["report"] = _reference(report_path)
    selected_path = artifacts / "selected_seats.json" if artifacts else None
    if selected_path and selected_path.is_file():
        refs["selected_seats"] = _reference(selected_path)

    return {
        "schema": SCHEMA,
        "status": VERIFIED if not issues else BLOCKED,
        "run": str(run_path),
        "research_family": RESEARCH_FAMILY,
        "replay_start": run.get("start"),
        "replay_end": run.get("end"),
        "fit": protocol.get("fit"),
        "evaluation": protocol.get("evaluation"),
        "case_counts": {"total": len(cases), "completed": completed, "failed_or_stopped": failed},
        "completed_sessions": completed_sessions,
        "completed_orders": completed_orders,
        "completed_fills": completed_fills,
        "cases": cases,
        "stop_points": stop_points,
        "coverage": run.get("coverage", {}),
        "selected_seats": run.get("selected_seats", []),
        "references": refs,
        "issues": issues,
        "execution_admitted": False,
        "strategy_validated": False,
        "research_only": True,
        "selection_enabled": False,
        "broker_or_ths_interacted": False,
        "publication_timing_verified": False,
        "pit_membership_verified": False,
        "causal_seat_alpha_verified": False,
        "limitations": [
            "Public-seat disclosures are event snapshots, not a person's full account or complete trades.",
            "Publication timestamps and historical PIT membership are unknown; lagged cases are availability assumptions.",
            "Completed cases are historical diagnostics without an untouched holdout or causal attribution.",
            "Failed cases stop on unresolved held/execution price references; their complete-window returns are not reportable.",
            "No broker, Tonghuashun, login, prefill, submit, cancel, transfer or paid data action was attempted.",
        ],
    }


def render_report(receipt: dict[str, Any]) -> str:
    lines = ["# Public Seat Replay Audit", "", f"`{receipt['status']}`; `NO_TRADE`.",
             f"Replay: {receipt.get('replay_start')}..{receipt.get('replay_end')}; "
             f"cases {receipt['case_counts']['completed']} completed / {receipt['case_counts']['failed_or_stopped']} stopped.",
             "", "|Case|Status|Sessions|Net return|MDD|Orders|Fills|", "|---|---|---:|---:|---:|---:|---:|"]
    for case in receipt["cases"]:
        if case["status"] == "completed":
            lines.append(f"|{case['signal']}|completed|{case['completed_sessions']}|"
                         f"{case.get('net_return', float('nan')):.6%}|{abs(case.get('max_drawdown', float('nan'))):.6%}|"
                         f"{int(case.get('orders', 0))}|{int(case.get('fills', 0))}|")
        else:
            lines.append(f"|{case['signal']}|{case['status']}|{case['completed_sessions']}|unknown|unknown|unknown|unknown|")
            lines.append(f"Failure: {case.get('error')}")
    lines += ["", f"Coverage: {json.dumps(receipt.get('coverage', {}), ensure_ascii=False)}",
              "", "- publication timing verified: false", "- PIT membership verified: false",
              "- causal seat alpha verified: false", "- execution_admitted: false", "",
              "Limitations:"]
    lines.extend(f"- {value}" for value in receipt["limitations"])
    return "\n".join(lines) + "\n"


def run(run_path: Path, output_path: Path = DEFAULT_OUTPUT, report_path: Path = DEFAULT_REPORT) -> dict[str, Any]:
    receipt = audit_run(run_path)
    receipt["observed_at"] = datetime.now(timezone.utc).isoformat()
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    receipt["receipt_path"] = str(output_path)
    receipt["report_path"] = str(report_path)
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(receipt), encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    receipt = run(args.run, args.output, args.report)
    print(json.dumps({"status": receipt["status"], "case_counts": receipt["case_counts"],
                      "completed_sessions": receipt["completed_sessions"],
                      "stop_points": len(receipt["stop_points"]),
                      "execution_admitted": receipt["execution_admitted"]}, ensure_ascii=False))
    return 0 if receipt["status"] == VERIFIED else 2


__all__ = ["SCHEMA", "VERIFIED", "BLOCKED", "audit_run", "render_report", "run", "main"]
