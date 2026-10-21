"""Compare two saved StockDB rolling Engine/Ledger diagnostics.

This is a read-only consumer of immutable replay receipts.  It does not
select, promote, or rerun a strategy.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path


SCHEMA = "stockdb_rolling_segment_compare_v1"
METRIC_FIELDS = (
    "net_return",
    "max_drawdown",
    "sharpe_zero_rf_252",
    "annualized_volatility",
    "worst_day",
    "fees",
    "turnover_value",
    "orders",
    "fills",
    "stale_valuation_days",
    "final_equity",
)


def _load_run(path: Path) -> dict:
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "run.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema") != "stockdb_rolling_low_volatility_ledger_v1":
        raise ValueError(f"Unsupported replay schema: {path}")
    if record.get("research_only") is not True or record.get("execution_admitted") is not False:
        raise ValueError(f"Replay is not explicitly research-only: {path}")
    return record


def _result_map(record: dict) -> dict[str, dict]:
    return {
        str(item["signal"]): item
        for item in record.get("results") or []
        if item.get("status") == "completed" and item.get("signal")
    }


def compare(full_run: Path, segment_run: Path) -> dict:
    full = _load_run(full_run)
    segment = _load_run(segment_run)
    full_results = _result_map(full)
    segment_results = _result_map(segment)
    signals = sorted(set(full_results) & set(segment_results))
    if not signals:
        raise ValueError("The two runs have no common completed signals")

    rows = []
    for signal in signals:
        full_result = full_results[signal]
        segment_result = segment_results[signal]
        row = {
            "signal": signal,
            "full_start": full.get("replay_start"),
            "full_end": full.get("replay_end"),
            "full_sessions": full_result.get("completed_sessions", full.get("sessions")),
            "segment_start": segment.get("replay_start"),
            "segment_end": segment.get("replay_end"),
            "segment_sessions": segment_result.get("completed_sessions", segment.get("sessions")),
        }
        for field in METRIC_FIELDS:
            row[f"full_{field}"] = full_result.get(field)
            row[f"segment_{field}"] = segment_result.get(field)
        if full_result.get("net_return") is not None and segment_result.get("net_return") is not None:
            row["segment_minus_full_net_return"] = (
                float(segment_result["net_return"]) - float(full_result["net_return"])
            )
        else:
            row["segment_minus_full_net_return"] = None
        rows.append(row)

    config_match = full.get("config") == segment.get("config")
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "full_run": str(Path(full_run).resolve()),
        "segment_run": str(Path(segment_run).resolve()),
        "comparison": {
            "signals": signals,
            "signal_count": len(signals),
            "same_strategy_config": config_match,
            "same_rule_different_window": config_match,
            "independent_holdout": False,
            "selection_performed": False,
            "execution_admitted": False,
            "research_only": True,
        },
        "rows": rows,
        "limitations": [
            "The segment is a same-rule diagnostic window, not an untouched independent holdout.",
            "The two windows are not a controlled strategy experiment; market regime and sample length differ.",
            "StockDB point-in-time membership, listing master and corporate actions remain unresolved.",
            "No candidate is promoted and no account or broker state is changed by this consumer.",
        ],
    }


def write_result(result: dict, output_root: Path) -> Path:
    output_root = Path(output_root).resolve()
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"Output is non-empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=False)
    rows = result["rows"]
    with (output_root / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output_root / "comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    report = [
        "# StockDB Rolling Segment Comparison",
        "",
        "Status: `completed_research_only`; execution admitted: `false`.",
        f"Same strategy configuration: `{result['comparison']['same_strategy_config']}`.",
        "Independent holdout: `false`; selection performed: `false`.",
        "",
        "| Signal | Full window | Full net | Full MDD | Segment window | Segment net | Segment MDD | Segment minus full | Orders / fills |",
        "|---|---|---:|---:|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        def pct(key):
            value = row.get(key)
            return "unknown" if value is None else f"{float(value):.4%}"

        report.append(
            f"| {row['signal']} | {row['full_start']}..{row['full_end']} | "
            f"{pct('full_net_return')} | {pct('full_max_drawdown')} | "
            f"{row['segment_start']}..{row['segment_end']} | {pct('segment_net_return')} | "
            f"{pct('segment_max_drawdown')} | {pct('segment_minus_full_net_return')} | "
            f"{row.get('segment_orders', 'unknown')} / {row.get('segment_fills', 'unknown')} |"
        )
    report += ["", "Limitations:"]
    report.extend(f"- {item}" for item in result["limitations"])
    (output_root / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return output_root


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-run", required=True, type=Path)
    parser.add_argument("--segment-run", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = compare(args.full_run, args.segment_run)
    output = write_result(result, args.out)
    print(json.dumps({"out": str(output), "rows": result["rows"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
