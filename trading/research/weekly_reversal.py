"""Fixed five-session reversal versus matched momentum, using the sole ledger."""
import argparse
from bisect import bisect_right
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..engine import ordinary_share_terms
from ..entry_risk import BLOCK_REASON, RAW_FLOOR1_AND_NOTICES, RAW_PRICE_FLOOR1, entry_risk_contract
from ..notice_risk import NOTICE_BLOCK_REASON, NoticeRiskView
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .continuous_momentum import fractional_allocation_contract
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .low_risk import LowRiskFeed, SKIP, WINDOW, portfolio
from .market_replay import checkpoint_used_symbols, validate_prefix_inputs
from .stock_defensive import stage_metrics

START, END = "2024-01-02", "2026-09-30"
VARIANTS = ("weekly_momentum_12_1_20", "weekly_reversal_5_20")
GUARDED_VARIANTS = tuple(signal + "_entry_floor1" for signal in VARIANTS)
NOTICE_VARIANTS = tuple(signal + "_entry_notices" for signal in VARIANTS)
VARIANTS_BY_POLICY = {None: VARIANTS, RAW_PRICE_FLOOR1: GUARDED_VARIANTS,
                      RAW_FLOOR1_AND_NOTICES: NOTICE_VARIANTS}
BASE_VARIANTS = {variant: base for group in VARIANTS_BY_POLICY.values() for variant, base in zip(group, VARIANTS)}
SOURCE_URL = "https://mysimon.rochester.edu/novy-marx/research/RRLP.pdf"


def decision_days(calendar):
    dates = [day for day in calendar if START <= day <= END]
    return dates[4::5]


def features(base):
    closes = base.closes.loc[:END].where(base.volumes.loc[:END].gt(0))
    closes = closes.where(np.isfinite(closes) & closes.gt(0))
    days = decision_days(base.calendar)
    returns = closes.pct_change(fill_method=None)
    return {
        "momentum": (closes.shift(SKIP) / closes.shift(WINDOW) - 1).reindex(days),
        "reversal": -(closes / closes.shift(5) - 1).reindex(days),
        "volatility": (returns.rolling(WINDOW, min_periods=WINDOW).std(ddof=1) * np.sqrt(252)).reindex(days),
        "median_amount": base.amounts.loc[:END].rolling(20, min_periods=20).median().reindex(days),
    }


def rank(pool, variant):
    if variant not in BASE_VARIANTS:
        raise ValueError("Unknown fixed weekly variant")
    key = "momentum" if BASE_VARIANTS[variant] == VARIANTS[0] else "reversal"
    return sorted([{**row, "score": row[key]} for row in pool],
                  key=lambda row: (-row["score"], row["symbol"]))


def action_priorities(ranks, actions, states):
    """Data acquisition only: recent fixed ranks, never action-quality selection."""
    days = sorted(ranks.date.unique())
    groups = ranks.groupby("date").symbol.apply(set).to_dict()
    used = [checkpoint_used_symbols(state) for state in states]
    priorities, deferred = [], []
    unknown = actions.loc[actions.dividReserveToStockPs.eq("")
                          & actions.dividOperateDate.between(START, END)]
    for row in unknown.to_dict("records"):
        i = bisect_right(days, row["dividRegistDate"])
        if not any(row["symbol"] in groups[day] for day in days[max(0, i - 2):i]):
            continue
        safe = not any(row["dividOperateDate"] <= state["day"] and row["symbol"] in involved
                       for state, involved in zip(states, used))
        (priorities if safe else deferred).append(row)
    return sorted(priorities, key=lambda r: (r["dividOperateDate"], r["symbol"])), deferred


