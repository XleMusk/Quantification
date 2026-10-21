"""Fixed, unlevered CSI300 variance overlay on monthly stock targets."""
import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import IntentKind, PortfolioIntent, TradingMode
from ..feed import HistoricalFeed
from ..market_snapshot import FIXED_SCOPE, INGESTION_SCOPE
from ..rules import stock_board
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .experiment import baseline_ledger_result, write_json
from .market_indices import load as load_indices, normalize
from .monthly_timing import MonthlyFeed, PERIOD_END, implementation_hashes, month_ends

VARIANTS = ("monthly_momentum", "monthly_momentum_fixed_exposure", "monthly_momentum_inverse_variance")
TRAIN_END = "2023-12-31"
SOURCES = (
    ("MoreiraMuir_JF2017.pdf", "https://www.dropbox.com/scl/fi/hr4yrg08phjubsnu8b57k/JF_PublishedVersion_Moreira_Muir_2017.pdf?rlkey=6g4duv191ljrxbsr3vv99w7jz&dl=1",
     "f0c9f2099b083e6d8d0eba64eb0cedd49c3e4c47f0a51c2181f163252ce8a9b1"),
    ("CederburgEtAl_JFE2020.pdf", "https://www.lehigh.edu/~xuy219/research/COWY.pdf",
     "5f82689700a360bed1fd371637d4a62090c6715d0cc9bd85c36fc78bc6129919"),
)


def variance_table(frame, calendar, end):
    """Use every available daily return in each completed exchange month."""
    frame = normalize(frame, "sh.000300", [d for d in calendar if d <= end])
    if not np.isclose(frame.preclose.iloc[1:], frame.close.iloc[:-1], rtol=0, atol=.0001).all():
        raise ValueError("CSI300 previous-close continuity conflict")
    returns = frame.close / frame.preclose - 1
    rows = []
    for day in month_ends(calendar, end):
        values = returns.loc[frame.date.str[:7].eq(day[:7])]
        variance = float(((values - values.mean()) ** 2).sum())
        rows.append({"date": day, "daily_observations": len(values), "realized_variance": variance,
                     "monthly_return": float((1 + values).prod() - 1)})
    return pd.DataFrame(rows)


def calibration(table):
    if table.date.duplicated().any() or table.date.tolist() != sorted(table.date):
        raise ValueError("Variance months must be unique and chronological")
    train = table.loc[table.date.str.startswith("2023-")].copy()
    if train.date.str[:7].tolist() != [f"2023-{month:02d}" for month in range(1, 13)]:
        raise ValueError("Require all twelve completed 2023 calibration months")
    if (train.daily_observations.lt(10).any() or not np.isfinite(train.realized_variance).all()
            or train.realized_variance.le(0).any() or not np.isfinite(train.monthly_return).all()):
        raise ValueError("Invalid/insufficient calibration variance")
    previous = train.realized_variance.shift(1).iloc[1:]
    original = train.monthly_return.iloc[1:]
    unscaled = original / previous
    constant = float(original.std(ddof=1) / unscaled.std(ddof=1))
    if not np.isfinite(constant) or constant <= 0:
        raise ValueError("Degenerate 2023-only normalization")
    fixed = float((constant / previous).clip(upper=1).mean())
    return {"train_start": "2023-01-01", "train_end": TRAIN_END, "paired_months": len(original),
            "constant": constant, "fixed_control_scale": fixed,
            "constant_rule": "std(r_month)/std(r_month/RV_previous_month), ddof=1, 2023 only; frozen afterwards",
            "control_rule": "Mean of min(1,c/RV_previous_month) over the same 2023 training pairs; not matched using evaluation returns/exposures"}


