"""Use the saved public-seat database and full stock snapshot in the existing trend ledger."""
import argparse
from contextlib import closing
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import SignalFrame, TradingMode
from ..settings import DATA_ROOT, ROOT
from ..worker import lock
from .bulk_feed import BulkResearchFeed
from .bulk_trend import BulkTrendFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .market_replay import validate_prefix_inputs
from .seat_dataset import DEFAULT_DATASET
from .seat_trend import MODES, event_signals, select_seats
from .seats import derived_seat_entity


ID = "full_source_public_seat_donchian_v1"
FIT = ("2024-09-26", "2025-09-25")
EVALUATION = ("2025-09-26", "2026-09-24")
LAGS = (1, 2)
THRESHOLD = .01
SOURCE = "https://github.com/vnpy/vnpy_ctastrategy/blob/main/vnpy_ctastrategy/strategies/turtle_signal_strategy.py"
LIMITS = [
    "Source-covered supported SH/SZ equities, not independent all-A/BSE/ETF coverage or historical PIT membership",
    "Public seat/stock disclosures are not any person's full trades, holdings or private account statement",
    "Exact seat names selected by earlier stock-day frequency only; no celebrity identity or return-based selection",
    "Publication timestamps unknown: one/two full-session availability assumptions, not verified historical publication",
    "Missing disclosures never imply no trading; multi-day, generic or conflicting seat amounts do not enter H1/H4",
    "Known-source dates and units are checked against pinned raw turnover; this is not an independent exchange census",
    "Existing long-only close-confirmed Donchian20/10 and ATR20 rank, not exact vn.py Turtle stops/shorts/pyramiding",
    "140000CNY synthetic starting capital per hypothesis; sole Engine/Ledger, no client account or actual broker fill",
    "Inspected historical development period, no untouched holdout, PIT admission, causal seat alpha or profit guarantee",
    "Full target updates keep existing daily resizing/costs; unresolved held actions stop the affected account",
]


def portfolio():
    return replace(StrategyConfig(), rebalance_sessions=1, retain_rank=100, initial_cash=140000).validate()


def load_disclosures(database, symbols, start, end):
    """Read-only aggregation keeps event versions and does not add buy/sell listings twice."""
    query = """SELECT event_key,trade_date,symbol,seat_name,seat_code,window_kind,
        MIN(buy_cny) buy_cny,MIN(sell_cny) sell_cny,MIN(net_cny) net_cny,
        MIN(period_turnover_cny) period_turnover_cny,
        MIN(identity_usable) identity_usable,MIN(amount_valid) amount_valid,
        COUNT(*) raw_listings,MIN(retrieved_at) first_retrieved_at,
        COUNT(DISTINCT buy_cny) buy_versions,COUNT(DISTINCT sell_cny) sell_versions,
        COUNT(DISTINCT period_turnover_cny) turnover_versions
        FROM seat_observations WHERE trade_date BETWEEN ? AND ? GROUP BY event_key"""
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        events = pd.read_sql_query(query, db, params=(start, end))
        summaries = pd.read_sql_query("""SELECT DISTINCT trade_date,symbol,window_kind
            FROM stock_disclosures WHERE trade_date BETWEEN ? AND ?""", db, params=(start, end))
    original_rows = int(events.raw_listings.sum())
    events = events.loc[events.symbol.isin(symbols)].copy()
    summaries = summaries.loc[summaries.symbol.isin(symbols)].copy()
    entities = [derived_seat_entity(row.seat_name, row.identity_usable) for row in events.itertuples()]
    events["entity_kind"] = [row["entity_kind"] for row in entities]
    events["seat_identity_usable"] = [row["seat_identity_usable"] for row in entities]
    events["reported_identity_usable"] = events["identity_usable"]
    events["identity_usable"] = events["seat_identity_usable"]
    amount_columns = ["buy_cny", "sell_cny", "net_cny", "period_turnover_cny"]
    events[amount_columns] = events[amount_columns].apply(pd.to_numeric, errors="coerce")
    amounts = events[["buy_cny", "sell_cny", "net_cny", "period_turnover_cny"]]
    valid = (events.seat_identity_usable.eq(True) & events.amount_valid.eq(1) & events.window_kind.eq("single")
             & np.isfinite(amounts).all(axis=1) & events.period_turnover_cny.gt(0)
             & events.buy_cny.ge(0) & events.sell_cny.ge(0)
             & (events.buy_cny - events.sell_cny - events.net_cny).abs().le(.02)
             & amounts[["buy_cny", "sell_cny"]].max(axis=1).le(events.period_turnover_cny * 1.001)
             & events[["buy_versions", "sell_versions", "turnover_versions"]].eq(1).all(axis=1))
    keys = ["trade_date", "symbol", "seat_name", "seat_code"]
    versions = events.loc[valid].groupby(keys).event_key.nunique()
    conflict_keys = set(versions.loc[versions.gt(1)].index)
    events["amount_conflict"] = [tuple(row) in conflict_keys for row in events[keys].to_numpy()]
    events["eligible"] = valid & ~events.amount_conflict
    events = events.sort_values(["trade_date", "symbol", "seat_name", "event_key"], kind="stable")
    coverage = {"raw_listings_in_requested_interval": original_rows, "in_scope_event_versions": len(events),
                "in_scope_raw_listings": int(events.raw_listings.sum()), "in_scope_summary_versions": len(summaries),
                "eligible_single_events": int(events.eligible.sum()), "conflicting_event_versions": int(events.amount_conflict.sum()),
                "investor_category_aggregate_events_excluded": int(events.entity_kind.eq("investor_category_aggregate").sum()),
                "window_counts": events.window_kind.value_counts().to_dict(),
                "event_symbols": int(events.symbol.nunique()), "published_at_known": False}
    return events, summaries, coverage