def repair_sources(run_folder, primary_event_view, reuse_root, *, execute=False):
    from .action_events import amend_secondary
    from .action_terms import collect

    run_folder = Path(run_folder).resolve()
    record = json.loads((run_folder / "run.json").read_text(encoding="utf-8"))
    if (record.get("research_family") != "weekly_reversal"
            or record.get("status") != "failed_weekly_reversal_diagnostic"
            or record["protocol"]["signals"] not in [list(group) for group in VARIANTS_BY_POLICY.values()]
            or [r["signal"] for r in record["results"]] != record["protocol"]["signals"]):
        raise ValueError("Source repair requires the actual failed fixed weekly pair")
    root = Path(record["artifacts"])
    ranks = pd.read_csv(root / "source_target_ranks.csv", dtype={"date": str, "symbol": str})
    states = [json.loads((root / signal / "account.json").read_text(encoding="utf-8"))
              for signal in record["protocol"]["signals"]]
    actions = pd.read_parquet(Path(record["inputs"]["event_view"]) / "canonical_actions.parquet").fillna("")
    priorities, deferred = action_priorities(ranks, actions, states)
    reuse_root = Path(reuse_root).resolve()
    reused = json.loads((reuse_root / "result.json").read_text(encoding="utf-8"))
    archived = {r["symbol"] for r in reused["records"] if r["status"] == "source_histories_compared"}
    fresh = list(dict.fromkeys(r["symbol"] for r in priorities if r["symbol"] not in archived))
    selected = fresh[:64]
    keys = {(r["symbol"], r["dividOperateDate"]) for r in priorities}
    output = {"source_run": str(run_folder), "priority_events": len(priorities),
              "priority_symbols": len({r["symbol"] for r in priorities}), "deferred_past_events": len(deferred),
              "fresh_remaining": len(fresh), "selected_fresh": selected,
              "basis": "Union of prior two fixed decision rank30 sets at registration. Acquisition priority only; does not prove actual holdings or exhaust unsold exits. Never filter strategy on future action completeness",
              "execution_admitted": False}
    if not execute:
        return output
    if selected:
        relevant_archived = sorted({r["symbol"] for r in priorities} & archived)
        source_root, sources = collect(primary_event_view, selected + relevant_archived,
                                       reuse_root=reuse_root, retain_archived=False)
    else:
        source_root, sources = reuse_root, reused
    matched = sorted({(c["symbol"], c["date"]) for r in sources["records"]
                      for c in r.get("candidates", []) if (c["symbol"], c["date"]) in keys})
    output.update(source_root=str(source_root), matched_events=len(matched), source_errors=sources["source_errors"])
    if matched:
        events, references, _, _ = amend_secondary(record["inputs"]["event_view"],
            record["inputs"]["reference_view"], source_root, matched)
        output.update(event_view=str(events), reference_view=str(references))
    receipt_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    write_json(Path(source_root) / ("weekly_repair_" + receipt_id + ".json"), output)
    return output


class WeeklyFeed:
    def __init__(self, base, pools, config, variant, fractional_allocation=None, entry_risk_policy=None,
                 risk_notice_view=None):
        if variant not in BASE_VARIANTS:
            raise ValueError("Unknown fixed weekly variant")
        entry_risk_contract(entry_risk_policy, risk_notice_view)
        if variant not in VARIANTS_BY_POLICY[entry_risk_policy]:
            raise ValueError("Weekly signal identity must match its fixed entry-risk policy")
        self.base, self.config, self.variant = base, config, variant
        self.entry_risk_policy = entry_risk_policy
        self.risk_notice_view = risk_notice_view
        self.fractional_allocation = fractional_allocation_contract(fractional_allocation)
        self.audit, self.ranking_rows, self.planned = [], [], {}
        for day in decision_days(base.calendar):
            pool = pools[day]
            if isinstance(pool, SignalUnavailable):
                self.planned[day] = (pool.reason, None, None)
                continue
            ranked = rank(pool, variant)
            self.planned[day] = ("five_session_ranking" if ranked else "known_empty_eligible_pool", len(pool), ranked)
            self.ranking_rows.extend({"date": day, "variant": variant, "rank": i, **row}
                                     for i, row in enumerate(ranked[:config.retain_rank], 1))

    def __getattr__(self, key):
        return getattr(self.base, key)

    def ordinary_share_allocation_receipt(self, action, quantity, context):
        contract = self.fractional_allocation
        if contract is None:
            return None
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Fractional-share assumption is historical-replay-only")
        terms = ordinary_share_terms(action)
        exact = quantity * (Decimal(terms["dividStocksPs"]) + Decimal(terms["dividReserveToStockPs"]))
        rounding = ROUND_FLOOR if contract["rounding"] == "floor" else ROUND_CEILING
        return {"kind": "ordinary_share_allocation", "account_id": context.account_id,
                "mode": context.mode.value, "registered_quantity": quantity, "action_terms": terms,
                "allocated_shares": int(exact.to_integral_value(rounding=rounding)),
                "source_kind": "explicit_replay_assumption", "source_ref": contract["id"],
                "allocation_policy": contract["id"]}

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != self.config:
            raise ValueError("Weekly research is replay-only with unchanged portfolio/cost rules")
        if not START <= day <= END or day not in self.base.day_index:
            raise ValueError("Weekly decision outside the fixed calendar")
        reason, size, candidates = self.planned.get(day, ("not_five_session_close", None, None))
        self.audit.append({"date": day, "variant": self.variant, "decision": reason,
                           "eligible_count": size,
                           "candidate_count": len(candidates) if candidates is not None else None,
                           "intent": "NO_CHANGE" if candidates is None else "FULL_TARGET"})
        if candidates is None:
            return SignalUnavailable(reason)
        return SignalFrame.from_legacy(candidates, day, "research:" + self.variant, context)


