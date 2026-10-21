"""Fixed low-volatility / momentum ablation through the sole stock ledger."""
import argparse
import ast
from collections import Counter
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..rules import stock_board
from ..report import metrics as portfolio_metrics
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .market_replay import validate_prefix_inputs
from .monthly_timing import PERIOD_END, month_ends

VARIANTS = ("momentum_12_1_20", "low_volatility_252_20", "low_vol_half_momentum_20")
WINDOW = 252
SKIP = 21
EXTENSION_END = "2026-12-31"
FIXED_PROTOCOL_FIELDS = ("id", "predefined_variants", "portfolio", "primary_sources", "source_review",
                         "common_pool", "feature_rule", "selection", "hybrid", "execution",
                         "differences", "hypothesis", "fresh_holdout_claimed", "tuning", "execution_admitted")


def portfolio(initial_cash=140000):
    # The feed supplies custom scores; the engine still owns selection and sizing.
    return replace(StrategyConfig(), signal="equal_weight", lookback=WINDOW, skip=SKIP,
                   holdings=20, retain_rank=30, rebalance_sessions=1,
                   initial_cash=initial_cash).validate()


def features(base, end, *, feature_days=None):
    """Do not treat suspended quotes as zero-return, low-risk observations."""
    closes = base.closes.loc[:end].where(base.volumes.loc[:end].gt(0))
    closes = closes.where(np.isfinite(closes) & closes.gt(0))
    returns = closes.pct_change(fill_method=None)
    volatility = returns.rolling(WINDOW, min_periods=WINDOW).std(ddof=1) * np.sqrt(252)
    momentum = closes.shift(SKIP) / closes.shift(WINDOW) - 1
    liquidity = base.amounts.loc[:end].rolling(20, min_periods=20).median()
    ends = month_ends(base.calendar, end) if feature_days is None else list(feature_days)
    if feature_days is not None and (ends != sorted(set(ends)) or any(day not in base.calendar or day > end for day in ends)):
        raise ValueError("Feature days must be distinct ordered covered sessions within the cutoff")
    return {"momentum": momentum.reindex(ends), "volatility": volatility.reindex(ends),
            "median_amount": liquidity.reindex(ends)}


