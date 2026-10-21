"""Fixed stock / sovereign-bond / gold allocation through the sole ledger."""
import argparse
from ast import literal_eval
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import uuid

import pandas as pd

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..feed import HistoricalFeed
from ..report import metrics as portfolio_metrics
from ..rules import execution_rule
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .fund_replay_snapshot import SCOPE, FUND_SCOPE
from .low_risk import LowRiskFeed, VARIANTS, features, portfolio
from .market_replay import validate_prefix_inputs

DEFENSIVE = ("511010.SSE", "518880.SSE")
START, END = "2024-01-02", "2026-06-30"
SLEEVE_WEIGHT = .2
POLICY_FIELDS = ("id", "config", "allocation", "selection", "execution", "basis", "tuning",
                 "fresh_holdout_claimed", "execution_admitted")


def mixed_config(initial_cash=140000):
    return replace(portfolio(initial_cash), holdings=22, retain_rank=32).validate()


class MixedRows:
    def __init__(self, stocks, funds):
        self.stocks, self.funds = stocks, funds

    def get(self, day, default=None):
        rows = dict(self.stocks.rows.get(day, {}))
        rows.update({s: r for s, r in self.funds.rows.get(day, {}).items() if s in DEFENSIVE})
        return rows if rows else default


class MixedResearchFeed:
    """Join quotes/profiles/actions, never accounts or independently simulated curves."""
    def __init__(self, stocks, funds, end):
        if (stocks.manifest["scope"] != BULK_SCOPE or not stocks.manifest.get("replay_only")
                or funds.manifest["scope"] not in {SCOPE, FUND_SCOPE}
                or not funds.manifest.get("replay_only")
                or end > min(stocks.manifest["end"], funds.manifest["end"])
                or START not in stocks.calendar or end not in stocks.calendar):
            raise ValueError("Require aligned research stock/fund sources within both coverage windows")
        days = [d for d in stocks.calendar if START <= d <= end]
        if days != [d for d in funds.calendar if START <= d <= end]:
            raise ValueError("Mixed research cannot silently intersect or omit exchange sessions")
        if set(DEFENSIVE) & set(stocks.symbols) or not set(DEFENSIVE) <= set(funds.instrument_profiles):
            raise ValueError("Both predeclared defensive funds need distinct explicit profiles")
        self.stocks, self.funds, self.end = stocks, funds, end
        self.calendar = stocks.calendar
        self.symbols = list(stocks.symbols) + list(DEFENSIVE)
        self.instruments = {**stocks.instruments, **{s: funds.instruments[s] for s in DEFENSIVE}}
        self.instrument_profiles = {s: funds.instrument_profiles[s] for s in DEFENSIVE}
        for s in DEFENSIVE:
            execution_rule(s, START, self.instrument_profiles)
            execution_rule(s, end, self.instrument_profiles)
            if any(s not in funds.rows.get(day, {}) for day in days):
                raise ValueError(f"Missing defensive raw quote: {s}")
        self.volumes = pd.concat([stocks.volumes, funds.volumes.loc[:, list(DEFENSIVE)]], axis=1)
        self.rows = MixedRows(stocks, funds)
        self.actions = stocks.actions + [a for a in funds.actions if a["symbol"] in DEFENSIVE]
        self.manifest = {**stocks.manifest, "scope": "full_source_stocks_plus_two_defensive_funds_research_only",
            "end": end, "fund_dataset": str(funds.folder), "execution_admitted": False,
            "limitations": [*stocks.manifest["limitations"],
                "Only the two predeclared reviewed defensive funds; not all ETF coverage",
                "Fund reference/status fields retain the reviewed snapshot's calculated research basis",
                "Primary cash coverage does not independently establish complete fund share actions",
                "No prospective/independent validation, current signal or broker connection"]}

    def __getattr__(self, name):
        return getattr(self.stocks, name)

    def validate_session(self, state, day):
        if not START <= day <= self.end or day not in self.funds.calendar:
            raise ValueError("Mixed replay outside the joint covered period")
        self.stocks.validate_session(state, day)


