"""One fixed, training-only family ridge score consumed by the existing ledger."""
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
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .bulk_model_inputs import native_binding
from .capital_allocation_replay import SavedRankFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .factor_families import forward_open_labels, read_open_panel
from .factors import catalog
from .family_portfolios import (END, START, bind_stock_inputs, common_pool,
                                ranked_recipe, read_native_monthends)
from .low_risk import LowRiskFeed, VARIANTS, features as low_risk_features, portfolio
from .market_replay import validate_prefix_inputs
from .monthly_timing import month_ends
from .stock_defensive import stage_metrics


ID = "family_ridge_rank_2024_fixed_v1"
SIGNAL = "stock_family_ridge_rank"
TRAIN_START, TRAIN_END = "2024-01-02", "2024-12-31"
HORIZON, ALPHA, MIN_MONTHS, MIN_PAIRS = 20, 1.0, 8, 100
FAMILIES = ("risk", "trend", "volume_stability")
CONTROLS = ("stock_low_vol_control", "stock_low_vol_half_momentum_control", "stock_equal_three_families")
RIDGE_SOURCE = "https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.Ridge.html"
EXPANDING_ID = "family_ridge_monthly_expanding_v1"
EXPANDING_SIGNAL = "stock_family_ridge_expanding"
ROLLING_SOURCE = "https://github.com/microsoft/qlib/blob/main/qlib/workflow/task/gen.py"


def family_matrix(pool, directions):
    rows = ranked_recipe(pool, "equal_three_families", directions)
    if not rows:
        raise ValueError("Empty current common family pool")
    frame = pd.DataFrame(rows).set_index("symbol").sort_index()
    result = frame[["risk_rank", "trend_rank", "volume_stability_rank"]].copy()
    result.columns = list(FAMILIES)
    # Cross-sectional ranks use only this decision's available signals, never labels.
    return result - result.mean()


def training_rows(features, forward, calendar, *, train_end=TRAIN_END):
    if (features.index.has_duplicates or features.columns.tolist() != list(FAMILIES)
            or not np.isfinite(features.to_numpy()).all()):
        raise ValueError("Distinct finite family-rank rows required")
    calendar = pd.DatetimeIndex(calendar)
    if not calendar.is_unique or not calendar.is_monotonic_increasing:
        raise ValueError("Full ordered trading calendar required")
    locations = {day: i for i, day in enumerate(calendar)}
    train_end = pd.Timestamp(train_end)
    if train_end not in locations or train_end < pd.Timestamp(TRAIN_START):
        raise ValueError("Training cutoff must be a pinned calendar session")
    days = features.index.get_level_values("date")
    selected = features.loc[(days >= pd.Timestamp(TRAIN_START)) & (days <= train_end)]
    rows, counts = [], []
    for day, batch in selected.groupby(level="date", sort=True):
        i = locations[day]
        exit_day = calendar[i + HORIZON + 1] if i + HORIZON + 1 < len(calendar) else None
        count = {"date": day, "signal_rows": len(batch), "exit_date": exit_day,
                 "labeled_rows": 0, "missing_labels": None, "purged_cross_boundary": 0}
        if exit_day is None or exit_day > train_end:
            count["purged_cross_boundary"] = len(batch)
        else:
            symbols = batch.index.get_level_values("symbol")
            observed = forward.loc[day].reindex(symbols)
            finite = np.isfinite(observed.to_numpy())
            count.update(labeled_rows=int(finite.sum()), missing_labels=int((~finite).sum()))
            if finite.sum() < MIN_PAIRS:
                raise ValueError(f"Insufficient observed training labels: {day}")
            part = batch.loc[finite].copy()
            part["forward_return"] = observed.to_numpy()[finite]
            ranked = part.forward_return.rank(pct=True)
            part["target_rank"] = ranked - ranked.mean()
            part["sample_weight"] = 1 / len(part)
            rows.append(part)
        counts.append(count)
    if len(rows) < MIN_MONTHS:
        raise ValueError("Insufficient fixed training months; do not shorten the horizon or move the boundary")
    return pd.concat(rows).sort_index(), pd.DataFrame(counts)


