"""Aggregate non-overlapping independent StockDB/Tencent window replays.

This consumes completed independent-sample receipts only.  It does not merge
cash, positions or returns into one account and never promotes a strategy.
Window non-overlap is checked; calendar continuity is deliberately not inferred
from the individual receipts.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd


SCHEMA = "stockdb_independent_window_stability_v1"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _load_calendar(path: Path) -> tuple[list[str], dict]:
    """Load a pinned ordered A-share calendar without inferring dates."""
    import csv

    path = path.resolve()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    dates = sorted({str(row.get("date") or "").strip() for row in rows if str(row.get("date") or "").strip()})
    if not dates:
        raise ValueError(f"Calendar has no date rows: {path}")
    return dates, {
        "path": str(path),
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "date_count": len(dates),
        "start": dates[0],
        "end": dates[-1],
    }


def _calendar_diagnostics(records: list[tuple[Path, dict]], calendar_path: Path | None) -> dict:
    if calendar_path is None:
        return {
            "provided": False,
            "window_coverage_verified": False,
            "calendar_continuity_verified": False,
            "windows": [],
            "gaps": [],
        }
    calendar, metadata = _load_calendar(calendar_path)
    calendar_set = set(calendar)
    diagnostics = []
    for path, record in records:
        evaluation = record["evaluation_window"]
        start, end = str(evaluation["start"]), str(evaluation["end"])
        expected = [day for day in calendar if start <= day <= end]
        observed = [str(day) for day in evaluation.get("sessions") or []]
        expected_set, observed_set = set(expected), set(observed)
        diagnostics.append({
            "run_id": path.name,
            "start": start,
            "end": end,
            "expected_sessions": len(expected),
            "observed_sessions": len(observed),
            "missing_sessions": sorted(expected_set - observed_set),
            "unexpected_sessions": sorted(observed_set - calendar_set),
            "window_coverage_verified": expected == sorted(observed) and not (observed_set - calendar_set),
        })
    gaps = []
    for previous, current in zip(diagnostics, diagnostics[1:]):
        between = [day for day in calendar if previous["end"] < day < current["start"]]
        if between:
            gaps.append({"after": previous["end"], "before": current["start"], "sessions": between})
    return {
        "provided": True,
        "calendar": metadata,
        "window_coverage_verified": all(item["window_coverage_verified"] for item in diagnostics),
        "calendar_continuity_verified": (
            all(item["window_coverage_verified"] for item in diagnostics) and not gaps
        ),
        "windows": diagnostics,
        "gaps": gaps,
    }


def aggregate(run_paths: list[Path], out: Path, *, calendar_path: Path | None = None) -> tuple[Path, dict]:
    if not run_paths:
        raise ValueError("At least one independent-sample run is required")
    records = []
    for path in run_paths:
        path = path.resolve()
        record = _load(path / "run.json" if path.is_dir() else path)
        if record.get("schema") != "stockdb_independent_sample_compare_v1":
            raise ValueError(f"Not an independent sample comparison: {path}")
        if record.get("status") != "completed_research_only":
            raise ValueError(f"Run is not complete: {path}")
        if record.get("independent_sample") is not True or record.get("prior_forward_overlap") is True:
            raise ValueError(f"Run is not a non-overlapping independent sample: {path}")
        evaluation = record.get("evaluation_window") or {}
        start, end = str(evaluation.get("start") or ""), str(evaluation.get("end") or "")
        if start <= "2026-10-09" and end >= "2026-09-24":
            raise ValueError(f"Run overlaps the prior forward diagnostic: {path}")
        records.append((path, record))

    records.sort(key=lambda item: (item[1]["evaluation_window"]["start"], item[1]["evaluation_window"]["end"]))
    windows = []
    previous_end = None
    provider = None
    for path, record in records:
        evaluation = record["evaluation_window"]
        start, end = str(evaluation["start"]), str(evaluation["end"])
        if previous_end is not None and start <= previous_end:
            raise ValueError(f"Independent windows overlap: {start} <= {previous_end}")
        previous_end = end
        provider = provider or record.get("alternate_provider")
        if record.get("alternate_provider") != provider:
            raise ValueError("Independent windows use different alternate providers")
        for result in record.get("ledger_results") or []:
            windows.append({
                "run_id": path.name,
                "artifact": str(path),
                "selection_cutoff": record.get("selection_cutoff"),
                "start": start,
                "end": end,
                "sessions": len(evaluation.get("sessions") or []),
                "candidate": result.get("candidate"),
                "net_return": result.get("net_return"),
                "max_drawdown": result.get("max_drawdown"),
                "orders": result.get("orders"),
                "fills": result.get("fills"),
                "paired_rows": record.get("provider_complete_paired_rows"),
                "target_universe_count": record.get("target_universe_count"),
                "target_basket_size": record.get("target_basket_size"),
            })

    frame = pd.DataFrame(windows)
    if frame.empty or frame["candidate"].isna().any():
        raise ValueError("Completed runs have no usable candidate results")
    frame.to_csv(out / "window_results.csv", index=False)
    summaries = []
    for candidate, group in frame.groupby("candidate", sort=True):
        returns = pd.to_numeric(group["net_return"], errors="raise")
        drawdowns = pd.to_numeric(group["max_drawdown"], errors="raise")
        summaries.append({
            "candidate": candidate,
            "windows": int(len(group)),
            "positive_windows": int((returns > 0).sum()),
            "mean_net_return": float(returns.mean()),
            "median_net_return": float(returns.median()),
            "worst_net_return": float(returns.min()),
            "best_net_return": float(returns.max()),
            "worst_max_drawdown": float(drawdowns.min()),
            "total_sessions": int(pd.to_numeric(group["sessions"], errors="raise").sum()),
            "total_orders": int(pd.to_numeric(group["orders"], errors="raise").sum()),
            "total_fills": int(pd.to_numeric(group["fills"], errors="raise").sum()),
            "paired_rows_all_windows": bool((pd.to_numeric(group["paired_rows"], errors="raise") > 0).all()),
        })

    calendar_diagnostics = _calendar_diagnostics(records, calendar_path)
    record = {
        "schema": SCHEMA,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "completed_research_only",
        "source_runs": [str(path) for path, _ in records],
        "window_count": len(records),
        "window_start": records[0][1]["evaluation_window"]["start"],
        "window_end": records[-1][1]["evaluation_window"]["end"],
        "non_overlapping": True,
        "calendar_continuity_verified": calendar_diagnostics["calendar_continuity_verified"],
        "calendar_window_coverage_verified": calendar_diagnostics["window_coverage_verified"],
        "calendar_diagnostics": calendar_diagnostics,
        "alternate_provider": provider,
        "strategy_summaries": summaries,
        "research_only": True,
        "execution_admitted": False,
        "strategy_validated": False,
        "selection_enabled": False,
        "limitations": [
            "Window returns reset the paper account at each selection cutoff and are not one continuous account return",
            "Calendar continuity is not verified unless a pinned ordered calendar is supplied and all adjacent windows are contiguous",
            "StockDB raw prices remain unadjusted and PIT membership, corporate actions and broker evidence are not established",
            "Provider equality is measured only for the fixed target rows in each source receipt",
        ],
    }
    _write(out / "run.json", record)
    lines = [
        "# Independent StockDB/Tencent Window Stability",
        "",
        "Status: `completed_research_only`; execution admitted: `False`.",
        f"Windows: `{record['window_count']}`; range: `{record['window_start']}..{record['window_end']}`; provider: `{provider}`.",
        f"Windows are non-overlapping; per-window calendar coverage verified: `{record['calendar_window_coverage_verified']}`; "
        f"cross-window continuity verified: `{record['calendar_continuity_verified']}`.",
        "Returns are reset per selection window and must not be interpreted as a single compounded account result.",
        "",
        "|Candidate|Windows|Positive|Mean|Median|Worst|Worst MDD|Orders|Fills|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        lines.append(
            f"|{item['candidate']}|{item['windows']}|{item['positive_windows']}|"
            f"{item['mean_net_return']:.4%}|{item['median_net_return']:.4%}|"
            f"{item['worst_net_return']:.4%}|{item['worst_max_drawdown']:.4%}|"
            f"{item['total_orders']}|{item['total_fills']}|"
        )
    lines.extend(["", "Artifacts: " + str(out)])
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out, record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, help="Pinned ordered calendar.csv used for coverage diagnostics")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=False)
    out, record = aggregate(args.runs, args.out, calendar_path=args.calendar)
    print(json.dumps({"artifact": str(out), "strategy_summaries": record["strategy_summaries"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
