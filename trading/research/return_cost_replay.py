"""One train2024 return forecast, two predeclared selectors, the sole ledger."""
import argparse
from datetime import datetime
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import time
import uuid

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from ..config import StrategyConfig
from ..contracts import (Signal, SignalFrame, SignalKind, SignalTime, SecurityId,
                         SignalUnavailable, TradingMode, decision_time)
from ..expected_returns import EqualSlotReturnPolicy
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .family_portfolios import START, END, bind_stock_inputs
from .family_ridge import ALPHA, FAMILIES, HORIZON, TRAIN_START, TRAIN_END
from .market_replay import validate_prefix_inputs
from .monthly_timing import month_ends
from .stock_defensive import stage_metrics


ID = "family_return_cost_selection_2024_fixed_v2"
CASES = {"stock_family_return_top20": False, "stock_family_return_cost_top20": True}
METHOD = "https://web.stanford.edu/~boyd/papers/pdf/markowitz.pdf"


def fit_return_training(train, counts, calendar):
    if (train.index.names != ["date", "symbol"] or train.index.has_duplicates
            or not train.index.is_monotonic_increasing
            or not set([*FAMILIES, "forward_return", "sample_weight"]) <= set(train.columns)
            or not np.isfinite(train[[*FAMILIES, "forward_return", "sample_weight"]].to_numpy()).all()):
        raise ValueError("Original observed finite train2024 family/return pairs required")
    days = train.index.get_level_values("date")
    calendar = pd.DatetimeIndex(calendar)
    labeled = counts.loc[counts.labeled_rows.gt(0)].copy()
    if (len(labeled) < 8 or days.min() < pd.Timestamp(TRAIN_START) or days.max() > pd.Timestamp(TRAIN_END)
            or set(days.unique()) != set(pd.to_datetime(labeled.date))
            or train.groupby(level="date").size().tolist() != labeled.labeled_rows.tolist()):
        raise ValueError("Frozen original labeled months changed")
    for row in labeled.itertuples():
        location = calendar.get_loc(pd.Timestamp(row.date)) + HORIZON + 1
        if (location >= len(calendar) or calendar[location] != pd.Timestamp(row.exit_date)
                or calendar[location] > pd.Timestamp(TRAIN_END)):
            raise ValueError("Forecast calibration labels are not matured at the frozen boundary")
    totals = train.sample_weight.groupby(level="date").sum()
    expected = 1 / train.groupby(level="date").forward_return.transform("size")
    if not np.allclose(totals, 1, atol=1e-12, rtol=0) or not np.allclose(train.sample_weight, expected, atol=1e-15, rtol=0):
        raise ValueError("Original equal-month observed-pair loss required")
    target = train.forward_return - train.groupby(level="date").forward_return.transform("mean")
    model = Ridge(alpha=ALPHA, fit_intercept=True, solver="cholesky")
    model.fit(train[list(FAMILIES)], target, sample_weight=train.sample_weight)
    if not np.isfinite(model.coef_).all() or not np.isfinite(model.intercept_):
        raise ValueError("Nonfinite fixed relative-return forecast")
    return model, target


class SavedReturnFeed:
    def __init__(self, base, forecasts, config, dates):
        self.base, self.config = base, config
        expected = pd.to_datetime([day for day in month_ends(base.calendar, END) if START <= day <= END])
        if (forecasts.index.names != ["date", "symbol"] or forecasts.index.has_duplicates
                or not forecasts.index.is_monotonic_increasing
                or not forecasts.index.get_level_values("date").unique().equals(expected)
                or not np.isfinite(forecasts.expected_return.to_numpy()).all()
                or not set(forecasts.index.get_level_values("symbol")) <= set(base.symbols)
                or list(dates) != [day for day in base.calendar if START <= day <= END]):
            raise ValueError("Full original month-end return forecast pool required")
        self.forecasts = {day.strftime("%Y-%m-%d"): batch.droplevel("date").expected_return
                          for day, batch in forecasts.groupby(level="date", sort=True)}

    def __getattr__(self, name):
        return getattr(self.base, name)

    def signal_frame(self, day, config, context):
        if config != self.config or context.mode != TradingMode.REPLAY:
            raise ValueError("Return forecasts are fixed-cost historical research only")
        values = self.forecasts.get(day)
        if values is None:
            return SignalUnavailable("not_original_month_end")
        stamp = SignalTime(day + "T15:00:00+08:00", decision_time(day),
                           availability_evidence="historical_eod_assumption")
        signals = tuple(Signal(SecurityId(symbol), float(value), SignalKind.EXPECTED_RETURN, stamp,
                               horizon_sessions=HORIZON)
                        for symbol, value in values.items())
        return SignalFrame(day, signals, ID, context)