def fit_training(features, forward, calendar, *, train_end=TRAIN_END):
    train, counts = training_rows(features, forward, calendar, train_end=train_end)
    model = Ridge(alpha=ALPHA, fit_intercept=True, solver="cholesky")
    model.fit(train[list(FAMILIES)], train.target_rank, sample_weight=train.sample_weight)
    if not np.isfinite(model.coef_).all() or not np.isfinite(model.intercept_):
        raise ValueError("Finite training-only ridge coefficients required")
    return model, train, counts


def save_predictions(model, features, folder, config):
    days = features.index.get_level_values("date")
    current = features.loc[(days >= pd.Timestamp(START)) & (days <= pd.Timestamp(END))].copy()
    current["score"] = model.predict(current[list(FAMILIES)])
    return save_rankings(current, folder, config)


def save_rankings(current, folder, config):
    current.to_parquet(folder / "monthly_scores.parquet")
    rows = []
    for day, batch in current.groupby(level="date", sort=True):
        frame = batch.droplevel("date").reset_index().sort_values(
            ["score", "symbol"], ascending=[False, True], kind="stable")
        if len(frame) < config.retain_rank:
            raise ValueError("Fixed20/rank30 selector requires the full current rank buffer")
        rows.extend({"date": day.strftime("%Y-%m-%d"), "rank": i, **row}
                    for i, row in enumerate(frame.head(config.retain_rank).to_dict("records"), 1))
    pd.DataFrame(rows).to_csv(folder / "rankings.csv", index=False)
    return len(current)


def expanding_predictions(features, forward, calendar):
    calendar = pd.DatetimeIndex(calendar)
    days = features.index.get_level_values("date")
    evaluation = features.loc[(days >= pd.Timestamp(START)) & (days <= pd.Timestamp(END))]
    if evaluation.empty:
        raise ValueError("Original evaluation month-end feature rows required")
    predictions, models, splits = [], [], []
    for day, batch in evaluation.groupby(level="date", sort=True):
        location = calendar.get_loc(day)
        if location < 1:
            raise ValueError("Prediction needs an earlier observed training cutoff")
        cutoff = calendar[location - 1]
        # Labels mature at their scheduled exit, not their feature date.
        model, train, counts = fit_training(features, forward, calendar, train_end=cutoff)
        counts.insert(0, "model_date", day)
        splits.append(counts)
        models.append({"date": day.strftime("%Y-%m-%d"), "training_cutoff": cutoff.strftime("%Y-%m-%d"),
                       "last_training_exit": counts.loc[counts.labeled_rows.gt(0), "exit_date"].max(),
                       "training_months": train.index.get_level_values("date").nunique(),
                       "training_rows": len(train), "coefficients": dict(zip(FAMILIES, model.coef_)),
                       "intercept": model.intercept_})
        scored = batch.copy()
        scored["score"] = model.predict(batch[list(FAMILIES)])
        if not np.isfinite(scored.score.to_numpy()).all():
            raise ValueError("Nonfinite expanding predictions")
        predictions.append(scored)
    return pd.concat(predictions).sort_index(), models, pd.concat(splits, ignore_index=True)


def control_candidates(source, extra_ids):
    result = {row["signal"]: (source, row, Path(source["artifacts"]) / row["signal"])
              for row in source["results"] if row["signal"] in CONTROLS}
    for identifier in extra_ids:
        record = json.loads((ROOT / "outputs/market_replay" / identifier / "run.json").read_text(encoding="utf-8"))
        if (record.get("status") != "completed_capital_allocation_diagnostics"
                or record.get("source_run") != source["output_source"]
                or record.get("start") != START or record.get("end") != END
                or record.get("sessions") != source["sessions"]):
            raise ValueError("Extra control must be a terminal explicit refresh of the same family source")
        for row in record["controls"]:
            if row["signal"] in CONTROLS:
                if not row.get("replayed") or row.get("fractional_allocation_assumption") is not None:
                    raise ValueError("Original controls cannot import implementation or fractional assumptions")
                result[row["signal"]] = (record, row, Path(row["source_artifacts"]))
    if set(result) != set(CONTROLS):
        raise ValueError("All three original fixed stock controls required")
    return result