class StockDefensiveFeed:
    def __init__(self, base, values, variant, initial_cash, *, stock_signal=None):
        self.base, self.variant = base, variant
        self.stock_config = portfolio(initial_cash)
        self.stock_signal = (stock_signal if stock_signal is not None else
                             LowRiskFeed(base.stocks, values, variant, base.end, diagnostic_extension=True))
        self.audit = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != mixed_config(self.stock_config.initial_cash):
            raise ValueError("Fixed stock-defensive allocation is replay-only with its declared config")
        frame = self.stock_signal.signal_frame(day, self.stock_config, context)
        if isinstance(frame, SignalUnavailable):
            return frame
        # Reserve two admitted slots. Shifting stock ranks by two and rank30->32
        # preserves the engine's original twenty-stock rank-buffer selection.
        candidates = [{"symbol": s, "score": float(2 - i), "fixed_defensive_slot": 1}
                      for i, s in enumerate(DEFENSIVE)]
        candidates.extend({"symbol": row["symbol"], "score": float(-rank),
                           "original_stock_score": row["score"]}
                          for rank, row in enumerate(frame.ranked_candidates(), start=1))
        return SignalFrame.from_legacy(candidates, day, "research:balanced_40_20_20:" + self.variant, context)

    def allocate(self, intent):
        admitted = dict(intent.weights)
        if not set(DEFENSIVE) <= set(admitted):
            raise ValueError("The selector must admit both fixed defensive slots before allocation")
        weights = {s: SLEEVE_WEIGHT if s in DEFENSIVE else .02 for s in admitted}
        self.audit.extend({"date": intent.decided_at[:10], "symbol": s, "target_weight": w}
                          for s, w in weights.items())
        return replace(intent, weights=tuple(weights.items()))


def saved_stock_controls(run_folder, stocks, config, dates, variants):
    """Read a completed same-input prefix, without replaying or combining its curve."""
    run_folder = Path(run_folder)
    record = json.loads((run_folder / "run.json").read_text(encoding="utf-8"))
    keys = ("price_view", "raw_view", "listing_master",
            "source_codes", "canonical_codes", "unsupported_instruments")
    stock_control = (record["status"] == "completed_low_risk_diagnostic"
                     and record["research_family"] == "low_risk_stock_selection"
                     and record["protocol"]["id"] == "low_risk_price_volume_ablation_v1")
    continuous_control = (record["status"] in {"completed_continuous_momentum_diagnostic", "failed_continuous_momentum_diagnostic"}
                          and record["research_family"] == "continuous_information_momentum"
                          and record["protocol"]["id"] == "continuous_information_momentum_quintiles_v1"
                          and tuple(variants) == ("momentum_12_1_20",))
    if (not (stock_control or continuous_control)
            or record["protocol"]["portfolio"] != portfolio(config.initial_cash).to_dict()
            or any(record["inputs"][key] != stocks.manifest[key] for key in keys)
            or record["start"] != dates[0] or record["end"] < dates[-1]):
        raise ValueError("Controls need completed stock accounts on the exact same source/rule/cost inputs")
    results = []
    for variant in variants:
        case = next(row for row in record["results"] if row["signal"] == variant)
        if case["status"] != "completed":
            raise ValueError("A failed prefix is not a completed stock comparison")
        folder = Path(record["artifacts"]) / variant
        daily = pd.read_csv(folder / "daily.csv", converters={"stale_symbols": literal_eval})
        selected = daily.loc[daily.date.between(dates[0], dates[-1])]
        if selected.date.tolist() != dates:
            raise ValueError("Stock control prefix does not cover the exact joint session list")
        state = json.loads((folder / "account.json").read_text(encoding="utf-8"))
        ignored = validate_prefix_inputs(record["inputs"], stocks.manifest, record["end"], state)
        results.append({"signal": variant, "allocation": "stock80_cash20", "status": "completed",
                        "basis": "read_only_metrics_of_completed_same_account_prefix",
                        "input_compatibility": "existing prefix guard; only uninvolved economic terms may differ",
                        "uninvolved_source_changes": ignored,
                        "source_run": str(run_folder.resolve()), "source_daily": str(folder / "daily.csv"),
                        "annual_same_account": stage_metrics(folder, config.initial_cash, end=dates[-1]),
                        **portfolio_metrics(selected.to_dict("records"), config.initial_cash)})
    return results


def stage_metrics(folder, initial_cash, *, end=None):
    daily = pd.read_csv(folder / "daily.csv", converters={"stale_symbols": literal_eval})
    if end is not None:
        daily = daily.loc[daily.date.le(end)]
    rows = []
    for year in ("2024", "2025", "2026"):
        selected = daily.loc[daily.date.str.startswith(year)]
        if len(selected):
            first = selected.index[0]
            opening = float(daily.loc[first - 1, "equity"]) if first else initial_cash
            rows.append({"year": year, "opening_equity": opening, "account_reset": False,
                         **portfolio_metrics(selected.to_dict("records"), opening)})
    return rows


