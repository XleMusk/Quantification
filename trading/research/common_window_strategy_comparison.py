"""Compare retained strategies on one common saved-account window.

The long-window reports preserve each strategy's native history.  This module
adds the roadmap's next decision slice: read the already completed account
artifacts, intersect their available dates, and calculate the same metrics on
that single overlap.  It never reruns a strategy or mutates a ledger.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import json
from math import sqrt
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

from ..settings import ROOT
from .long_window_strategy_comparison import build_receipt as build_long_window_receipt


SCHEMA = "quantification_common_window_strategy_comparison_v1"
RECEIPT_NAME = "20261026_common_window_strategy_comparison_v1.json"
REPORT_NAME = "20261026_common_window_strategy_comparison_v1.md"
STRATEGY_IDS = (
    "low_vol_half_momentum_20",
    "mature_factor_balanced_ridge",
    "mature_factor_balanced_ridge_h60",
)
FULL_STRATEGY_IDS = (
    "fixed_low_volatility_252_20",
    "low_vol_half_momentum_20",
    "momentum_12_1_20",
    "mature_factor_balanced_ridge",
    "alpha158_lgbm_cap500",
    "mature_factor_balanced_ridge_h60",
)
FULL_SCHEMA = "quantification_full_panel_common_window_strategy_comparison_v1"
FULL_RECEIPT_NAME = "20261027_full_panel_common_window_strategy_comparison_v1.json"
FULL_REPORT_NAME = "20261027_full_panel_common_window_strategy_comparison_v1.md"


def _date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _sum_order_field(path: Path, field: str, dates: set[date]) -> float:
    total = 0.0
    for row in _read_rows(path):
        day = _date(row["day"])
        if day in dates:
            total += float(row.get(field) or 0.0)
    return total


def _count_rows(path: Path, dates: set[date]) -> int:
    return sum(_date(row["day"]) in dates for row in _read_rows(path))


def _daily_metrics(rows: Iterable[dict[str, str]], dates: set[date]) -> dict[str, Any]:
    selected = [row for row in rows if _date(row["date"]) in dates]
    if not selected:
        raise ValueError("No daily account rows in common date intersection")
    equity = [float(row["equity"]) for row in selected]
    returns = [equity[index] / equity[index - 1] - 1.0 for index in range(1, len(equity))]
    running_peak = equity[0]
    drawdowns: list[float] = []
    for value in equity:
        running_peak = max(running_peak, value)
        drawdowns.append(value / running_peak - 1.0)
    volatility = 0.0
    if len(returns) > 1:
        avg = mean(returns)
        volatility = sqrt(sum((value - avg) ** 2 for value in returns) / (len(returns) - 1))
    sharpe = None if volatility == 0.0 else mean(returns) / volatility * sqrt(252.0)
    return {
        "sessions": len(selected),
        "window_start_equity": equity[0],
        "window_end_equity": equity[-1],
        "net_return": equity[-1] / equity[0] - 1.0,
        "max_drawdown": min(drawdowns),
        "sharpe_zero_rf_252": sharpe,
        "window_turnover_value": sum(float(row.get("turnover_value") or 0.0) for row in selected),
    }


def _resolve_entries(strategy_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    source = build_long_window_receipt(include_h60=True)
    by_id = {entry["id"]: entry for entry in source["strategies"]}
    requested = tuple(strategy_ids or STRATEGY_IDS)
    missing = [strategy_id for strategy_id in requested if strategy_id not in by_id]
    if missing:
        raise ValueError(f"Missing retained comparison entries: {missing}")
    snapshots = {by_id[strategy_id]["stockdb_snapshot"] for strategy_id in requested}
    if len(snapshots) != 1:
        raise ValueError(f"Strategies do not share one StockDB snapshot: {sorted(snapshots)}")
    return [by_id[strategy_id] for strategy_id in requested]


def build_receipt(*, strategy_ids: Iterable[str] | None = None,
                  schema: str = SCHEMA,
                  status: str = "COMPLETED_RESEARCH_ONLY_COMMON_WINDOW") -> dict[str, Any]:
    requested = tuple(strategy_ids or STRATEGY_IDS)
    entries = _resolve_entries() if strategy_ids is None else _resolve_entries(requested)
    daily_by_entry: list[tuple[dict[str, Any], list[dict[str, str]]]] = []
    for entry in entries:
        daily_path = Path(entry["source_paths"]["daily"])
        order_path = Path(entry["source_paths"]["orders"])
        fill_path = Path(entry["source_paths"]["fills"])
        if not daily_path.exists() or not order_path.exists() or not fill_path.exists():
            raise FileNotFoundError(f"Missing saved account artifact for {entry['id']}")
        daily_by_entry.append((entry, _read_rows(daily_path)))
    date_sets = [{_date(row["date"]) for row in rows} for _, rows in daily_by_entry]
    common_dates = set.intersection(*date_sets)
    if not common_dates:
        raise ValueError("No common daily account dates")
    common_start = min(common_dates)
    common_end = max(common_dates)

    strategies: list[dict[str, Any]] = []
    for entry, rows in daily_by_entry:
        metrics = _daily_metrics(rows, common_dates)
        metrics["orders"] = _count_rows(Path(entry["source_paths"]["orders"]), common_dates)
        metrics["fills"] = _count_rows(Path(entry["source_paths"]["fills"]), common_dates)
        # Ledger CSV stores fee cents; reports use RMB.
        metrics["fees_rmb"] = _sum_order_field(Path(entry["source_paths"]["orders"]), "fees", common_dates) / 100.0
        strategies.append({
            "id": entry["id"],
            "label": entry["label"],
            "candidate_spec_id": entry["candidate_spec_id"],
            "metrics": metrics,
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
        "schema": schema,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False,
        "strategy_validated": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "paper_allowed": False,
        "promotion_allowed": False,
        "common_window": {
            "start": common_start.isoformat(),
            "end": common_end.isoformat(),
            "sessions": len(common_dates),
            "basis": "intersection_of_saved_daily_account_dates",
        },
        "comparison_contract": {
            "shared_stockdb_snapshot": True,
            "shared_engine_ledger_path": True,
            "initial_cash_rmb": 140000.0,
            "metrics": ["sessions", "orders", "fills", "net_return", "max_drawdown", "sharpe_zero_rf_252", "fees_rmb", "window_turnover_value"],
            "research_decision_rule": "common-window net_return > 0 and Sharpe > 0 means CONTINUE_RESEARCH_ONLY",
            "no_parameter_retuning": True,
            "strategy_ids": list(requested),
        },
        "stockdb_snapshot": entries[0]["stockdb_snapshot"],
        "strategies": strategies,
        "limitations": [
            "The common window is derived from saved completed account artifacts; no strategy was rerun.",
            "The overlap comparison does not establish PIT completeness, forward validation, or adoption permission.",
            "Simulated fills are not broker or Tonghuashun observations; execution_admitted remains false.",
        ],
    }


def build_full_receipt() -> dict[str, Any]:
    """Compare every saved long-window strategy on one shared overlap.

    This is additive evidence. The retained three-strategy receipt stays
    unchanged for existing adoption bindings; the full panel is a separate
    same-snapshot diagnostic covering fixed, momentum, Ridge, Alpha158/LGBM
    and the prespecified Ridge h60 variant.
    """

    return build_receipt(
        strategy_ids=FULL_STRATEGY_IDS,
        schema=FULL_SCHEMA,
        status="COMPLETED_RESEARCH_ONLY_FULL_PANEL_COMMON_WINDOW",
    )


def render_markdown(receipt: dict[str, Any], *, title: str | None = None) -> str:
    window = receipt["common_window"]
    lines = [
        f"# {title or 'Common-window strategy comparison (2026-10-26)'}",
        "",
        f"Common saved-account window: `{window['start']}..{window['end']}` ({window['sessions']} sessions).",
        "",
        "This is a derived comparison over the same frozen StockDB snapshot and shared Engine/Ledger artifacts; it does not rerun strategies or access broker/THS channels.",
        "",
        "| Strategy | Sessions | Orders | Fills | Net return | Max drawdown | Sharpe | Order fees | Decision |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for entry in receipt["strategies"]:
        metrics = entry["metrics"]
        sharpe = "null" if metrics["sharpe_zero_rf_252"] is None else f"{metrics['sharpe_zero_rf_252']:.4f}"
        lines.append(
            f"| {entry['label']} (`{entry['id']}`) | {metrics['sessions']} | {metrics['orders']} | {metrics['fills']} | "
            f"{metrics['net_return']:.4%} | {metrics['max_drawdown']:.4%} | {sharpe} | {metrics['fees_rmb']:.2f} | {entry['research_decision']} |"
        )
    lines += ["", "## Boundaries", ""]
    lines.extend(f"- {item}" for item in receipt["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_outputs(receipt_path: Path | None = None, report_path: Path | None = None) -> tuple[Path, Path]:
    receipt_path = receipt_path or ROOT / "outputs/pipeline_receipts" / RECEIPT_NAME
    report_path = report_path or ROOT / "outputs/research_reports" / REPORT_NAME
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = build_receipt()
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_markdown(receipt), encoding="utf-8")
    return receipt_path, report_path


def write_full_outputs(receipt_path: Path | None = None,
                       report_path: Path | None = None) -> tuple[Path, Path]:
    """Write the additive full-panel common-window evidence."""

    receipt_path = receipt_path or ROOT / "outputs/pipeline_receipts" / FULL_RECEIPT_NAME
    report_path = report_path or ROOT / "outputs/research_reports" / FULL_REPORT_NAME
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = build_full_receipt()
    receipt["receipt_path"] = str(receipt_path.resolve())
    receipt["report_path"] = str(report_path.resolve())
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(
        render_markdown(receipt, title="Full-panel common-window strategy comparison (2026-10-27)"), encoding="utf-8")
    return receipt_path, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--full-panel", action="store_true",
                        help="write the additive six-strategy common-window panel")
    args = parser.parse_args(argv)
    if args.full_panel:
        receipt_path, report_path = write_full_outputs(args.receipt, args.report)
    else:
        receipt_path, report_path = write_outputs(args.receipt, args.report)
    print(json.dumps({"receipt": str(receipt_path), "report": str(report_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
