"""Diagnose saved-ledger robustness without creating a new replay."""
import argparse
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import uuid

from . import replay_reports, settings
from . import research_comparison


DEFAULT_WINDOWS = (20, 60, 120)
DEFAULT_COST_STRESS_BPS = (0, 5, 10, 20, 50)


def _finite(value):
    return value is not None and math.isfinite(float(value))


def _max_drawdown(equity):
    if not equity or any(not _finite(value) or value <= 0 for value in equity):
        return None
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def _sharpe(equity):
    if len(equity) < 2 or any(not _finite(value) or value <= 0 for value in equity):
        return None
    returns = [equity[index] / equity[index - 1] - 1.0 for index in range(1, len(equity))]
    if not returns:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    deviation = math.sqrt(variance)
    return mean / deviation * math.sqrt(252.0) if deviation > 0 else None


def _quantile(values, probability):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _load_marks(case):
    record, external_value = research_comparison._read_saved_replay(case["run_id"])
    if external_value is not None:
        value = external_value
        marks = value["daily_marks"]
        config = value["config"]
    else:
        value = replay_reports.detail(case["run_id"], case["signal"], include_marks=True)
        config = value["config"]
        saved_record, artifacts = replay_reports._read(case["run_id"])
        folder = (artifacts / saved_record.get("ledger_folders", {}).get(case["signal"], case["signal"])).resolve()
        with (folder / "daily.csv").open(encoding="utf-8-sig", newline="") as stream:
            marks = list(csv.DictReader(stream))
    if not marks:
        raise ValueError(f"Saved replay has no daily marks: {case['case_id']}")
    normalized = []
    for mark in marks:
        date = str(mark["date"])[:10]
        equity = float(mark["equity"])
        if not _finite(equity) or equity <= 0:
            raise ValueError(f"Invalid saved equity for {case['case_id']} on {date}")
        normalized.append({"date": date, "equity": equity,
                           "turnover_value": float(mark.get("turnover_value", 0.0))})
    dates = [mark["date"] for mark in normalized]
    if dates != sorted(set(dates)):
        raise ValueError(f"Saved marks are not a unique ordered calendar: {case['case_id']}")
    return normalized, float(config["initial_cash"]), record


def _window_row(case, marks, window_length, start_index, end_index, initial_cash):
    opening = marks[start_index - 1]["equity"] if start_index else initial_cash
    selected = marks[start_index:end_index + 1]
    equity = [opening] + [mark["equity"] for mark in selected]
    turnover = sum(mark["turnover_value"] for mark in selected)
    net_return = equity[-1] / opening - 1.0
    return {
        "case_id": case["case_id"],
        "run_id": case["run_id"],
        "signal": case["signal"],
        "label": case.get("label") or case["signal"],
        "window_sessions": window_length,
        "start": selected[0]["date"],
        "end": selected[-1]["date"],
        "opening_equity": opening,
        "final_equity": equity[-1],
        "net_return": net_return,
        "max_drawdown": _max_drawdown(equity),
        "sharpe_zero_rf_252": _sharpe(equity),
        "turnover_value": turnover,
    }


def _summary(case, rows):
    returns = [row["net_return"] for row in rows]
    drawdowns = [row["max_drawdown"] for row in rows if row["max_drawdown"] is not None]
    sharpes = [row["sharpe_zero_rf_252"] for row in rows if row["sharpe_zero_rf_252"] is not None]
    return {
        "case_id": case["case_id"],
        "run_id": case["run_id"],
        "signal": case["signal"],
        "label": case.get("label") or case["signal"],
        "window_sessions": rows[0]["window_sessions"] if rows else None,
        "window_count": len(rows),
        "positive_window_ratio": sum(value > 0 for value in returns) / len(returns) if returns else None,
        "return_q25": _quantile(returns, 0.25),
        "return_median": _quantile(returns, 0.50),
        "return_q75": _quantile(returns, 0.75),
        "worst_window_return": min(returns) if returns else None,
        "best_window_return": max(returns) if returns else None,
        "worst_window_drawdown": min(drawdowns) if drawdowns else None,
        "median_window_drawdown": _quantile(drawdowns, 0.50),
        "median_window_sharpe": _quantile(sharpes, 0.50),
        "status": "computed_from_saved_marks" if rows else "no_saved_window",
    }


