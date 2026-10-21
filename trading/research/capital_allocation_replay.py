"""Fixed close-lot and minimum-fee ablations using saved family ranks and one ledger."""
import argparse
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import time
import uuid

import pandas as pd

from ..config import StrategyConfig
from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..engine import ordinary_share_terms
from ..feed import HistoricalFeed
from ..portfolio_allocation import LOT_ALLOCATION, OPEN_LOT_ALLOCATION
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .family_portfolios import START, END, bind_stock_inputs
from .market_replay import validate_prefix_inputs
from .continuous_momentum import fractional_allocation_contract, check_fractional_continuation
from .monthly_timing import month_ends
from .stock_defensive import MixedResearchFeed, StockDefensiveFeed, stage_metrics
from ..report import metrics as portfolio_metrics


CASES = tuple(f"{prefix}_{recipe}" for recipe in ("low_vol_half_momentum_control", "equal_three_families")
              for prefix in ("stock", "mixed"))
ARMS = ("close_lots", "close_lots_fee_buffer")
PROFITABILITY_FAMILY = "economic_profitability_reporting_calendar_v1"
PROFITABILITY_CASES = ("profit_matched_low_vol", "profit_matched_low_vol_half_momentum",
                       "profitability_only", "risk_momentum_profitability")


def source_layout(source, allocation_arm="close_lots"):
    if allocation_arm not in {"close_lots", "open_lots"}:
        raise ValueError("Unknown fixed allocation arm")
    if (source["start"] != START or source["end"] != END
            or source["protocol"]["fresh_holdout_claimed"] is not False):
        raise ValueError("Completed exact development source required")
    if (source.get("status") == "completed_family_portfolio_diagnostics"
            and source.get("research_family") == "fixed_family_incremental"):
        if allocation_arm != "close_lots":
            raise ValueError("Open-lot comparison currently supports the fixed stock profitability source only")
        return CASES, ARMS, False
    if (source.get("status") != "completed_profitability_diagnostics"
            or source.get("research_family") != PROFITABILITY_FAMILY
            or source["protocol"].get("id") != PROFITABILITY_FAMILY
            or source["protocol"].get("signals") != list(PROFITABILITY_CASES)
            or set(source["protocol"]["signal_portfolio_configs"]) != set(PROFITABILITY_CASES)
            or not source.get("rankings") or not source["protocol"].get("financial_receipt")
            or not source["protocol"].get("cohort_policy")):
        raise ValueError("Completed fixed-family or reporting-calendar profitability source required")
    return PROFITABILITY_CASES, (allocation_arm,), True


def validate_control_cache(cached, source_path, source):
    if (cached.get("status") not in {"failed_capital_allocation_diagnostics", "completed_capital_allocation_diagnostics"}
            or cached.get("research_family") != "capital_lot_fee_implementation"
            or cached.get("source_run") != str(source_path)
            or cached["protocol"].get("source_rankings") != source.get("rankings")
            or cached["protocol"].get("signal_source_family", "fixed_family_incremental") != source.get("research_family")
            or any(cached.get(k) != source.get(k) for k in ("start", "end", "sessions", "symbols", "scope", "dataset"))
            or any(cached["protocol"].get(k) != source["protocol"].get(k) for k in
                   ("formulas", "financial_view", "financial_receipt", "cohort_policy"))):
        raise ValueError("Cached controls require the same fixed source, sample and financial contract")
    controls = {row["signal"]: row for row in cached["controls"]}
    if len(controls) != len(cached["controls"]):
        raise ValueError("Duplicate cached controls")
    return controls


