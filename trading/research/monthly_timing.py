"""Fixed monthly momentum / CSI300 trend-gate adaptations in the sole ledger."""
import argparse
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..feed import HistoricalFeed
from ..market_snapshot import FIXED_SCOPE, INGESTION_SCOPE
from ..rules import stock_board
from ..settings import DATA_ROOT, ROOT
from .experiment import baseline_ledger_result, write_json
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .market_indices import load as load_indices, normalize

SOURCE_URL = "https://mebfaber.com/2017/12/13/episode-86-quantitative-approach-tactical-asset-allocation/"
VARIANTS = ("monthly_momentum", "monthly_momentum_csi300_10m")
PERIOD_END = "2025-12-31"


def month_ends(calendar, end):
    if not calendar or calendar != sorted(set(calendar)):
        raise ValueError("Expected a unique chronological exchange calendar")
    for day in calendar:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("Invalid exchange session")
    # A later calendar session confirms a completed month; its price is not used.
    return [day for day, following in zip(calendar, calendar[1:])
            if day <= end and day[:7] != following[:7]]


def trend_table(closes, calendar, end):
    if closes.index.has_duplicates or not closes.index.is_monotonic_increasing:
        raise ValueError("Index closes must have unique chronological dates")
    ends = month_ends(calendar, end)
    months = pd.period_range(calendar[0][:7], end[:7], freq="M")
    monthly = pd.Series(np.nan, index=months, dtype=float)
    for day in ends:
        value = closes.get(day, np.nan)
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"Missing/invalid month-end index close: {day}")
        monthly.at[pd.Period(day[:7], freq="M")] = value
    average = monthly.rolling(10, min_periods=10).mean()
    rows = []
    for day in ends:
        period = pd.Period(day[:7], freq="M")
        value, mean = float(monthly.at[period]), float(average.at[period])
        gate = ("warmup" if not np.isfinite(mean) else
                "risk_on" if value > mean else "risk_off" if value < mean else "equal_no_change")
        rows.append({"date": day, "index_close": value,
                     "sma_10_months": mean, "gate": gate})
    return pd.DataFrame(rows, columns=["date", "index_close", "sma_10_months", "gate"])


def index_inputs(folder, stock_calendar, end, *, terminal_calendar=False):
    folder = Path(folder).resolve()
    manifest, calendar = load_indices(folder)
    required = [day for day in stock_calendar if day <= end]
    stock_following = next((day for day in stock_calendar if day > end), None)
    index_following = next((day for day in calendar if day > end), None)
    if ([day for day in calendar if day <= end] != required or stock_following is None and not terminal_calendar
            or index_following != stock_following):
        raise ValueError("Stock and index calendars must match through the cutoff and its next session")
    receipt = manifest["receipts"].get("sh.000300")
    if receipt is None:
        raise ValueError("Missing CSI300 history receipt")
    for file_key, hash_key in (("file", "sha256"), ("raw_file", "raw_sha256")):
        path = (folder / receipt[file_key]).resolve()
        path.relative_to(folder)
        if sha256(path.read_bytes()).hexdigest() != receipt[hash_key]:
            raise ValueError("CSI300 history differs from its saved receipt")
    # A terminal cutoff does not invent its last month-end or a later execution.
    frame = pd.read_parquet(folder / receipt["file"], filters=[("date", "<=", end)])
    frame = normalize(frame, "sh.000300", [d for d in calendar if d <= end])
    closes = frame.set_index("date").close
    return trend_table(closes, calendar, end), {
        "folder": str(folder), "manifest_sha256": sha256((folder / "manifest.json").read_bytes()).hexdigest(),
        "receipt": receipt, "price_cutoff": end, "price_rows_consumed": len(frame),
        "calendar_sha256": manifest["calendar_sha256"],
        "use": "index_points_for_market_gate_only_not_tradable_security",
        "independent_index_company_reconciliation": False}