def report(out, record):
    lines = ["# Fixed Family Ridge / Cash Ledger", "", "Research development only; NO_TRADE.",
             f"{START}..{END};140000CNY; original month-end20/rank30 selector and costs.",
             f"Training: {TRAIN_START}..{TRAIN_END};20-session next-open labels; alpha1; no grid/CV/return tuning.", "",
             "| Case | Status | Sessions | Net | MDD | Sharpe | Fees CNY |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in [*record["controls"], *record["results"]]:
        usable = row["status"] == "completed" and row.get("comparable_under_current_sources", True)
        if usable:
            lines.append(f"| {row['signal']} | completed | {row['completed_sessions']} | "
                         f"{row['net_return']:.2%} | {abs(row['max_drawdown']):.2%} | "
                         f"{row['sharpe_zero_rf_252']:.4f} | {row['fees']:.2f} |")
        else:
            lines.append(f"| {row['signal']} | failed / source-unavailable | {row.get('completed_sessions', 0)} | unknown | unknown | unknown | unknown |")
            lines.append(str(row.get("error") or row.get("comparison_error")))
    if record.get("model"):
        lines += ["", f"Training months: {record['model']['training_months']}; observed pairs: {record['model']['training_rows']}.",
                  f"Coefficients (relative-rank score, NOT expected CNY returns): `{record['model']['coefficients']}`."]
    if record.get("comparisons"):
        lines += ["", "## Same-Basis Net Increment", "",
                  "| Original control | Net difference pp | MDD magnitude difference pp | Fee difference CNY |",
                  "| --- | ---: | ---: | ---: |"]
        for row in record["comparisons"]:
            lines.append(f"| {row['control']} | {row['net_difference'] * 100:.2f} | "
                         f"{row['drawdown_magnitude_difference'] * 100:.2f} | {row['fee_difference']:.2f} |")
    lines += ["", "## Boundaries", "",
              "- Date-equal training loss: each observed training month has total sample weight1; alpha1 penalizes the squared coefficient norm. No fitted post-2024 target enters training.",
              "- Ranks/centering use the current finite-feature common pool before observing future labels. Training target ranks use observed endpoints only; missing labels remain unknown and can bias estimation.",
              "- Only three previously declared families, not all718 definitions or Alpha158 feature coefficients. Two trend inputs receive one family; training diagnostic directions are retained.",
              "- Frozen train direction was learned on the broader diagnostic pool, not the strict252-session common pool. Few month-end anchors and one training year do not establish robust alpha.",
              "- No industry/size neutrality, independent/PIT/fresh-holdout, prospective or black-swan acceptance. Seen2025/2026 are development evidence.",
              "- Original Engine/Ledger owns all cash/shares/orders/fills/T+1/lots/costs/actions; fractional receipts remain unknown by default. No new account engine or paper/live signal.",
              "- Controls reuse only complete compatible saved accounts; incompatible controls require an explicit refresh or stay unavailable. Not a matched LightGBM comparison (different frequency/period/pool).",
              f"- Method API: [scikit-learn Ridge]({RIDGE_SOURCE}); local installed version is recorded, no dependency changed.",
              f"- Saved model, scores, split counts and real replay outputs: `{record['artifacts']}`."]
    if record.get("error"):
        lines += ["", record["error"]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_id, *, event_view=None, reference_view=None, control_runs=(), refresh_controls=False):
    source_path = ROOT / "outputs/market_replay" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("status") != "completed_family_portfolio_diagnostics"
            or source.get("research_family") != "fixed_family_incremental"
            or source.get("start") != START or source.get("end") != END
            or source["protocol"]["fresh_holdout_claimed"] is not False):
        raise ValueError("Completed fixed-family2025/2026 development source required")
    source["output_source"] = str(source_path)
    inputs = bind_stock_inputs(source["protocol"]["stock_inputs"], event_view, reference_view)
    config = StrategyConfig(**source["protocol"]["signal_portfolio_configs"][CONTROLS[0]]).validate()
    if config != portfolio() or any(source["protocol"]["signal_portfolio_configs"][case] != config.to_dict() for case in CONTROLS):
        raise ValueError("Original common stock selector and140000CNY config required")
    controls = control_candidates(source, control_runs)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    protocol = {"id": ID, "portfolio": config.to_dict(), "signals": [SIGNAL],
                "signal_portfolio_configs": {SIGNAL: config.to_dict()}, "stock_inputs": inputs,
                "training_period": [TRAIN_START, TRAIN_END], "label_horizon": HORIZON,
                "families": list(FAMILIES), "alpha": ALPHA, "fit_intercept": True, "solver": "cholesky",
                "training_directions": source["protocol"]["training_directions"],
                "sample_weight": "1/observed_pairs_per_month", "min_training_months": MIN_MONTHS,
                "min_pairs_per_month": MIN_PAIRS, "parameters_searched": False,
                "source_run": str(source_path), "control_runs": list(control_runs),
                "alpha158_recomputed": False, "fractional_allocation": None,
                "fresh_holdout_claimed": False, "execution_admitted": False, "method_source": RIDGE_SOURCE}
    record = {"id": identifier, "status": "running", "research_family": "regularized_family_rank",
              "purpose": ID, "start": START, "end": END, "sessions": source["sessions"],
              "dataset": inputs["price_view"], "scope": "source_stocks_common252_pool_research_only",
              "artifacts": str(artifacts), "protocol": protocol, "controls": [], "results": [], "comparisons": [],
              "strategy_validated": False, "execution_admitted": False, "sklearn_version": version("scikit-learn")}
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    stocks, begin = None, time.monotonic()
    try:
        stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        validate_prefix_inputs(source["protocol"]["stock_manifest"], stocks.manifest, None)
        protocol["stock_manifest"] = stocks.manifest
        record["symbols"] = len(stocks.symbols) - len(stocks.unsupported_instruments)
        dates = [day for day in stocks.calendar if START <= day <= END]
        if not dates or dates[0] != START or dates[-1] != END or len(dates) != source["sessions"]:
            raise ValueError("Exact original424-day replay calendar required")
        anchors = [day for day in month_ends(stocks.calendar, END) if day >= TRAIN_START]
        native = read_native_monthends(stocks, inputs["native_view"], anchors)
        helper = LowRiskFeed(stocks, low_risk_features(stocks, END), VARIANTS[1], END, diagnostic_extension=True)
        matrices, pools = {}, []
        for day in anchors:
            pool = helper.pool(day, config)
            if not isinstance(pool, list):
                raise ValueError(f"Missing fixed family pool: {day}: {pool}")
            shared = common_pool(pool, native.loc[pd.Timestamp(day)])
            matrices[pd.Timestamp(day)] = family_matrix(shared, protocol["training_directions"])
            pools.append({"date": day, "original_pool": len(pool), "common_pool": len(shared)})
        features = pd.concat(matrices, names=["date", "symbol"]).sort_index()
        features.to_parquet(artifacts / "monthly_family_features.parquet")
        pd.DataFrame(pools).to_csv(artifacts / "pool_counts.csv", index=False)
        _, _, provider = native_binding(stocks, inputs["native_view"], catalog())
        calendar = pd.to_datetime(stocks.calendar)
        opening = read_open_panel(provider, stocks.symbols, calendar)
        forward = forward_open_labels(opening, HORIZON)
        model, train, counts = fit_training(features, forward, calendar)
        del opening, forward
        train.to_parquet(artifacts / "training_rows.parquet")
        counts.to_csv(artifacts / "training_split_counts.csv", index=False)
        metadata = {"id": ID, "coefficients": dict(zip(FAMILIES, model.coef_)), "intercept": model.intercept_,
                    "alpha": ALPHA, "training_rows": len(train), "training_months": train.index.get_level_values("date").nunique(),
                    "last_training_exit": counts.loc[counts.labeled_rows.gt(0), "exit_date"].max(),
                    "sklearn_version": record["sklearn_version"], "prediction_units": "relative return-rank score, NOT expected CNY/percent returns",
                    "execution_admitted": False}
        write_json(artifacts / "model.json", metadata)
        record["model"] = metadata
        signal_folder = artifacts / SIGNAL
        signal_inputs = artifacts / "predictions"
        signal_inputs.mkdir()
        record["prediction_rows"] = save_predictions(model, features, signal_inputs, config)
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        print(f"Frozen ridge: {len(train)} observed pairs / {metadata['training_months']} training months; {metadata['coefficients']}", flush=True)
        ranked = SavedRankFeed(stocks, signal_inputs, ID, config, dates)
        result = {"signal": SIGNAL, **baseline_ledger_result(ranked, config, dates, signal_folder)}
        if result["completed_sessions"]:
            result["annual_same_account"] = stage_metrics(signal_folder, config.initial_cash)
        record["results"].append(result)
        print(f"Ridge ledger: {result['status']} {result['completed_sessions']}/{len(dates)}; net={result.get('net_return')}", flush=True)
        write_json(out / "run.json", record)
        for case in CONTROLS:
            control_record, row, folder = controls[case]
            if row["status"] != "completed" or row["completed_sessions"] != len(dates):
                raise ValueError("Original control needs a complete original ledger")
            checkpoint = load_replay_checkpoint(folder, config, dates)
            try:
                ignored = validate_prefix_inputs(control_record["protocol"]["stock_manifest"], stocks.manifest,
                                                 checkpoint["state"]["day"], checkpoint["state"])
                matched, error = True, None
            except ValueError as failure:
                matched, ignored, error = False, [], str(failure)
            control = {**row, "source_artifacts": str(folder), "replayed": False,
                       "comparable_under_current_sources": matched, "comparison_error": error,
                       "uninvolved_source_changes": ignored}
            if not matched and refresh_controls:
                original_folder = Path(source["artifacts"]) / case
                control_feed = SavedRankFeed(stocks, original_folder, row["recipe"], config, dates)
                folder = artifacts / "controls" / case
                control = {"signal": case, **baseline_ledger_result(control_feed, config, dates, folder),
                           "source_artifacts": str(folder), "replayed": True}
                control["comparable_under_current_sources"] = control["status"] == "completed"
            record["controls"].append(control)
            if result["status"] == "completed" and control["status"] == "completed" and control["comparable_under_current_sources"]:
                record["comparisons"].append({"control": case, "net_difference": result["net_return"] - control["net_return"],
                    "drawdown_magnitude_difference": abs(result["max_drawdown"]) - abs(control["max_drawdown"]),
                    "fee_difference": result["fees"] - control["fees"]})
            write_json(out / "run.json", record)
            print(f"Control {case}: {control['status']}; matched={control['comparable_under_current_sources']}; replayed={control['replayed']}", flush=True)
        record["status"] = "completed_family_ridge_diagnostics" if result["status"] == "completed" else "failed_family_ridge_ledger"
    except Exception as error:
        record.update(status="failed_family_ridge_preparation", error=f"{type(error).__name__}: {error}")
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = round(time.monotonic() - begin, 3)
        write_json(out / "run.json", record)
        report(out, record)
    print(str(out), flush=True)
    return out, record


