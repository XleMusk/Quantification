"""Audit a frozen shortline score CSV before it can enter the candidate panel.

The audit is deliberately read-only.  It never changes ``paper_candidate_only``
and reports a blocked receipt when a producer has emitted only raw scores.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd


SCHEMA = "quantification_shortline_score_gate_audit_v1"
READY = "VERIFIED_SHORTLINE_SCORE_GATE_READY"
BLOCKED = "SHORTLINE_SCORE_GATE_BLOCKED"
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_shortline_score_gate_audit_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_shortline_score_gate_audit_v1.md")
REQUIRED = {"event_id", "symbol", "date", "stage", "model_hit5_probability", "paper_candidate_only"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flag(value: Any) -> bool | None:
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def audit_scores(path: Path, *, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    path = path.resolve()
    issues: list[str] = []
    if not path.is_file():
        return {
            "schema": SCHEMA, "status": BLOCKED, "input": {"path": str(path), "exists": False},
            "issues": ["input.missing"], "execution_admitted": False, "selection_enabled": False,
            "paper_candidate_only_true": 0, "paper_candidate_only_false": 0,
            "limitations": ["Missing score input cannot be treated as an empty candidate set."],
        }
    digest = _sha256(path)
    try:
        frame = pd.read_csv(path, dtype={"event_id": str, "symbol": str, "date": str})
    except Exception as exc:
        return {
            "schema": SCHEMA, "status": BLOCKED,
            "input": {"path": str(path), "exists": True, "sha256": digest},
            "issues": [f"input.read:{type(exc).__name__}:{exc}"],
            "execution_admitted": False, "selection_enabled": False,
            "paper_candidate_only_true": 0, "paper_candidate_only_false": 0,
            "limitations": ["Malformed score input cannot be admitted to panel selection."],
        }
    missing = sorted(REQUIRED - set(frame.columns))
    if missing:
        issues.append(f"columns.missing:{','.join(missing)}")
    rows = int(len(frame))
    true_count = false_count = invalid_flags = 0
    if "paper_candidate_only" in frame:
        parsed_flags = frame["paper_candidate_only"].map(_flag)
        invalid_flags = int(parsed_flags.isna().sum())
        true_count = int(parsed_flags.eq(True).sum())
        false_count = int(parsed_flags.eq(False).sum())
        if invalid_flags:
            issues.append(f"paper_candidate_only.invalid:{invalid_flags}")
    event_duplicates = 0
    identity_key = ["event_id"]
    # A single saved CSV may intentionally carry one score per model for the
    # same event.  The panel contract still requires uniqueness within a
    # model, so include model identity when the producer supplies it.
    if "model_kind" in frame:
        identity_key.append("model_kind")
    if "generation_id" in frame:
        identity_key.append("generation_id")
    if "event_id" in frame:
        event_duplicates = int(frame.duplicated(identity_key, keep=False).sum())
        if event_duplicates:
            issues.append(f"event_identity.duplicate_rows:{event_duplicates}")
    date_min = date_max = None
    invalid_dates = 0
    if "date" in frame:
        parsed_dates = pd.to_datetime(frame["date"], errors="coerce")
        invalid_dates = int(parsed_dates.isna().sum())
        if invalid_dates:
            issues.append(f"date.invalid:{invalid_dates}")
        valid = parsed_dates.dropna()
        if len(valid):
            date_min, date_max = valid.min().strftime("%Y-%m-%d"), valid.max().strftime("%Y-%m-%d")
        if start and date_min and date_min < start:
            issues.append(f"date.before_requested_start:{date_min}<{start}")
        if end and date_max and date_max > end:
            issues.append(f"date.after_requested_end:{date_max}>{end}")
    invalid_symbols = 0
    if "symbol" in frame:
        invalid_symbols = int((~frame["symbol"].fillna("").str.upper().str.endswith((".SSE", ".SZSE"))).sum())
        if invalid_symbols:
            issues.append(f"symbol.invalid:{invalid_symbols}")
    invalid_stage = 0
    if "stage" in frame:
        stages = pd.to_numeric(frame["stage"], errors="coerce")
        invalid_stage = int((stages.isna() | ~stages.isin((1, 2, 3))).sum())
        if invalid_stage:
            issues.append(f"stage.invalid:{invalid_stage}")
    invalid_scores = 0
    if "model_hit5_probability" in frame:
        scores = pd.to_numeric(frame["model_hit5_probability"], errors="coerce")
        invalid_scores = int((scores.isna() | ~scores.map(math.isfinite) | scores.lt(0) | scores.gt(1)).sum())
        if invalid_scores:
            issues.append(f"model_hit5_probability.invalid:{invalid_scores}")
    if not issues and true_count == 0:
        issues.append("paper_candidate_only.none_true")
    status = READY if not issues else BLOCKED
    return {
        "schema": SCHEMA,
        "status": status,
        "input": {"path": str(path), "exists": True, "sha256": digest, "rows": rows,
                   "date_min": date_min, "date_max": date_max},
        "paper_candidate_only_true": true_count,
        "paper_candidate_only_false": false_count,
        "paper_candidate_only_invalid": invalid_flags,
        "event_id_duplicate_rows": event_duplicates,
        "event_identity_key": identity_key,
        "invalid_dates": invalid_dates,
        "invalid_symbols": invalid_symbols,
        "invalid_stages": invalid_stage,
        "invalid_scores": invalid_scores,
        "requested_range": {"start": start, "end": end},
        "selection_enabled": False if status == BLOCKED else True,
        "target_generation_allowed": False,
        "strategy_validated": False,
        "execution_admitted": False,
        "issues": issues,
        "limitations": [
            "A raw score file becomes a candidate-panel input only when rows are explicitly marked paper_candidate_only=true.",
            "This audit does not change flags, select candidates, generate targets, orders or fills.",
            "Passing this schema gate does not establish PIT publication, company-action safety or strategy validation.",
        ],
    }


def render_report(receipt: dict[str, Any]) -> str:
    input_info = receipt.get("input", {})
    lines = ["# Shortline Score Gate Audit", "", f"`{receipt['status']}`; `NO_TRADE`.",
             f"Input: `{input_info.get('path')}`; rows `{input_info.get('rows', 0)}`; "
             f"date `{input_info.get('date_min')}`..`{input_info.get('date_max')}`.", "",
             f"`paper_candidate_only=true`: **{receipt.get('paper_candidate_only_true', 0)}**; "
             f"false: **{receipt.get('paper_candidate_only_false', 0)}**.", "",
             "Issues:"]
    lines.extend(f"- {item}" for item in receipt.get("issues", []))
    if not receipt.get("issues"):
        lines.append("- none")
    lines += ["", "The audit is read-only; no flags were changed and no panel/target/order was generated.",
              "", *["- " + item for item in receipt.get("limitations", [])]]
    return "\n".join(lines) + "\n"


def run(path: Path, output_path: Path = DEFAULT_OUTPUT, report_path: Path = DEFAULT_REPORT,
        *, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    receipt = audit_scores(path, start=start, end=end)
    receipt["observed_at"] = datetime.now(timezone.utc).isoformat()
    output_path, report_path = output_path.resolve(), report_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    receipt["receipt_path"], receipt["report_path"] = str(output_path), str(report_path)
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(receipt), encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args(argv)
    receipt = run(args.scores, args.output, args.report, start=args.start, end=args.end)
    print(json.dumps({"status": receipt["status"], "rows": receipt.get("input", {}).get("rows", 0),
                      "paper_candidate_only_true": receipt.get("paper_candidate_only_true", 0),
                      "issues": receipt["issues"], "execution_admitted": False}, ensure_ascii=False))
    return 0 if receipt["status"] == READY else 2


__all__ = ["SCHEMA", "READY", "BLOCKED", "audit_scores", "render_report", "run", "main"]