def turnover_checks(events, seats, base):
    selected = events.loc[events.eligible & events.seat_name.isin(seats)]
    selected = selected.drop_duplicates(["trade_date", "symbol", "period_turnover_cny"])
    parts = []
    for day, group in selected.groupby("trade_date", sort=True):
        rows = base.rows.get(day, {})
        for event in group.itertuples():
            quote = rows.get(event.symbol)
            amount = quote.get("amount") if quote else None
            error = (event.period_turnover_cny / amount - 1
                     if amount is not None and np.isfinite(amount) and amount > 0 else None)
            parts.append({"date": day, "symbol": event.symbol, "disclosed_cny": event.period_turnover_cny,
                          "raw_cny": amount, "relative_error": error,
                          "status": "unknown_raw_turnover" if error is None else
                                    "matched_within_one_percent" if abs(error) <= .01 else "turnover_conflict"})
    checks = pd.DataFrame(parts, columns=["date", "symbol", "disclosed_cny", "raw_cny", "relative_error", "status"])
    bad = checks.loc[checks.status.ne("matched_within_one_percent")]
    return checks, None if len(checks) and not len(bad) else (
        "Selected-seat turnover not fully reconciled: " + str(bad.status.value_counts().to_dict())
        if len(checks) else "No selected single-day seat turnovers to reconcile")


class BulkSeatFeed:
    def __init__(self, trend, signals, mode, config):
        if mode not in MODES or config != portfolio():
            raise ValueError("Original fixed full-source trend portfolio and declared seat mode required")
        self.base, self.signals, self.mode, self.config = trend, signals, mode, config
        self.symbol_index = {symbol: i for i, symbol in enumerate(trend.symbols)}
        self.confirmed, self.vetoed, self.cursor = set(), set(), -1
        self.audit, self.rankings = [], []
        self.by_day = {}
        for (day, symbol), event in signals.items():
            if day not in trend.day_index or symbol not in self.symbol_index:
                raise ValueError("Delayed event identity outside the pinned trend inputs")
            self.by_day.setdefault(day, []).append((symbol, event))

    def __getattr__(self, key):
        return getattr(self.base, key)

    def scores(self, day, config):
        if config != self.config:
            raise ValueError("Seat comparison cannot change portfolio/cost configuration")
        position = self.base.day_index[day]
        if position < self.cursor:
            raise ValueError("Seat state cannot be consumed backwards")
        for index in range(self.cursor + 1, position + 1):
            for symbol, event in self.by_day.get(self.base.calendar[index], []):
                col = self.symbol_index[symbol]
                if self.base.trend["trend_active"][index, col]:
                    key = (symbol, int(self.base.trend["regime"][index, col]))
                    if event.get("buy") or (self.mode == "TREND_ANY_LHB" and event.get("lhb")):
                        self.confirmed.add(key)
                    if event.get("sell"):
                        self.vetoed.add(key)
        self.cursor = position
        candidates = self.base.scores(day, config)
        selected = []
        for row in candidates:
            key = (row["symbol"], int(self.base.trend["regime"][position, self.symbol_index[row["symbol"]]]))
            confirmed = self.mode not in {"TREND_H1", "TREND_H1_H4", "TREND_ANY_LHB"} or key in self.confirmed
            allowed = self.mode not in {"TREND_H4", "TREND_H1_H4"} or key not in self.vetoed
            if confirmed and allowed:
                selected.append(row)
        self.audit.append({"date": day, "mode": self.mode, "trend_candidates": len(candidates),
                           "seat_filtered_candidates": len(selected), "delayed_events": len(self.by_day.get(day, []))})
        self.rankings.extend({"date": day, "rank": rank, **row} for rank, row in enumerate(selected[:config.retain_rank], 1))
        return selected

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Archived seat hypotheses are historical replay only")
        return SignalFrame.from_legacy(self.scores(day, config), day, "research:" + ID + ":" + self.mode, context)