def validate_fund_prefix(before, after, last_day):
    days = [d for d in before.calendar if d <= last_day]
    if days != [d for d in after.calendar if d <= last_day]:
        raise ValueError("Fund continuation calendar prefix changed")
    fields = ["date", "symbol", "open", "high", "low", "close", "preclose", "volume", "amount",
              "tradestatus", "isST", "adjustflag"]
    def quotes(feed):
        return (feed.raw.loc[feed.raw.symbol.isin(DEFENSIVE) & feed.raw.date.le(last_day), fields]
                .sort_values(["date", "symbol"]).reset_index(drop=True))
    try:
        pd.testing.assert_frame_equal(quotes(before), quotes(after), check_dtype=False, check_exact=True)
    except AssertionError as error:
        raise ValueError("Fund continuation raw quote/activity/reference prefix changed") from error
    for symbol in DEFENSIVE:
        old, new = before.instruments[symbol], after.instruments[symbol]
        if any(old.get(k) != new.get(k) for k in ("ipoDate", "outDate", "instrument_type", "board")):
            raise ValueError("Fund continuation instrument identity changed")
        if any(execution_rule(symbol, day, before.instrument_profiles) !=
               execution_rule(symbol, day, after.instrument_profiles) for day in days if day >= START):
            raise ValueError("Fund continuation historical execution rules changed")
    dates = ("dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate")
    amounts = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
    def amount(action, field):
        try:
            value = Decimal(str(action.get(field, "")))
        except InvalidOperation as error:
            raise ValueError("Fund continuation economic amount is unknown") from error
        if not value.is_finite():
            raise ValueError("Fund continuation economic amount is unknown")
        return value
    def actions(feed):
        return sorted((tuple(a.get(k, "") for k in ("symbol", *dates)) +
                       tuple(amount(a, k) for k in amounts) for a in feed.actions
                       if a["symbol"] in DEFENSIVE and any(a.get(k) and a[k] <= last_day for k in dates)),
                      key=lambda row: (row[0], row[2]))
    if actions(before) != actions(after):
        raise ValueError("Fund continuation economic action/entitlement prefix changed")
    return {"last_preserved_date": last_day, "funds": list(DEFENSIVE), "unchanged_observed_prefix": True,
            "basis": "Same calendar/raw prices/activity/reference/actions and dated rule semantics; profile IDs may extend coverage. Not independent source or complete share-action admission"}


def continuation_cases(run_folder, base, protocol, variants):
    if run_folder is None:
        return {}, None
    run_folder = Path(run_folder).resolve()
    record = json.loads((run_folder / "run.json").read_text(encoding="utf-8"))
    previous = record["protocol"]
    if (record["status"] not in {"completed_stock_defensive_diagnostic", "failed_stock_defensive_diagnostic"}
            or any(previous.get(key) != protocol[key] for key in POLICY_FIELDS)
            or previous["start"] != protocol["start"] or previous["end"] >= protocol["end"]
            or not set(variants) <= set(previous["variants"])):
        raise ValueError("Continuation requires a terminal shorter fixed mixed comparison with unchanged policy")
    before = HistoricalFeed(previous["fund_dataset"])
    funds = validate_fund_prefix(before, base.funds, previous["end"])
    dates = [d for d in base.calendar if protocol["start"] <= d <= protocol["end"]]
    contract = {key: protocol[key] for key in POLICY_FIELDS}
    checkpoints = {}
    for variant in variants:
        case = next(row for row in record["results"] if row["signal"] == variant)
        if case["status"] not in {"completed", "failed"}:
            raise ValueError("Mixed continuation case must have an actual terminal account")
        folder = Path(case.get("source_case_folder", Path(record["artifacts"]) / variant))
        checkpoint = load_replay_checkpoint(folder, mixed_config(), dates,
            portfolio_policy_contract=contract, legacy_policy_protocol=run_folder / "protocol.json")
        source_inputs = case.get("source_stock_inputs", previous["stock_inputs"])
        changes = validate_prefix_inputs(source_inputs, base.stocks.manifest, checkpoint["state"]["day"], checkpoint["state"])
        if case["completed_sessions"] != len(checkpoint["daily"]):
            raise ValueError("Mixed case result count does not match its actual checkpoint")
        checkpoint["uninvolved_source_changes"] = changes
        checkpoints[variant] = checkpoint
    return checkpoints, funds


