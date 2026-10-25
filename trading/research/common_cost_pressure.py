"""Apply deterministic extra turnover-cost pressure to a common-window receipt.

This is a derived research diagnostic. It reads the saved daily account rows
already bound by ``common_window_strategy_comparison`` and subtracts an
additional basis-point cost from turnover. It never reruns a strategy, writes
the ledger, or changes candidate adoption permissions.
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


SCHEMA = "quantification_common_window_cost_pressure_v1"
SOURCE_RECEIPT = ROOT / "outputs/pipeline_receipts/20261026_common_window_strategy_comparison_v1.json"
RECEIPT_NAME = "20261026_common_window_cost_pressure_v1.json"
REPORT_NAME = "20261026_common_window_cost_pressure_v1.md"
DEFAULT_BPS = (50.0, 100.0)


def _day(value: str) -> date:
    return date.fromisoformat(value[:10])


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _metrics(rows: Iterable[dict[str, str]], dates: set[date], bps: float) -> dict[str, float | int | None]:
    selected = [row for row in rows if _day(row["date"]) in dates]
    if not selected:
        raise ValueError("No daily rows in the requested common window")
    equities: list[float] = []
    extra_cost = 0.0
    for row in selected:
        extra_cost += float(row.get("turnover_value") or 0.0) * bps / 10_000.0
        equities.append(float(row["equity"]) - extra_cost)
    returns = [equities[index] / equities[index - 1] - 1.0 for index in range(1, len(equities))]
    peak = equities[0]
    drawdowns: list[float] = []
    for value in equities:
        peak = max(peak, value)
        drawdowns.append(value / peak - 1.0)
    volatility = 0.0
    if len(returns) > 1:
        average = mean(returns)
        volatility = sqrt(sum((value - average) ** 2 for value in returns) / (len(returns) - 1))
    sharpe = None if volatility == 0.0 else mean(returns) / volatility * sqrt(252.0)
    return {
        "sessions": len(selected),
        "extra_cost_rmb": extra_cost,
        "window_start_equity": equities[0],
        "window_end_equity": equities[-1],
        "net_return": equities[-1] / equities[0] - 1.0,
        "max_drawdown": min(drawdowns),
        "sharpe_zero_rf_252": sharpe,
    }


def build_receipt(source: Mapping[str, Any], bps_levels: Iterable[float] = DEFAULT_BPS) -> dict[str, Any]:
    window = source.get("common_window")
    if not isinstance(window, Mapping):
        raise ValueError("common-window receipt has no common_window")
    start = _day(str(window["start"]))
    end = _day(str(window["end"]))
    bps = tuple(float(value) for value in bps_levels)
    if not bps or any(value <= 0 for value in bps):
        raise ValueError("bps_levels must contain positive values")
    loaded: list[tuple[Mapping[str, Any], list[dict[str, str]]]] = []
    for entry in source.get("strategies", []):
        paths = entry.get("source_paths") or {}
        daily_path = Path(str(paths.get("daily", "")))
        if not daily_path.is_file():
            raise FileNotFoundError(f"Missing saved daily account artifact: {daily_path}")
        loaded.append((entry, _read_rows(daily_path)))
    date_sets = [
        {_day(row["date"]) for row in rows if start <= _day(row["date"]) <= end}
        for _, rows in loaded
    ]
    common_dates = set.intersection(*date_sets) if date_sets else set()
    if not common_dates or len(common_dates) != int(window.get("sessions", len(common_dates))):
        raise ValueError("Saved daily rows do not reproduce the common-window session intersection")

    strategies: list[dict[str, Any]] = []
    for entry, rows in loaded:
        dates = common_dates
        pressure = []
        for level in bps:
            metrics = _metrics(rows, dates, level)
            metrics["bps"] = level
            pressure.append(metrics)
        base = entry.get("metrics") or {}
        strategies.append({
            "id": entry.get("id"),
            "label": entry.get("label"),
            "base_net_return": float(base.get("net_return", 0.0)),
            "base_fees_rmb": float(base.get("fees_rmb", 0.0)),
            "base_window_turnover_value": float(base.get("window_turnover_value", 0.0)),
            "pressure": pressure,
            "research_decision_unchanged": entry.get("research_decision"),
        })
    return {
        "schema": SCHEMA,
        "status": "COMPLETED_RESEARCH_ONLY_COST_PRESSURE",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_receipt": str(SOURCE_RECEIPT),
        "common_window": dict(window),
        "stockdb_snapshot": source.get("stockdb_snapshot"),
        "bps_levels": list(bps),
        "strategies": strategies,
        "contract": {
            "same_saved_account_rows": True,
            "same_stockdb_snapshot": True,
            "extra_cost_formula": "turnover_value_rmb * bps / 10000",
            "base_fees_preserved": True,
            "strategy_rerun": False,
            "ledger_mutated": False,
            "adoption_decision_changed": False,
        },
        "execution_admitted": False,
        "broker_or_ths_interacted": False,
        "limitations": [
            "This is a derived turnover-cost sensitivity over saved account rows; no strategy was rerun.",
            "The pressure result remains research-only and does not establish PIT, forward validation or adoption.",
        ],
    }


def render_report(receipt: Mapping[str, Any]) -> str:
    lines = [
        "# Common-window extra cost pressure",
        "",
        f"Window: `{receipt['common_window']['start']}..{receipt['common_window']['end']}` ({receipt['common_window']['sessions']} sessions).",
        "",
        "Extra costs are applied to saved daily turnover using the same account rows; no strategy was rerun.",
        "",
        "| Strategy | Base return | +50bp return | +100bp return | +50bp extra cost | +100bp extra cost | Decision unchanged |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for strategy in receipt["strategies"]:
        pressure = {float(row["bps"]): row for row in strategy["pressure"]}
        p50, p100 = pressure.get(50.0), pressure.get(100.0)
        lines.append(
            f"| {strategy['label']} (`{strategy['id']}`) | {strategy['base_net_return']:.4%} | "
            f"{p50['net_return']:.4%} | {p100['net_return']:.4%} | {p50['extra_cost_rmb']:.2f} | "
            f"{p100['extra_cost_rmb']:.2f} | {strategy['research_decision_unchanged']} |"
        )
    lines += ["", "The pressure sensitivity does not promote or stop a candidate; it is an economic robustness input for the next research decision.", ""]
    return "\n".join(lines)


def write_outputs(source_path: str | Path = SOURCE_RECEIPT, receipt_path: str | Path | None = None,
                  report_path: str | Path | None = None, bps_levels: Iterable[float] = DEFAULT_BPS) -> tuple[Path, Path]:
    source_file = Path(source_path).expanduser().resolve()
    source = json.loads(source_file.read_text(encoding="utf-8"))
    receipt = build_receipt(source, bps_levels)
    receipt_path = Path(receipt_path or ROOT / "outputs/pipeline_receipts" / RECEIPT_NAME).expanduser().resolve()
    report_path = Path(report_path or ROOT / "outputs/research_reports" / REPORT_NAME).expanduser().resolve()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(receipt), encoding="utf-8")
    return receipt_path, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-receipt", type=Path, default=SOURCE_RECEIPT)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--bps", type=float, nargs="+", default=list(DEFAULT_BPS))
    args = parser.parse_args(argv)
    receipt, report = write_outputs(args.source_receipt, args.receipt, args.report, args.bps)
    print(json.dumps({"receipt": str(receipt), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_receipt", "main", "render_report", "write_outputs"]