class MonthlyFeed:
    def __init__(self, base, table, variant, end):
        if variant not in VARIANTS or end > PERIOD_END:
            raise ValueError("Unknown fixed variant or inspected-period boundary exceeded")
        self.base, self.variant, self.end = base, variant, end
        self.ends = set(month_ends(base.calendar, end))
        self.gates = table.set_index("date").to_dict("index")
        self.audit = []

    def __getattr__(self, key):
        return getattr(self.base, key)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Cached monthly research inputs are not paper/live signals")
        if config.signal != "momentum" or config.rebalance_sessions != 1:
            raise ValueError("Monthly policy requires daily decisions and the fixed momentum score")
        if day > self.end:
            raise ValueError("Monthly adaptation cannot evaluate beyond its fixed cutoff")
        reason, gate, candidates = "not_month_end", None, None
        if day in self.ends:
            gate = self.gates.get(day)
            reason = "monthly_momentum"
            if self.variant == VARIANTS[1]:
                reason = gate["gate"] if gate else "index_gate_batch_missing"
                if reason == "risk_off":
                    candidates = []
                elif reason == "risk_on":
                    candidates = self.base.scores(day, config)
            else:
                candidates = self.base.scores(day, config)
        elif self.variant == VARIANTS[1]:
            completed = [d for d, row in self.gates.items() if d < day and d in self.ends
                         and row["gate"] in {"risk_on", "risk_off"}]
            previous = self.gates[max(completed)] if completed else None
            # A blocked exit must retain the decided cash target, not wait a month.
            if previous and previous["gate"] == "risk_off":
                reason, gate, candidates = "carry_cash_target", previous, []
        if isinstance(candidates, SignalUnavailable):
            reason, candidates = candidates.reason, None
        self.audit.append({"date": day, "variant": self.variant, "decision": reason,
                           "candidate_count": len(candidates) if candidates is not None else None,
                           "index_close": gate["index_close"] if gate else None,
                           "sma_10_months": gate["sma_10_months"] if gate else None,
                           "intent": "NO_CHANGE" if candidates is None else "FULL_TARGET"})
        if candidates is None:
            return SignalUnavailable(reason)
        return SignalFrame.from_legacy(candidates, day, "research:" + self.variant, context)


def implementation_hashes():
    files = (Path(__file__), ROOT / "trading/engine.py", ROOT / "trading/ledger.py",
             ROOT / "trading/feed.py", ROOT / "trading/config.py", ROOT / "trading/rules.py",
             ROOT / "trading/simulated_broker.py", ROOT / "trading/contracts.py",
             Path(__file__).with_name("experiment.py"), Path(__file__).with_name("market_indices.py"),
             ROOT / "trading/report.py")
    return {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest() for path in files}