def reuse_completed_cases(run_folder, base, protocol, variants):
    if run_folder is None:
        return {}
    run_folder = Path(run_folder).resolve()
    record = json.loads((run_folder / "run.json").read_text(encoding="utf-8"))
    fields = ("id", "start", "end", "sessions", "config", "fund_dataset", "fund_profiles",
              "allocation", "selection", "execution", "basis", "tuning", "fresh_holdout_claimed", "execution_admitted")
    if (record["status"] not in {"completed_stock_defensive_diagnostic", "failed_stock_defensive_diagnostic"}
            or any(record["protocol"][key] != protocol[key] for key in fields)):
        raise ValueError("Reuse requires a terminal fixed mixed comparison with unchanged allocation/rules/dates/funds")
    reused = {}
    for case in record["results"]:
        variant = case["signal"]
        if variant not in variants or case["status"] != "completed":
            continue
        if variant in reused or case["completed_sessions"] != protocol["sessions"]:
            raise ValueError("Only unique complete mixed accounts can be reused")
        folder = Path(case.get("source_case_folder", Path(record["artifacts"]) / variant))
        receipt = json.loads((folder / "replay.json").read_text(encoding="utf-8"))
        state = json.loads((folder / "account.json").read_text(encoding="utf-8"))
        if (receipt["status"] != "completed" or receipt["config"] != protocol["config"]
                or not receipt.get("portfolio_policy_enabled") or receipt["completed_sessions"] != protocol["sessions"]
                or state["day"] != protocol["end"] or state.get("broker_binding")):
            raise ValueError("Saved mixed result is not its terminal unbound fixed-policy account")
        source_inputs = case.get("source_stock_inputs", record["protocol"]["stock_inputs"])
        ignored = validate_prefix_inputs(source_inputs, base.stocks.manifest, protocol["end"], state)
        reused[variant] = {**case, "source_run": str(run_folder), "source_case_folder": str(folder.resolve()),
                           "source_stock_inputs": source_inputs, "uninvolved_source_changes": ignored,
                           "result_reuse": "read_only_completed_account_metrics_no_replay_or_new_account",
                           "reused_sessions": protocol["sessions"], "newly_completed_sessions": 0}
    return reused