def run(price_view, event_view, reference_view, listing_master, source_pdf,
        initial_cash=140000, *, fractional_allocation=None, resume_run=None, entry_risk_policy=None,
        risk_notices=None):
    config = portfolio(initial_cash)
    notice_view = NoticeRiskView(risk_notices) if risk_notices is not None else None
    entry_contract = entry_risk_contract(entry_risk_policy, notice_view)
    variants = VARIANTS_BY_POLICY[entry_risk_policy]
    source_pdf = Path(source_pdf).resolve()
    with source_pdf.open("rb") as stream:
        if stream.read(4) != b"%PDF":
            raise ValueError("Expected the saved author PDF")
    base = BulkResearchFeed(price_view, event_view, reference_view, listing_master)
    try:
        if (base.manifest["scope"] != BULK_SCOPE or START not in base.calendar
                or END not in base.calendar or base.manifest["end"] < END):
            raise ValueError("Require covered aligned full-source stock inputs")
        dates = [day for day in base.calendar if START <= day <= END]
        inputs = {key: base.manifest[key] for key in ("price_view", "event_view", "reference_view",
            "raw_view", "listing_master", "source_codes", "canonical_codes", "unsupported_instruments", "limitations")}
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        out = ROOT / "outputs/market_replay" / identifier
        artifacts = DATA_ROOT / "research/market_replay" / identifier
        out.mkdir(parents=True, exist_ok=False)
        artifacts.mkdir(parents=True, exist_ok=False)
        fraction = fractional_allocation_contract(fractional_allocation)
        protocol = {"id": ("five_session_reversal_momentum_pair_entry_notices_v3" if notice_view else
                           "five_session_reversal_momentum_pair_entry_floor1_v2" if entry_contract
                           else "five_session_reversal_momentum_pair_v1"), "signals": list(variants),
            "portfolio": config.to_dict(), "signal_portfolio_configs": {s: config.to_dict() for s in variants},
            "bulk_inputs": inputs, "decision_cadence": "every_five_sessions_anchored_20240102",
            "source": {"url": SOURCE_URL, "local_pdf": str(source_pdf),
                "review": "Jan26,2023 author PDF sections2-3 and relevant appendix methodology reviewed; not a full-paper replication"},
            "common_pool": "Same supported ordinary SH/SZ pool for both: active non-ST, age365, complete252-return volatility,252-minus21 momentum,five-session return and20-day median amount>=20mCNY. No future action-completeness filter",
            "features": "No halted/missing quotes filled. Reversal=-(P[t]/P[t-5]-1); momentum=P[t-21]/P[t-252]-1; finite positive252 daily-return sample volatility required for common comparability, not used as a rank",
            "selection": "Five exchange sessions anchored to inception; first signal on session5, then10,15 etc; no forced final partial-week rebalance. Descending score/symbol tie-break;20 holdings/rank30 retention, equal4percent requested weights/gross80percent. No required negative-return or positive-momentum sign",
            "execution": "Same Engine/Ledger; completed close signal/next-session raw open, stock T1/100-share lots/limits/participation/actions and configured costs. Cash earns zero. No broker binding",
            "hypothesis": "Fixed five-day reversal may differ in net Sharpe/drawdown from the matched weekly momentum; weekly turnover costs can negate a signal. Retain negatives and failures without tuning",
            "paper_differences": "Long-only20-stock weekly/rank-buffer diagnostic, not value-weighted US long-short quintile sorts. No earnings-announcement adjustment or industry-relative return, so fundamental news and industry trends remain confounds. No independently verified turnover denominator: do not claim the paper's liquidity-conditioned strategy",
            "literature_lesson": "Paper finds volatility affects reversal speed and turnover persistence; low turnover does not promise stronger first-week returns. Its authors warn standalone reversal turnover can be impractical. This baseline is needed before any news/industry-adjusted or natural-rebalance filter",
            "basis": "Already-inspected2024-2026 historical diagnostic, not untouched/prospective validation",
            "fractional_allocation": fraction, "tuning": False, "fresh_holdout_claimed": False,
            "selection_is_point_in_time": False, "execution_admitted": False,
            "limitations": [*base.manifest["limitations"],
                "Not independently all A-shares/ETF/BSE or PIT constituent coverage",
                "Price-derived formation returns are not independently verified dividend-reinvestment total returns",
                "No black-swan drawdown guarantee, profitability claim or broker readiness"]}
        if fraction:
            protocol["limitations"].extend([fraction["basis"], fraction["limits"]])
        if entry_contract:
            protocol.update(entry_risk_policy=entry_contract,
                basis="Post-incident design after inspecting the original600321 delisting failure and2024-2026 data; not independent/untouched/prospective validation")
            protocol["limitations"].append(entry_contract["limits"])
        if notice_view:
            protocol["basis"] = "Post-incident price/notice entry restrictions after inspecting600321 and600811 failures; one explicitly selected seed notice, not complete market/PIT or independent validation"
            protocol["limitations"].append(notice_view.binding["time_basis"])
        record = {"status": "running", "research_family": "weekly_reversal",
            "purpose": "fixed_five_session_reversal_momentum_diagnostic", "dataset": str(base.folder.resolve()),
            "artifacts": str(artifacts.resolve()), "scope": BULK_SCOPE, "symbols": len(base.symbols),
            "start": START, "end": END, "sessions": len(dates), "inputs": inputs,
            "protocol": protocol, "results": [], "strategy_validated": False, "live_ready": False,
            "started_at": datetime.now().astimezone().isoformat()}
        checkpoints, prefixes = {}, {}
        if resume_run is not None:
            previous = json.loads((Path(resume_run) / "run.json").read_text(encoding="utf-8"))
            if (previous.get("status") != "failed_weekly_reversal_diagnostic"
                    or previous.get("research_family") != record["research_family"]
                    or [row["signal"] for row in previous["results"]] != list(variants)
                    or any(previous["protocol"].get(k) != v for k, v in protocol.items()
                           if k not in {"bulk_inputs", "limitations"})):
                raise ValueError("Resume requires the same failed fixed weekly pair and unchanged rules")
            for case in previous["results"]:
                signal = case["signal"]
                checkpoint = load_replay_checkpoint(Path(previous["artifacts"]) / signal, config, dates,
                                                    entry_risk_policy=entry_risk_policy, risk_notice_view=notice_view)
                validate_prefix_inputs(previous["inputs"], inputs, checkpoint["state"]["day"], checkpoint["state"])
                prefix = pd.read_csv(Path(previous["artifacts"]) / (signal + "_decisions.csv"))
                prefix = prefix.loc[prefix.date.le(checkpoint["state"]["day"])].copy()
                if prefix.date.tolist() != dates[:len(checkpoint["daily"])]:
                    raise ValueError("Saved weekly decisions differ from the continued ledger")
                checkpoints[signal], prefixes[signal] = checkpoint, prefix.to_dict("records")
            record["resume_run"] = str(Path(resume_run).resolve())
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        try:
            print("Compute fixed whole-source five-session ranks", flush=True)
            values = features(base)
            pool_feed = LowRiskFeed(base, values, "momentum_12_1_20", END, diagnostic_extension=True)
            pools = {day: pool_feed.pool(day, config) for day in decision_days(base.calendar)}
            feeds = {s: WeeklyFeed(base, pools, config, s, fractional_allocation, entry_risk_policy, notice_view)
                     for s in variants}
            ranks = [row for feed in feeds.values() for row in feed.ranking_rows]
            pd.DataFrame(ranks).to_csv(artifacts / "source_target_ranks.csv", index=False)
            symbols = sorted({row["symbol"] for row in ranks})
            write_json(artifacts / "source_plan.json", {"start": START, "end": END,
                "source_symbols": symbols, "rank_rows": len(ranks),
                "basis": "Full-universe fixed ranks for data acquisition only; not a universe cap or executed holdings",
                "execution_admitted": False})
            for signal, feed in feeds.items():
                print(f"Replay {signal}; common{len(decision_days(base.calendar))} decision closes", flush=True)
                feed.audit.extend(prefixes.get(signal, []))
                try:
                    result = baseline_ledger_result(feed, config, dates, artifacts / signal,
                                                   checkpoint=checkpoints.get(signal))
                    result.update(signal=signal, label=signal,
                                  decisions=dict(Counter(row["decision"] for row in feed.audit)))
                    if fraction:
                        result.update(label=signal + f" / fractional shares {fractional_allocation} ASSUMPTION",
                                      fractional_allocation_assumption=fraction)
                    if result["status"] == "completed":
                        result["annual_same_account"] = stage_metrics(artifacts / signal, initial_cash)
                    if entry_contract:
                        state = json.loads((artifacts / signal / "account.json").read_text(encoding="utf-8"))
                        blocks = [event for event in state["events"]
                                  if event.get("reason") in {BLOCK_REASON, NOTICE_BLOCK_REASON}]
                        result["entry_risk_blocks"] = {"buy_attempts": len(blocks),
                            "symbols": dict(Counter(event["symbol"] for event in blocks)),
                            "basis": "Committed ledger events, not guaranteed delist avoidance"}
                        columns = ["day", "symbol", "side", "qty", "reason",
                                   "entry_risk_policy", "minimum_raw_rmb", "raw_open_rmb", "raw_preclose_rmb"]
                        if notice_view:
                            result["entry_risk_blocks"]["reasons"] = dict(Counter(event["reason"] for event in blocks))
                            columns += ["risk_event_id", "publication_on", "known_at_proxy", "risk_source_url", "risk_coverage"]
                        pd.DataFrame(blocks, columns=columns).to_csv(artifacts / signal / "entry_risk_blocks.csv", index=False)
                    record["results"].append(result)
                    write_json(out / "run.json", record)
                finally:
                    pd.DataFrame(feed.audit).to_csv(artifacts / (signal + "_decisions.csv"), index=False)
            terminal = "completed" if all(r["status"] == "completed" for r in record["results"]) else "failed"
            record["status"] = terminal + "_weekly_reversal_diagnostic"
        except Exception as error:
            record.update(status="failed_weekly_reversal_preparation", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            record["completed_at"] = datetime.now().astimezone().isoformat()
            write_json(out / "run.json", record)
        print(json.dumps({"output": str(out), "status": record["status"], "results": record["results"]}, ensure_ascii=False), flush=True)
        return out
    finally:
        base.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("price-view", "event-view", "reference-view", "listing-master", "source-pdf"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--initial-cash", type=float, default=140000)
    parser.add_argument("--fractional-allocation", choices=("floor", "ceil"),
                        help="Fixed replay-only remainder sensitivity; not actual account allocation")
    parser.add_argument("--resume-run", type=Path, help="Continue unchanged failed ledgers after compatible source correction")
    parser.add_argument("--entry-risk", choices=(RAW_PRICE_FLOOR1, RAW_FLOOR1_AND_NOTICES),
                        help="New fixed post-incident pair; below1CNY raw-quote buys blocked, ranks unchanged")
    parser.add_argument("--risk-notices", type=Path, help="Pinned partial primary notice view, required for the notice policy")
    args = parser.parse_args(argv)
    out = run(args.price_view, args.event_view, args.reference_view, args.listing_master, args.source_pdf,
              args.initial_cash, fractional_allocation=args.fractional_allocation, resume_run=args.resume_run,
              entry_risk_policy=args.entry_risk, risk_notices=args.risk_notices)
    return 0 if json.loads((out / "run.json").read_text(encoding="utf-8"))["status"].startswith("completed_") else 1


def sources_main(argv=None):
    parser = argparse.ArgumentParser(description="Batch data acquisition for the unchanged weekly ledger pair")
    for name in ("run", "primary-event-view", "reuse-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Collect at most64 fresh histories and publish compatible matched terms")
    args = parser.parse_args(argv)
    print(json.dumps(repair_sources(args.run, args.primary_event_view, args.reuse_root,
                                    execute=args.execute), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
