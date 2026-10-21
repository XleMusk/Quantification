"""Combine fixed shortline stage, touchability and model diagnostics.

The output is a research receipt for deciding which board stages deserve a
separate signal test.  It never creates candidates, targets, orders or THS
payloads.  Forward labels and next-open clearance are explicitly diagnostic
only; they cannot enter signal selection.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from ..settings import ROOT


def _load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def assemble(baseline: dict, touchability: dict, lgbm: dict) -> dict:
    baseline_rows = {int(row["stage"]): row for row in baseline.get("rows", [])}
    touch_rows = {int(row["stage"]): row for row in touchability.get("by_stage", [])}
    model_rows = {}
    for row in lgbm.get("results", []):
        model_rows.setdefault(str(row["fold"]), {})[int(row["stage"])] = {
            "test_events": int(row["test_events"]),
            "test_hit5_rate": float(row["test_hit5_rate"]),
            "model_auc": float(row["model_auc"]),
            "model_average_precision": float(row["model_average_precision"]),
            "top10pct_events": int(row["top10pct_events"]),
            "top10pct_hit5_rate": float(row["top10pct_hit5_rate"]),
            "baseline_brier": float(row["baseline_brier"]),
            "model_brier": float(row["model_brier"]),
        }
    rows = []
    for stage in (1, 2, 3):
        base = baseline_rows.get(stage, {})
        touch = touch_rows.get(stage, {})
        rows.append({
            "stage": stage,
            "events": int(base.get("events", 0)),
            "hit_4_rate": float(base.get("hit_4_rate", 0.0)),
            "hit_5_rate": float(base.get("hit_5_rate", 0.0)),
            "next_open_clearance_rate": float(touch.get("next_open_clearance_rate", 0.0)),
            "next_high_touches_limit_up_rate": float(touch.get("next_high_touches_limit_up_rate", 0.0)),
            "hit_5_conditional_open_clearance_rate": float(touch.get("hit_5_conditional_open_clearance_rate", 0.0)),
            "model_by_fold": {fold: values.get(stage) for fold, values in model_rows.items()},
        })
    return {
        "schema": "a_share_shortline_stage_selection_diagnostic_v1",
        "status": "completed_research_diagnostic",
        "created_at": datetime.now().astimezone().isoformat(),
        "stages": rows,
        "screening_hypothesis": {
            "signal_stage_scope": [1, 2, 3],
            "interpretation": "stage is a signal-date state variable; higher stage has higher base continuation but lower available entry liquidity",
            "signal_date_fields": list(lgbm.get("features", [])),
            "post_event_fields": ["hit_4", "hit_5", "next_open_clearance", "next_high_touches_limit_up"],
            "post_event_fields_used_for_selection": False,
            "next_open_clearance_is_executable": False,
        },
        "future_labels_loaded_for_diagnostic": True,
        "future_labels_used_for_selection": False,
        "candidate_generated": False,
        "target_generated": False,
        "orders_generated": False,
        "ths_payload_generated": False,
        "execution_admitted": False,
        "strategy_validated": False,
        "limitations": [
            "Close-based continuation labels and daily touchability proxies are not fills.",
            "No queue position, intraday latency, T+1, costs or sector/theme mapping is included.",
            "The model folds are dated diagnostics, not a final untouched holdout.",
        ],
    }


def run(baseline: Path, touchability: Path, lgbm: Path, out: Path) -> Path:
    record = assemble(_load(baseline), _load(touchability), _load(lgbm))
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    result = out / "result.json"
    result.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Shortline stage selection diagnostic",
        "",
        "This is a research receipt only. It does not create candidates, targets, orders or THS payloads.",
        "",
        "| Stage | Events | Hit 4 | Hit 5 | Open clearance | High touches limit | Hit 5 conditional clearance |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in record["stages"]:
        lines.append(
            f"| {row['stage']} | {row['events']} | {row['hit_4_rate']:.2%} | {row['hit_5_rate']:.2%} | "
            f"{row['next_open_clearance_rate']:.2%} | {row['next_high_touches_limit_up_rate']:.2%} | "
            f"{row['hit_5_conditional_open_clearance_rate']:.2%} |"
        )
    lines += [
        "",
        "Forward labels and touchability are included for diagnosis only; they are not signal fields.",
        "`execution_admitted=false`, `strategy_validated=false`, and no downstream artifact was generated.",
        f"Result: `{result}`",
    ]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--touchability", type=Path, required=True)
    parser.add_argument("--lgbm", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.baseline, args.touchability, args.lgbm, args.out)
    print(json.dumps({"result": str(result), "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