def validate_capital_resume(previous, current):
    if (previous.get("status") not in {"failed_capital_allocation_diagnostics", "completed_capital_allocation_diagnostics"}
            or previous.get("research_family") != "capital_lot_fee_implementation"
            or any(previous.get(key) != current.get(key) for key in (
                "source_run", "start", "end", "sessions", "symbols", "scope"))):
        raise ValueError("Resume requires the same terminal capital experiment and source")
    for key in ("id", "cases", "arms", "signal_portfolio_configs", "lot_allocation",
                "fund_dataset", "fund_profiles", "family_weights", "training_directions", "initial_cash", "portfolio",
                "formulas", "financial_view", "financial_receipt", "cohort_policy", "source_rankings"):
        if previous["protocol"].get(key) != current["protocol"].get(key):
            raise ValueError(f"Capital continuation contract changed: {key}")
    signals = previous["protocol"]["signals"]
    if (not set(current["protocol"]["signals"]) <= set(signals)
            or len(previous["results"]) != len(signals)
            or {row["signal"] for row in previous["results"]} != set(signals)
            or "stock_manifest" not in previous["protocol"]):
        raise ValueError("All original terminal capital case records required")


class SavedRankFeed:
    def __init__(self, base, folder, recipe, config, dates, fractional_allocation=None):
        self.base, self.recipe, self.config = base, recipe, config
        self.fractional_allocation = fractional_allocation_contract(fractional_allocation)
        frame = pd.read_csv(Path(folder) / "rankings.csv", dtype={"date": str, "symbol": str})
        ends = [day for day in month_ends(base.calendar, END) if START <= day <= END]
        if set(frame.date) != set(ends) or not set(ends) <= set(dates):
            raise ValueError("Saved ranks must cover the exact existing month-end decisions")
        self.ranks = {}
        for day, batch in frame.groupby("date", sort=True):
            batch = batch.sort_values("rank")
            if (batch["rank"].tolist() != list(range(1, config.retain_rank + 1))
                    or batch.symbol.duplicated().any() or not set(batch.symbol) <= set(base.symbols)
                    or not batch.score.map(lambda score: pd.notna(score) and abs(score) < float("inf")).all()
                    or not batch.score.is_monotonic_decreasing):
                raise ValueError("Saved top30 ranks are insufficient or invalid for the fixed20/rank30 selector")
            self.ranks[day] = batch[["symbol", "score"]].to_dict("records")

    def __getattr__(self, name):
        return getattr(self.base, name)

    def ordinary_share_allocation_receipt(self, action, quantity, context):
        contract = self.fractional_allocation
        if contract is None:
            return None
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Fractional sensitivity is historical-replay-only")
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
            raise ValueError("Saved family ranks require the same replay selector config")
        if day not in self.ranks:
            return SignalUnavailable("not_month_end")
        return SignalFrame.from_legacy(self.ranks[day], day, "research:fixed_family:" + self.recipe, context)


def save_allocation_rows(folder):
    state = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    rows = []
    for event in state["events"]:
        if event["kind"] in {"close_lot_allocation", "open_lot_allocation"}:
            rows.extend({"date": event["day"], **estimate,
                         "stock_budget_cny": event["stock_budget_cny"],
                         "allocated_stock_cny": event["allocated_stock_cny"],
                         "squared_error_before": event["squared_error_before"],
                         "squared_error_after": event["squared_error_after"]}
                        for estimate in event["estimates"])
    pd.DataFrame(rows).to_csv(folder / "allocation.csv", index=False)
    return {"allocation_decisions": sum(event["kind"] in {"close_lot_allocation", "open_lot_allocation"} for event in state["events"]),
            "fee_buffer_deferrals": sum(event["kind"] == "rebalance_deferred" for event in state["events"]),
            "raised_lot_estimates": sum(row.get("quantity_at_decision_close", row.get("quantity_at_execution_mark")) > row["floor_quantity"] for row in rows)}