def rank(pool, variant):
    if variant not in VARIANTS:
        raise ValueError("Unknown fixed low-risk variant")
    if variant == VARIANTS[2]:
        ordered = sorted(pool, key=lambda row: (row["volatility"], row["symbol"]))
        pool = ordered[:(len(ordered) + 1) // 2]
    rows = [{**row, "score": -row["volatility"] if variant == VARIANTS[1] else row["momentum"]}
            for row in pool]
    return sorted(rows, key=lambda row: (-row["score"], row["symbol"]))


class LowRiskFeed:
    def __init__(self, base, values, variant, end, *, diagnostic_extension=False):
        if (variant not in VARIANTS or end > (EXTENSION_END if diagnostic_extension else PERIOD_END)):
            raise ValueError("Unknown fixed variant or cutoff beyond 2025")
        self.base, self.values, self.variant, self.end = base, values, variant, end
        self.ends = set(month_ends(base.calendar, end))
        self.audit = []
        self.ranking_rows = []

    def __getattr__(self, key):
        return getattr(self.base, key)

    def pool(self, day, config):
        if any(day not in frame.index for frame in self.values.values()):
            return SignalUnavailable("low_risk_feature_batch_missing")
        if self.base.day_index[day] < WINDOW:
            return SignalUnavailable("low_risk_252_session_warmup")
        if any(not np.isfinite(frame.loc[day]).any() for frame in self.values.values()):
            return SignalUnavailable("low_risk_feature_batch_missing")
        rows = self.base.rows.get(day)
        if not rows:
            return SignalUnavailable("low_risk_raw_batch_missing")
        candidates = []
        for symbol in self.base.symbols:
            if symbol in self.base.unsupported_instruments:
                continue
            row, info = rows.get(symbol), self.base.instruments[symbol]
            if (row is None or row["tradestatus"] != 1 or row["isST"] != 0
                    or not np.isfinite(row["volume"]) or row["volume"] <= 0):
                continue
            if (not info.get("ipoDate") or pd.isna(info["ipoDate"])
                    or (pd.Timestamp(day) - pd.Timestamp(info["ipoDate"])).days < config.min_listing_days
                    or (info.get("outDate") and day >= info["outDate"])
                    or day <= self.base.manifest["selection_date"]):
                continue
            values = {key: float(frame.at[day, symbol]) for key, frame in self.values.items()}
            if (not np.isfinite(list(values.values())).all() or values["volatility"] <= 0
                    or values["median_amount"] < config.min_amount):
                continue
            candidates.append({"symbol": symbol, **values})
        return candidates

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Low-risk research is replay-only, not a current paper/live signal")
        if config != portfolio(config.initial_cash):
            raise ValueError("Low-risk research requires the fixed 20-name portfolio/cost config")
        if day > self.end or day not in self.base.day_index:
            raise ValueError("Low-risk decision outside the pinned calendar/cutoff")
        reason, pool, candidates = "not_month_end", None, None
        if day in self.ends:
            pool = self.pool(day, config)
            if isinstance(pool, SignalUnavailable):
                reason = pool.reason
                pool = None
            else:
                candidates = rank(pool, self.variant)
                reason = "monthly_ranking" if candidates else "known_empty_eligible_pool"
        self.audit.append({"date": day, "variant": self.variant, "decision": reason,
                           "eligible_count": len(pool) if pool is not None else None,
                           "candidate_count": len(candidates) if candidates is not None else None,
                           "intent": "NO_CHANGE" if candidates is None else "FULL_TARGET"})
        if candidates is None:
            return SignalUnavailable(reason)
        self.ranking_rows.extend({"date": day, "rank": i, **row}
                                 for i, row in enumerate(candidates[:config.retain_rank], start=1))
        return SignalFrame.from_legacy(candidates, day, "research:" + self.variant, context)


def source_plan(base, values, variants, start, end, config, *, diagnostic_extension=False):
    """Plan acquisition from fixed ranks, not returns or action completeness."""
    rows, unavailable = [], []
    for day in month_ends(base.calendar, end):
        if day < start:
            continue
        pool = LowRiskFeed(base, values, variants[0], end,
                           diagnostic_extension=diagnostic_extension).pool(day, config)
        if isinstance(pool, SignalUnavailable):
            unavailable.append({"date": day, "reason": pool.reason})
            continue
        for variant in variants:
            rows.extend({"date": day, "variant": variant, "rank": i, **row}
                        for i, row in enumerate(rank(pool, variant)[:config.retain_rank], start=1))
    return {"status": "prepared_fixed_rank_source_acquisition", "start": start, "end": end,
            "variants": list(variants), "retain_rank": config.retain_rank,
            "source_symbols": sorted({row["symbol"] for row in rows}),
            "rank_rows": rows, "unavailable_months": unavailable,
            "execution_admitted": False, "account_created": False,
            "limitations": [
                "Acquisition priority only, not executed holdings, fills or a capped strategy universe",
                "No account or return metrics; planned ranks use the existing diagnostic features",
                "Future action completeness never filters the ranking or changes the fixed strategy",
                "Does not guarantee coverage of failed exits, aliases or independently missing events"]}


def validate_continuation(previous, current):
    """Extend a terminal fixed experiment without changing its account/rules."""
    original = previous.get("purpose") == "fixed_low_risk_2024_2025_diagnostic"
    if (previous.get("status") not in {"completed_low_risk_diagnostic", "failed_low_risk_diagnostic"}
            or (original and (previous["status"] != "completed_low_risk_diagnostic" or previous["end"] != PERIOD_END))
            or (not original and previous.get("purpose") != "fixed_low_risk_2026_continuation_diagnostic")
            or previous.get("research_family") != "low_risk_stock_selection"
            or any(previous[key] != current[key] for key in ("scope", "symbols", "start"))
            or previous["end"] > current["end"] or previous["sessions"] > current["sessions"]
            or any(previous["protocol"].get(key) != current["protocol"].get(key) for key in FIXED_PROTOCOL_FIELDS)
            or not set(current["protocol"]["signals"]) <= set(previous["protocol"]["signals"])):
        raise ValueError("Continuation requires a terminal fixed low-risk comparison with unchanged rules/costs")
    for signal in current["protocol"]["signals"]:
        cases = [row for row in previous["results"] if row["signal"] == signal]
        if (len(cases) != 1 or cases[0]["status"] not in {"completed", "failed"}
                or (original and (cases[0]["status"] != "completed"
                                  or cases[0]["completed_sessions"] != previous["sessions"]))):
            raise ValueError("Continuation needs an actual terminal account for every selected fixed case")


def extension_metrics(folder):
    """Report the new calendar stage with its carried account, not reset cash."""
    frame = pd.read_csv(folder / "daily.csv", dtype={"date": str})
    selected = frame.date.gt(PERIOD_END)
    first = int(np.flatnonzero(selected)[0])
    rows = frame.loc[selected].copy()
    # The existing metrics needs the stale-mark lists, not their CSV strings.
    rows["stale_symbols"] = rows.stale_symbols.map(ast.literal_eval)
    opening = float(frame.iloc[first - 1].equity)
    return {"start": rows.date.iloc[0], "end": rows.date.iloc[-1], "opening_equity": opening,
            "account_reset": False, "independent_validation": False,
            **portfolio_metrics(rows.to_dict("records"), opening)}


def run(price_view, event_view, reference_view, listing_master, source_review,
        start="2024-01-02", end=PERIOD_END, initial_cash=140000, *, variants=VARIANTS,
        plan_sources=False, continue_run=None, fresh_2026=False):
    extension = continue_run is not None
    if extension and fresh_2026:
        raise ValueError("Choose --continue-run or --fresh-2026, not both")
    if fresh_2026 and (start != "2026-01-05" or end <= PERIOD_END):
        raise ValueError("Fresh 2026 account requires the first covered session 2026-01-05 and an end after 2025-12-31")
    max_end = EXTENSION_END if extension or fresh_2026 else PERIOD_END
    if not "2024-01-01" <= start <= end <= max_end:
        raise ValueError("Fixed 2024-2025 diagnostic; 2026 requires --continue-run or --fresh-2026")
    if extension and not PERIOD_END < end <= EXTENSION_END:
        raise ValueError("Continuation explicitly extends the unchanged account into 2026")
    previous = (json.loads((Path(continue_run) / "run.json").read_text(encoding="utf-8"))
                if extension else None)
    variants = tuple(variants)
    if not variants or len(set(variants)) != len(variants) or not set(variants) <= set(VARIANTS):
        raise ValueError("Select unique predefined low-risk variants, not a parameter search")
    config = portfolio(initial_cash)
    reviewed = json.loads(Path(source_review).read_text(encoding="utf-8"))
    base = BulkResearchFeed(price_view, event_view, reference_view, listing_master)
    try:
        if (base.manifest["scope"] != BULK_SCOPE or start not in base.calendar or end not in base.calendar
                or start < base.manifest["start"] or end > base.manifest["end"]):
            raise ValueError("Require covered aligned source-wide SH/SZ research views")
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        out = ROOT / "outputs/market_replay" / identifier
        artifacts = DATA_ROOT / "research/market_replay" / identifier
        out.mkdir(parents=True, exist_ok=False)
        artifacts.mkdir(parents=True, exist_ok=False)
        inputs = {key: base.manifest[key] for key in (
            "price_view", "event_view", "reference_view", "raw_view", "listing_master",
            "source_codes", "canonical_codes", "unsupported_instruments", "limitations")}
        protocol = {
            "id": "low_risk_price_volume_ablation_v1", "signals": list(variants),
            "predefined_variants": list(VARIANTS),
            "portfolio": config.to_dict(), "stock_inputs": inputs,
            "primary_sources": reviewed, "source_review": str(Path(source_review).resolve()),
            "period": {"start": start, "end": end, "signal_price_cutoff": end},
            "common_pool": "Ordinary supported SH/SZ, active non-ST, positive volume, listing age365, no past outDate; complete finite252-return volatility, 252-minus21 momentum and20-day median turnover>=20mCNY",
            "feature_rule": "Reconstructed adjusted closes masked on nonpositive volume; no forward fill. Annualized sample std of252 daily returns (ddof1); momentum=P[t-21]/P[t-252]-1. Same complete-feature intersection for all variants",
            "selection": "Monthly completed exchange month-ends;20 names, keep held names within rank30; equal4% equity targets,80% maximum requested gross. Fewer names leave cash; no positive-momentum gate",
            "hybrid": "Retain lowest-volatility ceil(N/2) of the eligible pool; deterministic symbol tie-break; then rank momentum. Low-vol control ranks minusvol, momentum control ranks momentum",
            "execution": "Existing Engine/Ledger only, next-session raw open, existing stock lots/T1/limits/costs; no interim rebalance retry assumed filled. No broker binding",
            "differences": "Not exact Conservative Formula or China study replication: no PIT index/large-cap membership, market-cap weighting,36 monthly return volatility, net payout yield, distress/crash model or quarterly100-stock portfolio. Local252 daily observations and20-stock rank buffer",
            "hypothesis": "Low-volatility selection, and momentum within a low-volatility pool, may improve cost-inclusive Sharpe/drawdown relative to the matched20-stock momentum control. Mixed and negative results retained",
            "fresh_holdout_claimed": False, "tuning": False, "execution_admitted": False,
            "limitations": [*base.manifest["limitations"],
                "2024-2025 already inspected; rules fixed before this experiment, not an independent selection/holdout",
                "Price-derived momentum is not an independently verified dividend-reinvestment total return",
                "EOD availability assumed; halted/missing quotes invalidate the full volatility window",
                "No industry neutrality, fund instruments, black-swan protection or profitability guarantee"],
        }
        if fresh_2026:
            protocol["fresh_reset"] = {
                "account_reset": True,
                "start_cash": float(initial_cash),
                "start_session": start,
                "carried_account": False,
                "untouched_holdout": False,
            }
            protocol["limitations"].append(
                "2026 uses an independent reset account, but the calendar/data period was already inspected; it is not an untouched holdout or live-readiness result")
        dates = [day for day in base.calendar if start <= day <= end]
        purpose = ("fixed_low_risk_2026_continuation_diagnostic" if extension
                   else "fixed_low_risk_2026_fresh_reset_diagnostic" if fresh_2026
                   else "fixed_low_risk_2024_2025_diagnostic")
        record = {"status": "running", "research_family": "low_risk_stock_selection",
                  "purpose": purpose, "scope": BULK_SCOPE,
                  "symbols": len(base.symbols), "start": start, "end": end, "sessions": len(dates),
                  "inputs": inputs, "protocol": protocol, "artifacts": str(artifacts), "results": [],
                  "started_at": datetime.now().astimezone().isoformat(),
                  "strategy_validated": False, "live_ready": False}
        checkpoints = {}
        if extension:
            validate_continuation(previous, record)
            record["continue_run"] = str(Path(continue_run).resolve())
            record["prefix_ignored_uninvolved_changes"] = {}
            for variant in variants:
                checkpoint = load_replay_checkpoint(Path(previous["artifacts"]) / variant, config, dates)
                case = next(row for row in previous["results"] if row["signal"] == variant)
                count = len(checkpoint["daily"])
                if (count != case["completed_sessions"] or not count
                        or checkpoint["state"]["day"] < PERIOD_END
                        or (case["status"] == "completed" and count != previous["sessions"])):
                    raise ValueError("Low-risk continuation account is not its saved terminal prefix")
                ignored = validate_prefix_inputs(previous["inputs"], inputs,
                                                 checkpoint["state"]["day"], checkpoint["state"])
                record["prefix_ignored_uninvolved_changes"][variant] = ignored or []
                checkpoints[variant] = checkpoint
            extension_start = next(day for day in dates if day > PERIOD_END)
            protocol["continuation"] = {"source_run": record["continue_run"],
                "extension_start": extension_start, "extension_end": end,
                "account_reset": False, "rules_and_costs_unchanged": True,
                "reused_sessions": {s: len(c["daily"]) for s, c in checkpoints.items()},
                "new_stage_already_inspected": True, "independent_validation": False}
            protocol["limitations"].append("2026 is an unchanged-rule diagnostic continuation of the saved account, not a fresh holdout or reset account; no performance-selected changes")
        with (out / "protocol.json").open("x", encoding="utf-8") as stream:
            json.dump(protocol, stream, indent=2, allow_nan=False)
        write_json(out / "run.json", record)
        try:
            values = features(base, end)
            if plan_sources:
                plan = source_plan(base, values, variants, extension_start if extension else start,
                                   end, config, diagnostic_extension=extension or fresh_2026)
                write_json(out / "source_plan.json", {**plan, "inputs": inputs})
                record.update(status="prepared_low_risk_source_targets",
                              source_plan=str(out / "source_plan.json"),
                              source_symbols=len(plan["source_symbols"]), account_created=False,
                              completed_at=datetime.now().astimezone().isoformat())
                write_json(out / "run.json", record)
                print(out, flush=True)
                return out
            for variant in variants:
                wrapper = LowRiskFeed(base, values, variant, end,
                                      diagnostic_extension=extension or fresh_2026)
                options = {"checkpoint": checkpoints[variant]} if extension else {}
                result = baseline_ledger_result(wrapper, config, dates, artifacts / variant, **options)
                result.update(signal=variant, label=variant.replace("_", " "))
                if extension:
                    result["checkpoint_sessions"] = len(checkpoints[variant]["daily"])
                    result["decision_audit_basis"] = "newly advanced sessions only; prior audit retained in continue_run"
                    if result["status"] == "completed":
                        result["extension_metrics"] = extension_metrics(artifacts / variant)
                pd.DataFrame(wrapper.audit).to_csv(artifacts / f"{variant}_decisions.csv", index=False)
                pd.DataFrame(wrapper.ranking_rows).to_csv(artifacts / f"{variant}_ranks_1_30.csv", index=False)
                result["decisions"] = dict(Counter(row["decision"] for row in wrapper.audit))
                record["results"].append(result)
                write_json(out / "run.json", record)
                print(f"{variant}: {result['status']}; return={result.get('net_return')}; sessions={result['completed_sessions']}", flush=True)
            record["status"] = ("completed" if all(row["status"] == "completed" for row in record["results"])
                                else "failed") + "_low_risk_diagnostic"
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
    finally:
        base.close()


def report(out, record):
    protocol = record["protocol"]
    lines = ["# Fixed Low-Risk Stock Selection", "", f"Status: {record['status']}. NO_TRADE.",
             f"{record['symbols']} source-covered SH/SZ identities; {record['start']} to {record['end']}; {record['sessions']} sessions; CNY{protocol['portfolio']['initial_cash']:.0f}.",
             "Matched capital, stocks, complete-feature eligibility, dates, costs and sole Engine/Ledger. Only ranking/filter differs.",
             "Not a fresh holdout, complete all-A/ETF universe, original-paper replication or trading admission.", "",
             "| Variant | Status / completed sessions | Net return | Max drawdown | Sharpe zero rf | Worst day | Fees CNY |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for row in record["results"]:
        if row["status"] == "completed":
            lines.append(f"| {row['signal']} | completed / {row['completed_sessions']} | {row['net_return']:.2%} | {row['max_drawdown']:.2%} | {row['sharpe_zero_rf_252']} | {row['worst_day']:.2%} | {row['fees']:.2f} |")
        else:
            lines.append(f"| {row['signal']} | failed / {row['completed_sessions']} | unavailable | unavailable | unavailable | unavailable | unavailable |")
            lines.append(f"Failure: {row['error']}; account basis: {row['account_basis']}; attempted {row['attempted_session']}.")
    if protocol.get("continuation"):
        lines += ["", "## 2026 Carried-Account Stage", "",
                  "No reset cash/positions. Earlier sessions are reused, not re-executed. This already-inspected stage is not independent validation.",
                  "| Variant | Reused sessions | New-stage return | New-stage drawdown | New-stage Sharpe zero rf |",
                  "|---|---:|---:|---:|---:|"]
        for row in record["results"]:
            stage = row.get("extension_metrics")
            lines.append(f"| {row['signal']} | {row['checkpoint_sessions']} | " +
                         (f"{stage['net_return']:.2%} | {stage['max_drawdown']:.2%} | {stage['sharpe_zero_rf_252']} |"
                          if stage else "unavailable | unavailable | unavailable |"))
    lines += ["", "## Fixed Rules", "", protocol["common_pool"], protocol["feature_rule"],
              protocol["selection"], protocol["hybrid"], protocol["execution"], "",
              "## Boundaries", "", protocol["differences"], *protocol["limitations"],
              "Smaller drawdown without positive cost-inclusive performance does not establish usable alpha. Compare only the matched20-name controls, not older5-name runs as a signal-only experiment.",
              f"Artifacts: `{record['artifacts']}`.", ""]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("price-view", "event-view", "reference-view", "listing-master", "source-review"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--end", default=PERIOD_END)
    parser.add_argument("--initial-cash", type=float, default=140000)
    parser.add_argument("--variant", action="append", choices=VARIANTS,
                        help="Only these predefined cases; default all three, no parameter search")
    parser.add_argument("--plan-sources", action="store_true",
                        help="Compute full-period fixed top30 acquisition priorities only; no account or backtest")
    parser.add_argument("--continue-run", type=Path,
                        help="Carry a terminal fixed low-risk account into an explicit 2026 diagnostic; no reset or rule tuning")
    parser.add_argument("--fresh-2026", action="store_true",
                        help="Run the unchanged fixed rules from the first covered 2026 session 2026-01-05; not an untouched holdout")
    args = parser.parse_args(argv)
    out = run(args.price_view, args.event_view, args.reference_view, args.listing_master,
              args.source_review, args.start, args.end, args.initial_cash,
              variants=VARIANTS if args.variant is None else args.variant,
              plan_sources=args.plan_sources, continue_run=args.continue_run,
              fresh_2026=args.fresh_2026)
    status = json.loads((out / "run.json").read_text(encoding="utf-8"))["status"]
    return 0 if status.startswith("completed_") or status == "prepared_low_risk_source_targets" else 1


if __name__ == "__main__":
    raise SystemExit(main())