def report(out, record):
    lines = ["# Full-Source Public Seat / Trend Comparison", "", "NO_TRADE. " + record["status"],
             f"Fit: {FIT[0]}..{FIT[1]}; evaluation: {EVALUATION[0]}..{EVALUATION[1]}.",
             "Unchanged earlier seat rules and trend windows; no parameter or seat-return search.",
             "|Case|Assumed publication lag|Status|Saved sessions|Net return|MDD magnitude|Sharpe zero-rf252|Fees CNY|",
             "|---|---:|---|---:|---:|---:|---:|---:|"]
    for case in record["results"]:
        result = case
        if result["status"] == "completed":
            sharpe = result["sharpe_zero_rf_252"]
            lines.append(f"|{case['signal']}|{case['lag']}|completed|{result['completed_sessions']}|"
                         f"{result['net_return']:.6%}|{abs(result['max_drawdown']):.6%}|{sharpe}|{result['fees']:.2f}|")
        else:
            lines.append(f"|{case['signal']}|{case['lag']}|{result['status']}|{result.get('completed_sessions', 0)}|unknown|unknown|unknown|unknown|")
            lines.append("Failure: " + result["error"])
    if record.get("error"):
        lines += ["", record["error"]]
    lines += ["", "Selected exact seat names: " + json.dumps(record.get("selected_seats", []), ensure_ascii=False),
              "", "Coverage: " + json.dumps(record.get("coverage", {}), ensure_ascii=False),
              "", *["- " + value for value in LIMITS], "", "Rule reference: " + SOURCE,
              "Artifacts: " + record["artifacts"]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def resume_check(previous, protocol, manifest, dates, config):
    if (previous.get("research_family") != ID
            or previous.get("status") not in {"completed_seat_bulk_diagnostic", "partial_seat_bulk_diagnostic"}
            or previous.get("execution_admitted") is not False
            or len(previous["results"]) != len(protocol["cases"])
            or any(previous["protocol"].get(key) != value for key, value in protocol.items() if key != "stock_inputs")
            or [{key: row[key] for key in ("signal", "mode", "lag")} for row in previous["results"]] != protocol["cases"]):
        raise ValueError("Seat continuation requires the same terminal hypotheses, disclosure snapshot, dates and costs")
    validate_prefix_inputs(previous["stock_manifest"], manifest, None)
    checkpoints, unchanged = {}, {}
    for case in previous["results"]:
        folder = Path(previous["artifacts"]) / case["signal"]
        if case["status"] == "failed_preparation" and not folder.exists():
            checkpoints[case["signal"]] = None
            continue
        checkpoint = load_replay_checkpoint(folder, config, dates, previous["protocol"]["portfolio"])
        state = checkpoint["state"]
        first_use = {}
        # This fixed research family starts with cash and no positions, via
        # baseline_ledger_result. Never enable this exception for broker imports.
        for row in [*state["orders"].values(), *state["fills"].values(), *state["events"]]:
            involved = set(row.get("targets", {}))
            if row.get("symbol"):
                involved.add(row["symbol"])
            if involved:
                day = row["day"]
                for symbol in involved:
                    first_use[symbol] = min(day, first_use.get(symbol, day))
        ignored = validate_prefix_inputs(previous["stock_manifest"], manifest, state["day"], state,
                                         flat_start_first_use=first_use)
        checkpoints[case["signal"]] = checkpoint
        unchanged[case["signal"]] = ignored
    return checkpoints, unchanged


def append_signal_observations(feed, destination, checkpoint):
    old = Path(checkpoint["source"]) if checkpoint is not None else None
    for filename, rows, parquet in (("candidate_counts.csv", feed.audit, False), ("rankings.parquet", feed.rankings, True)):
        frame = pd.DataFrame(rows)
        if old is not None and (old / filename).exists():
            prior = pd.read_parquet(old / filename) if parquet else pd.read_csv(old / filename, dtype={"date": str})
            frame = pd.concat([prior, frame], ignore_index=True)
        if parquet:
            frame.to_parquet(destination / filename, index=False)
        else:
            frame.to_csv(destination / filename, index=False)


def run(source_run, dataset=DEFAULT_DATASET, *, resume_run=None, event_view=None, reference_view=None,
        resume_cases=(), fresh_input_override=False):
    source_run, dataset = Path(source_run).resolve(), Path(dataset).resolve()
    source = json.loads(source_run.read_text(encoding="utf-8"))
    status = json.loads((dataset / "status.json").read_text(encoding="utf-8"))
    plan = json.loads((dataset / "plan.json").read_text(encoding="utf-8"))
    if (source.get("execution_admitted") is not False or source.get("status", "").startswith("running")
            or status["status"] != "source_snapshot_complete"
            or status["raw_rows_downloaded"] != plan["expected_raw_rows"]
            or plan["requested_start"] > FIT[0] or plan["end"] < EVALUATION[1]):
        raise ValueError("Terminal research market source and complete covering public-seat snapshot required")
    inputs = {key: source["protocol"]["stock_inputs"][key]
              for key in ("price_view", "event_view", "reference_view", "listing_master")}
    if ((event_view is None) != (reference_view is None)
            or (event_view is not None or resume_cases) and resume_run is None and not fresh_input_override):
        raise ValueError("Changed event/reference pair and selected continuation require a terminal seat run")
    if fresh_input_override and (event_view is None or reference_view is None or resume_run is not None or resume_cases):
        raise ValueError("Fresh input override requires a paired event/reference view and no resume arguments")
    if event_view is not None:
        inputs.update(event_view=str(Path(event_view).resolve()), reference_view=str(Path(reference_view).resolve()))
    previous = json.loads((Path(resume_run).resolve() / "run.json").read_text(encoding="utf-8")) if resume_run else None
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root, out = DATA_ROOT / "research/seat_bulk_replay" / identifier, ROOT / "outputs/seat_bulk_replay" / identifier
    root.mkdir(parents=True, exist_ok=False)
    out.mkdir(parents=True, exist_ok=False)
    config = portfolio()
    cases = [("TREND", 0)] + [(mode, lag) for lag in LAGS for mode in MODES[1:]]
    protocol = {"id": ID, "source_run": str(source_run), "seat_dataset": str(dataset), "stock_inputs": inputs,
                "fit": list(FIT), "evaluation": list(EVALUATION), "lags": list(LAGS), "portfolio": config.to_dict(),
                "minimum_seat_events": 2, "maximum_seats": 10, "seat_net_turnover_threshold": THRESHOLD,
                "cases": [{"signal": mode if not lag else f"lag{lag}_{mode}", "mode": mode, "lag": lag} for mode, lag in cases],
                "formula": "Original Donchian20/10/ATR20 and earlier H1/H4/ANY_LHB regime flags",
                "availability": "unknown publication timestamp; lagged decision close then next-session fills",
                "live_orders": False, "execution_admitted": False, "limits": LIMITS}
    record = {"id": identifier, "research_family": ID, "status": "preparing", "protocol": protocol,
              "artifacts": str(root), "start": EVALUATION[0], "end": EVALUATION[1], "results": [], "execution_admitted": False}
    if previous is not None:
        record.update(resume_run=str(Path(resume_run).resolve()), requested_resume_cases=list(resume_cases))
    write_json(root / "protocol.json", protocol)
    write_json(out / "run.json", record)
    shutil.copy2(__file__, root / "implementation.py")
    base = None
    with lock(dataset / "bulk_replay.lock"):
        try:
            base = BulkResearchFeed(**inputs)
            if EVALUATION[0] not in base.calendar or EVALUATION[1] not in base.calendar:
                raise ValueError("Both fixed evaluation endpoints must be present in the source calendar")
            dates = [day for day in base.calendar if EVALUATION[0] <= day <= EVALUATION[1]]
            checkpoints = {}
            if previous is not None:
                if (len(set(resume_cases)) != len(resume_cases)
                        or not set(resume_cases) <= {case["signal"] for case in protocol["cases"]}):
                    raise ValueError("Select distinct declared seat cases for continuation")
                checkpoints, unchanged = resume_check(previous, protocol, base.manifest, dates, config)
                record["resume_unaffected_past_actions"] = unchanged
                prepared = Path(previous["artifacts"])
                events = pd.read_parquet(prepared / "disclosure_events.parquet")
                summaries = pd.read_parquet(prepared / "stock_disclosures.parquet")
                seats, coverage = previous["selected_seats"], dict(previous["coverage"])
                counts = pd.read_csv(prepared / "seat_training_counts.csv")
                checks = pd.read_csv(prepared / "turnover_checks.csv", dtype={"date": str})
                unit_error = None if len(checks) and checks.status.eq("matched_within_one_percent").all() else "Saved seat turnover remains unreconciled"
                record["prepared_disclosures_reused"] = str(prepared)
            else:
                events, summaries, coverage = load_disclosures(dataset / "disclosures.sqlite3", base.symbols, FIT[0], EVALUATION[1])
                seats, counts = select_seats(events, FIT)
                checks, unit_error = turnover_checks(events, seats, base)
            events.to_parquet(root / "disclosure_events.parquet", index=False)
            summaries.to_parquet(root / "stock_disclosures.parquet", index=False)
            counts.to_csv(root / "seat_training_counts.csv", index=False)
            write_json(root / "selected_seats.json", {"seats": seats, "fit": list(FIT), "selection_uses_returns": False})
            checks.to_csv(root / "turnover_checks.csv", index=False)
            coverage.update(selected_seats=len(seats), selected_turnover_checks=len(checks),
                            turnover_check_states=checks.status.value_counts().to_dict())
            record.update(selected_seats=seats, coverage=coverage, sessions=len(dates), stock_manifest=base.manifest)
            write_json(out / "run.json", record)
            trend = BulkTrendFeed(base)
            record.update(status="running", trend_data_checks=trend.data_checks)
            write_json(out / "run.json", record)
            for case in protocol["cases"]:
                mode, lag = case["mode"], case["lag"]
                checkpoint = checkpoints.get(case["signal"])
                if previous is not None and checkpoint is not None and (
                        len(checkpoint["daily"]) == len(dates) or resume_cases and case["signal"] not in resume_cases):
                    shutil.copytree(checkpoint["source"], root / case["signal"])
                    saved = next(row for row in previous["results"] if row["signal"] == case["signal"])
                    result = {**saved, "reused_sessions": len(checkpoint["daily"]), "newly_completed_sessions": 0,
                              "resume_source": checkpoint["source"], "reused_without_replay": True}
                elif unit_error and mode != "TREND":
                    result = {"status": "failed_preparation", "error": unit_error, "completed_sessions": 0}
                else:
                    signals = {} if not lag else event_signals(events, summaries.to_dict("records"), seats, base.calendar, lag, THRESHOLD)
                    feed = BulkSeatFeed(trend, signals, mode, config)
                    destination = root / case["signal"]
                    result = baseline_ledger_result(feed, config, dates, destination, checkpoint=checkpoint)
                    append_signal_observations(feed, destination, checkpoint)
                record["results"].append({**case, **result})
                write_json(out / "run.json", record)
                report(out, record)
                print(json.dumps({"case": case["signal"], **result}), flush=True)
            record["status"] = ("completed_seat_bulk_diagnostic" if all(case["status"] == "completed" for case in record["results"])
                                else "partial_seat_bulk_diagnostic")
        except Exception as error:
            record.update(status="failed_seat_bulk_preparation", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            if base is not None:
                base.close()
            write_json(out / "run.json", record)
            report(out, record)
            print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--seat-dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--resume-run", type=Path)
    parser.add_argument("--event-view", type=Path)
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--resume-case", action="append", default=[])
    parser.add_argument("--fresh-input-override", action="store_true",
                        help="Start a new full replay with the explicit event/reference pair; do not reuse a seat checkpoint")
    args = parser.parse_args(argv)
    _, result = run(args.source_run, args.seat_dataset, resume_run=args.resume_run,
                    event_view=args.event_view, reference_view=args.reference_view, resume_cases=args.resume_case,
                    fresh_input_override=args.fresh_input_override)
    return 0 if result["status"] == "completed_seat_bulk_diagnostic" else 1
