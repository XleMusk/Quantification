"""Compare two completed mature-factor Engine/Ledger replays.

The comparison is deliberately read-only. It consumes saved replay outputs,
checks that both runs are research-only, aligns their daily equity paths, and
writes a machine-readable receipt plus a concise report. It never refits a
model, changes a target, mutates a ledger, or grants execution permission.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


REQUIRED_STATUS = "completed_research_only_mature_factor_replay"
FORBIDDEN_TRUE_FLAGS = (
    "execution_admitted",
    "strategy_validated",
    "ledger_mutated",
    "pit_verified",
    "paper_allowed",
    "promotion_allowed",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read replay JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Replay JSON must be an object: {path}")
    return value


def _resolve_result(value: str | Path) -> Path:
    path = Path(value)
    if path.is_dir():
        path = path / "result.json"
    if path.name != "result.json":
        raise ValueError(f"Replay input must be a result.json or its directory: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"Replay result not found: {path}")
    return path.resolve()


def _float(value: Any, field: str, path: Path) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Replay field {field!r} is not numeric in {path}") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"Replay field {field!r} is not finite in {path}")
    return result


def _csv_count(path: Path) -> int:
    if not path.is_file():
        raise FileNotFoundError(f"Replay artifact not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _daily_equity(path: Path) -> dict[str, float]:
    if not path.is_file():
        raise FileNotFoundError(f"Daily equity artifact not found: {path}")
    values: dict[str, float] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(handle)
        if not rows.fieldnames or not {"date", "equity"} <= set(rows.fieldnames):
            raise ValueError(f"Daily artifact needs date/equity columns: {path}")
        for row in rows:
            day = str(row.get("date") or "").strip()
            if not day or day in values:
                raise ValueError(f"Daily artifact has missing/duplicate date: {path} {day!r}")
            values[day] = _float(row.get("equity"), "daily.equity", path)
    if not values:
        raise ValueError(f"Daily artifact is empty: {path}")
    return values


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_var = sum((a - left_mean) ** 2 for a in left)
    right_var = sum((b - right_mean) ** 2 for b in right)
    if left_var <= 0 or right_var <= 0:
        return None
    return numerator / (left_var ** 0.5 * right_var ** 0.5)


def _validate_run(result_path: Path) -> dict[str, Any]:
    record = _read_json(result_path)
    if record.get("status") != REQUIRED_STATUS:
        raise ValueError(
            f"Only {REQUIRED_STATUS} is comparable; got {record.get('status')!r} in {result_path}"
        )
    for flag in FORBIDDEN_TRUE_FLAGS:
        if record.get(flag) is True:
            raise ValueError(f"Research-only gate {flag}=true in {result_path}")
    metrics = record.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError(f"Replay metrics missing in {result_path}")
    for key in ("net_return", "max_drawdown", "sharpe_zero_rf_252", "fees", "turnover_value"):
        _float(metrics.get(key), f"metrics.{key}", result_path)
    for key in ("start_date", "end_date", "replay_sessions"):
        if key not in record:
            raise ValueError(f"Replay field {key!r} missing in {result_path}")
    return record


def _run_summary(result_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    root = result_path.parent
    metrics = record["metrics"]
    daily = _daily_equity(root / "daily.csv")
    return {
        "result": str(result_path),
        "strategy_id": record.get("strategy_id"),
        "variant_id": record.get("variant_id"),
        "horizon_sessions": record.get("horizon_sessions"),
        "start_date": record["start_date"],
        "end_date": record["end_date"],
        "sessions": int(record["replay_sessions"]),
        "symbols": int(record.get("symbols", 0)),
        "selected_event_rows": int(record.get("selected_event_rows", 0)),
        "blocked_order_events": int(record.get("blocked_order_event_count", 0)),
        "orders": _csv_count(root / "orders.csv"),
        "fills": _csv_count(root / "fills.csv"),
        "metrics": {
            "net_return": _float(metrics["net_return"], "metrics.net_return", result_path),
            "max_drawdown": _float(metrics["max_drawdown"], "metrics.max_drawdown", result_path),
            "sharpe_zero_rf_252": _float(metrics["sharpe_zero_rf_252"], "metrics.sharpe_zero_rf_252", result_path),
            "fees": _float(metrics["fees"], "metrics.fees", result_path),
            "turnover_value": _float(metrics["turnover_value"], "metrics.turnover_value", result_path),
            "stale_valuation_days": int(metrics.get("stale_valuation_days", 0)),
        },
        "daily_equity": daily,
    }


def compare_replays(left: str | Path, right: str | Path) -> dict[str, Any]:
    """Return a fixed-window, read-only comparison of two saved replays."""
    left_path, right_path = _resolve_result(left), _resolve_result(right)
    if left_path == right_path:
        raise ValueError("Comparison requires two distinct replay results")
    left_summary = _run_summary(left_path, _validate_run(left_path))
    right_summary = _run_summary(right_path, _validate_run(right_path))
    common = {
        "start_date": left_summary["start_date"],
        "end_date": left_summary["end_date"],
        "sessions": left_summary["sessions"],
    }
    for field in ("start_date", "end_date", "sessions"):
        if left_summary[field] != right_summary[field]:
            raise ValueError(f"Replay windows differ at {field}: {left_summary[field]!r} vs {right_summary[field]!r}")
    days = sorted(set(left_summary["daily_equity"]) & set(right_summary["daily_equity"]))
    if len(days) != common["sessions"]:
        raise ValueError(f"Daily equity overlap {len(days)} does not equal replay sessions {common['sessions']}")
    left_values = [left_summary["daily_equity"][day] for day in days]
    right_values = [right_summary["daily_equity"][day] for day in days]
    equity_gaps = [right_value - left_value for left_value, right_value in zip(left_values, right_values)]
    left_metrics, right_metrics = left_summary["metrics"], right_summary["metrics"]
    delta = {key: right_metrics[key] - left_metrics[key] for key in (
        "net_return", "max_drawdown", "sharpe_zero_rf_252", "fees", "turnover_value"
    )}
    return {
        "schema": "mature_factor_horizon_comparison_v1",
        "status": "COMPLETED_RESEARCH_ONLY_HORIZON_COMPARISON",
        "machine_status": {
            "execution_admitted": False,
            "paper_allowed": False,
            "promotion_allowed": False,
            "live_execution_ready": False,
            "decision": "NO_ADOPTION_RESEARCH_ONLY",
        },
        "comparison": {"baseline": left_summary, "variant": right_summary, "common_window": common},
        "aligned_daily": {
            "days": len(days),
            "equity_correlation": _correlation(left_values, right_values),
            "final_equity_delta": equity_gaps[-1],
            "max_absolute_equity_gap": max(abs(gap) for gap in equity_gaps),
            "variant_minus_baseline_equity_gap": equity_gaps,
        },
        "metric_delta_variant_minus_baseline": delta,
        "interpretation": {
            "baseline": "20-session mature-factor Ridge replay",
            "variant": "predeclared 60-session mature-factor Ridge replay",
            "selection": "Neither run is selected from this comparison; both remain development diagnostics.",
            "evidence_boundary": "Shared historical window and Engine/Ledger path; no PIT-complete or untouched holdout evidence.",
        },
    }


def write_artifacts(comparison: dict[str, Any], receipt_path: str | Path, report_path: str | Path) -> None:
    receipt = Path(receipt_path).resolve()
    report = Path(report_path).resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    baseline = comparison["comparison"]["baseline"]
    variant = comparison["comparison"]["variant"]
    delta = comparison["metric_delta_variant_minus_baseline"]
    lines = [
        "# Mature Factor Horizon Comparison",
        "",
        "Research-only read of two completed Engine/Ledger replays; `NO_TRADE`.",
        "",
        f"- Baseline: `{baseline['result']}` ({baseline['strategy_id']}, 20-session labels).",
        f"- Variant: `{variant['result']}` ({variant['variant_id'] or variant['strategy_id']}, 60-session labels).",
        f"- Common window: `{baseline['start_date']}` to `{baseline['end_date']}` ({baseline['sessions']} sessions).",
        "",
        "| metric | baseline | variant | variant - baseline |",
        "|---|---:|---:|---:|",
        f"| net return | {baseline['metrics']['net_return']:.4%} | {variant['metrics']['net_return']:.4%} | {delta['net_return']:+.4%} |",
        f"| max drawdown | {baseline['metrics']['max_drawdown']:.4%} | {variant['metrics']['max_drawdown']:.4%} | {delta['max_drawdown']:+.4%} |",
        f"| Sharpe (0% rf) | {baseline['metrics']['sharpe_zero_rf_252']:.4f} | {variant['metrics']['sharpe_zero_rf_252']:.4f} | {delta['sharpe_zero_rf_252']:+.4f} |",
        f"| fees (CNY) | {baseline['metrics']['fees']:.2f} | {variant['metrics']['fees']:.2f} | {delta['fees']:+.2f} |",
        f"| turnover (CNY) | {baseline['metrics']['turnover_value']:.2f} | {variant['metrics']['turnover_value']:.2f} | {delta['turnover_value']:+.2f} |",
        f"| orders / fills | {baseline['orders']} / {baseline['fills']} | {variant['orders']} / {variant['fills']} | — |",
        "",
        f"Aligned daily equity correlation: `{comparison['aligned_daily']['equity_correlation']:.6f}`.",
        f"Variant final equity minus baseline: `{comparison['aligned_daily']['final_equity_delta']:+.2f}` CNY.",
        f"Maximum absolute aligned equity gap: `{comparison['aligned_daily']['max_absolute_equity_gap']:.2f}` CNY.",
        "",
        "## Decision boundary",
        "",
        "This comparison does not choose a winner or promote a candidate. Both runs share a historical development window; the source StockDB labels and publication/revision timing are not PIT-complete, and no untouched post-cutoff holdout was used. `execution_admitted=false`, `paper_allowed=false`, and `promotion_allowed=false` remain explicit.",
        "",
        "The 60-session variant produced a lower net return and lower fees in this window. That is a bounded observation, not an independent alpha conclusion or a live-trading recommendation.",
    ]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="20-session replay directory or result.json")
    parser.add_argument("--variant", required=True, help="60-session replay directory or result.json")
    parser.add_argument("--receipt-out", required=True, type=Path)
    parser.add_argument("--report-out", required=True, type=Path)
    args = parser.parse_args(argv)
    comparison = compare_replays(args.baseline, args.variant)
    write_artifacts(comparison, args.receipt_out, args.report_out)
    print(json.dumps({"status": comparison["status"], "receipt": str(args.receipt_out.resolve()), "report": str(args.report_out.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
