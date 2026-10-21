"""Saved 2023 value/size/activity ranks through the existing cash/share ledger."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import time
import uuid

import numpy as np
import pandas as pd

from a_stock_committee.market_data import normalize_symbol
from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, write_json
from .low_risk import portfolio
from .monthly_timing import month_ends
from .stockdb_factor_diagnostics import BLEND, DIRECTIONS, START, END


CASES = (BLEND, "small_size_common_pool_control")
SIZE = "log_float_mv_candidate"
RULE_SOURCES = {
    "seller_only_2008": "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/200809/t20080919_76432.htm",
    "tax_cut_2023_08_28": "https://m.mof.gov.cn/czxw/202308/t20230827_3904226.htm",
    "main_board_2023": "https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/c_20250612_10824490.shtml",
    "chinext_2020": "https://investor.szse.cn/knowledge/stock/chinext/t20200729_580056.html",
    "star_2019": "https://edu.sse.com.cn/tib/ysptj/c/4768085.shtml",
}
LIMITS = [
    "2023 is already inspected retrospective development, not an untouched test or current signal",
    "Retrieved valuations/listing metadata are not revision-safe PIT or independently complete membership",
    "Same finite four-feature pool for both arms; outcomes and future action quality do not filter candidates",
    "Existing raw quotes, company-action terms and sole cash/share ledger; unresolved inputs stop affected arms",
    "No implicit fractional-share allocation, unknown event zero, new backtester or second application account",
    "Modelled transfer fee, commission, slippage and participation remain assumptions, not verified broker tariffs",
    "Failed prefixes are not full-year strategy returns; compare arms only after both complete the same period",
    "No horizon/direction search, parameter fit, forced final liquidation, THS interaction or execution admission; NO_TRADE",
]


def monthly_ranks(signals, calendar, config, rename=None):
    if (signals.index.names != ["date", "symbol"] or signals.index.has_duplicates
            or set(signals.columns) != set(DIRECTIONS) | {BLEND}):
        raise ValueError("Unique saved fixed-factor scores required")
    days = signals.index.get_level_values("date")
    if not days.isin(pd.to_datetime(calendar)).all():
        raise ValueError("Saved score days are not in the pinned account calendar")
    common = signals.loc[np.isfinite(signals).all(axis=1)]
    anchors = [day for day in month_ends(calendar, END) if START <= day <= END]
    rows, counts, unavailable = [], [], {}
    alias = {}
    if rename:
        alias[normalize_symbol(rename["old"]).vt_symbol] = normalize_symbol(rename["new"]).vt_symbol
    for day in anchors:
        if calendar.index(day) + 1 >= len(calendar) or calendar[calendar.index(day) + 1] > END:
            unavailable[day] = "next_execution_session_outside_fixed_period"
            continue
        if pd.Timestamp(day) not in common.index.get_level_values("date"):
            if pd.Timestamp(day) < days.min():
                unavailable[day] = "saved_liquidity_warmup_unknown"
                continue
            raise ValueError(f"Missing fixed monthly decision pool: {day}")
        pool = common.xs(pd.Timestamp(day), level="date")
        if len(pool) < config.retain_rank:
            raise ValueError(f"Insufficient common monthly rank buffer: {day}")
        counts.append({"date": day, "common_rows": len(pool)})
        for case in CASES:
            score = pool[BLEND if case == BLEND else SIZE]
            frame = score.rename("score").reset_index().sort_values(
                ["score", "symbol"], ascending=[False, True], kind="stable").head(config.retain_rank)
            frame["security_identity"] = frame.symbol
            frame["symbol"] = frame.symbol.replace(alias)
            if frame.symbol.duplicated().any():
                raise ValueError("Canonical engine alias introduced duplicate ranks")
            frame["date"], frame["case"] = day, case
            frame["rank"] = range(1, len(frame) + 1)
            rows.extend(frame.to_dict("records"))
    if not rows:
        raise ValueError("No monthly decisions available")
    return pd.DataFrame(rows), counts, unavailable


class HistoricalFactorFeed:
    def __init__(self, base, rankings, case, config, unavailable):
        self.base, self.case, self.config = base, case, config
        self.unavailable = unavailable
        self.manifest = {**base.manifest, "start": max(START, base.calendar[0]),
                         "historical_rule_profile": "established_sh_sz_2023_with_dated_stamp_tax"}
        selected = rankings.loc[rankings.case.eq(case)]
        self.ranks = {}
        for day, batch in selected.groupby("date", sort=True):
            ordered = batch.sort_values("rank")
            if (ordered["rank"].tolist() != list(range(1, config.retain_rank + 1))
                    or not set(ordered.symbol) <= set(base.symbols)
                    or not ordered.score.is_monotonic_decreasing):
                raise ValueError("Invalid saved monthly ranks for existing engine")
            self.ranks[day] = ordered[["symbol", "score"]].to_dict("records")

    def __getattr__(self, name):
        return getattr(self.base, name)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != self.config:
            raise ValueError("Historical factors require the fixed replay-only selector")
        if day not in self.ranks:
            return SignalUnavailable(self.unavailable.get(day, "not_month_end"))
        return SignalFrame.from_legacy(self.ranks[day], day, "research:stockdb_2023:" + self.case, context)


def report(out, record):
    lines = ["# 2023 Fixed Factor Ledger Replay", "", "NO_TRADE; retrospective development, not validation.",
             f"Saved signal source: {record['source_run']}", f"Artifacts: {record['artifacts']}",
             "", "Selected arms: " + ", ".join(record["plan"]["cases"]),
             "Each uses one existing cash/share ledger, the same eligible pool, 20 names/rank30,",
             "monthly decisions, next-session execution, 80% requested gross and CNY140,000 synthetic capital.",
             "No final liquidation or December decision executing in 2024.",
             "Standard configured seller tax: 0.1% before 2023-08-28, 0.05% thereafter.",
             "Existing post-2023 fee assumptions and ETF rules are unchanged.", "",
             "| Arm | Status | Completed sessions | Net return | Max drawdown | Sharpe zero-rf 252 | Fees CNY |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in record["results"]:
        value = lambda key: f"{row[key]:.6f}" if row.get(key) is not None else "not available"
        lines.append(f"| {row['signal']} | {row['status']} | {row['completed_sessions']} | "
                     f"{value('net_return')} | {value('max_drawdown')} | {value('sharpe_zero_rf_252')} | {value('fees')} |")
        if row.get("error"):
            lines.append(f"\nBlocked at {row['attempted_session']}, last completed {row['last_completed_session']}: {row['error']}\n")
    if record.get("comparison"):
        lines.append(f"\nFull-period blend minus size-control net return: {record['comparison']['net_difference']:.6f}.")
    if record.get("error"):
        lines.append("\nSetup failure: " + record["error"])
    lines += ["", *["- " + item for item in LIMITS], "", "## Historical Rule Sources", ""]
    lines += [f"- [{name}]({url})" for name, url in RULE_SOURCES.items()]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_run, event_view, reference_view, cases=CASES):
    cases = tuple(cases)
    if not cases or len(set(cases)) != len(cases) or not set(cases) <= set(CASES):
        raise ValueError("Choose unique existing fixed replay arms; no new signal")
    source_run = Path(source_run).resolve()
    source = json.loads((source_run / "result.json").read_text(encoding="utf-8"))
    parent = json.loads((source_run / "plan.json").read_text(encoding="utf-8"))
    if (source.get("status") != "completed_research_diagnostics" or parent.get("scopes", {}).get("2023_full") != [START, END]
            or parent.get("directions") != DIRECTIONS or parent.get("blend", {}).get("id") != BLEND):
        raise ValueError("Completed saved2023 fixed-factor diagnostic source required")
    config = portfolio()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/stockdb_factor_replay" / identifier
    out = ROOT / "outputs/stockdb_factor_replay" / identifier
    root.mkdir(parents=True, exist_ok=False)
    out.mkdir(parents=True, exist_ok=False)
    inputs = {"price_view": parent["price_view"], "listing_master": parent["universe"],
              "event_view": str(Path(event_view).resolve()), "reference_view": str(Path(reference_view).resolve())}
    plan = {"id": identifier, "source_run": str(source_run), "cases": list(cases), "start": START, "end": END,
            "portfolio": config.to_dict(), "stock_inputs": inputs, "cost_profile": "dated_stamp_tax_2023_v1",
            "selection": "common finite four inputs; monthly20/rank30; ties by symbol; next-session execution",
            "size_control": "saved oriented negative log float market value on the exact blend pool",
            "period_end": "no final target whose execution falls beyond2023; mark remaining holdings, no forced sell",
            "source_diagnostics_already_inspected": True, "fresh_holdout_claimed": False,
            "parameters_searched": False, "rule_sources": RULE_SOURCES, "limitations": LIMITS,
            "execution_admitted": False, "created_at": datetime.now().astimezone().isoformat()}
    record = {"id": identifier, "status": "running", "source_run": str(source_run), "artifacts": str(root),
              "start": START, "end": END, "results": [], "plan": plan, "execution_admitted": False}
    write_json(root / "plan.json", plan)
    shutil.copy2(__file__, root / "implementation.py")
    write_json(out / "run.json", record)
    base, begin = None, time.monotonic()
    try:
        signals = pd.read_parquet(source_run / "fixed_signals.parquet")
        base = BulkResearchFeed(**inputs)
        dates = [day for day in base.calendar if START <= day <= END]
        if len(dates) != source["calendar_sessions"]:
            raise ValueError("Saved signal/account calendar coverage changed")
        ranks, counts, unavailable = monthly_ranks(signals, base.calendar, config, base.rename)
        ranks.to_csv(root / "rankings.csv", index=False)
        pd.DataFrame(counts).to_csv(root / "pool_counts.csv", index=False)
        record.update(sessions=len(dates), pool_counts=counts, unavailable_decisions=unavailable,
                      stock_manifest={**base.manifest, "start": max(START, base.calendar[0]),
                                      "historical_rule_profile": "established_sh_sz_2023_with_dated_stamp_tax"})
        write_json(out / "run.json", record)
        print(f"Fixed common pool: {counts}; unavailable monthly decisions: {unavailable}", flush=True)
        for case in cases:
            feed = HistoricalFactorFeed(base, ranks, case, config, unavailable)
            result = {"signal": case, **baseline_ledger_result(feed, config, dates, root / case)}
            record["results"].append(result)
            write_json(out / "run.json", record)
            print(f"{case}: {result['status']} {result['completed_sessions']}/{len(dates)} net={result.get('net_return')} {result.get('error', '')}", flush=True)
        complete = all(row["status"] == "completed" for row in record["results"])
        record["status"] = "completed_factor_ledger_diagnostics" if complete else "blocked_factor_ledger_diagnostics"
        if complete and set(cases) == set(CASES):
            by_case = {row["signal"]: row for row in record["results"]}
            left, right = (by_case[case] for case in CASES)
            record["comparison"] = {"net_difference": left["net_return"] - right["net_return"],
                                    "drawdown_magnitude_difference": abs(left["max_drawdown"]) - abs(right["max_drawdown"])}
    except Exception as error:
        record.update(status="failed_factor_ledger_setup", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if base is not None:
            base.close()
        record["elapsed_seconds"] = time.monotonic() - begin
        write_json(out / "run.json", record)
        write_json(root / "result.json", record)
        report(out, record)
        print(str(out / "REPORT.md"), flush=True)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--source-run", type=Path)
    selection.add_argument("--prepare-actions", help="Terminal fixed replay ID; reuse histories for both saved rank buffers")
    parser.add_argument("--event-view", type=Path)
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--primary-view", type=Path, help="Original primary-only action scope for source matching")
    parser.add_argument("--max-fresh", type=int, default=64)
    parser.add_argument("--execute", action="store_true", help="Opt in to public action-history acquisition, never account execution")
    parser.add_argument("--case", action="append", choices=CASES,
                        help="Run an existing unblocked arm; never infer a two-arm comparison from one arm")
    args = parser.parse_args(argv)
    if args.prepare_actions:
        if not args.primary_view or not args.execute or args.event_view or args.reference_view or args.case:
            parser.error("Preparation needs --primary-view and --execute; inputs/ranks come only from the saved replay")
        from .value_portfolios import prepare_actions
        prepare_actions(args.prepare_actions, args.primary_view, args.max_fresh, stockdb=True)
        return 0
    if not args.event_view or not args.reference_view or args.primary_view or args.execute:
        parser.error("Replay needs aligned --event-view and --reference-view, not preparation options")
    result = run(args.source_run, args.event_view, args.reference_view, args.case or CASES)
    return 0 if result["status"] == "completed_factor_ledger_diagnostics" else 2


if __name__ == "__main__":
    raise SystemExit(main())