def report(out, record):
    controls = {row["signal"]: row for row in record["controls"]}
    lines = ["# Capital / Lot / Minimum-Fee Ledger Comparison", "",
             f"{START}..{END};140000CNY; unchanged saved top30 ranks,20-stock selector and costs. NO_TRADE.",
             ("Explicitly refreshed affected controls run under the current sources; compatible completed implementation arms reuse their full ledgers."
              if record.get("refresh_controls") else "Existing completed controls are read, not replayed. Only the selected fixed new implementation arms run."), "",
             "| Case | Arm | Status | Net | MDD | Sharpe | Fees CNY | Net vs control pp | Fee difference CNY |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for case in record["results"]:
        if case["status"] != "completed":
            lines.append(f"| {case['control']} | {case['arm']} | FAILED {case['completed_sessions']}/{record['sessions']} | unknown | unknown | unknown | unknown | unknown | unknown |")
            lines.append(case["error"])
            continue
        control = controls[case["control"]]
        if not control.get("comparable_under_current_sources", True):
            lines.append(f"| {case['control']} | {case['arm']} | completed; control-source comparison unavailable | "
                         f"{case['net_return']:.2%} | {abs(case['max_drawdown']):.2%} | {case['sharpe_zero_rf_252']:.4f} | "
                         f"{case['fees']:.2f} | unknown | unknown |")
            continue
        case["net_return_difference"] = case["net_return"] - control["net_return"]
        case["fee_difference_cny"] = case["fees"] - control["fees"]
        case["drawdown_magnitude_difference"] = abs(case["max_drawdown"]) - abs(control["max_drawdown"])
        lines.append(f"| {case['control']} | {case['arm']} | completed | {case['net_return']:.2%} | "
                     f"{abs(case['max_drawdown']):.2%} | {case['sharpe_zero_rf_252']:.4f} | {case['fees']:.2f} | "
                     f"{case['net_return_difference'] * 100:.2f} | {case['fee_difference_cny']:.2f} |")
    lines += ["", "## Fixed Rules and Limits", "",
              f"- Signal source family: `{record['protocol'].get('signal_source_family', 'fixed_family_incremental')}`; financial pool/report-period rules and saved ranks remain frozen when present.",
              f"- Fractional ordinary-share sensitivity: `{record['protocol'].get('fractional_allocation')}`. Default is unresolved without an account receipt. Floor/ceil are assumptions, not observed credits or a global worst-case envelope.",
              "- Explicit action/reference source amendments are recorded in the protocol; original controls are not silently relabelled as replayed under new sources.",
              "- Saved prefixes and controls retain the existing past-involved-economics guard. Incompatible control comparisons remain unknown; affected restarts must be explicitly named.",
              ("- At the execution attempt, apply the unchanged lot objective to prior-decision weights/current marked equity and quotes; emit integer desired quantities directly, not close-rounded weights."
               if record["protocol"]["lot_allocation"] == OPEN_LOT_ALLOCATION else
               "- At the decision close, floor stock share estimates to board steps/minimums; consider one floor-to-ceiling move per admitted name."),
              "- Greedy positive squared-CNY tracking-error reduction per extra CNY, rank-order ties; unchanged stock-sleeve budget and single-name/gross caps.",
              "- No substitute candidates, concentration to minimum-commission break-even sizes, formula/sign/window tuning, next-open lookahead or new account engine.",
              "- Fixed fund weights are untouched. Unspent budget remains cash. Missing raw closes fail, never become invented zero prices.",
              ("- Open-lot quantities are attempt targets, NOT filled orders or a solver optimum; missing fresh opens remain explicit. Original delta/T+1/cash/fee/slippage/volume/limit/cap checks still execute. No rank or signal sees a future open."
               if record["protocol"]["lot_allocation"] == OPEN_LOT_ALLOCATION else
               "- These are weights estimated from current close/account, NOT frozen share orders or an optimal integer-portfolio solver. Next-open lot/cash/fee/limit checks remain unchanged."),
              "- Fee-buffer arm additionally uses the existing5CNY/.0003 commission internal-reweight policy for stocks AND funds; entries, full exits and cap-risk cuts remain exempt.",
              "- Rank buffers and membership rules are fixed, but changed holdings can alter later retained members; this is implementation-path evidence, not isolated fill addback.",
              "- Already-inspected development sample; no independent, prospective, broker, black-swan or profitability acceptance.",
              f"Detailed saved accounts/orders/fills/close allocation estimates: `{record['artifacts']}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_id, *, event_view=None, reference_view=None, resume_run=None, restart_cases=(), only_cases=(),
        fractional_allocation=None, refresh_controls=False, allocation_arm="close_lots", control_run=None):
    source_path = ROOT / "outputs/market_replay" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    cases, arms, profitability = source_layout(source, allocation_arm)
    lot_policy = OPEN_LOT_ALLOCATION if allocation_arm == "open_lots" else LOT_ALLOCATION
    cached = (json.loads((ROOT / "outputs/market_replay" / control_run / "run.json").read_text(encoding="utf-8"))
              if control_run else None)
    cached_controls = validate_control_cache(cached, source_path, source) if cached else {}
    inputs = bind_stock_inputs(source["protocol"]["stock_inputs"], event_view, reference_view)
    previous = (json.loads((ROOT / "outputs/market_replay" / resume_run / "run.json").read_text(encoding="utf-8"))
                if resume_run else None)
    restart_cases = set(restart_cases)
    all_signals = [f"{case}__{arm}" for case in cases for arm in arms]
    signals = list(only_cases or (previous["protocol"]["signals"] if previous else all_signals))
    if len(set(signals)) != len(signals) or not set(signals) <= set(all_signals):
        raise ValueError("Selected capital cases must name distinct existing fixed arms")
    signals = [signal for signal in all_signals if signal in signals]
    if restart_cases and (previous is None or not restart_cases <= set(signals)):
        raise ValueError("Explicit capital restarts require a matching prior experiment and named cases")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs/market_replay" / identifier
    artifacts = DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    protocol = {**source["protocol"], "id": (("profitability_open_lot_implementation_v1" if allocation_arm == "open_lots" else
                                            "profitability_close_lot_implementation_v1") if profitability
                                            else "capital_close_lot_fee_ablation_v1"),
                "source_run": str(source_path), "cases": list(cases), "arms": list(arms),
                "signal_source_family": source["research_family"],
                "source_rankings": source.get("rankings"),
                "signals": signals,
                "signal_portfolio_configs": {f"{case}__{arm}": source["protocol"]["signal_portfolio_configs"][case]
                                             for case in cases for arm in arms},
                "lot_allocation": lot_policy, "policy_parameters_searched": False,
                "fractional_allocation": fractional_allocation_contract(fractional_allocation),
                "ranking_basis": "Saved decision-date top30; sufficient for the unchanged20/rank30 selector; not all718 individual accounts"}
    record = {"id": identifier, "status": "running", "research_family": "capital_lot_fee_implementation",
              "purpose": protocol["id"],
              "start": START, "end": END, "sessions": source["sessions"], "symbols": source["symbols"],
              "fund_dataset": source["protocol"].get("fund_dataset"),
              "dataset": source["dataset"], "scope": source["scope"], "protocol": protocol,
              "source_run": str(source_path), "artifacts": str(artifacts), "controls": [], "results": [],
              "strategy_validated": False, "execution_admitted": False,
              "resume_run": resume_run, "restart_cases": sorted(restart_cases), "refresh_controls": refresh_controls,
              "control_run": control_run}
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    stocks, started = None, time.monotonic()
    try:
        stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        if not event_view and stocks.manifest != source["protocol"]["stock_manifest"]:
            raise ValueError("Capital ablation needs the exact original stock snapshot manifest")
        validate_prefix_inputs(source["protocol"]["stock_manifest"], stocks.manifest, None)
        protocol.update(stock_inputs=inputs, stock_manifest=stocks.manifest, action_source_override=bool(event_view))
        if profitability:
            base = stocks
        else:
            funds = HistoricalFeed(protocol["fund_dataset"])
            base = MixedResearchFeed(stocks, funds, END)
            if base.instrument_profiles != protocol["fund_profiles"]:
                raise ValueError("Capital ablation fund profiles changed")
        dates = [day for day in base.calendar if START <= day <= END]
        if dates[0] != START or dates[-1] != END or len(dates) != source["sessions"]:
            raise ValueError("Capital ablation calendar changed")
        if previous:
            validate_capital_resume(previous, record)
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        original_results = {row["signal"]: row for row in source["results"]}
        print(f"Pinned capital ablation: {len(dates)} sessions, saved ranks; no feature/library recomputation", flush=True)
        for case in cases:
            if not any(f"{case}__{arm}" in signals for arm in arms):
                continue
            control = original_results[case]
            if control["status"] != "completed" or control["completed_sessions"] != len(dates):
                raise ValueError("Every chosen control must have a complete matched account")
            source_folder = Path(source["artifacts"]) / case
            config = StrategyConfig(**source["protocol"]["signal_portfolio_configs"][case]).validate()
            receipt = json.loads((source_folder / "replay.json").read_text(encoding="utf-8"))
            control_checkpoint = load_replay_checkpoint(source_folder, config, dates,
                                                       portfolio_policy_contract=receipt.get("portfolio_policy_contract"))
            # A control under the old snapshot is comparable only if the original prefix guard accepts it.
            try:
                ignored = validate_prefix_inputs(source["protocol"]["stock_manifest"], stocks.manifest,
                                                 control_checkpoint["state"]["day"], control_checkpoint["state"])
                comparable = True
                control_error = None
            except ValueError as error:
                comparable, ignored, control_error = False, [], str(error)
            recipe = PROFITABILITY_FAMILY + ":" + case if profitability else control["recipe"]
            allocation = "stock" if profitability else control["allocation"]
            rank_folder = Path(source["rankings"]) / case if profitability else source_folder
            stock_config = (config if profitability else StrategyConfig(
                **source["protocol"]["signal_portfolio_configs"]["stock_" + recipe]).validate())
            ranked = SavedRankFeed(base, rank_folder, recipe, stock_config, dates, fractional_allocation)
            feed, options = ranked, {}
            if case.startswith("mixed_"):
                feed = StockDefensiveFeed(base, None, recipe, config.initial_cash, stock_signal=ranked)
                options = {"portfolio_policy": feed.allocate,
                           "portfolio_policy_contract": receipt["portfolio_policy_contract"]}
            control_result = {**control, "source_artifacts": str(source_folder), "replayed": False,
                              "comparable_under_current_sources": comparable,
                              "uninvolved_source_changes": ignored, "comparison_error": control_error}
            if cached:
                candidate = cached_controls.get(case)
                if not candidate or candidate["status"] != "completed" or not candidate.get("comparable_under_current_sources"):
                    raise ValueError(f"Complete source-compatible cached control required: {case}")
                cached_folder = Path(candidate["source_artifacts"])
                cached_receipt = json.loads((cached_folder / "replay.json").read_text(encoding="utf-8"))
                checkpoint = load_replay_checkpoint(cached_folder, config, dates,
                    portfolio_policy_contract=cached_receipt.get("portfolio_policy_contract"))
                if len(checkpoint["daily"]) != len(dates):
                    raise ValueError("Cached control is not a full matched account")
                cached_ignored = validate_prefix_inputs(cached["protocol"]["stock_manifest"], stocks.manifest,
                                                       checkpoint["state"]["day"], checkpoint["state"])
                control_result = {**candidate, **portfolio_metrics(checkpoint["daily"], config.initial_cash),
                    "replayed": False, "reused_control_run": control_run,
                    "comparable_under_current_sources": True, "comparison_error": None,
                    "uninvolved_source_changes": cached_ignored}
                comparable = True
            if refresh_controls and not comparable:
                control_folder = artifacts / "controls" / case
                control_ranked = SavedRankFeed(base, rank_folder, recipe, stock_config, dates)
                control_feed, control_options = control_ranked, {}
                if case.startswith("mixed_"):
                    control_feed = StockDefensiveFeed(base, None, recipe, config.initial_cash, stock_signal=control_ranked)
                    control_options = {"portfolio_policy": control_feed.allocate,
                                       "portfolio_policy_contract": receipt["portfolio_policy_contract"]}
                control_result = {"signal": case, "recipe": recipe, "allocation": allocation,
                    **baseline_ledger_result(control_feed, config, dates, control_folder, **control_options),
                    "source_artifacts": str(control_folder), "original_source_artifacts": str(source_folder),
                    "replayed": True, "original_source": str(source_path),
                    "fractional_allocation_assumption": None}
                control_result["comparable_under_current_sources"] = control_result["status"] == "completed"
                print(f"Refreshed {case}: {control_result['status']}; net={control_result.get('net_return')}", flush=True)
            record["controls"].append(control_result)
            for arm in arms:
                signal = f"{case}__{arm}"
                if signal not in signals:
                    continue
                folder = artifacts / signal
                arm_options = dict(options)
                ignored = []
                if previous and signal not in restart_cases:
                    checkpoint = load_replay_checkpoint(Path(previous["artifacts"]) / signal, config, dates,
                        portfolio_policy_contract=options.get("portfolio_policy_contract"),
                        lot_allocation=lot_policy, commission_deadband=arm == "close_lots_fee_buffer")
                    ignored = validate_prefix_inputs(previous["protocol"]["stock_manifest"], stocks.manifest,
                                                     checkpoint["state"]["day"], checkpoint["state"])
                    prior_case = next(row for row in previous["results"] if row["signal"] == signal)
                    check_fractional_continuation(previous["protocol"], protocol, checkpoint, prior_case)
                    arm_options["checkpoint"] = checkpoint
                    print(f"{signal}: continuing after {len(checkpoint['daily'])} saved sessions", flush=True)
                result = {"signal": signal, "control": case, "recipe": recipe, "arm": arm,
                          "allocation": allocation,
                          **baseline_ledger_result(feed, config, dates, folder, **arm_options,
                                                   lot_allocation=lot_policy,
                                                   commission_deadband=arm == "close_lots_fee_buffer")}
                if "checkpoint" in arm_options:
                    result.update(resume_source=arm_options["checkpoint"]["source"], uninvolved_source_changes=ignored)
                if fractional_allocation:
                    result.update(fractional_allocation_assumption=protocol["fractional_allocation"],
                                  label=signal + f" / fractional shares {fractional_allocation} ASSUMPTION")
                result.update(save_allocation_rows(folder))
                if result["completed_sessions"]:
                    result["annual_same_account"] = stage_metrics(folder, config.initial_cash)
                record["results"].append(result)
                write_json(out / "run.json", record)
                print(f"{signal}: {result['status']} {result['completed_sessions']}/{len(dates)}; "
                      f"net={result.get('net_return')}; fees={result.get('fees')}", flush=True)
        record["status"] = ("completed_capital_allocation_diagnostics" if all(row["status"] == "completed" for row in record["results"])
                            else "failed_capital_allocation_diagnostics")
        report(out, record)
        return out, record
    except Exception as error:
        record.update(status="failed_capital_allocation_preparation", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(out / "run.json", record)
        print(str(out), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--event-view")
    parser.add_argument("--reference-view")
    parser.add_argument("--allocation-arm", choices=("close_lots", "open_lots"), default="close_lots",
                        help="Fixed stock allocation timing; old close policy remains the default")
    parser.add_argument("--control-run", help="Reuse verified full same-source controls from a terminal capital run, no replay")
    parser.add_argument("--resume-run", help="Continue matching terminal accounts; reuse complete compatible ledgers")
    parser.add_argument("--restart-case", action="append", default=[], help="Explicitly replay this affected account from cash inception")
    parser.add_argument("--only-case", action="append", default=[], help="Run only this existing fixed arm; others remain under their original sources")
    parser.add_argument("--fractional-allocation", choices=("floor", "ceil"), help="Explicit unbound historical sensitivity; NOT observed allocated shares")
    parser.add_argument("--refresh-controls", action="store_true", help="Replay source-incompatible selected controls only; compatible saved controls stay read-only, no refit")
    args = parser.parse_args(argv)
    _, record = run(args.source_run, event_view=args.event_view, reference_view=args.reference_view,
                    resume_run=args.resume_run, restart_cases=args.restart_case, only_cases=args.only_case,
                    fractional_allocation=args.fractional_allocation, refresh_controls=args.refresh_controls,
                    allocation_arm=args.allocation_arm, control_run=args.control_run)
    return 0 if record["status"] == "completed_capital_allocation_diagnostics" else 1
