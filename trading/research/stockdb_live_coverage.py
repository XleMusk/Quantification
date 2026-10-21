"""Verify current StockDB service coverage without changing any snapshot.

This is intentionally smaller than a market-data collector: it asks the
already-running local StockDB service for a caller-selected symbol/date sample
and writes a receipt that downstream freshness gates can consume.  It never
fills missing bars, promotes the service into HistoricalFeed, or admits an
order.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from ..settings import DATA_ROOT, ROOT
from ..stockdb_source import ENDPOINT, source_bar


SCHEMA = "stockdb_live_coverage_receipt_v1"
DEFAULT_SYMBOLS = (
    "000001.SZSE",
    "600519.SSE",
    "300750.SZSE",
    "601318.SSE",
    "002594.SZSE",
)


def _normal_symbol(value: str) -> str:
    value = str(value).strip().upper()
    if "." not in value and len(value) == 6 and value.isdigit():
        value += ".SSE" if value.startswith("6") else ".SZSE"
    return value


def _normal_day(value: str) -> str:
    return datetime.strptime(str(value).replace("-", ""), "%Y%m%d").date().isoformat()


def verify(symbols: list[str], days: list[str], *, out: Path | None = None) -> tuple[Path, dict]:
    symbols = sorted({_normal_symbol(symbol) for symbol in symbols})
    days = sorted({_normal_day(day) for day in days})
    if not symbols or not days:
        raise ValueError("At least one symbol and date are required")

    observations = []
    errors = []
    service_unavailable = False
    for symbol in symbols:
        for day in days:
            if service_unavailable:
                observations.append({"symbol": symbol, "date": day, "available": False,
                                     "fields": None, "query_attempted": False,
                                     "not_attempted_reason": "service_unavailable_after_network_failure"})
                continue
            try:
                bar = source_bar(symbol, day)
                observations.append({
                    "symbol": symbol,
                    "date": day,
                    "available": bar is not None,
                    "query_attempted": True,
                    "fields": ({key: bar.get(key) for key in
                                ("date", "code", "close", "pct_chg", "volume", "amount")}
                               if bar is not None else None),
                })
            except Exception as exc:  # preserve the concrete service failure in the receipt
                message = f"{type(exc).__name__}: {exc}"
                errors.append({"symbol": symbol, "date": day, "error": message})
                observations.append({"symbol": symbol, "date": day,
                                     "available": False, "fields": None, "error": message,
                                     "query_attempted": True})
                # A refused connection/timeout concerns the shared endpoint,
                # not a missing quote. Do not fan it out into invented absences.
                service_unavailable = isinstance(exc, OSError)

    latest_day = days[-1]
    latest_rows = [row for row in observations if row["date"] == latest_day]
    if errors:
        status = "service_error"
    elif latest_rows and all(row["available"] for row in latest_rows):
        status = "current_day_observed"
    elif any(row["available"] for row in latest_rows):
        status = "partial_current_day_observed"
    elif any(row["available"] for row in observations):
        status = "stale_current_day"
    else:
        status = "no_requested_coverage"

    record = {
        "schema": SCHEMA,
        "status": status,
        "observed_at": datetime.now().astimezone().isoformat(),
        "endpoint": ENDPOINT,
        "symbols": symbols,
        "dates": days,
        "latest_requested_date": latest_day,
        "observations": observations,
        "errors": errors,
        "attempted_queries": sum(row["query_attempted"] for row in observations),
        "not_attempted_queries": sum(not row["query_attempted"] for row in observations),
        "latest_requested_coverage": "unknown_service_error" if errors else "sample_observations_only",
        "research_only": True,
        "execution_admitted": False,
        "imputation_performed": False,
        "snapshot_modified": False,
        "historical_feed_promoted": False,
        "next_action": (
            "allow a downstream current-day research refresh only after the full requested sample is observed"
            if status == "current_day_observed"
            else "keep current-day refresh blocked and wait for a complete closed-session batch"
        ),
    }

    if out is None:
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = ROOT / "outputs" / "stockdb_live_coverage" / identifier
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    path = out / "receipt.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = [
        "# StockDB live coverage receipt",
        "",
        f"- Status: `{status}`",
        f"- Endpoint: `{ENDPOINT}`",
        f"- Latest requested date: `{latest_day}`",
        f"- Available rows: `{sum(row['available'] for row in observations)}/{len(observations)}`",
        f"- Errors: `{len(errors)}`",
        f"- Queries attempted / skipped: `{record['attempted_queries']}/{record['not_attempted_queries']}`",
        "- No bar was imputed, no snapshot was changed, and no execution admission was made.",
        "",
        f"Receipt: `{path}`",
    ]
    (out / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return path, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", dest="symbols",
                        help="SSE/SZSE symbol; repeat for multiple symbols")
    parser.add_argument("--date", action="append", dest="days",
                        help="YYYY-MM-DD or YYYYMMDD; repeat for multiple dates")
    parser.add_argument("--out", type=Path,
                        help="Output directory for receipt.json and REPORT.md")
    args = parser.parse_args(argv)
    path, record = verify(args.symbols or list(DEFAULT_SYMBOLS),
                          args.days or [datetime.now().date().isoformat()], out=args.out)
    print(json.dumps({"receipt": str(path), "status": record["status"],
                      "available_rows": sum(row["available"] for row in record["observations"]),
                      "total_rows": len(record["observations"]),
                      "execution_admitted": False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