def _cost_rows(case, marks, first_index, initial_cash, stress_bps):
    selected = marks[first_index:]
    opening = marks[first_index - 1]["equity"] if first_index else initial_cash
    final_equity = selected[-1]["equity"]
    turnover = sum(mark["turnover_value"] for mark in selected)
    baseline_return = final_equity / opening - 1.0
    rows = []
    for bps in stress_bps:
        additional_cost = turnover * bps / 10000.0
        rows.append({
            "case_id": case["case_id"],
            "run_id": case["run_id"],
            "signal": case["signal"],
            "label": case.get("label") or case["signal"],
            "stress_additional_slippage_bps": bps,
            "opening_equity": opening,
            "turnover_value": turnover,
            "baseline_net_return": baseline_return,
            "additional_cost": additional_cost,
            "stressed_net_return": baseline_return - additional_cost / opening,
            "status": "arithmetic_stress_on_saved_turnover",
        })
    return rows


def diagnose(comparison_path, windows=DEFAULT_WINDOWS, stress_bps=DEFAULT_COST_STRESS_BPS):
    comparison_path = Path(comparison_path).resolve()
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    if comparison.get("status") not in {"compared_saved_accounts", "no_common_completed_window"}:
        raise ValueError("Input must be a saved-ledger comparison")
    if comparison.get("strategy_validated") is not False or comparison.get("execution_admitted") is not False:
        raise ValueError("Comparison must remain unvalidated and unadmitted")
    windows = tuple(sorted(set(int(value) for value in windows)))
    stress_bps = tuple(sorted(set(int(value) for value in stress_bps)))
    if not windows or any(value < 2 for value in windows):
        raise ValueError("Window lengths must be at least two sessions")
    if not stress_bps or any(value < 0 for value in stress_bps):
        raise ValueError("Stress slippage must be non-negative")

    rolling_rows, summaries, cost_rows = [], [], []
    cases = [case for case in comparison.get("cases", []) if case.get("status") == "comparable"]
    for case in cases:
        marks, initial_cash, _record = _load_marks(case)
        start, end = comparison["start"], comparison["end"]
        selected = [mark for mark in marks if start <= mark["date"] <= end]
        if not selected or selected[0]["date"] != start or selected[-1]["date"] != end:
            raise ValueError(f"Comparison case no longer covers exact window: {case['case_id']}")
        first_index = marks.index(selected[0])
        if [mark["date"] for mark in selected] != comparison.get("calendar", []):
            raise ValueError(f"Comparison calendar differs from saved marks: {case['case_id']}")
        for length in windows:
            rows = []
            if len(selected) >= length:
                for start_index in range(first_index, first_index + len(selected) - length + 1):
                    rows.append(_window_row(case, marks, length, start_index, start_index + length - 1, initial_cash))
            rolling_rows.extend(rows)
            summaries.append(_summary(case, rows))
        cost_rows.extend(_cost_rows(case, marks, first_index, initial_cash, stress_bps))

    return {
        "status": "saved_mark_robustness_diagnostic",
        "comparison": str(comparison_path),
        "start": comparison.get("start"),
        "end": comparison.get("end"),
        "sessions": comparison.get("sessions", 0),
        "cases": len(cases),
        "window_lengths": list(windows),
        "cost_stress_bps": list(stress_bps),
        "rolling_windows": rolling_rows,
        "rolling_window_summaries": summaries,
        "cost_sensitivity": cost_rows,
        "fresh_holdout_claimed": False,
        "strategy_validated": False,
        "execution_admitted": False,
        "backtest_run": False,
        "basis": "Read-only rolling-window and arithmetic cost-stress diagnostics from existing sole-ledger daily marks; no curve reset, refit or new replay",
        "limits": [
            "All windows reuse the already inspected comparison period and are not fresh holdout evidence",
            "Rolling windows overlap and are not independent observations",
            "Cost stress subtracts turnover multiplied by additional slippage; it is not a new order-level replay",
            "No strategy selection, promotion, account mutation or live execution is implied",
            "Missing or stale marks remain a source limitation and are not filled with synthetic values",
        ],
    }