def index_inputs(folder, calendar, end):
    folder = Path(folder).resolve()
    manifest, index_calendar = load_indices(folder)
    required = [day for day in calendar if day <= end]
    following = next((day for day in calendar if day > end), None)
    if ([day for day in index_calendar if day <= end] != required or following is None
            or next((day for day in index_calendar if day > end), None) != following):
        raise ValueError("Stock/index calendars must match through the cutoff and its next session")
    receipt = manifest["receipts"].get("sh.000300")
    if receipt is None:
        raise ValueError("Missing CSI300 source receipt")
    for name, digest in (("file", "sha256"), ("raw_file", "raw_sha256")):
        path = (folder / receipt[name]).resolve()
        path.relative_to(folder)
        if sha256(path.read_bytes()).hexdigest() != receipt[digest]:
            raise ValueError("CSI300 source receipt changed")
    frame = pd.read_parquet(folder / receipt["file"], filters=[("date", "<=", end)])
    return variance_table(frame, calendar, end), {
        "folder": str(folder), "manifest_sha256": sha256((folder / "manifest.json").read_bytes()).hexdigest(),
        "receipt": receipt, "price_rows_consumed": len(frame), "price_cutoff": end,
        "use": "index_points_for_risk_context_only_not_security_prices",
        "independent_index_company_reconciliation": False}


class VariancePolicy:
    def __init__(self, table, fitted, variant, end):
        if variant not in VARIANTS or end > PERIOD_END:
            raise ValueError("Unknown fixed variant or period beyond 2025")
        if (fitted["train_end"] != TRAIN_END or fitted["paired_months"] != 11
                or not np.isfinite(fitted["constant"]) or fitted["constant"] <= 0
                or not 0 < fitted["fixed_control_scale"] <= 1):
            raise ValueError("Invalid frozen calibration")
        self.rows = table.set_index("date").to_dict("index")
        self.fitted, self.variant, self.end, self.audit = dict(fitted), variant, end, []

    def __call__(self, intent):
        if intent.context.mode != TradingMode.REPLAY or intent.kind != IntentKind.FULL_TARGET:
            raise ValueError("Variance policy is replay-only")
        day = datetime.fromisoformat(intent.decided_at).date().isoformat()
        if not "2024-01-01" <= day <= self.end:
            raise ValueError("Variance policy exceeds fixed evaluation boundary")
        row = self.rows.get(day)
        scale, reason = None, "variance_batch_missing"
        if self.variant == VARIANTS[0]:
            scale, reason = 1., "unmanaged_control"
        elif self.variant == VARIANTS[1]:
            scale, reason = self.fitted["fixed_control_scale"], "training_fixed_control"
        elif row is not None:
            variance = row["realized_variance"]
            if np.isfinite(variance) and variance > 0 and row["daily_observations"] >= 10:
                scale = min(1., self.fitted["constant"] / variance)
                reason = "inverse_previous_month_variance_for_next_month"
            else:
                reason = "invalid_or_insufficient_month_variance"
        self.audit.append({"date": day, "variant": self.variant, "reason": reason, "scale": scale,
                           "variance": row["realized_variance"] if row else None,
                           "gross_before": sum(w for _, w in intent.weights),
                           "gross_target": sum(w for _, w in intent.weights) * scale if scale is not None else None,
                           "intent": "NO_CHANGE" if scale is None else "FULL_TARGET"})
        if scale is None:
            return PortfolioIntent.no_change(reason, context=intent.context, decided_at=intent.decided_at)
        return replace(intent, weights=tuple((s, w * scale) for s, w in intent.weights),
                       source="research:" + self.variant)


def source_receipts():
    rows = []
    for name, url, digest in SOURCES:
        path = DATA_ROOT / "research/sources/volatility_management" / name
        if sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Reviewed primary PDF changed")
        rows.append({"file": str(path), "url": url, "sha256": digest,
                     "review": "formation/calibration/implementation and critique sections read, not full-paper replication"})
    return rows


