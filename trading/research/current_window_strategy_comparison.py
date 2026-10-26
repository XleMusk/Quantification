"""Compare two completed StockDB replays on one explicit terminal window.

The replay runners already produced the account artifacts.  This module only
reads those artifacts, intersects their saved daily dates, and writes a
research-only comparison.  It is intentionally explicit about run and account
paths so a new close can be compared without changing the retained long-window
receipt or silently reusing an older terminal date.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import json
from math import sqrt
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping

from ..settings import ROOT


SCHEMA = "quantification_current_window_strategy_comparison_v1"
RECEIPT_NAME = "20261026_current_window_fixed_candidate_v1.json"
REPORT_NAME = "20261026_current_window_fixed_candidate_v1.md"
RUN_SCHEMA = "stockdb_rolling_low_volatility_ledger_v1"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _parse_day(value: str) -> date:
    return date.fromisoformat(value[:10])


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _account_paths(account_dir: Path) -> dict[str, Path]:
    paths = {name: account_dir / f"{name}.csv" for name in ("daily", "orders", "fills")}
    paths["account"] = account_dir / "account.json"
    paths["replay"] = account_dir / "replay.json"
    missing = [f"{name}: {path}" for name, path in paths.items() if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing saved account artifacts: " + "; ".join(missing))
    return paths


def _run_entry(run_path: Path, account_dir: Path, label: str, spec_id: str) -> dict[str, Any]:
    run = _read_json(run_path)
    if run.get("schema") != RUN_SCHEMA or run.get("status") != "completed_research_only":
        raise ValueError(f"Run is not a completed research-only StockDB replay: {run_path}")
    unsafe = [key for key in ("execution_admitted", "strategy_validated", "fresh_holdout_claimed")
              if run.get(key) is True]
    if unsafe:
        raise ValueError(f"Unsafe or overstated run flags in {run_path}: {unsafe}")
    results = run.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError(f"Expected exactly one completed strategy result: {run_path}")
    result = results[0]
    if result.get("status") != "completed":
        raise ValueError(f"Strategy result is not completed: {run_path}")
    protocol_path = run_path.with_name("protocol.json")
    protocol = _read_json(protocol_path) if protocol_path.exists() else {}
    snapshot = protocol.get("snapshot") or run.get("resume_original_snapshot")
    if not snapshot:
        raise ValueError(f"Run has no StockDB snapshot lineage: {run_path}")
    paths = _account_paths(account_dir)
    daily_rows = _read_rows(paths["daily"])
    days = [_parse_day(row["date"]) for row in daily_rows]
    if len(days) != len(set(days)) or days != sorted(days):
        raise ValueError(f"daily.csv dates are not unique and sorted: {paths['daily']}")
    expected_sessions = int(result.get("sessions", len(daily_rows)))
    if len(daily_rows) != expected_sessions:
        raise ValueError(f"daily.csv/session mismatch for {run_path}: {len(daily_rows)} != {expected_sessions}")
    return {
        "id": str(result.get("signal") or run.get("variants", [label])[0]),
        "label": label,
        "candidate_spec_id": spec_id,
        "run": run,
        "result": result,
        "protocol": protocol,
        "snapshot": str(Path(snapshot).expanduser().resolve()),
        "account_dir": str(account_dir.expanduser().resolve()),
        "source_paths": {name: str(path.expanduser().resolve()) for name, path in paths.items()},
        "daily_rows": daily_rows,
    }


def _sum_field(path: Path, field: str, dates: set[date]) -> float:
    return sum(float(row.get(field) or 0.0) for row in _read_rows(path) if _parse_day(row["day"]) in dates)


def _count_rows(path: Path, dates: set[date]) -> int:
    return sum(_parse_day(row["day"]) in dates for row in _read_rows(path))


def _daily_metrics(rows: Iterable[Mapping[str, str]], dates: set[date]) -> dict[str, Any]:
    selected = sorted((row for row in rows if _parse_day(row["date"]) in dates), key=lambda row: row["date"])
    if not selected:
        raise ValueError("No daily account rows in common date intersection")
    equity = [float(row["equity"]) for row in selected]
    returns = [equity[index] / equity[index - 1] - 1.0 for index in range(1, len(equity))]
    peak = equity[0]
    drawdowns: list[float] = []
    for value in equity:
        peak = max(peak, value)
        drawdowns.append(value / peak - 1.0)
    volatility = 0.0
    if len(returns) > 1:
        average = mean(returns)
        volatility = sqrt(sum((value - average) ** 2 for value in returns) / (len(returns) - 1))
    return {
        "sessions": len(selected),
        "window_start_equity": equity[0],
        "window_end_equity": equity[-1],
        "net_return": equity[-1] / equity[0] - 1.0,
        "max_drawdown": min(drawdowns),
        "sharpe_zero_rf_252": None if volatility == 0.0 else mean(returns) / volatility * sqrt(252.0),
        "window_turnover_value": sum(float(row.get("turnover_value") or 0.0) for row in selected),
    }


def _entry_metrics(entry: Mapping[str, Any], common_dates: set[date]) -> dict[str, Any]:
    metrics = _daily_metrics(entry["daily_rows"], common_dates)
    paths = {key: Path(value) for key, value in entry["source_paths"].items()}
    metrics["orders"] = _count_rows(paths["orders"], common_dates)
    metrics["fills"] = _count_rows(paths["fills"], common_dates)
    metrics["fees_rmb"] = _sum_field(paths["orders"], "fees", common_dates) / 100.0
    return metrics


def build_receipt(
    *,
    fixed_run: str | Path,
    fixed_account_dir: str | Path,
    candidate_run: str | Path,
    candidate_account_dir: str | Path,
) -> dict[str, Any]:
    entries = [
        _run_entry(Path(fixed_run).expanduser().resolve(), Path(fixed_account_dir), "固定低波动", "stockdb_low_volatility_252_20"),
        _run_entry(Path(candidate_run).expanduser().resolve(), Path(candidate_account_dir), "低波/半动量", "stockdb_low_vol_half_momentum_20"),
    ]
    snapshots = {entry["snapshot"] for entry in entries}
    if len(snapshots) != 1:
        raise ValueError(f"Replays do not share one StockDB snapshot: {sorted(snapshots)}")
    common_dates = set.intersection(*({ _parse_day(row["date"]) for row in entry["daily_rows"] } for entry in entries))
    if not common_dates:
        raise ValueError("No common daily account dates")
    common_start, common_end = min(common_dates), max(common_dates)
    strategies = []
    for entry in entries:
        metrics = _entry_metrics(entry, common_dates)
        strategies.append({
            "id": entry["id"],
            "label": entry["label"],
            "candidate_spec_id": entry["candidate_spec_id"],
            "metrics": metrics,
            "native_replay_window": {
                "start": entry["run"].get("replay_start"),
                "end": entry["run"].get("replay_end"),
                "sessions": int(entry["result"].get("sessions", 0)),
            },
            "source_paths": entry["source_paths"],
            "execution_admitted": False,
            "strategy_validated": False,
            "ledger_mutated": False,
            "research_decision": (
                "CONTINUE_RESEARCH_ONLY"
                if metrics["net_return"] > 0 and (metrics["sharpe_zero_rf_252"] or 0.0) > 0
                else "STOP_RESEARCH_ONLY"
            ),
        })
    return {
        "schema": SCHEMA,
        "status": "COMPLETED_RESEARCH_ONLY_CURRENT_WINDOW_COMPARISON",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False,
        "strategy_validated": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "promotion_allowed": False,
        "stockdb_snapshot": next(iter(snapshots)),
        "common_window": {
            "start": common_start.isoformat(),
            "end": common_end.isoformat(),
            "sessions": len(common_dates),
            "basis": "intersection_of_explicit_saved_daily_account_dates",
        },
        "comparison_contract": {
            "shared_stockdb_snapshot": True,
            "shared_engine_ledger_path": True,
            "initial_cash_rmb": 140000.0,
            "metrics": ["sessions", "orders", "fills", "net_return", "max_drawdown", "sharpe_zero_rf_252", "fees_rmb", "window_turnover_value"],
            "research_decision_rule": "common-window net_return > 0 and Sharpe > 0 means CONTINUE_RESEARCH_ONLY",
            "no_strategy_rerun": True,
            "no_parameter_retuning": True,
        },
        "strategies": strategies,
        "limitations": [
            "The comparison reads completed research-only account artifacts and does not rerun either strategy.",
            "The terminal overlap is a short current-window diagnostic; it does not replace long-window or prospective validation.",
            "PIT completeness, broker/THS observation, strategy validation and promotion remain unproven.",
        ],
    }


def render_markdown(receipt: Mapping[str, Any]) -> str:
    window = receipt["common_window"]
    lines = [
        "# Current-window fixed versus half-momentum comparison",
        "",
        f"Common saved-account window: `{window['start']}..{window['end']}` ({window['sessions']} sessions).",
        "",
        "This report reads the explicit 2026-10-26 StockDB replay artifacts on a shared Engine/Ledger path. It is a research-only terminal-window diagnostic; no broker or Tonghuashun channel was accessed.",
        "",
        "| Strategy | Sessions | Orders | Fills | Net return | Max drawdown | Sharpe | Fees (RMB) | Decision |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for entry in receipt["strategies"]:
        metrics = entry["metrics"]
        sharpe = "null" if metrics["sharpe_zero_rf_252"] is None else f"{metrics['sharpe_zero_rf_252']:.4f}"
        lines.append(
            f"| {entry['label']} (`{entry['id']}`) | {metrics['sessions']} | {metrics['orders']} | {metrics['fills']} | "
            f"{metrics['net_return']:.4%} | {metrics['max_drawdown']:.4%} | {sharpe} | {metrics['fees_rmb']:.2f} | {entry['research_decision']} |"
        )
    lines += [
        "",
        "`execution_admitted=false`; `strategy_validated=false`; `promotion_allowed=false`.",
        "",
        "## Boundaries",
        "",
    ]
    lines.extend(f"- {item}" for item in receipt["limitations"])
    return "\n".join(lines) + "\n"


def write_outputs(
    *,
    fixed_run: str | Path,
    fixed_account_dir: str | Path,
    candidate_run: str | Path,
    candidate_account_dir: str | Path,
    receipt_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> tuple[Path, Path]:
    receipt_path = Path(receipt_path or ROOT / "outputs/pipeline_receipts" / RECEIPT_NAME).expanduser().resolve()
    report_path = Path(report_path or ROOT / "outputs/research_reports" / REPORT_NAME).expanduser().resolve()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = build_receipt(
        fixed_run=fixed_run,
        fixed_account_dir=fixed_account_dir,
        candidate_run=candidate_run,
        candidate_account_dir=candidate_account_dir,
    )
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_markdown(receipt), encoding="utf-8")
    return receipt_path, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixed-run", type=Path, required=True)
    parser.add_argument("--fixed-account-dir", type=Path, required=True)
    parser.add_argument("--candidate-run", type=Path, required=True)
    parser.add_argument("--candidate-account-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    receipt_path, report_path = write_outputs(
        fixed_run=args.fixed_run,
        fixed_account_dir=args.fixed_account_dir,
        candidate_run=args.candidate_run,
        candidate_account_dir=args.candidate_account_dir,
        receipt_path=args.receipt,
        report_path=args.report,
    )
    print(json.dumps({"receipt": str(receipt_path), "report": str(report_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_receipt", "main", "render_markdown", "write_outputs"]