def selection_summary(folder):
    state = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    decisions = [row for row in state["events"] if row["kind"] == "expected_return_selection"]
    rows = []
    for row in decisions:
        rows.append({"date": row["day"], "cost_aware": row["cost_aware"],
                     "candidates": row["candidate_count"], "selected": len(row["selected_symbols"]),
                     "retained_actual_holdings": sum(d["held_quantity"] > 0 and d["symbol"] in row["selected_symbols"]
                                                     for d in row["diagnostics"]),
                     "changed_vs_same_account_raw_top20": len(row["cost_changed_members"]) // 2,
                     "estimated_legal_gross": row.get("estimated_selected_legal_gross"),
                     "selected_zero_legal_quantity": row.get("selected_zero_legal_quantity"),
                     "estimated_transition_cost_cny": row["estimated_selected_transition_cost_cents"] / 100})
    pd.DataFrame(rows).to_csv(folder / "selection_decisions.csv", index=False)
    return {"decision_count": len(rows),
            "decisions_changed_by_cost": sum(row["changed_vs_same_account_raw_top20"] > 0 for row in rows),
            "replaced_slots_vs_same_account_raw_top20": sum(row["changed_vs_same_account_raw_top20"] for row in rows),
            "estimated_decision_cost_cny": sum(row["estimated_transition_cost_cny"] for row in rows),
            "cost_estimates_are_actual_fills": False}


