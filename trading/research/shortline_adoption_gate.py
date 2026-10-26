"""Bind bounded shortline replays to an explicit research-only adoption gate.

The gate consumes the immutable native candidate handoff and the independent
BaoStock bounded replay.  It makes the model-level decision visible without
turning a bounded experiment into paper or live permission.
"""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_shortline_adoption_gate_v1"
REPLAY_SCHEMA = "shortline_baostock_candidate_replay_binding_v1"
HANDOFF_SCHEMA = "native_shortline_candidate_handoff_v1"
DEFAULT_REPLAY = Path("outputs/pipeline_receipts/20261026_shortline_baostock_candidate_replay_v1.json")
DEFAULT_HANDOFF = Path("outputs/shortline_native_candidate_handoff/20261026_source1015_ipoqualified_v1/receipt.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261026_shortline_adoption_gate_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261026_shortline_adoption_gate_v1.md")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"receipt must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def _model_decision(row: Mapping[str, Any], handoff_row: Mapping[str, Any]) -> dict[str, Any]:
    selected = int(handoff_row.get("selected_count", 0))
    orders = int(row.get("orders", 0))
    fills = int(row.get("fills", 0))
    sessions = int(row.get("replay_sessions", 0))
    if selected <= 0:
        action = "BLOCKED_COVERAGE"
        reason = "candidate handoff contains no selected rows"
    elif sessions <= 0:
        action = "BLOCKED_COVERAGE"
        reason = "bounded replay contains no sessions"
    elif orders <= 0 or fills <= 0:
        action = "BLOCKED_EXECUTION_REFERENCE"
        reason = "bounded replay produced no orders and fills"
    else:
        action = "CONTINUE_RESEARCH_ONLY"
        reason = "bounded replay completed; PIT, full-window and forward gates remain open"
    return {
        "model_kind": row.get("model_kind"),
        "selected_count": selected,
        "replay_sessions": sessions,
        "orders": orders,
        "fills": fills,
        "net_return": row.get("net_return"),
        "max_drawdown": row.get("max_drawdown"),
        "blocked_order_event_count": int(row.get("blocked_order_event_count", 0)),
        "research_action": action,
        "decision_reason": reason,
        "paper_allowed": False,
        "promotion_allowed": False,
    }


def build_gate(replay_path: Path = DEFAULT_REPLAY, handoff_path: Path = DEFAULT_HANDOFF) -> dict[str, Any]:
    replay = _read(replay_path)
    handoff = _read(handoff_path)
    _require(replay.get("schema") == REPLAY_SCHEMA, f"replay schema mismatch: {replay_path}")
    _require(handoff.get("schema") == HANDOFF_SCHEMA, f"handoff schema mismatch: {handoff_path}")
    _require(replay.get("status") == "COMPLETED_RESEARCH_ONLY_BOUNDED_REPLAY", "replay is not complete")
    _require(replay.get("execution_admitted") is False, "replay crossed execution boundary")
    for field in ("paper_allowed", "promotion_allowed", "broker_interacted", "ths_interacted", "pit_verified", "corporate_actions_verified"):
        _require(replay.get(field) is False, f"replay boundary field is not false: {field}")
    _require(handoff.get("execution_admitted") is False, "handoff crossed execution boundary")
    _require(handoff.get("candidate_panel_only") is True, "handoff is not candidate-panel-only")
    handoff_models = {row.get("model_kind"): row for row in handoff.get("models", []) if isinstance(row, Mapping)}
    model_rows = []
    for row in replay.get("models", []):
        if not isinstance(row, Mapping) or not isinstance(row.get("model_kind"), str):
            raise ValueError("replay model row is malformed")
        handoff_row = handoff_models.get(row["model_kind"])
        _require(handoff_row is not None, f"model missing from candidate handoff: {row['model_kind']}")
        model_rows.append(_model_decision(row, handoff_row))
    _require(bool(model_rows), "replay has no model rows")
    return {
        "schema": SCHEMA,
        "status": "COMPLETED_RESEARCH_ONLY_SHORTLINE_ADOPTION_GATE",
        "execution_admitted": False,
        "paper_allowed": False,
        "promotion_allowed": False,
        "inputs": {
            "replay_receipt": str(replay_path.resolve()),
            "replay_receipt_sha256": _sha256(replay_path),
            "candidate_handoff": str(handoff_path.resolve()),
            "candidate_handoff_sha256": _sha256(handoff_path),
        },
        "models": model_rows,
        "summary": {
            "models": len(model_rows),
            "continue_research_only": sum(row["research_action"] == "CONTINUE_RESEARCH_ONLY" for row in model_rows),
            "blocked": sum(row["research_action"] != "CONTINUE_RESEARCH_ONLY" for row in model_rows),
            "orders": sum(row["orders"] for row in model_rows),
            "fills": sum(row["fills"] for row in model_rows),
        },
        "next_gates": [
            "complete StockDB-equivalent raw-bar history for the selected symbols",
            "prove PIT membership, company-action treatment and publication timing",
            "run the same frozen candidates on an untouched forward window",
            "keep the candidate research-only until all gates pass",
        ],
    }


def render_markdown(receipt: Mapping[str, Any]) -> str:
    lines = [
        "# Shortline adoption gate (research only)",
        "",
        "The native LR/LightGBM handoff and bounded BaoStock replay are joined here; this receipt does not authorize paper or live execution.",
        "",
        "| Model | Selected | Sessions | Orders | Fills | Net return | Decision |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in receipt["models"]:
        net = "n/a" if row["net_return"] is None else f"{float(row['net_return']):.4%}"
        lines.append(f"| {row['model_kind']} | {row['selected_count']} | {row['replay_sessions']} | {row['orders']} | {row['fills']} | {net} | {row['research_action']} |")
    lines += ["", "`execution_admitted=false`, `paper_allowed=false`, `promotion_allowed=false`.", ""]
    lines += [f"- {item}" for item in receipt["next_gates"]]
    return "\n".join(lines) + "\n"


def write_outputs(*, replay_path: Path = DEFAULT_REPLAY, handoff_path: Path = DEFAULT_HANDOFF,
                  output: Path = DEFAULT_OUTPUT, report: Path = DEFAULT_REPORT) -> tuple[Path, Path]:
    receipt = build_gate(replay_path, handoff_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(render_markdown(receipt), encoding="utf-8")
    return output, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--handoff", type=Path, default=DEFAULT_HANDOFF)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    output, report = write_outputs(replay_path=args.replay, handoff_path=args.handoff, output=args.output, report=args.report)
    print(json.dumps({"status": "COMPLETED_RESEARCH_ONLY_SHORTLINE_ADOPTION_GATE", "output": str(output), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