def run(dataset, indices, start="2024-01-02", end=PERIOD_END, initial_cash=140000,
        *, price_view=None, event_view=None, reference_view=None, listing_master=None,
        variants=VARIANTS):
    if not "2024-01-01" <= start <= end <= PERIOD_END:
        raise ValueError("Fixed 2024-2025 diagnostic only; no new 2026 adaptation replay")
    variants = tuple(variants)
    if not variants or len(set(variants)) != len(variants) or not set(variants) <= set(VARIANTS):
        raise ValueError("Select unique predefined variance variants; no parameter search")
    bulk = price_view is not None
    if bulk:
        if dataset is not None or event_view is None or reference_view is None:
            raise ValueError("Bulk variance research requires aligned price/event/reference views, not --dataset")
        feed = BulkResearchFeed(price_view, event_view, reference_view, listing_master)
    else:
        if dataset is None or any(value is not None for value in (event_view, reference_view, listing_master)):
            raise ValueError("Provide --dataset or the complete bulk views")
        feed = HistoricalFeed(dataset)
    if (feed.manifest["scope"] not in {FIXED_SCOPE, INGESTION_SCOPE, BULK_SCOPE} or start not in feed.calendar
            or end not in feed.calendar or start < feed.manifest["start"] or end > feed.manifest["end"]):
        raise ValueError("Require a covered immutable SH/SZ stock snapshot")
    config = replace(StrategyConfig(), signal="momentum", rebalance_sessions=1, initial_cash=initial_cash).validate()
    table, index_receipt = index_inputs(indices, feed.calendar, end)
    fitted = calibration(table)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    table.to_csv(artifacts / "completed_month_variance.csv", index=False)
    hashes = implementation_hashes()
    hashes[str(Path(__file__).relative_to(ROOT))] = sha256(Path(__file__).read_bytes()).hexdigest()
    protocol = {"id": "csi300_monthly_variance_" + ("bulk_source_v1" if bulk else "local_v1"),
        "signals": list(variants), "predefined_variants": list(VARIANTS), "portfolio": config.to_dict(),
        "signal_portfolio_configs": {v: config.to_dict() for v in variants},
        "stock_manifest_sha256": sha256((feed.folder / ("result.json" if bulk else "manifest.json")).read_bytes()).hexdigest(),
        "stock_input_sha256": {name: sha256((feed.folder / name).read_bytes()).hexdigest()
            for name in (() if bulk else ("manifest.json", "raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv"))},
        "index_inputs": index_receipt, "primary_sources": source_receipts(), "source_files_sha256": hashes,
        "calibration": fitted, "rule": "Monthly positive126-session stock momentum excluding latest21, five holdings/rank8; same existing eligibility and next-session ledger",
        "overlay": "RV=sum squared demeaned daily CSI300 close/preclose returns in the just-completed month; scale=min(1,c/RV), applied only to following-session targets; no leverage",
        "differences": "CSI300 price-index variance scales a different long-only stock basket, not its own US long-short factor excess returns; zero-interest cash, A-share costs/T+1/limits; 11-month calibration not the critique's 120-month training design",
        "hypothesis": "Dynamic scaling improves ledger Sharpe/drawdown relative to both unmanaged and training-fixed exposure; failure or mixed results are retained, no winner promotion",
        "execution": "Only reduce-only typed replay intents; default paper/live path unchanged; partial/blocked monthly rebalances are not assumed filled or retried between month-ends",
        "fresh_holdout_claimed": False, "tuning": False, "execution_admitted": False,
        "limitations": ["2024-2025 already inspected diagnostics; no independent/prospective validation",
                        ("Full canonical source-covered SH/SZ universe, no stock budget; not independently complete A/BSE/ETF or PIT coverage"
                         if bulk else "Partial provider-retained stock sample, non-PIT membership"),
                        "Historical EOD source availability is assumed, not publication-time verified",
                        "Last-month variance cannot anticipate a sudden gap/crash; blocked sells may exceed target risk",
                        "No ETF, broker/account evidence or guarantee of returns/risk protection"]}
    dates = [d for d in feed.calendar if start <= d <= end]
    record = {"status": "running", "dataset": str(feed.folder.resolve()), "artifacts": str(artifacts.resolve()),
              "purpose": "fixed_variance_overlay_2024_2025_diagnostic",
              "research_family": "volatility_timing", "scope": feed.manifest["scope"], "symbols": len(feed.symbols),
              "board_counts": dict(Counter("unsupported" if s in getattr(feed, "unsupported_instruments", {})
                                           else stock_board(s) for s in feed.symbols)),
              "start": start, "end": end, "sessions": len(dates), "protocol": protocol, "results": [],
              "started_at": datetime.now().astimezone().isoformat(), "command": sys.argv,
              "strategy_validated": False, "live_ready": False}
    if bulk:
        record["inputs"] = {key: feed.manifest[key] for key in (
            "price_view", "event_view", "reference_view", "raw_view", "listing_master",
            "source_codes", "canonical_codes", "unsupported_instruments", "limitations")}
        protocol["bulk_inputs"] = record["inputs"]
    with (out / "protocol.json").open("x", encoding="utf-8") as stream:
        json.dump(protocol, stream, indent=2, allow_nan=False)
    write_json(out / "run.json", record)
    try:
        for variant in variants:
            wrapper = MonthlyFeed(feed, pd.DataFrame(columns=["date"]), "monthly_momentum", end)
            policy = VariancePolicy(table, fitted, variant, end)
            result = baseline_ledger_result(wrapper, config, dates, artifacts / variant, target_policy=policy)
            result.update(signal=variant, label=variant.replace("_", " "))
            pd.DataFrame(policy.audit).to_csv(artifacts / f"{variant}_exposure.csv", index=False)
            result["exposure_decisions"] = dict(Counter(r["reason"] for r in policy.audit))
            record["results"].append(result)
            write_json(out / "run.json", record)
            print(f"{variant}: {result['status']}; return={result.get('net_return')}; sessions={result['completed_sessions']}", flush=True)
        record["status"] = ("completed" if all(r["status"] == "completed" for r in record["results"]) else "failed") + "_volatility_adaptation_diagnostic"
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
    protocol = record["protocol"]
    lines = ["# Fixed volatility-management adaptation", "", f"Status: {record['status']}. NO_TRADE.",
             f"{record['symbols']} {'full source-covered SH/SZ identities' if record['scope'] == BULK_SCOPE else 'partial stocks'}; {record['start']} to {record['end']}; {record['sessions']} sessions; capital CNY{protocol['portfolio']['initial_cash']:.0f}.",
             "Same stocks, dates, capital, costs, signal and sole Engine/Ledger. Only target exposure differs.",
             "Not a fresh holdout, full-market winner, original-paper replication or account approval.", "",
             "| Variant | Status | Net return | Max drawdown | Sharpe zero rf | Worst day | Fees CNY |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for row in record["results"]:
        if row["status"] == "completed":
            lines.append(f"| {row['signal']} | completed | {row['net_return']:.2%} | {row['max_drawdown']:.2%} | {row['sharpe_zero_rf_252']} | {row['worst_day']:.2%} | {row['fees']:.2f} |")
        else:
            lines.append(f"| {row['signal']} | failed: {row['error']} | unavailable | unavailable | unavailable | unavailable | unavailable |")
    lines += ["", "## Fixed Design", "", protocol["rule"], protocol["overlay"],
              json.dumps(protocol["calibration"], indent=2), protocol["execution"], "",
              "## Interpretation Boundaries", "", protocol["differences"], *protocol["limitations"],
              "Do not infer useful volatility timing merely from a smaller drawdown at lower exposure. Compare the fixed-exposure control; no retrospective matching or strategy selection.",
              "The papers' method/implementation/critique sections were read. Full-sample normalization and ex-post spanning weights are not real-time allocation evidence.",
              f"Artifacts: `{record['artifacts']}`.", ""]
    for source in protocol["primary_sources"]:
        lines.append(f"Primary source: [{source['file']}]({source['url']})")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path)
    source.add_argument("--price-view", type=Path)
    parser.add_argument("--event-view", type=Path)
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--listing-master", type=Path)
    parser.add_argument("--variant", action="append", choices=VARIANTS,
                        help="Run only these predefined cases; default runs all three")
    parser.add_argument("--indices", type=Path, required=True)
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default=PERIOD_END)
    parser.add_argument("--initial-cash", type=float, default=140000)
    args = parser.parse_args(argv)
    out = run(args.dataset, args.indices, args.start, args.end, args.initial_cash,
              price_view=args.price_view, event_view=args.event_view,
              reference_view=args.reference_view, listing_master=args.listing_master,
              variants=VARIANTS if args.variant is None else args.variant)
    return 0 if json.loads((out / "run.json").read_text(encoding="utf-8"))["status"].startswith("completed_") else 1


if __name__ == "__main__":
    raise SystemExit(main())