def report(out, record):
    lines = ["# Fixed Return Forecast / Cost-Aware Stock Selection", "", f"Status: {record['status']}; NO_TRADE.",
             f"Same {START}..{END}, 424 source sessions, synthetic140000CNY,20 equal slots,80% gross cap, original execution costs.",
             "One train2024 Ridge(alpha1), three original families, observed20-session next-open relative-return target.",
             "Subtract each observed training month's pool mean; equal-month weights; no post2024 fit, grid or tuned cost multiplier.",
             "Two exact additive top20 selectors: return-only versus return-minus-(include cost-exclude cost).",
             "Both value the observed legal post-change share position, NOT nominal money in an unbuyable slot.",
             "Both pay the ORIGINAL real ledger fees/slippage; return-only does NOT mean a zero-cost backtest.", "",
             "| Case | State / sessions | Net | MDD | Sharpe | Actual fees CNY |",
             "| --- | --- | ---: | ---: | ---: | ---: |"]
    for row in record["results"]:
        if row["status"] == "completed":
            lines.append(f"| {row['signal']} | completed/{row['completed_sessions']} | {row['net_return']:.4%} | "
                         f"{abs(row['max_drawdown']):.4%} | {row['sharpe_zero_rf_252']:.4f} | {row['fees']:.2f} |")
        else:
            lines.append(f"| {row['signal']} | {row['status']}/{row.get('completed_sessions', 0)} | unknown | unknown | unknown | unknown |")
    for row in record["results"]:
        if row.get("error"):
            lines += ["", f"- {row['signal']}: {row['error']}"]
    lines += ["", "Saved selection prefixes (different end dates are NOT a paired performance comparison):", "",
              "| Case | Decisions | Decisions changed by cost | Replaced slots | Estimated decision cost CNY |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for row in record["results"]:
        saved = row.get("selection", {})
        if saved:
            lines.append(f"| {row['signal']} | {saved['decision_count']} | {saved['decisions_changed_by_cost']} | "
                         f"{saved['replaced_slots_vs_same_account_raw_top20']} | {saved['estimated_decision_cost_cny']:.2f} |")
    if record.get("paired_comparison"):
        lines += ["", "Paired cost-selector change (cost arm minus identical-forecast return-only arm):",
                  json.dumps(record["paired_comparison"], ensure_ascii=False)]
    lines += ["", "## Scope / Limits", "",
              "- Forecasts are fitted fractional relative returns, NOT rank scores, absolute cash alpha, guaranteed returns or calibrated uncertainty bounds.",
              "- This fixed20 slot, observed-close legal-quantity additive objective has an exact sorting solution. It is NOT full Markowitz++, covariance optimization, market impact estimation or a private-fund reproduction.",
              "- At the close, observed actual holdings/equity and raw prices estimate one parent commission floor, sell tax, transfer fee and declared10bps slippage. Next-open gaps, partial-fill timing, liquidity blocks and corporate actions remain actual-ledger outcomes, not selection estimates.",
              "- Current full common pool is preserved; no future action completeness filter. Departed names do not gain a fabricated forecast or a protected incumbent slot.",
              "- Fixed20 slots declare80% nominal exposure; unreachable quantity has ZERO forecast benefit. Actual exposure/cash can differ. No confidence calibration, industry/style neutrality or black-swan protection claim. Forced risk exits remain unchanged.",
              "- V1 estimated positive nominal benefit for zero-quantity positions. Its saved prefixes are invalid as cost-improvement evidence. V2 uses copied exact forecasts, starts fresh diagnostic accounts and cannot resume a V1 policy.",
              "- Both new arms omit the old rank30 retention buffer. Compare their difference for the cost question; saved rank/static/expanding controls are context, NOT a cost-only causal experiment.",
              "- All2025/2026 were seen development data. Research-only StockDB/BaoStock and secondary action/PIT/industry limits remain; no prospective/independent test or paper/live admission.",
              f"- Public design source: [Boyd et al., sections2.3-2.4/3.1/4.1]({METHOD}); only return versus transition-cost principle adopted.",
              f"- Artifacts: `{record['artifacts']}`."]
    if record.get("error"):
        lines += ["", record["error"]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_id, *, resume_run=None, forecast_run=None, event_view=None, reference_view=None, replay_cases=()):
    source_path = ROOT / "outputs/market_replay" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("status") != "completed_expanding_family_diagnostics" or source["sessions"] != 424
            or source.get("execution_admitted") is not False):
        raise ValueError("Completed original expanding comparison required as current source binding")
    static = json.loads(Path(source["source_run"]).read_text(encoding="utf-8"))
    if static.get("status") != "completed_family_ridge_diagnostics":
        raise ValueError("Frozen static training receipts required")
    config = StrategyConfig(**source["protocol"]["portfolio"]).validate()
    if config != StrategyConfig(**static["protocol"]["portfolio"]).validate():
        raise ValueError("Original strategy/cost basis changed")
    previous = (json.loads((ROOT / "outputs/market_replay" / resume_run / "run.json").read_text(encoding="utf-8"))
                if resume_run else None)
    if (len(replay_cases) != len(set(replay_cases)) or not set(replay_cases) <= set(CASES)
            or (replay_cases and previous is None)):
        raise ValueError("Explicit unique source-change replay cases require terminal saved accounts")
    if previous is not None and forecast_run is not None:
        raise ValueError("Choose account continuation OR forecast-only reuse, never both")
    frozen = (json.loads((ROOT / "outputs/market_replay" / forecast_run / "run.json").read_text(encoding="utf-8"))
              if forecast_run else None)
    if previous is None and frozen is None and (event_view or reference_view):
        raise ValueError("Source amendments require saved forecasts and terminal account prefixes")
    model_source = previous or frozen
    inputs = bind_stock_inputs(model_source["protocol"]["stock_inputs"] if model_source else source["protocol"]["stock_inputs"],
                               event_view, reference_view)
    policies = {case: EqualSlotReturnPolicy(cost) for case, cost in CASES.items()}
    protocol = {"id": ID, "source_run": str(source_path), "stock_inputs": inputs,
                "portfolio": config.to_dict(), "signal_portfolio_configs": {case: config.to_dict() for case in CASES},
                "signals": list(CASES), "training_period": [TRAIN_START, TRAIN_END], "label_horizon": HORIZON,
                "alpha": ALPHA, "families": list(FAMILIES), "target": "forward_return_minus_observed_month_pool_mean",
                "selection_policies": {case: policy.contract(config) for case, policy in policies.items()},
                "cached_features": str(Path(static["artifacts"]) / "monthly_family_features.parquet"),
                "cached_train": str(Path(static["artifacts"]) / "training_rows.parquet"),
                "parameters_searched": False, "fresh_holdout_claimed": False, "execution_admitted": False,
                "method_source": METHOD}
    if frozen is not None:
        if (frozen.get("research_family") != "return_cost_selection"
                or frozen.get("status") not in {"failed_return_cost_ledger", "completed_return_cost_diagnostics"}
                or frozen.get("execution_admitted") is not False):
            raise ValueError("Terminal unadmitted saved return forecasts required")
        for key in ("source_run", "portfolio", "training_period", "label_horizon", "alpha", "families",
                    "target", "cached_features", "cached_train"):
            if frozen["protocol"].get(key) != protocol[key]:
                raise ValueError(f"Forecast-only reuse changed training/prediction basis: {key}")
        for key in ("price_view", "native_view", "listing_master"):
            if frozen["protocol"]["stock_inputs"].get(key) != source["protocol"]["stock_inputs"].get(key):
                raise ValueError(f"Frozen forecast source changed: {key}")
    if previous is not None:
        if previous.get("status") != "failed_return_cost_ledger" or previous.get("purpose") != ID:
            raise ValueError("Only terminal unresolved return-cost accounts may continue")
        for key, value in protocol.items():
            if key != "stock_inputs" and previous["protocol"].get(key) != value:
                raise ValueError(f"Saved forecast/selection policy changed: {key}")
        for key in ("price_view", "native_view", "listing_master"):
            if previous["protocol"]["stock_inputs"].get(key) != inputs.get(key):
                raise ValueError(f"Source calendar/forecast basis changed: {key}")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    for path in (Path(__file__), ROOT / "trading/expected_returns.py", ROOT / "trading/engine.py"):
        shutil.copy2(path, artifacts / path.name)
    record = {"id": identifier, "status": "running", "purpose": ID, "research_family": "return_cost_selection",
              "start": START, "end": END, "sessions": 424, "dataset": inputs["price_view"], "artifacts": str(artifacts),
              "scope": source["scope"], "symbols": source["symbols"],
              "protocol": protocol, "results": [], "controls": [], "strategy_validated": False,
              "execution_admitted": False, "sklearn_version": version("scikit-learn")}
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    stocks, begin = None, time.monotonic()
    try:
        stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        validate_prefix_inputs(source["protocol"]["stock_manifest"], stocks.manifest, None)
        protocol["stock_manifest"] = stocks.manifest
        dates = [day for day in stocks.calendar if START <= day <= END]
        if len(dates) != 424 or dates[0] != START or dates[-1] != END:
            raise ValueError("Original424-day calendar required")
        record["symbols"] = len(stocks.symbols) - len(stocks.unsupported_instruments)
        if model_source is None:
            train = pd.read_parquet(protocol["cached_train"])
            counts = pd.read_csv(Path(static["artifacts"]) / "training_split_counts.csv")
            model, target = fit_return_training(train, counts, pd.to_datetime(stocks.calendar))
            features = pd.read_parquet(protocol["cached_features"])
            current = features.loc[(features.index.get_level_values("date") >= pd.Timestamp(START))
                                   & (features.index.get_level_values("date") <= pd.Timestamp(END))].copy()
            current["expected_return"] = model.predict(current[list(FAMILIES)])
            current.to_parquet(artifacts / "monthly_return_forecasts.parquet")
            metadata = {"coefficients": dict(zip(FAMILIES, model.coef_)), "intercept": model.intercept_,
                        "training_rows": len(train), "training_months": train.index.get_level_values("date").nunique(),
                        "last_training_exit": pd.to_datetime(counts.loc[counts.labeled_rows.gt(0), "exit_date"]).max(),
                        "in_sample_weighted_rmse": float(np.sqrt(np.average((model.predict(train[list(FAMILIES)]) - target) ** 2,
                                                                            weights=train.sample_weight))),
                        "prediction_units": "20_session_relative_return_fraction; not_rank_score",
                        "uncertainty_calibrated": False}
            write_json(artifacts / "return_model.json", metadata)
            shutil.copy2(Path(static["artifacts"]) / "training_split_counts.csv", artifacts / "training_split_counts.csv")
        else:
            old = Path(model_source["artifacts"])
            for name in ("monthly_return_forecasts.parquet", "return_model.json", "training_split_counts.csv"):
                shutil.copy2(old / name, artifacts / name)
            metadata = json.loads((artifacts / "return_model.json").read_text(encoding="utf-8"))
            current = pd.read_parquet(artifacts / "monthly_return_forecasts.parquet")
            record.update(model_refit=False)
            if previous is not None:
                record.update(resume_run=resume_run)
            else:
                record.update(forecast_source_run=forecast_run, account_prefix_reused=False)
        record.update(model=metadata, prediction_rows=len(current))
        forecasts = SavedReturnFeed(stocks, current, config, dates)
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        print(f"Fixed train2024 return forecast: {metadata['training_rows']} pairs, {metadata['training_months']} months; two selectors, no search", flush=True)
        for case, policy in policies.items():
            checkpoint, full_reason = None, None
            if previous is not None:
                prior = next(row for row in previous["results"] if row["signal"] == case)
                folder = Path(prior["source_artifacts"])
                checkpoint = load_replay_checkpoint(folder, config, dates, portfolio_policy_contract=policy.contract(config))
                try:
                    validate_prefix_inputs(previous["protocol"]["stock_manifest"], stocks.manifest,
                                           checkpoint["state"]["day"], checkpoint["state"])
                except ValueError as error:
                    if (case not in replay_cases
                            or str(error) != "Past economic action changed for an involved symbol; full replay required"):
                        raise
                    checkpoint, full_reason = None, str(error)
                if case in replay_cases and full_reason is None:
                    raise ValueError("Unchanged account prefixes must continue, not be routinely replayed")
                if prior["status"] == "completed" and full_reason is None:
                    retained = artifacts / case
                    shutil.copytree(folder, retained)
                    record["results"].append({**prior, "source_artifacts": str(retained), "replayed": False})
                    write_json(out / "run.json", record)
                    continue
            folder = artifacts / case
            result = {"signal": case, "source_artifacts": str(folder), "replayed": True,
                      **baseline_ledger_result(forecasts, config, dates, folder, portfolio_policy=policy,
                                               portfolio_policy_contract=policy.contract(config), checkpoint=checkpoint)}
            if full_reason is not None:
                result["source_change_full_replay_reason"] = full_reason
            result["selection"] = selection_summary(folder)
            if result["completed_sessions"]:
                result["annual_same_account"] = stage_metrics(folder, config.initial_cash)
            record["results"].append(result)
            write_json(out / "run.json", record)
            print(f"{case}: {result['status']} {result['completed_sessions']}/424 net={result.get('net_return')}", flush=True)
        if all(row["status"] == "completed" for row in record["results"]):
            raw, net = record["results"]
            record["paired_comparison"] = {"net_difference_pp": (net["net_return"] - raw["net_return"]) * 100,
                "drawdown_magnitude_difference_pp": (abs(net["max_drawdown"]) - abs(raw["max_drawdown"])) * 100,
                "actual_fee_difference_cny": net["fees"] - raw["fees"],
                "actual_turnover_difference_cny": (net["turnover_over_initial"] - raw["turnover_over_initial"]) * config.initial_cash}
            record["status"] = "completed_return_cost_diagnostics"
        else:
            record["status"] = "failed_return_cost_ledger"
    except Exception as error:
        record.update(status="failed_return_cost_preparation", error=f"{type(error).__name__}: {error}")
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = round(time.monotonic() - begin, 3)
        write_json(out / "run.json", record)
        report(out, record)
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fixed train2024 relative return / transition-cost selectors through sole ledger")
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--resume-run")
    parser.add_argument("--forecast-run", help="Copy exact frozen forecasts, but open new diagnostic accounts with the current selection version")
    parser.add_argument("--replay-case", choices=list(CASES), action="append", default=[],
                        help="Reopen only an account whose past economic source amendment actually requires full historical replay")
    parser.add_argument("--event-view")
    parser.add_argument("--reference-view")
    args = parser.parse_args(argv)
    _, record = run(args.source_run, resume_run=args.resume_run, forecast_run=args.forecast_run,
                    event_view=args.event_view, reference_view=args.reference_view, replay_cases=args.replay_case)
    return 0 if record["status"] == "completed_return_cost_diagnostics" else 1