def expanding_report(out, record):
    lines = ["# Fixed Monthly Expanding Family Ridge", "", f"Status: {record['status']}. NO_TRADE.", "",
             "Question: does updating the same three-family Ridge with already matured labels improve the saved static fit?",
             f"Same {START}..{END} dates, common252 pool, 20/rank30 selector, 140000 synthetic CNY and original costs.",
             "At every original month-end: expanding train start2024-01-02; label exit no later than previous session.",
             "Original20-session next-open labels, equal-month loss weights, alpha1 and2024 factor directions retained.",
             "Only this update schedule added; no window, alpha, direction, seed, portfolio or final-period tuning.", "",
             "| Case | State / sessions | Net | MDD | Sharpe | Fees CNY | Account reused |",
             "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for row in [*record["controls"], *record["results"]]:
        if row["status"] == "completed":
            lines.append(f"| {row['signal']} | completed / {row['completed_sessions']} | {row['net_return']:.2%} | "
                         f"{abs(row['max_drawdown']):.2%} | {row['sharpe_zero_rf_252']:.4f} | {row['fees']:.2f} | {not row['replayed']} |")
        else:
            lines.append(f"| {row['signal']} | {row['status']} / {row.get('completed_sessions', 0)} | unknown | unknown | unknown | unknown | False |")
            lines.append(str(row.get("error")))
    if record.get("models"):
        first, last = record["models"][0], record["models"][-1]
        lines += ["", f"Saved fits: {len(record['models'])}; first/last training months {first['training_months']}/{last['training_months']}.",
                  f"Last coefficients: `{last['coefficients']}`; NOT expected CNY or portfolio weights."]
    for row in record["comparisons"]:
        lines.append(f"\nVersus {row['control']}: net {100 * row['net_difference']:+.2f}pp; "
                     f"MDD magnitude {100 * row['drawdown_magnitude_difference']:+.2f}pp; fees {row['fee_difference']:+.2f}CNY.")
    if record.get("results") and record["results"][0].get("annual_same_account"):
        lines += ["", "## Same Account Annual Stages", "", "No cash/position reset at year boundaries.", "",
                  "| Year | Net | MDD | Sharpe | Fees CNY |", "| --- | ---: | ---: | ---: | ---: |"]
        for row in record["results"][0]["annual_same_account"]:
            lines.append(f"| {row['year']} | {row['net_return']:.2%} | {abs(row['max_drawdown']):.2%} | "
                         f"{row['sharpe_zero_rf_252']:.4f} | {row['fees']:.2f} |")
    lines += ["", "## Boundaries", "",
              "- Existing saved features, current historical common pools and fixed direction are reused, not recomputed or rescreened. Only complete compatible controls are reused read-only.",
              "- All2025/2026 outcomes have already been inspected. Sequential label availability prevents this implementation's temporal leakage; it does NOT create an independent untouched test or historical PIT data.",
              "- Missing scheduled endpoint labels stay unknown; equal-month samples can still have selection bias. Adjusted provider returns are training labels only, not cash/share performance.",
              "- Source-covered SH/SZ equities, not independently complete A/BSE/ETF. Retrieval-time listing metadata and reconstructed actions retain original limitations.",
              "- Original Engine/Ledger owns cash/shares/orders/fills/costs/T+1/actions. No default strategy change, prospective account, live readiness or guaranteed drawdown/profit.",
              f"- Public method reference: [Qlib expanding/sliding RollingGen and label truncation]({ROLLING_SOURCE}); code path read, not full Qlib rolling experiment reproduction. Monthly schedule is this fixed local comparison, not Qlib's default40-session step.",
              f"- Estimator: [scikit-learn Ridge]({RIDGE_SOURCE}).", f"- Source: {record['source_run']}",
              f"- Artifacts: {record['artifacts']}"]
    if record.get("error"):
        lines.append("Failure: " + record["error"])
    if record.get("resume_run"):
        lines += [f"Saved continuation: {record['resume_run']}; models_refit=False; reused account sessions={record['reused_sessions']}."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_expanding_resume(previous, protocol):
    if (previous.get("status") not in {"failed_expanding_family_ledger", "completed_expanding_family_diagnostics"}
            or previous.get("purpose") != EXPANDING_ID or previous.get("execution_admitted") is not False
            or len(previous.get("results", [])) != 1 or previous["results"][0]["signal"] != EXPANDING_SIGNAL
            or not previous.get("models")):
        raise ValueError("Terminal saved expanding prediction/account source required")
    for key in ("id", "signals", "signal_portfolio_configs", "portfolio", "source_run", "cached_features",
                "training_period", "label_cutoff", "label_horizon", "families", "alpha", "solver",
                "fit_intercept", "training_directions", "sample_weight", "parameters_searched",
                "fresh_holdout_claimed", "execution_admitted"):
        if previous["protocol"].get(key) != protocol.get(key):
            raise ValueError(f"Saved expanding contract changed: {key}")
    for key in ("price_view", "listing_master", "native_view"):
        if previous["protocol"]["stock_inputs"][key] != protocol["stock_inputs"][key]:
            raise ValueError(f"Saved expanding input changed: {key}")


def run_expanding(source_id, *, resume_run=None, event_view=None, reference_view=None):
    source_path = ROOT / "outputs/market_replay" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("status") != "completed_family_ridge_diagnostics" or source.get("purpose") != ID
            or source["protocol"].get("id") != ID or source["protocol"].get("families") != list(FAMILIES)
            or source["protocol"].get("alpha") != ALPHA or source["protocol"].get("label_horizon") != HORIZON
            or source["protocol"].get("fresh_holdout_claimed") is not False
            or source["start"] != START or source["end"] != END):
        raise ValueError("Completed original fixed family Ridge development source required")
    config = StrategyConfig(**source["protocol"]["portfolio"]).validate()
    if config != portfolio() or [row["signal"] for row in source["results"]] != [SIGNAL]:
        raise ValueError("Original fixed portfolio and static Ridge baseline required")
    controls = [{**source["results"][0], "source_artifacts": str(Path(source["artifacts"]) / SIGNAL)},
                *source["controls"]]
    if ({row["signal"] for row in controls} != {SIGNAL, *CONTROLS} or len(controls) != 4
            or any(row["status"] != "completed" or row["completed_sessions"] != source["sessions"]
                   or not row.get("comparable_under_current_sources", True) for row in controls)):
        raise ValueError("All four completed compatible saved baseline accounts required")
    previous = (json.loads((ROOT / "outputs/market_replay" / resume_run / "run.json").read_text(encoding="utf-8"))
                if resume_run is not None else None)
    if previous is None and (event_view or reference_view):
        raise ValueError("Source amendments require saved predictions, never a new fit")
    inputs = bind_stock_inputs(previous["protocol"]["stock_inputs"] if previous else source["protocol"]["stock_inputs"],
                               event_view, reference_view)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    protocol = {**source["protocol"], "id": EXPANDING_ID, "signals": [EXPANDING_SIGNAL],
                "signal_portfolio_configs": {EXPANDING_SIGNAL: config.to_dict()},
                "source_run": str(source_path), "cached_features": str(Path(source["artifacts"]) / "monthly_family_features.parquet"),
                "training_period": [TRAIN_START, "expanding_to_previous_session_of_each_month_end"],
                "label_cutoff": "scheduled_exit <= previous_pinned_session; never feature_date alone",
                "method_source": ROLLING_SOURCE, "parameters_searched": False,
                "fresh_holdout_claimed": False, "execution_admitted": False}
    protocol["stock_inputs"] = inputs
    if previous is not None:
        validate_expanding_resume(previous, protocol)
    record = {"id": identifier, "status": "running", "research_family": "expanding_family_rank", "purpose": EXPANDING_ID,
              "source_run": str(source_path), "start": START, "end": END, "sessions": source["sessions"],
              "dataset": inputs["price_view"], "scope": source["scope"], "artifacts": str(artifacts), "protocol": protocol,
              "controls": [], "results": [], "comparisons": [], "models": [], "strategy_validated": False,
              "execution_admitted": False, "sklearn_version": version("scikit-learn")}
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    stocks, begin = None, time.monotonic()
    try:
        stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        validate_prefix_inputs(source["protocol"]["stock_manifest"], stocks.manifest, None)
        protocol["stock_manifest"] = stocks.manifest
        record["symbols"] = len(stocks.symbols) - len(stocks.unsupported_instruments)
        dates = [day for day in stocks.calendar if START <= day <= END]
        if not dates or dates[0] != START or dates[-1] != END or len(dates) != source["sessions"]:
            raise ValueError("Exact source replay calendar required")
        for control in controls:
            checkpoint = load_replay_checkpoint(Path(control["source_artifacts"]), config, dates)
            validate_prefix_inputs(source["protocol"]["stock_manifest"], stocks.manifest,
                                   checkpoint["state"]["day"], checkpoint["state"])
            record["controls"].append({**control, "replayed": False})
        features = pd.read_parquet(protocol["cached_features"])
        expected = pd.to_datetime([day for day in month_ends(stocks.calendar, END) if day >= TRAIN_START])
        if (features.index.names != ["date", "symbol"] or not features.index.is_monotonic_increasing
                or not features.index.get_level_values("date").unique().equals(expected)
                or not set(features.index.get_level_values("symbol")) <= set(stocks.symbols)):
            raise ValueError("Original complete ordered family feature decisions required")
        signal_inputs = artifacts / "predictions"
        checkpoint = None
        if previous is None:
            _, _, provider = native_binding(stocks, inputs["native_view"], catalog())
            calendar = pd.to_datetime(stocks.calendar)
            opening = read_open_panel(provider, stocks.symbols, calendar)
            forward = forward_open_labels(opening, HORIZON)
            scored, models, counts = expanding_predictions(features, forward, calendar)
            del opening, forward
            write_json(artifacts / "monthly_models.json", models)
            counts.to_csv(artifacts / "training_split_counts.csv", index=False)
            signal_inputs.mkdir()
            record["prediction_rows"] = save_rankings(scored, signal_inputs, config)
        else:
            old = Path(previous["artifacts"])
            checkpoint = load_replay_checkpoint(old / EXPANDING_SIGNAL, config, dates)
            ignored = validate_prefix_inputs(previous["protocol"]["stock_manifest"], stocks.manifest,
                                             checkpoint["state"]["day"], checkpoint["state"])
            models = json.loads((old / "monthly_models.json").read_text(encoding="utf-8"))
            if models != previous["models"]:
                raise ValueError("Saved monthly fit receipts changed")
            shutil.copytree(old / "predictions", signal_inputs)
            for name in ("monthly_models.json", "training_split_counts.csv"):
                shutil.copy2(old / name, artifacts / name)
            record.update(resume_run=resume_run, models_refit=False, reused_sessions=len(checkpoint["daily"]),
                          prediction_rows=previous["prediction_rows"], uninvolved_source_changes=ignored)
        record["models"] = models
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        print(f"Expanding fits {len(models)}; refit={previous is None}; training months {models[0]['training_months']}..{models[-1]['training_months']}; four saved controls reused", flush=True)
        ranked = SavedRankFeed(stocks, signal_inputs, EXPANDING_ID, config, dates)
        signal_folder = artifacts / EXPANDING_SIGNAL
        result = {"signal": EXPANDING_SIGNAL, "replayed": True,
                  **baseline_ledger_result(ranked, config, dates, signal_folder, checkpoint=checkpoint)}
        if result["completed_sessions"]:
            result["annual_same_account"] = stage_metrics(signal_folder, config.initial_cash)
        record["results"].append(result)
        if result["status"] == "completed":
            for control in record["controls"]:
                record["comparisons"].append({"control": control["signal"], "net_difference": result["net_return"] - control["net_return"],
                    "drawdown_magnitude_difference": abs(result["max_drawdown"]) - abs(control["max_drawdown"]),
                    "fee_difference": result["fees"] - control["fees"]})
        record["status"] = "completed_expanding_family_diagnostics" if result["status"] == "completed" else "failed_expanding_family_ledger"
    except Exception as error:
        record.update(status="failed_expanding_family_preparation", error=f"{type(error).__name__}: {error}")
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = round(time.monotonic() - begin, 3)
        write_json(out / "run.json", record)
        expanding_report(out, record)
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description="One fixed train2024 family ridge score through the sole stock ledger")
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--event-view")
    parser.add_argument("--reference-view")
    parser.add_argument("--control-run", action="append", default=[], help="Explicit saved original-control refresh; not an implementation arm")
    parser.add_argument("--refresh-controls", action="store_true", help="Replay only source-incompatible original controls; reuse compatible full accounts")
    parser.add_argument("--expanding", action="store_true", help="Fixed monthly expanding fit; source-run must be the completed static Ridge, all controls reused")
    parser.add_argument("--resume-run", help="Saved terminal expanding predictions/account; paired event/reference amendments only")
    args = parser.parse_args(argv)
    if args.expanding:
        if args.control_run or args.refresh_controls or (not args.resume_run and (args.event_view or args.reference_view)):
            parser.error("Expanding fit retains original inputs; amendments require --resume-run, all controls remain saved")
        _, record = run_expanding(args.source_run, resume_run=args.resume_run,
                                  event_view=args.event_view, reference_view=args.reference_view)
        return 0 if record["status"] == "completed_expanding_family_diagnostics" else 1
    if args.resume_run:
        parser.error("Saved expanding continuation requires --expanding")
    _, record = run(args.source_run, event_view=args.event_view, reference_view=args.reference_view,
                    control_runs=args.control_run, refresh_controls=args.refresh_controls)
    return 0 if record["status"] == "completed_family_ridge_diagnostics" else 1
