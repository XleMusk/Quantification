"""Read fixed forecasts and selection events; no accounts or strategy tuning."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd

from .. import replay_reports
from ..config import StrategyConfig
from ..expected_returns import EqualSlotReturnPolicy, POLICY_ID
from ..settings import DATA_ROOT, ROOT
from .experiment import write_json
from .family_ridge import ALPHA, FAMILIES, HORIZON, MIN_MONTHS, TRAIN_END
from .return_cost_replay import CASES, ID as SOURCE_ID, fit_return_training


ID = "fixed_return_forecast_selection_diagnostic_v1"
LIMITS = [
    "No replay, new account, forecast replacement, parameter search or execution admission; NO_TRADE",
    "2024 model-fit folds use only labels exiting strictly before the decision, with the original minimum8 months",
    "Family directions were previously learned using2024; these conditional folds do NOT validate the whole pipeline out of sample",
    "Provider-adjusted20-session relative labels are NOT executable, industry-neutral or benchmark excess returns",
    "Few monthly anchors, dependent securities and possibly overlapping horizons do NOT establish calibrated uncertainty",
    "Selection cost and benefit are decision-close estimates, NOT actual fee savings or counterfactual realized PnL",
    "Each raw frontier belongs to the SAME saved account at that decision; the two accounts diverge after execution",
    "Retention is measured before the saved decision, not reconstructed daily holdings or causal loss attribution",
    "All2025/2026 results remain seen development data; industry/PIT, stale marks and prospective evidence remain unresolved",
]


def conditional_folds(train, features, counts, calendar, holdings):
    counts = counts.copy()
    counts["date"] = pd.to_datetime(counts.date)
    counts["exit_date"] = pd.to_datetime(counts.exit_date)
    if counts.date.duplicated().any() or counts.exit_date.isna().any():
        raise ValueError("Distinct dated training split receipts required")
    labeled = counts.loc[counts.labeled_rows.gt(0)]
    calendar = pd.DatetimeIndex(calendar)
    for split in labeled.itertuples():
        exit_location = calendar.get_loc(split.date) + HORIZON + 1
        if (exit_location >= len(calendar) or calendar[exit_location] != split.exit_date
                or split.exit_date > pd.Timestamp(TRAIN_END)):
            raise ValueError("Conditional evaluation label must have its original scheduled exit")
    dates = train.index.get_level_values("date")
    if (train.index.has_duplicates or features.index.has_duplicates
            or features.index.names != ["date", "symbol"]
            or not np.isfinite(features[list(FAMILIES)].to_numpy()).all()
            or set(dates.unique()) != set(labeled.date)
            or any(int(row.labeled_rows) != int((dates == row.date).sum()) for row in labeled.itertuples())):
        raise ValueError("Cached training/features disagree with the original split receipts")
    summaries, predictions, skipped = [], [], []
    for split in labeled.itertuples():
        day = split.date
        eligible = labeled.loc[(labeled.date < day) & (labeled.exit_date < day)]
        if len(eligible) < MIN_MONTHS:
            skipped.append({"date": day.strftime("%Y-%m-%d"), "matured_training_months": len(eligible),
                            "reason": "original_minimum_training_months"})
            continue
        past = train.loc[dates.isin(eligible.date)]
        model, _ = fit_return_training(past, eligible, calendar)
        batch = features.xs(day, level="date")[list(FAMILIES)].copy()
        if len(batch) != split.signal_rows or len(batch) < holdings:
            raise ValueError("Conditional evaluation requires the original full feature pool")
        labels = train.xs(day, level="date").forward_return
        if not labels.index.isin(batch.index).all():
            raise ValueError("Training labels are absent from their original feature pool")
        batch["prediction"] = model.predict(batch[list(FAMILIES)])
        # Choose the head before joining unknown outcomes; never promote a lower rank.
        head = batch.reset_index().sort_values(["prediction", "symbol"], ascending=[False, True]).head(holdings).symbol
        batch["relative_label"] = labels - labels.mean()
        known = batch.relative_label.notna()
        if not np.isfinite(batch.loc[known, "relative_label"]).all():
            raise ValueError("Nonfinite cached forward label")
        error = batch.loc[known, "prediction"] - batch.loc[known, "relative_label"]
        rmse = float(np.sqrt(np.mean(error ** 2)))
        zero_rmse = float(np.sqrt(np.mean(batch.loc[known, "relative_label"] ** 2)))
        head_labels = batch.loc[head, "relative_label"]
        summary = {"date": day.strftime("%Y-%m-%d"), "label_exit": split.exit_date.strftime("%Y-%m-%d"),
            "training_months": len(eligible), "training_rows": len(past),
            "last_training_exit": eligible.exit_date.max().strftime("%Y-%m-%d"),
            "feature_rows": len(batch), "known_labels": int(known.sum()), "unknown_labels": int((~known).sum()),
            "coefficients": dict(zip(FAMILIES, model.coef_)), "rmse": rmse, "zero_forecast_rmse": zero_rmse,
            "mse_skill_vs_zero": 1 - rmse ** 2 / zero_rmse ** 2 if zero_rmse else None,
            "prediction_std": float(batch.prediction.std(ddof=0)),
            "label_std": float(batch.loc[known, "relative_label"].std(ddof=0)),
            "absolute_error_p90": float(error.abs().quantile(.9)),
            "rank_ic": float(batch.loc[known, "prediction"].corr(batch.loc[known, "relative_label"], method="spearman")),
            "head_size": holdings, "head_known_labels": int(head_labels.notna().sum()),
            "head_mean_prediction": float(batch.loc[head, "prediction"].mean()),
            "complete_head_relative_label": float(head_labels.mean()) if head_labels.notna().all() else None}
        summaries.append(summary)
        batch["head_selected"] = batch.index.isin(head)
        batch["date"] = day
        predictions.append(batch.reset_index())
    panel = pd.concat(predictions, ignore_index=True) if predictions else pd.DataFrame()
    summary = {"folds": summaries, "skipped": skipped, "monthly_units": len(summaries),
        "known_pair_rows": sum(row["known_labels"] for row in summaries),
        "equal_month_mse_skill_vs_zero": None,
        "uncertainty_calibrated": False, "whole_pipeline_out_of_sample": False}
    if summaries:
        mse = np.mean([row["rmse"] ** 2 for row in summaries])
        baseline = np.mean([row["zero_forecast_rmse"] ** 2 for row in summaries])
        summary["equal_month_mse_skill_vs_zero"] = float(1 - mse / baseline) if baseline else None
    return summary, panel


def selection_event(event, forecasts, holdings):
    if (event["policy_id"] != POLICY_ID or event["horizon_sessions"] != HORIZON
            or event["cost_scale"] != 1 or event["estimate_is_execution"] is not False):
        raise ValueError("Original fixed diagnostic selection policy required")
    detail = pd.DataFrame(event["diagnostics"]).set_index("symbol")
    selected = set(event["selected_symbols"])
    changed = set(event["cost_changed_members"])
    raw = selected ^ changed
    if (detail.index.has_duplicates or len(selected) != holdings or len(raw) != holdings
            or not (selected | raw) <= set(detail.index)
            or not detail.index.isin(forecasts.index).all()
            or event["candidate_count"] != len(forecasts)):
        raise ValueError("Saved selection must preserve both complete same-account frontiers")
    numeric = ["expected_return_fraction", "close_legal_notional_cents", "forecast_relative_benefit_cents",
               "estimated_include_cost_cents", "estimated_exclude_cost_cents", "selection_utility_cents"]
    if not np.isfinite(detail[numeric].to_numpy()).all():
        raise ValueError("Nonfinite saved selection estimate")
    if not np.allclose(detail.expected_return_fraction, forecasts.loc[detail.index, "expected_return"], rtol=0, atol=1e-12):
        raise ValueError("Selection event differs from the frozen forecasts")
    benefit = detail.close_legal_notional_cents * detail.expected_return_fraction
    penalty = detail.estimated_include_cost_cents - detail.estimated_exclude_cost_cents
    utility = benefit - (penalty if event["cost_aware"] else 0)
    if (not np.allclose(benefit, detail.forecast_relative_benefit_cents)
            or not np.allclose(utility, detail.selection_utility_cents)):
        raise ValueError("Saved additive benefit/cost units disagree")
    ordered = detail.reset_index()
    computed_raw = set(ordered.sort_values(["forecast_relative_benefit_cents", "symbol"], ascending=[False, True]).head(holdings).symbol)
    computed_chosen = set(ordered.sort_values(["selection_utility_cents", "symbol"], ascending=[False, True]).head(holdings).symbol)
    if raw != computed_raw or selected != computed_chosen:
        raise ValueError("Saved frontiers differ from the declared fixed additive selector")
    base_cost = event["estimated_departed_exit_cost_cents"] + detail.estimated_exclude_cost_cents.sum()

    def group(symbols):
        rows = detail.loc[sorted(symbols)]
        capital = float(rows.close_legal_notional_cents.sum())
        return {"members": len(rows), "held_before": int(rows.held_quantity.gt(0).sum()),
                "legal_notional_cny": capital / 100,
                "forecast_benefit_cny": float(rows.forecast_relative_benefit_cents.sum()) / 100,
                "mean_prediction": float(rows.expected_return_fraction.mean()) if len(rows) else None,
                "mean_prediction_rank": float(forecasts.expected_return.rank(ascending=False, method="min").loc[rows.index].mean()) if len(rows) else None,
                "family_mean": {name: float(forecasts.loc[rows.index, name].mean()) if len(rows) else None for name in FAMILIES}}

    actual, frontier = group(selected), group(raw)
    selected_cost = float(base_cost + penalty.loc[sorted(selected)].sum()) / 100
    raw_cost = float(base_cost + penalty.loc[sorted(raw)].sum()) / 100
    if not np.isclose(selected_cost, event["estimated_selected_transition_cost_cents"] / 100, rtol=0, atol=1e-8):
        raise ValueError("Estimated transition cost does not reconcile with saved event")
    benefit_loss = frontier["forecast_benefit_cny"] - actual["forecast_benefit_cny"]
    cost_saving = raw_cost - selected_cost
    if benefit_loss < -1e-8 or (event["cost_aware"] and cost_saving - benefit_loss < -1e-8):
        raise ValueError("Exact additive selector failed its recorded estimate objective")
    row = {"date": event["day"], "cost_aware": event["cost_aware"], "candidates": event["candidate_count"],
           "changed_slots": len(changed) // 2, "selected": actual, "same_account_raw_frontier": frontier,
           "added_to_raw_frontier": group(selected - raw), "dropped_from_raw_frontier": group(raw - selected),
           "estimated_selected_cost_cny": selected_cost, "estimated_raw_frontier_cost_cny": raw_cost,
           "forecast_benefit_given_up_cny": benefit_loss, "estimated_cost_saving_cny": cost_saving,
           "estimated_net_utility_gain_cny": cost_saving - benefit_loss,
           "selected_legal_gross": event["estimated_selected_legal_gross"],
           "selected_zero_legal_quantity": event["selected_zero_legal_quantity"]}
    detail["selected"] = detail.index.isin(selected)
    detail["same_account_raw_frontier"] = detail.index.isin(raw)
    detail["held_before"] = detail.held_quantity.gt(0)
    detail["prediction_rank"] = forecasts.expected_return.rank(ascending=False, method="min").reindex(detail.index)
    for name in FAMILIES:
        detail[name] = forecasts.loc[detail.index, name]
    detail["date"] = event["day"]
    return row, detail.reset_index()


def report(out, record):
    lines = ["# Fixed Return Forecast / Saved Selection Diagnostic", "", record["status"] + "; NO_TRADE.",
             "No historical account rerun, no production forecast refit, no strategy change.", ""]
    folds = record.get("conditional_model_fit", {})
    if folds:
        lines += ["## 2024 Conditional Time-Separated Model Fits", "",
                  f"Monthly units: {folds['monthly_units']}; known pair rows: {folds['known_pair_rows']}.",
                  "Directions already used2024: NOT whole-pipeline independent validation or calibrated uncertainty.", "",
                  "|Decision|Prior matured months|Prediction std|Label std|MSE skill vs zero|Rank IC|Head20 predicted|Head20 relative label / coverage|",
                  "|---|---:|---:|---:|---:|---:|---:|---|"]
        for row in folds["folds"]:
            head = f"{row['complete_head_relative_label']:.3%}" if row["complete_head_relative_label"] is not None else "unknown"
            lines.append(f"|{row['date']}|{row['training_months']}|{row['prediction_std']:.3%}|{row['label_std']:.3%}|"
                         f"{row['mse_skill_vs_zero']:.4f}|{row['rank_ic']:.4f}|{row['head_mean_prediction']:.3%}|"
                         f"{head} / {row['head_known_labels']}/{row['head_size']}|")
    if record.get("cases"):
        lines += ["", "## Saved Decisions: Each Account Versus Its Own Raw Frontier", "",
                  "CNY figures below sum separate close-time estimates, NOT compounded returns or realized savings.", "",
                  "|Case|Decisions|Changed slots|Mean retained /20|Predicted benefit given up CNY|Estimated cost saving CNY|Estimated utility gain CNY|",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for row in record["cases"]:
            lines.append(f"|{row['signal']}|{row['decisions']}|{row['changed_slots']}|{row['mean_selected_held_before']:.2f}|"
                         f"{row['forecast_benefit_given_up_cny']:.2f}|{row['estimated_cost_saving_cny']:.2f}|{row['estimated_net_utility_gain_cny']:.2f}|")
            if row["changed_slots"]:
                lines.append(f"Added-to-frontier prior holdings: {row['added_to_frontier_held_before']}/{row['changed_slots']}; "
                             f"its own raw frontier retains {row['mean_raw_frontier_held_before']:.2f}/20 on average.")
        lines += ["", "Per-month raw/selected/added/dropped membership, forecast rank and three-family exposure: selection.json and selection_members.csv."]
    lines += ["", "## Limits", "", *["- " + text for text in LIMITS], "", f"Artifacts: `{record['artifacts']}`."]
    if record.get("error"):
        lines += ["", record["error"]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(identifier):
    source, artifacts = replay_reports._read(identifier)
    protocol = source["protocol"]
    if (source.get("purpose") != SOURCE_ID or source.get("status") != "completed_return_cost_diagnostics"
            or source.get("execution_admitted") is not False or protocol["alpha"] != ALPHA
            or protocol["label_horizon"] != HORIZON or protocol["families"] != list(FAMILIES)
            or {row["signal"] for row in source["results"]} != set(CASES)):
        raise ValueError("Complete unadmitted fixed-return paired source required")
    config = StrategyConfig(**protocol["portfolio"]).validate()
    if any(protocol["selection_policies"][case] != EqualSlotReturnPolicy(cost).contract(config)
           for case, cost in CASES.items()):
        raise ValueError("Saved selection contracts changed")
    output_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs/return_forecast_diagnostics" / output_id
    external = DATA_ROOT / "research/return_forecast_diagnostics" / output_id
    out.mkdir(parents=True, exist_ok=False)
    external.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, external / "implementation.py")
    record = {"id": output_id, "purpose": ID, "status": "running", "source_run": identifier,
              "artifacts": str(external), "cases": [], "parameters_searched": False,
              "production_model_refit": False, "replayed": False, "execution_admitted": False, "limits": LIMITS}
    write_json(out / "run.json", record)
    try:
        for key in ("cached_train", "cached_features"):
            Path(protocol[key]).resolve().relative_to((DATA_ROOT / "research/market_replay").resolve())
        train = pd.read_parquet(protocol["cached_train"])
        features = pd.read_parquet(protocol["cached_features"])
        counts = pd.read_csv(artifacts / "training_split_counts.csv")
        calendar = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(Path(protocol["stock_inputs"]["event_view"]) / "source_input_calendar.csv").date))
        if not calendar.is_unique or not calendar.is_monotonic_increasing or pd.Timestamp(TRAIN_END) not in calendar:
            raise ValueError("Original ordered full trading calendar required")
        forecasts = pd.read_parquet(artifacts / "monthly_return_forecasts.parquet")
        model = json.loads((artifacts / "return_model.json").read_text(encoding="utf-8"))
        if (forecasts.index.has_duplicates or forecasts.index.names != ["date", "symbol"]
                or not np.isfinite(forecasts[[*FAMILIES, "expected_return"]].to_numpy()).all()
                or len(forecasts) != source["prediction_rows"]):
            raise ValueError("Distinct finite frozen forecast panel required")
        if (not forecasts.index.isin(features.index).all()
                or not np.allclose(forecasts[list(FAMILIES)], features.loc[forecasts.index, list(FAMILIES)], rtol=0, atol=1e-12)):
            raise ValueError("Frozen forecast features differ from the original cached panel")
        expected = forecasts[list(FAMILIES)].to_numpy() @ np.array([model["coefficients"][name] for name in FAMILIES]) + model["intercept"]
        if not np.allclose(expected, forecasts.expected_return, rtol=0, atol=1e-12):
            raise ValueError("Frozen forecasts differ from the saved model")
        record["fixed_forecast"] = {"model": model, "rows": len(forecasts),
            "monthly_units": forecasts.index.get_level_values("date").nunique(),
            "prediction_min": float(forecasts.expected_return.min()), "prediction_max": float(forecasts.expected_return.max()),
            "conditional_fit_only": True}
        fit, panel = conditional_folds(train, features, counts, calendar, protocol["portfolio"]["holdings"])
        record["conditional_model_fit"] = fit
        write_json(external / "conditional_fits.json", fit)
        panel.to_parquet(external / "conditional_predictions.parquet")
        print(f"Conditional2024 fits: {fit['monthly_units']} months, {fit['known_pair_rows']} known pairs; no whole-pipeline OOS claim", flush=True)
        all_members = []
        for case in source["results"]:
            folder = Path(case["source_artifacts"]).resolve()
            folder.relative_to(artifacts.resolve())
            account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
            events = [event for event in account["events"] if event["kind"] == "expected_return_selection"]
            days = forecasts.index.get_level_values("date").unique().strftime("%Y-%m-%d").tolist()
            if (account.get("broker_binding") or account["day"] != source["end"] or case["status"] != "completed"
                    or case["completed_sessions"] != source["sessions"] or [event["day"] for event in events] != days
                    or any(event["cost_aware"] != CASES[case["signal"]] for event in events)):
                raise ValueError("Complete same-date unbound historical selection account required")
            decisions = []
            for event in events:
                row, members = selection_event(event, forecasts.xs(pd.Timestamp(event["day"]), level="date"), protocol["portfolio"]["holdings"])
                decisions.append(row)
                members["signal"] = case["signal"]
                all_members.append(members)
            summary = {"signal": case["signal"], "decisions": len(decisions),
                "changed_slots": sum(row["changed_slots"] for row in decisions),
                "mean_selected_held_before": float(np.mean([row["selected"]["held_before"] for row in decisions])),
                "mean_raw_frontier_held_before": float(np.mean([row["same_account_raw_frontier"]["held_before"] for row in decisions])),
                "added_to_frontier_held_before": sum(row["added_to_raw_frontier"]["held_before"] for row in decisions),
                "dropped_from_frontier_held_before": sum(row["dropped_from_raw_frontier"]["held_before"] for row in decisions),
                **{key: sum(row[key] for row in decisions) for key in ("forecast_benefit_given_up_cny", "estimated_cost_saving_cny", "estimated_net_utility_gain_cny")},
                "decisions_detail": decisions}
            record["cases"].append(summary)
            print(f"{case['signal']}: {summary['decisions']} decisions, {summary['changed_slots']} changed slots; estimated benefit given up={summary['forecast_benefit_given_up_cny']:.2f}, cost saving={summary['estimated_cost_saving_cny']:.2f} CNY", flush=True)
        pd.concat(all_members, ignore_index=True).to_csv(external / "selection_members.csv", index=False)
        write_json(external / "selection.json", record["cases"])
        record["status"] = "completed_saved_forecast_diagnostics"
    except Exception as error:
        record.update(status="failed_saved_forecast_diagnostics", error=f"{type(error).__name__}: {error}")
    write_json(out / "run.json", record)
    report(out, record)
    print(out, flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read frozen return forecasts and actual saved selection estimates")
    parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    _, record = run(args.run)
    return 0 if record["status"] == "completed_saved_forecast_diagnostics" else 1
