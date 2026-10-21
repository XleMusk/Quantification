"""Summarize rolling stability of one completed fixed-cadence ledger replay.

This reads the saved daily equity path only. It does not refit, rerun, select a
window, or claim a fresh holdout; it is a descriptive companion to the fixed
cadence replay.
"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import ROOT


WINDOWS = (60, 120)


def _load_run(path):
    path = Path(path).resolve()
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("status") != "completed_factor_candidate_cadence_replay":
        raise ValueError("A completed fixed-cadence replay is required")
    if (record.get("result") or {}).get("status") != "completed":
        raise ValueError("The fixed-cadence ledger replay did not complete")
    artifacts = Path(record["artifacts"]).resolve()
    daily = artifacts / str(record["signal"]) / "daily.csv"
    if not daily.is_file():
        raise FileNotFoundError(f"Saved cadence daily ledger not found: {daily}")
    return path, record, daily


def _max_drawdown(values):
    values = np.asarray(values, dtype=float)
    if len(values) == 0 or not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("Window equity must contain finite positive values")
    return float(np.min(values / np.maximum.accumulate(values) - 1))


def summarize(daily_path):
    daily_path = Path(daily_path).resolve()
    frame = pd.read_csv(daily_path, parse_dates=["date"])
    required = {"date", "equity"}
    if not required <= set(frame.columns):
        raise ValueError(f"Saved daily ledger is missing columns: {sorted(required - set(frame.columns))}")
    if frame.empty or frame.date.duplicated().any() or not frame.date.is_monotonic_increasing:
        raise ValueError("Saved daily ledger must have unique ascending dates")
    equity = pd.to_numeric(frame.equity, errors="coerce")
    if not np.isfinite(equity).all() or (equity <= 0).any():
        raise ValueError("Saved daily ledger contains invalid equity values")
    windows = {}
    for window in WINDOWS:
        returns = (equity / equity.shift(window) - 1).dropna()
        drawdowns = [
            _max_drawdown(equity.iloc[index - window:index + 1])
            for index in range(window, len(equity))
        ]
        windows[str(window)] = {
            "window_count": int(len(returns)),
            "positive_windows": int((returns > 0).sum()),
            "positive_window_ratio": float((returns > 0).mean()),
            "return_median": float(returns.median()),
            "return_min": float(returns.min()),
            "return_max": float(returns.max()),
            "drawdown_worst": float(min(drawdowns)),
        }
    return {
        "status": "completed_saved_ledger_rolling_diagnostic",
        "sessions": int(len(frame)),
        "start": frame.date.iloc[0].date().isoformat(),
        "end": frame.date.iloc[-1].date().isoformat(),
        "windows": windows,
        "execution_admitted": False,
    }


def run(replay_path, destination=None):
    replay_path, replay, daily_path = _load_run(replay_path)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(destination).resolve() if destination else ROOT / "outputs" / "factor_candidate_cadence_stability" / identifier
    out.mkdir(parents=True, exist_ok=False)
    result = summarize(daily_path)
    result.update({
        "id": identifier,
        "replay_run": str(replay_path),
        "daily_ledger": str(daily_path),
        "daily_ledger_sha256": hashlib.sha256(daily_path.read_bytes()).hexdigest(),
        "signal": replay.get("signal"),
        "source_cutoff": replay.get("end"),
        "method": "saved_daily_equity_rolling_windows_v1",
        "limitations": [
            "Descriptive rolling windows over one completed fixed-cadence ledger path",
            "No model refit, cadence search, new price data, or fresh holdout is claimed",
            "Positive windows do not establish causal factor alpha or execution readiness",
        ],
    })
    (out / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Fixed-Cadence Rolling Stability",
        "",
        "Research development only; `NO_TRADE`.",
        "",
        f"Source replay: `{replay_path}`",
        f"Daily ledger: `{daily_path}`",
        "",
        "| Window | Positive | Median | Worst |",
        "|---:|---:|---:|---:|",
    ]
    for window in WINDOWS:
        row = result["windows"][str(window)]
        lines.append(f"| {window} sessions | {row['positive_windows']}/{row['window_count']} ({row['positive_window_ratio']:.1%}) | {row['return_median']:.2%} | {row['return_min']:.2%} |")
    lines.extend(["", *[f"- {item}" for item in result["limitations"]], ""])
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"output": str(out), "result": result}, ensure_ascii=False), flush=True)
    return out, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    run(args.run, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