def run(dataset, indices, start="2024-01-02", end=PERIOD_END, initial_cash=140000,
        *, price_view=None, event_view=None, reference_view=None, listing_master=None):
    if not "2024-01-01" <= start <= end <= PERIOD_END:
        raise ValueError("This adaptation is confined to 2024-2025, not the inspected 2026 period")
    bulk = price_view is not None
    if bulk:
        if dataset is not None or event_view is None or reference_view is None:
            raise ValueError("Bulk monthly research requires aligned price/event/reference views, not --dataset")
        feed = BulkResearchFeed(price_view, event_view, reference_view, listing_master)
    else:
        if dataset is None or any(value is not None for value in (event_view, reference_view, listing_master)):
            raise ValueError("Provide --dataset or the complete bulk views")
        feed = HistoricalFeed(dataset)
    if feed.manifest["scope"] not in {FIXED_SCOPE, INGESTION_SCOPE, BULK_SCOPE}:
        raise ValueError("Expected a declared immutable SH/SZ stock research snapshot")
    if (start not in feed.calendar or end not in feed.calendar
            or start < feed.manifest["start"] or end > feed.manifest["end"]):
        raise ValueError("Replay dates must be covered exchange sessions")
    config = replace(StrategyConfig(), signal="momentum", rebalance_sessions=1,
                     initial_cash=initial_cash).validate()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs/market_replay" / identifier
    artifacts = DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    dates = [day for day in feed.calendar if start <= day <= end]
    record = {"status": "running", "dataset": str(feed.folder.resolve()), "artifacts": str(artifacts.resolve()),
              "purpose": "fixed_monthly_rule_adaptation_2024_2025_diagnostic", "research_family": "monthly_timing",
              "scope": feed.manifest["scope"], "symbols": len(feed.symbols),
              "board_counts": dict(Counter("unsupported" if s in getattr(feed, "unsupported_instruments", {})
                                           else stock_board(s) for s in feed.symbols)),
              "start": start, "end": end, "sessions": len(dates), "strategy_validated": False,
              "live_ready": False, "results": [], "started_at": datetime.now().astimezone().isoformat()}
    if bulk:
        record["inputs"] = {key: feed.manifest[key] for key in (
            "price_view", "event_view", "reference_view", "raw_view", "listing_master",
            "source_codes", "canonical_codes", "unsupported_instruments", "limitations")}
    try:
        table, index_receipt = index_inputs(indices, feed.calendar, end)
        protocol = {"id": "monthly_momentum_csi300_10m_" + ("bulk_source_v2" if bulk else "local_v2"), "signals": list(VARIANTS),
                    "portfolio": config.to_dict(),
                    "signal_portfolio_configs": {variant: config.to_dict() for variant in VARIANTS},
                    "stock_manifest_sha256": sha256((feed.folder / ("result.json" if bulk else "manifest.json")).read_bytes()).hexdigest(),
                    "stock_files_sha256": {name: sha256((feed.folder / name).read_bytes()).hexdigest()
                        for name in (() if bulk else ("raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv"))},
                    "index_inputs": index_receipt, "source_url": SOURCE_URL,
                    "source_review": "Author's original-rule explanation read; no full-paper replication claim",
                    "rule": "Existing positive 126-session momentum skipping latest21, existing60-session vol/liquidity/listing/status eligibility; five holdings/rank8; only completed month-end target decisions",
                    "gate_rule": "CSI300 price close > mean of last10 completed monthly closes including current: risk on; below: empty full target; equal/missing/warmup: NO_CHANGE",
                    "cash_target_persistence": "After a risk-off month-end, carry its empty full target on subsequent closes until a valid risk-on month-end; retry only through existing next-session simulation, never treat an intent as a filled exit",
                    "revision_reason": "v1 code-path review found blocked exits would otherwise be forgotten on non-month-end days; fixed in v2, not selected using portfolio returns. v1 artifacts remain separate",
                    "execution": "Next exchange session through unchanged Engine/Ledger; daily corporate actions and ST exits remain active; costs, raw prices, T+1, lot/board/limit/volume rules unchanged",
                    "paper_differences": "Local stock ranking with a market-wide CSI300 price-index gate, not original multi-asset own-total-return timing; next-session rather than same-close trades; zero-interest cash rather than T-bills; explicit fees/tax/slippage",
                    "tuning": False, "fresh_holdout_claimed": False, "selection_is_point_in_time": False,
                    "independent_validation": False, "execution_admitted": False,
                    "period_boundary": "2024-2025 only; prior diagnostics include these years; no untouched claim and no new2026 replay",
                    "limitations": [("Full canonical source-covered SH/SZ universe, no stock budget; not independently complete A/BSE/ETF or PIT coverage"
                                     if bulk else "Partial retrieval-order/provider universe, not full market or CSI300 membership"),
                                    "Historical EOD availability is assumed, not verified publication-time PIT",
                                    "Monthly timing cannot guarantee sudden-crash protection or liquidate suspended/limit-down positions",
                                    "Unresolved account corporate actions stop that branch; no flooring, stock removal or guessed allocation"],
                    "source_files_sha256": implementation_hashes()}
        if bulk:
            protocol["bulk_inputs"] = record["inputs"]
        record["protocol"] = protocol
        # Freeze the two rules and all input hashes before any account advances.
        with (out / "protocol.json").open("x", encoding="utf-8") as stream:
            json.dump(protocol, stream, ensure_ascii=False, indent=2, allow_nan=False)
        table.to_csv(artifacts / "month_end_gate.csv", index=False)
        write_json(out / "run.json", record)
        for variant in VARIANTS:
            wrapper = MonthlyFeed(feed, table, variant, end)
            result = baseline_ledger_result(wrapper, config, dates, artifacts / variant)
            result.update(signal=variant, label=variant.replace("_", " "))
            pd.DataFrame(wrapper.audit).to_csv(artifacts / f"{variant}_decisions.csv", index=False)
            result["decisions"] = dict(Counter(row["decision"] for row in wrapper.audit))
            record["results"].append(result)
            write_json(out / "run.json", record)
            print(f"{variant}: {result['status']}; return={result.get('net_return')}; "
                  f"sessions={result['completed_sessions']}", flush=True)
        record["status"] = ("completed" if all(r["status"] == "completed" for r in record["results"])
                            else "failed") + "_monthly_adaptation_diagnostic"
        record["completed_at"] = datetime.now().astimezone().isoformat()
        write_json(out / "run.json", record)
        pd.DataFrame(record["results"]).to_csv(out / "metrics.csv", index=False)
        report(out, record)
    except Exception as error:
        record.update(status="failed_preparation_or_execution", error=f"{type(error).__name__}: {error}")
        write_json(out / "run.json", record)
        raise
    print(out, flush=True)
    return out