def run(price_view, event_view, reference_view, listing_master, fund_dataset, stock_baseline_run,
        *, end=END, variants=VARIANTS, reuse_run=None, continue_run=None):
    variants = tuple(variants)
    if not variants or len(set(variants)) != len(variants) or not set(variants) <= set(VARIANTS):
        raise ValueError("Choose only unique existing fixed stock variants")
    if reuse_run is not None and continue_run is not None:
        raise ValueError("Choose same-period result reuse or account continuation, not both")
    config = mixed_config()
    stocks = BulkResearchFeed(price_view, event_view, reference_view, listing_master)
    try:
        funds = HistoricalFeed(fund_dataset)
        base = MixedResearchFeed(stocks, funds, end)
        dates = [d for d in base.calendar if START <= d <= end]
        controls = saved_stock_controls(stock_baseline_run, stocks, config, dates, variants)
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        out = ROOT / "outputs/market_replay" / identifier
        artifacts = DATA_ROOT / "research/market_replay" / identifier
        out.mkdir(parents=True, exist_ok=False)
        artifacts.mkdir(parents=True, exist_ok=False)
        protocol = {"id": "stock_defensive_fixed_40_20_20_v1", "variants": variants,
            "start": START, "end": end, "sessions": len(dates), "config": config.to_dict(),
            "stock_inputs": stocks.manifest, "fund_dataset": str(funds.folder.resolve()),
            "fund_profiles": base.instrument_profiles, "stock_baseline_run": str(Path(stock_baseline_run).resolve()),
            "allocation": "Fixed40percent stock sleeve:20names each2percent;20percent511010 sovereign-bond ETF;20percent518880 gold ETF;20percentzero-yield cash. Fewer eligible or unfilled stock names leave extra cash; no redistribution or weight search",
            "selection": "Unchanged three stock rankings/20names/rank30buffer/complete-feature eligibility. Two fixed defensive candidate slots, selector22names/rank32, then bounded typed allocation; fund ordinal slots are not alpha/return forecasts",
            "execution": "One existing Engine/Ledger per candidate, shared cash across stocks/funds; raw next-session open, original stock/fund lots/T1/T0/mill prices/fees/tax/slippage/participation. Missing stock signal is NO_CHANGE for the entire portfolio; no assumed blocked fills/retries",
            "basis": "Local predeclared50/50 split of risky stock and two equal defensive sleeves at80percentgross, not an original-paper formula or independent selection. Rules fixed before these mixed accounts; prior stock/fund outcomes already inspected",
            "controls": "Read-only common-date metrics from completed stock80/cash20 accounts; existing prefix compatibility guard rejects changed prices/costs/involved economic terms. Additional uninvolved source terms are explicit, not silently identical snapshots. No rerun, synthetic weighted curve, fund subaccount or reset wealth",
            "comparison_limitations": "Changing allocation changes exposure and lot affordability, not just alpha. Gold/bonds have their own risk; lower drawdown alone is not excess performance. Comparison ends at the joint pinned stock/fund coverage, not inferred future data",
            "tuning": False, "fresh_holdout_claimed": False, "execution_admitted": False,
            "limitations": base.manifest["limitations"]}
        write_json(out / "protocol.json", protocol)
        record = {"status": "running", "protocol": protocol, "artifacts": str(artifacts),
                  "reuse_run": str(Path(reuse_run).resolve()) if reuse_run else None,
                  "continue_run": str(Path(continue_run).resolve()) if continue_run else None,
                  "fund_prefix_compatibility": None,
                  "stock_controls": controls, "results": [], "strategy_validated": False, "live_ready": False}
        write_json(out / "run.json", record)
        try:
            reused = reuse_completed_cases(reuse_run, base, protocol, variants)
            checkpoints, fund_prefix = continuation_cases(continue_run, base, protocol, variants)
            record["fund_prefix_compatibility"] = fund_prefix
            write_json(out / "run.json", record)
            values = features(stocks, end)
            for variant in variants:
                if variant in reused:
                    record["results"].append(reused[variant])
                    write_json(out / "run.json", record)
                    print(f"{variant}: reused completed{len(dates)} sessions; no replay", flush=True)
                    continue
                wrapper = StockDefensiveFeed(base, values, variant, config.initial_cash)
                result = baseline_ledger_result(wrapper, config, dates, artifacts / variant,
                    portfolio_policy=wrapper.allocate, checkpoint=checkpoints.get(variant),
                    portfolio_policy_contract={key: protocol[key] for key in POLICY_FIELDS})
                result.update(signal=variant, allocation="stock40_bond20_gold20_cash20")
                if variant in checkpoints:
                    checkpoint = checkpoints[variant]
                    result["uninvolved_source_changes"] = checkpoint["uninvolved_source_changes"]
                    result["source_prefix_folder"] = checkpoint["source"]
                if result["status"] == "completed":
                    result["annual_same_account"] = stage_metrics(artifacts / variant, config.initial_cash)
                    if variant in checkpoints and result["completed_sessions"] > len(checkpoint["daily"]):
                        daily = pd.read_csv(artifacts / variant / "daily.csv", converters={"stale_symbols": literal_eval})
                        added = daily.iloc[len(checkpoint["daily"]):]
                        opening = checkpoint["daily"][-1]["equity"] if checkpoint["daily"] else config.initial_cash
                        result["continued_stage"] = {"start": added.date.iloc[0], "end": added.date.iloc[-1],
                            "opening_equity": opening, "account_reset": False,
                            **portfolio_metrics(added.to_dict("records"), opening)}
                pd.DataFrame(wrapper.audit).to_csv(artifacts / f"{variant}_targets.csv", index=False)
                pd.DataFrame(wrapper.stock_signal.audit).to_csv(artifacts / f"{variant}_stock_decisions.csv", index=False)
                record["results"].append(result)
                write_json(out / "run.json", record)
                print(f"{variant}: {result['status']}; sessions={result['completed_sessions']}; return={result.get('net_return')}", flush=True)
            record["status"] = ("completed" if all(r["status"] == "completed" for r in record["results"])
                                else "failed") + "_stock_defensive_diagnostic"
            write_json(out / "run.json", record)
            report(out, record)
        except Exception as error:
            record.update(status="failed_preparation_or_execution", error=f"{type(error).__name__}: {error}")
            write_json(out / "run.json", record)
            raise
        print(out, flush=True)
        return out
    finally:
        stocks.close()


