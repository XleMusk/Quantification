"""Turn saved candidate replay results into a fixed research decision matrix.

The matrix answers three distinct questions for each candidate: did a complete
window show an opportunity, did the path actually enter (orders and fills),
and should the candidate continue to research or stop.  Incomplete cases are
blocked rather than scored.  Publication timing, PIT membership and causal
seat attribution remain promotion gates and are never inferred from returns.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_research_decision_matrix_v1"
VERIFIED = "VERIFIED_RESEARCH_DECISION_MATRIX"
DEFAULT_INPUT = Path("outputs/pipeline_receipts/20261024_public_seat_replay_audit_v2.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_research_decision_matrix_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_research_decision_matrix_v1.md")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def classify_case(case: Mapping[str, Any], *, expected_sessions: int) -> dict[str, Any]:
    """Apply the predeclared complete-window decision rule to one case."""

    status = str(case.get("status", ""))
    sessions = int(case.get("completed_sessions", 0) or 0)
    orders = int(case.get("orders", 0) or 0)
    fills = int(case.get("fills", 0) or 0)
    complete = status == "completed" and sessions == expected_sessions and orders > 0 and fills > 0
    enterable = orders > 0 and fills > 0
    if not complete:
        decision = "BLOCKED_INCOMPLETE_WINDOW"
    elif float(case.get("net_return", 0.0)) > 0:
        decision = "CONTINUE_RESEARCH_ONLY"
    else:
        decision = "STOP_RESEARCH_ONLY"
    return {
        "signal": str(case.get("signal", "")), "mode": str(case.get("mode", "")),
        "lag": case.get("lag"), "status": status, "completed_sessions": sessions,
        "expected_sessions": expected_sessions, "complete_window": complete,
        "post_event_opportunity": float(case["net_return"]) if complete else None,
        "max_drawdown": float(case["max_drawdown"]) if complete else None,
        "enterable": enterable, "orders": orders, "fills": fills,
        "decision": decision, "promotion_allowed": False,
        "blocked_reason": None if complete else str(case.get("error") or "incomplete_or_stopped"),
    }


def build_matrix(source: Mapping[str, Any]) -> dict[str, Any]:
    cases = source.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Replay receipt has no cases")
    completed_session_values = [int(c.get("completed_sessions", 0) or 0) for c in cases
                                if c.get("status") == "completed"]
    if not completed_session_values:
        raise ValueError("Replay receipt has no completed case to establish the window")
    expected_sessions = max(completed_session_values)
    rows = [classify_case(case, expected_sessions=expected_sessions) for case in cases]
    counts = {}
    for row in rows:
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
    return {
        "schema": SCHEMA, "status": VERIFIED, "source_schema": source.get("schema"),
        "source_receipt": str(DEFAULT_INPUT.resolve()), "evaluation_window": {
            "start": source.get("replay_start"), "end": source.get("replay_end"),
            "expected_sessions": expected_sessions,
        },
        "policy": {
            "complete_window_required": True,
            "enterable_requires_orders_and_fills": True,
            "positive_complete_net_return": "CONTINUE_RESEARCH_ONLY",
            "nonpositive_complete_net_return": "STOP_RESEARCH_ONLY",
            "incomplete_or_stopped": "BLOCKED_INCOMPLETE_WINDOW",
            "promotion_allowed": False,
        },
        "cases": rows, "decision_counts": counts,
        "promotion_gates": {
            "publication_timing_verified": bool(source.get("publication_timing_verified")),
            "pit_membership_verified": bool(source.get("pit_membership_verified")),
            "causal_seat_alpha_verified": bool(source.get("causal_seat_alpha_verified")),
            "promotion_allowed": False,
        },
        "execution_admitted": False, "selection_enabled": False,
        "broker_or_ths_interacted": False, "research_only": True,
        "limitations": [
            "Continue means continue research only; it does not promote a strategy or create a paper target.",
            "Incomplete cases retain their stop/error evidence and are not assigned a return.",
            "Publication timing, PIT membership and causal attribution remain required for any promotion.",
        ],
    }


def build_report(result: Mapping[str, Any]) -> str:
    lines = ["# Research decision matrix", "",
             f"Status: `{result['status']}`; expected complete sessions: {result['evaluation_window']['expected_sessions']}; promotion: `false`.", "",
             "| Signal | Complete window | Opportunity | Enterable | Decision |", "|---|---:|---:|---:|---|"]
    for row in result["cases"]:
        opportunity = "unknown" if row["post_event_opportunity"] is None else f"{row['post_event_opportunity']:.2%}"
        lines.append(f"| {row['signal']} | {row['complete_window']} | {opportunity} | {row['enterable']} | {row['decision']} |")
    lines += ["", "No candidate is selected for paper or live execution. This matrix only makes the saved research decision explicit.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    source = _read(args.input.resolve())
    result = build_matrix(source)
    result["source_receipt"] = str(args.input.resolve())
    output, report = args.output.resolve(), args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(build_report(result), encoding="utf-8")
    print(json.dumps({"status": result["status"], "decision_counts": result["decision_counts"],
                      "promotion_allowed": result["promotion_gates"]["promotion_allowed"],
                      "execution_admitted": result["execution_admitted"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["classify_case", "build_matrix", "build_report", "main", "VERIFIED"]