def report(out, record):
    lines = ["# Fixed monthly strategy adaptation", "", f"Status: {record['status']}. NO_TRADE.",
             f"{record['symbols']} {'full source-covered' if record['scope'] == BULK_SCOPE else 'partial acquired'} SH/SZ stock identities; {record['start']} to {record['end']}, {record['sessions']} sessions.",
             f"Source snapshot: `{record['dataset']}`. Sole-ledger artifacts: `{record['artifacts']}`.",
             "Both variants use identical cash, stock sample, dates and costs; only the market gate differs.",
             "2024-2025 diagnostic only, not a fresh holdout, original-paper replication, ETF replay or account eligibility.", "",
             "| Variant | Status | Net return | Max drawdown | Sharpe (zero rf) | Fees CNY | Orders |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for row in record["results"]:
        if row["status"] == "completed":
            sharpe = row["sharpe_zero_rf_252"]
            lines.append(f"| {row['signal']} | completed | {row['net_return']:.2%} | {row['max_drawdown']:.2%} | "
                         f"{sharpe if sharpe is None else round(sharpe, 4)} | {row['fees']:.2f} | {row['orders']} |")
        else:
            lines.append(f"| {row['signal']} | failed: {row['error']} | unavailable | unavailable | unavailable | unavailable | unavailable |")
    lines += ["", "## Fixed Rules", "", record["protocol"]["rule"], record["protocol"]["gate_rule"],
              record["protocol"]["execution"], record["protocol"]["cash_target_persistence"],
              "A final month-end decision may remain pending beyond the evaluation end; no future fill is counted.",
              "No new monthly signal means NO_CHANGE unless carrying a previously decided cash target. Missing month-end inputs never imply a new sell command.",
              "## Boundaries", "", record["protocol"]["paper_differences"], *record["protocol"]["limitations"], "",
              "No model training, 2026 adaptation return inspection, default-feed mutation, account import or broker operations.",
              f"Author rule reference: [{SOURCE_URL}]({SOURCE_URL})", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path)
    source.add_argument("--price-view", type=Path)
    parser.add_argument("--event-view", type=Path)
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--listing-master", type=Path)
    parser.add_argument("--indices", type=Path, required=True)
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default=PERIOD_END)
    parser.add_argument("--initial-cash", type=float, default=140000)
    args = parser.parse_args(argv)
    out = run(args.dataset, args.indices, args.start, args.end, args.initial_cash,
              price_view=args.price_view, event_view=args.event_view,
              reference_view=args.reference_view, listing_master=args.listing_master)
    return 0 if json.loads((out / "run.json").read_text(encoding="utf-8"))["status"].startswith("completed_") else 1


if __name__ == "__main__":
    raise SystemExit(main())