def _fmt(value, percent=False):
    if value is None:
        return "unavailable"
    return f"{value:.2%}" if percent else f"{value:.4f}"


def report(value):
    lines = [
        "# Saved-Mark Robustness Diagnostic",
        "",
        f"{value['start']}..{value['end']}: {value['sessions']} sessions, {value['cases']} comparable cases.",
        "This is an offline diagnostic from existing sole-ledger daily marks. NO_TRADE.",
        "",
        "## Rolling windows",
        "",
        "| Case | Window | Count | Positive ratio | Return median | Worst return | Worst MDD | Median Sharpe | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in value["rolling_window_summaries"]:
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            row["label"], row["window_sessions"], row["window_count"], _fmt(row["positive_window_ratio"], True),
            _fmt(row["return_median"], True), _fmt(row["worst_window_return"], True),
            _fmt(row["worst_window_drawdown"], True), _fmt(row["median_window_sharpe"]), row["status"]))
    lines += ["", "## Additional slippage stress", "",
              "The stressed result subtracts turnover * additional slippage from the saved full-window return; it does not rerun fills.", "",
              "| Case | Extra slippage bps | Baseline return | Additional cost CNY | Stressed return |", "|---|---:|---:|---:|---:|"]
    for row in value["cost_sensitivity"]:
        lines.append("| {} | {} | {} | {:.2f} | {} |".format(
            row["label"], row["stress_additional_slippage_bps"], _fmt(row["baseline_net_return"], True),
            row["additional_cost"], _fmt(row["stressed_net_return"], True)))
    lines += ["", "## Boundaries", "", *value["limits"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", required=True, help="Saved research comparison.json")
    parser.add_argument("--window", dest="windows", action="append", type=int,
                        help="Rolling window length in sessions; repeatable")
    parser.add_argument("--cost-stress-bps", dest="stress_bps", action="append", type=int,
                        help="Additional slippage stress in bps; repeatable")
    parser.add_argument("--out-dir", help="Optional output directory")
    args = parser.parse_args(argv)
    value = diagnose(args.comparison, args.windows or DEFAULT_WINDOWS, args.stress_bps or DEFAULT_COST_STRESS_BPS)
    out = Path(args.out_dir).resolve() if args.out_dir else (
        settings.ROOT / "outputs" / "research_robustness" /
        (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]))
    out.mkdir(parents=True, exist_ok=False)
    (out / "robustness.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    with (out / "rolling_windows.csv").open("w", encoding="utf-8", newline="") as stream:
        rows = value["rolling_windows"]
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["status"])
        writer.writeheader()
        writer.writerows(rows)
    with (out / "rolling_window_summaries.csv").open("w", encoding="utf-8", newline="") as stream:
        rows = value["rolling_window_summaries"]
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["status"])
        writer.writeheader()
        writer.writerows(rows)
    with (out / "cost_sensitivity.csv").open("w", encoding="utf-8", newline="") as stream:
        rows = value["cost_sensitivity"]
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["status"])
        writer.writeheader()
        writer.writerows(rows)
    (out / "REPORT.md").write_text(report(value), encoding="utf-8")
    print(json.dumps({key: value[key] for key in ("status", "start", "end", "sessions", "cases", "window_lengths", "cost_stress_bps")}, ensure_ascii=False))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
