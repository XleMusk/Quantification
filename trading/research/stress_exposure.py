"""Read-only holdings exposure on retrospective stress dates."""
import argparse
import ast
from datetime import datetime
import json
from pathlib import Path
import re
import uuid

import pandas as pd

from .. import settings
from .bulk_feed import BulkResearchFeed
from .experiment import write_json
from .risk_concentration import daily_stress
from .turnover_attribution import action_map, project_orders


LIMITS = [
    "Stress dates are selected retrospectively from the saved account; this is not an untouched test",
    "Exposure uses saved event holdings and raw close marks, not a counterfactual or security-level PnL attribution",
    "Unknown, stale or unmarked holdings remain unknown and are not assigned zero value",
    "Historical research only; no paper/live/broker admission and NO_TRADE",
]


def _plain(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError(f"{label} must be a short plain identifier")
    return value


def stress_dates(daily, limit=10):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    stress = daily_stress(daily)
    candidates = pd.concat([
        stress.dropna(subset=["daily_return"]).nsmallest(limit, "daily_return"),
        stress.nsmallest(limit, "drawdown"),
    ], ignore_index=True)
    return candidates.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def _holdings_asof(calendar, snapshots):
    event_days = sorted(snapshots)
    current = {}
    index = 0
    result = {}
    for day in calendar:
        while index < len(event_days) and event_days[index] <= day:
            current = dict(snapshots[event_days[index]])
            index += 1
        result[day] = dict(current)
    return result


def exposure_rows(daily, snapshots, raw_lookup, limit=10):
    selected = stress_dates(daily, limit)
    calendar = daily["date"].astype(str).tolist()
    asof = _holdings_asof(calendar, snapshots)
    rows = []
    for stress in selected.to_dict("records"):
        day = str(stress["date"])
        raw = raw_lookup(day) or {}
        held = asof.get(day, {})
        stale = ast.literal_eval(stress["stale_symbols"]) if isinstance(stress["stale_symbols"], str) else []
        for symbol, quantity in held.items():
            quote = raw.get(symbol) or {}
            close = quote.get("close")
            known = symbol not in stale and close is not None and pd.notna(close) and float(close) > 0
            value = float(quantity) * float(close) if known else None
            rows.append({
                "date": day,
                "symbol": symbol,
                "quantity": int(quantity),
                "raw_close_rmb": float(close) if known else None,
                "marked_value_cny": value,
                "equity_cny": float(stress["equity"]),
                "known_mark": bool(known),
                "stale_mark": symbol in stale,
                "daily_return": float(stress["daily_return"]) if pd.notna(stress["daily_return"]) else None,
                "drawdown": float(stress["drawdown"]),
            })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["exposure_weight"] = frame["marked_value_cny"] / frame["equity_cny"]
    frame["date_rank"] = frame.groupby("date")["marked_value_cny"].rank(method="first", ascending=False, na_option="bottom")
    return frame.sort_values(["date", "known_mark", "marked_value_cny", "symbol"],
                             ascending=[True, False, False, True], na_position="last").reset_index(drop=True)


def summarize(frame, selected):
    if frame.empty:
        raise ValueError("No saved holdings were available on selected stress dates")
    known = frame.loc[frame.known_mark]
    by_date = frame.groupby("date", as_index=False).agg(
        positions=("symbol", "size"),
        unknown_positions=("known_mark", lambda values: int((~values).sum())),
        known_marked_value_cny=("marked_value_cny", "sum"),
        equity_cny=("equity_cny", "first"),
        daily_return=("daily_return", "first"),
        drawdown=("drawdown", "first"),
    )
    by_date["known_exposure_weight"] = by_date.known_marked_value_cny / by_date.equity_cny
    exposure = (known.groupby("symbol", as_index=False)
                .agg(stress_days=("date", "nunique"), total_marked_value_cny=("marked_value_cny", "sum"),
                     mean_exposure_weight=("exposure_weight", "mean"), max_exposure_weight=("exposure_weight", "max")))
    exposure = exposure.sort_values(["mean_exposure_weight", "symbol"], ascending=[False, True]).reset_index(drop=True)
    return by_date, exposure, {
        "status": "completed_saved_stress_exposure",
        "stress_dates": int(len(selected)),
        "exposure_rows": int(len(frame)),
        "known_rows": int(len(known)),
        "unknown_rows": int((~frame.known_mark).sum()),
        "top_symbol_by_mean_known_weight": exposure.iloc[0].symbol if len(exposure) else None,
        "execution_admitted": False,
        "strategy_validated": False,
        "limitations": LIMITS,
    }


def _saved_record(identifier):
    identifier = _plain(identifier, "saved run")
    candidates = [
        settings.ROOT / "outputs/family_pipeline" / identifier / "run.json",
        settings.ROOT / "outputs/factor_candidate_family_replays" / identifier / "run.json",
    ]
    report_path = next((path for path in candidates if path.exists()), None)
    if report_path is None:
        raise FileNotFoundError(f"Saved research run not found: {candidates[0]} or {candidates[1]}")
    return json.loads(report_path.read_text(encoding="utf-8")), report_path


def run(saved_run, signal, *, limit=10):
    saved_run = _plain(saved_run, "saved run")
    signal = _plain(signal, "signal")
    record, report_path = _saved_record(saved_run)
    if record.get("status") not in {"completed_mature_month_pipeline_ledger",
                                     "completed_factor_candidate_family_replay"}:
        raise ValueError("Only a completed unbound saved research account can be consumed")
    fresh_holdout = record.get("protocol", {}).get("fresh_holdout_claimed",
                                                     record.get("fresh_holdout_claimed"))
    if record.get("execution_admitted") is not False or fresh_holdout is not False:
        raise ValueError("Saved run is not an unbound diagnostic without a fresh holdout claim")
    result = next((row for row in record.get("results", []) if row.get("signal") == signal), None)
    if result is None and record.get("result", {}).get("signal") == signal:
        result = record["result"]
    if result is None or result.get("status") != "completed":
        raise ValueError("Requested saved signal is not a completed account")
    account_path = Path(record["artifacts"]) / signal / "account.json"
    daily_path = Path(record["artifacts"]) / signal / "daily.csv"
    if not account_path.exists() or not daily_path.exists():
        raise FileNotFoundError("Saved family account and daily marks are required")
    account = json.loads(account_path.read_text(encoding="utf-8"))
    daily = pd.read_csv(daily_path, dtype={"date": str, "stale_symbols": str})
    manifest = record.get("protocol", {}).get("stock_manifest") or record.get("inputs")
    if not manifest or not all(manifest.get(key) for key in
                               ("price_view", "event_view", "reference_view", "listing_master")):
        raise ValueError("Saved research account has no complete pinned bulk-input manifest")
    feed = BulkResearchFeed(manifest["price_view"], manifest["event_view"], manifest["reference_view"],
                            manifest["listing_master"])
    snapshots = {}
    try:
        _, _, _ = project_orders(account, action_map(record, saved_run), daily_holdings=snapshots)
        rows = exposure_rows(daily, snapshots, lambda day: feed.rows.get(day, {}), limit=limit)
        selected = stress_dates(daily, limit)
        by_date, exposure, summary = summarize(rows, selected)
    finally:
        feed.close()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/stress_exposure" / identifier
    artifacts = settings.DATA_ROOT / "research/stress_exposure" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    rows.to_csv(artifacts / "stress_exposure.csv", index=False)
    by_date.to_csv(artifacts / "stress_days.csv", index=False)
    exposure.to_csv(artifacts / "stress_symbol_summary.csv", index=False)
    summary.update({"id": identifier, "saved_run": saved_run, "signal": signal,
                    "source_run_type": record.get("status"),
                    "source_account": str(account_path), "artifacts": str(artifacts)})
    write_json(artifacts / "summary.json", summary)
    write_json(out / "run.json", summary)
    lines = ["# Saved Account Stress-Date Exposure", "", "Read-only historical exposure diagnostics; NO_TRADE.",
             "Positions are reconstructed from saved ledger events and marked with the pinned raw close source.",
             "Stress dates are selected retrospectively from the same saved account; no account replay or refit was run.", "",
             "| Date | Daily return | Drawdown | Positions | Unknown marks | Known exposure |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in by_date.to_dict("records"):
        lines.append(f"| {row['date']} | {row['daily_return']:.2%} | {row['drawdown']:.2%} | {int(row['positions'])} | "
                     f"{int(row['unknown_positions'])} | {row['known_exposure_weight']:.2%} |")
    lines += ["", "## Highest Mean Known Exposure", "", "| Symbol | Stress days | Mean exposure | Max exposure |", "| --- | ---: | ---: | ---: |"]
    for row in exposure.head(10).to_dict("records"):
        lines.append(f"| {row['symbol']} | {int(row['stress_days'])} | {row['mean_exposure_weight']:.2%} | {row['max_exposure_weight']:.2%} |")
    lines += ["", "## Limits", "", *["- " + item for item in LIMITS], "", f"Artifacts: `{artifacts}`", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(str(out), flush=True)
    return out, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--family-run", help="Saved family-pipeline run id")
    group.add_argument("--saved-run", help="Saved factor-family or family-pipeline run id")
    parser.add_argument("--signal", required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    run(args.saved_run or args.family_run, args.signal, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