def report(out, record):
    p = record["protocol"]
    lines = ["# Fixed Stock / Bond / Gold Comparison", "", f"{record['status']}. NO_TRADE.",
             f"{p['start']} to {p['end']}; {p['sessions']}sessions; CNY140000 original capital.",
             "All source-covered stock identities remain in the original ranking; two fixed reviewed defensive funds.",
             "", "| Stock rule | Allocation | Status | Net return | Max drawdown | Sharpe zero rf |",
             "|---|---|---|---:|---:|---:|"]
    for row in record["stock_controls"] + record["results"]:
        metrics = (f"{row['net_return']:.2%} | {row['max_drawdown']:.2%} | {row['sharpe_zero_rf_252']}"
                   if row["status"] == "completed" else "unavailable | unavailable | unavailable")
        lines.append(f"| {row['signal']} | {row['allocation']} | {row['status']} | {metrics} |")
    failures = [row for row in record["results"] if row["status"] != "completed"]
    for row in failures:
        lines.extend(["", f"{row['signal']} failed after {row['completed_sessions']} sessions: {row['error']}"])
    lines += ["", "## Same-Account Annual Stages", "",
              "| Stock rule | Allocation | Year | Net return | Max drawdown | Sharpe zero rf |",
              "|---|---|---|---:|---:|---:|"]
    for row in record["stock_controls"] + record["results"]:
        for stage in row.get("annual_same_account", []):
            sharpe = f"{stage['sharpe_zero_rf_252']:.3f}" if stage['sharpe_zero_rf_252'] is not None else "n/a"
            lines.append(f"| {row['signal']} | {row['allocation']} | {stage['year']} | "
                         f"{stage['net_return']:.2%} | {stage['max_drawdown']:.2%} | {sharpe} |")
    for row in record["results"]:
        if row.get("source_case_folder"):
            lines.extend(["", f"{row['signal']}: reused completed account `{row['source_case_folder']}`; "
                          f"uninvolved source changes: {row['uninvolved_source_changes']}. No rerun."])
        if row.get("source_prefix_folder"):
            lines.extend(["", f"{row['signal']}: preserved{row['reused_sessions']} sessions from "
                          f"`{row['source_prefix_folder']}`; actually advanced{row['newly_completed_sessions']} new sessions."])
    if any(row.get("continued_stage") for row in record["results"]):
        lines += ["", "## Newly Continued Stage", "",
                  "| Stock rule | Period | Opening equity | Net return | Max drawdown | Sharpe zero rf |",
                  "|---|---|---:|---:|---:|---:|"]
        for row in record["results"]:
            stage = row.get("continued_stage")
            if stage:
                sharpe = f"{stage['sharpe_zero_rf_252']:.3f}" if stage['sharpe_zero_rf_252'] is not None else "n/a"
                lines.append(f"| {row['signal']} | {stage['start']} to {stage['end']} | {stage['opening_equity']:.2f} | "
                             f"{stage['net_return']:.2%} | {stage['max_drawdown']:.2%} | {sharpe} |")
    lines += ["", p["allocation"], p["selection"], p["execution"], p["basis"], p["controls"],
              "", "## Boundaries", p["comparison_limitations"], *p["limitations"],
              "No performance-selected weights, independent validation, profitability or crash-protection claim.",
              f"Artifacts: `{record['artifacts']}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("price-view", "event-view", "reference-view", "listing-master", "fund-dataset", "stock-baseline-run"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--end", default=END)
    parser.add_argument("--variant", action="append", choices=VARIANTS)
    previous = parser.add_mutually_exclusive_group()
    previous.add_argument("--reuse-run", type=Path, help="Reuse compatible completed cases; only failed/missing cases actually replay")
    previous.add_argument("--continue-run", type=Path, help="Carry fixed-policy accounts into later covered dates; no reset or repeated prefix")
    args = parser.parse_args(argv)
    out = run(args.price_view, args.event_view, args.reference_view, args.listing_master,
              args.fund_dataset, args.stock_baseline_run, end=args.end,
              variants=VARIANTS if args.variant is None else args.variant, reuse_run=args.reuse_run,
              continue_run=args.continue_run)
    return int(json.loads((out / "run.json").read_text())["status"] != "completed_stock_defensive_diagnostic")
