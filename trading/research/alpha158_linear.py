"""Matched Alpha158 fixed Ridge and all-seed rank ensemble, using saved native data."""
import argparse
from copy import deepcopy
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
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .bulk_model_inputs import dated_eligibility, native_binding
from .experiment import ScoreFeed, baseline_ledger_result, export_scores, load_replay_checkpoint, write_json
from .factors import LABEL, catalog, daily_ic, ic_summary, segment_mask
from .market_replay import validate_prefix_inputs


ID = "alpha158_fixed_linear_allseed_comparison_v1"
SIGNALS = ("alpha158_fixed_ridge", "alpha158_allseed_rank_mean")
SEEDS = (17, 29, 43)
ALPHA = 1.0
METHOD_SOURCES = ["https://scikit-learn.org/1.7/modules/generated/sklearn.linear_model.Ridge.html",
                  "https://scikit-learn.org/1.7/modules/generated/sklearn.preprocessing.StandardScaler.html"]


def saved_score(path):
    frame = pd.read_parquet(path)
    if set(frame.columns) != {"datetime", "instrument", "score", "available_at", "mode"}:
        raise ValueError("Saved predictions require the original dated research schema")
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    expected = (frame["datetime"].dt.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15, minutes=5)).dt.tz_convert("UTC")
    if (not frame["mode"].eq("historical_research_only").all()
            or not pd.to_datetime(frame["available_at"], utc=True, format="ISO8601").equals(expected)):
        raise ValueError("Saved prediction mode/availability differs from the original close-time contract")
    score = frame.set_index(["datetime", "instrument"]).score.rename_axis(["date", "symbol"]).sort_index()
    if score.index.has_duplicates or not np.isfinite(score.to_numpy()).all():
        raise ValueError("Seed predictions must be distinct and finite; missing seeds cannot be averaged away")
    return score


def equal_seed_ranks(scores):
    if tuple(scores) != SEEDS or any(not score.index.equals(scores[SEEDS[0]].index) for score in scores.values()):
        raise ValueError("All fixed seeds and identical prediction pools are required")
    return pd.DataFrame(scores).groupby(level="date").rank(pct=True).mean(axis=1).rename("score")


def standardized_target(target):
    mean = target.groupby(level="date").transform("mean")
    std = target.groupby(level="date").transform("std")
    return ((target - mean) / std.where(std > 0)).fillna(0)


def fit_fixed(train, target, medians):
    if (not train.index.equals(target.index) or train.columns.tolist() != medians.index.tolist()
            or not np.isfinite(target.to_numpy()).all() or not np.isfinite(medians.to_numpy()).all()):
        raise ValueError("Aligned observed training labels and saved train medians required")
    values = train.fillna(medians).to_numpy(dtype=np.float64, copy=True)
    scaler = StandardScaler(copy=False)
    model = Ridge(alpha=ALPHA, fit_intercept=True, solver="cholesky", copy_X=False)
    with threadpool_limits(limits=2):
        values = scaler.fit_transform(values)
        model.fit(values, standardized_target(target).to_numpy(dtype=np.float64))
    return model, scaler


def predict_fixed(model, scaler, features, medians):
    if features.columns.tolist() != medians.index.tolist():
        raise ValueError("Inference feature order changed")
    values = features.fillna(medians).to_numpy(dtype=np.float64, copy=True)
    with threadpool_limits(limits=2):
        score = model.predict(scaler.transform(values))
    if not np.isfinite(score).all():
        raise ValueError("Nonfinite linear predictions")
    return pd.Series(score, index=features.index, name="score")


def source_models(source):
    if (source.get("status") != "completed" or source.get("experiment_kind") != "alpha158_ingestion_ledger_comparison"
            or source.get("candidate_search_performed") is not False or source.get("fresh_holdout_claimed") is not False
            or source.get("live_ready") is not False or source["config"]["seeds"] != list(SEEDS)):
        raise ValueError("Completed fixed all-seed full-source diagnostic required")
    folders = {seed: Path(source["model_sources"][f"alpha158_seed{seed}"]) for seed in SEEDS}
    roots = {folder.parent for folder in folders.values()}
    if len(roots) != 1:
        raise ValueError("Seed models must have one shared original training run")
    root = roots.pop()
    original = json.loads((ROOT / "outputs/factor_mining" / root.name / "run.json").read_text(encoding="utf-8"))
    if (original.get("config") != source["config"]
            or any(original["bulk_inputs"][key] != source["bulk_inputs"][key] for key in ("price_view", "native_view", "listing_master"))):
        raise ValueError("Native price/pool/model training configuration changed")
    medians, scores = {}, {}
    for seed, folder in folders.items():
        medians[seed] = pd.read_csv(folder / "preprocessing.csv", index_col=0)["train_median"].astype("float32")
        scores[seed] = saved_score(folder / "historical_scores.parquet")
    if any(not value.equals(medians[SEEDS[0]]) for value in medians.values()):
        raise ValueError("Saved seed training preprocessing differs")
    equal_seed_ranks(scores)
    return root, folders, medians[SEEDS[0]], scores


def load_matched_features(feed, portfolio, native_view, labels, segments, expected):
    definitions = catalog()
    native_view, receipt, _ = native_binding(feed, native_view, definitions)
    if (labels.index.names != ["date", "symbol"] or labels.index.has_duplicates
            or len(labels) != receipt["rows"]):
        raise ValueError("Cached labels do not have the original provider calendar grid")
    eligible = dated_eligibility(feed, portfolio)
    if not labels.index.sort_values().equals(eligible.index.sort_values()):
        raise ValueError("Cached label security/date identities changed")
    train_frames, evaluation_frames = [], []
    counts = {"train_eligible": 0, "train_labeled": 0, "evaluation_eligible": 0}
    for part in receipt["partitions"]:
        frame = pd.read_parquet(native_view / part["file"])
        wanted = pd.MultiIndex.from_product([pd.to_datetime(feed.calendar), part["symbols"]], names=["date", "symbol"]).sort_values()
        if not frame.index.equals(wanted) or frame.columns.tolist() != definitions.name.tolist() or len(frame) != part["rows"]:
            raise ValueError("Native feature code/date/formula identities changed")
        allowed = eligible.reindex(frame.index, fill_value=False).to_numpy()
        train_dates = segment_mask(frame.index, labels, *segments["train"], require_label=False)
        train = allowed & segment_mask(frame.index, labels, *segments["train"])
        evaluation = allowed & segment_mask(frame.index, labels, *segments["test"], require_label=False)
        train_frames.append(frame.loc[train])
        evaluation_frames.append(frame.loc[evaluation])
        counts["train_eligible"] += int((allowed & train_dates).sum())
        counts["train_labeled"] += int(train.sum())
        counts["evaluation_eligible"] += int(evaluation.sum())
        print(f"Reuse native {part['file']}: train={int(train.sum())}, evaluation={int(evaluation.sum())}", flush=True)
    train = pd.concat(train_frames).sort_index()
    evaluation = pd.concat(evaluation_frames).sort_index()
    if (counts["train_eligible"] != int(expected.loc["train", "eligible_signals"])
            or counts["train_labeled"] != int(expected.loc["train", "labeled_rows"])
            or counts["evaluation_eligible"] != int(expected.loc["test", "eligible_signals"])
            or train.empty or evaluation.empty):
        raise ValueError("Linear input pools do not match the original full-source model splits")
    return train, evaluation, counts


def report(out, record):
    lines = ["# Matched Alpha158 Linear / All-Seed Comparison", "", f"Status: {record['status']}. NO_TRADE.", "",
             "Frozen158 native features, original five-session next-open labels,2024 training pool/medians and2026 inference pool.",
             "Fixed Ridge(alpha1,cholesky) adds training-only StandardScaler, same daily-zscore labels as LightGBM.",
             "No feature selection, alpha grid, validation-return fitting or2026 feedback. Existing LightGBM used2025 early stopping;",
             "Ridge does NOT use2025 labels. This is a declared method comparison, not model complexity as the sole changed variable.",
             "Equal ensemble = mean of all THREE seeds' within-day percentile ranks, NOT average account equity / best seed.",
             "Five holdings/rank8, rebalance every5 sessions,140000CNY, original costs/dates and sole Engine/Ledger.",
             "Cached native features/labels and all original model accounts reused.2026 is inspected development, not independent test.", "",
             "|Case|State/sessions|Net|MDD|Sharpe|Fees|", "|---|---|---:|---:|---:|---:|"]
    for row in record["controls"] + record["results"]:
        if row["status"] == "completed":
            lines.append(f"|{row['signal']}|completed/{row['completed_sessions']}|{row['net_return']:.2%}|"
                         f"{abs(row['max_drawdown']):.2%}|{row['sharpe_zero_rf_252']:.3f}|{row['fees']:.2f}|")
        else:
            lines.append(f"|{row['signal']}|{row['status']}/{row.get('completed_sessions', 0)}|unknown|unknown|unknown|unknown|")
            lines.append(f"\nFailure: {row.get('error')}\n")
    for row in record["comparisons"]:
        lines.append(f"\n{row['signal']} vs {row['control']}: net{100 * row['net_difference']:+.2f}pp; "
                     f"MDD magnitude{100 * row['drawdown_difference']:+.2f}pp; fees{row['fee_difference']:+.2f}CNY.")
    lines += ["", "Source retrieval-time pool/reconstructed corporate events are not PIT or independently complete market coverage.",
              "Score/IC/coefficient diagnostics are not expected CNY, target weights or profit/execution admission.",
              f"Source: {record['source_run']}", f"Artifacts: {record['artifacts']}", "",
              "Method sources: " + ", ".join(f"[official documentation]({url})" for url in METHOD_SOURCES)]
    if record.get("error"):
        lines.append("Preparation failure: " + record["error"])
    if record.get("resume_run"):
        lines += ["", f"Saved-prediction continuation: {record['resume_run']}; models_refit=False.",
                  "Completed accounts reused only after checking amended inputs against their actual account prefixes."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_id):
    source_path = ROOT / "outputs/factor_mining" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    model_root, model_folders, medians, seed_scores = source_models(source)
    portfolio = StrategyConfig(**source["portfolio_config"]).validate()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    protocol = {"id": ID, "source_run": str(source_path), "signals": list(SIGNALS),
                "portfolio": portfolio.to_dict(), "signal_portfolio_configs": {signal: portfolio.to_dict() for signal in SIGNALS},
                "stock_inputs": source["bulk_inputs"], "segments": source["config"]["segments"],
                "model_sources": {seed: str(folder) for seed, folder in model_folders.items()},
                "cached_labels": str(model_root / "labels.parquet"), "alpha": ALPHA,
                "linear_training": "2024_only_daily_zscore_labels_train_medians_plus_StandardScaler",
                "ensemble": "equal_within_day_percentile_ranks_of_ALL_seeds17_29_43",
                "native_features_recomputed": False, "labels_recomputed": False,
                "parameters_searched": False, "fresh_holdout_claimed": False, "execution_admitted": False}
    record = {"id": identifier, "status": "running", "purpose": ID, "source_run": str(source_path),
              "research_family": "alpha158_linear_ensemble", "artifacts": str(artifacts), "protocol": protocol,
              "dataset": source["bulk_inputs"]["price_view"], "scope": source["config"]["scope"],
              "symbols": source["data_quality"]["symbols"], "start": source["config"]["segments"]["test"][0],
              "end": source["config"]["segments"]["test"][1], "sessions": 0,
              "controls": [], "results": [], "comparisons": [], "prediction_diagnostics": [],
              "strategy_validated": False, "execution_admitted": False, "sklearn_version": version("scikit-learn")}
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    feed, begin = None, time.monotonic()
    try:
        inputs = protocol["stock_inputs"]
        feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        validate_prefix_inputs(source["dataset_manifest"], feed.manifest, None)
        protocol["stock_manifest"] = feed.manifest
        dates = [day for day in feed.calendar if record["start"] <= day <= record["end"]]
        record.update(start=dates[0], end=dates[-1], sessions=len(dates))
        for row in source["portfolio_metrics"]:
            seed = row["seed"]
            signal = f"alpha158_seed{seed}" if row["variant"] == "B0" else portfolio.signal
            folder = Path(source["data_output"]) / (f"B0_seed{seed}/replay" if seed is not None else "rule_replay")
            if row["status"] != "completed" or row["completed_sessions"] != len(dates):
                raise ValueError("All original seeds AND rule controls must be complete")
            load_replay_checkpoint(folder, portfolio, dates)
            record["controls"].append({"signal": signal, **row, "source_artifacts": str(folder), "replayed": False})
        labels = pd.read_parquet(model_root / "labels.parquet")
        expected = pd.read_csv(ROOT / "outputs/factor_mining" / source_id / "split_counts.csv", index_col="segment")
        train, evaluation, counts = load_matched_features(feed, portfolio, inputs["native_view"], labels, protocol["segments"], expected)
        if not evaluation.index.equals(seed_scores[SEEDS[0]].index):
            raise ValueError("Linear and existing LightGBM inference pools differ")
        target = labels[LABEL].reindex(train.index)
        record["matched_rows"] = counts
        record["training_dates"] = train.index.get_level_values("date").nunique()
        record["last_training_label_exit"] = labels.exit_date.reindex(train.index).max()
        model, scaler = fit_fixed(train, target, medians)
        print(f"Fixed Alpha158 Ridge trained: {len(train)} rows/{record['training_dates']} days, no validation search", flush=True)
        predictions = {SIGNALS[0]: predict_fixed(model, scaler, evaluation, medians),
                       SIGNALS[1]: equal_seed_ranks(seed_scores)}
        coefficients = pd.DataFrame({"feature": medians.index, "train_median": medians.to_numpy(),
                                     "train_mean": scaler.mean_, "train_scale": scaler.scale_, "coefficient": model.coef_})
        coefficients.to_csv(artifacts / "linear_preprocessing_coefficients.csv", index=False)
        write_json(artifacts / "linear_model.json", {"id": ID, "alpha": ALPHA, "intercept": model.intercept_,
            "features": medians.index.tolist(), "coefficient": model.coef_.tolist(), "train_mean": scaler.mean_.tolist(),
            "train_scale": scaler.scale_.tolist(), "train_median": medians.tolist(), "sklearn_version": record["sklearn_version"]})
        del train, target, evaluation
        label = labels[LABEL].where(labels.exit_date.le(pd.Timestamp(record["end"])))
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        for signal, score in predictions.items():
            model_folder = artifacts / "predictions" / signal
            model_folder.mkdir(parents=True)
            frame = export_scores(score)
            frame.to_parquet(model_folder / "historical_scores.parquet", index=False)
            ic = daily_ic(score.to_frame(), label, source["config"]["min_pairs"]).score
            ic.rename("rank_ic").to_csv(model_folder / "daily_rank_ic.csv")
            record["prediction_diagnostics"].append({"signal": signal, "prediction_rows": len(score), **ic_summary(ic)})
            result = {"signal": signal, **baseline_ledger_result(ScoreFeed(feed, frame), portfolio, dates, artifacts / signal)}
            record["results"].append(result)
            for control in record["controls"]:
                if result["status"] == "completed":
                    record["comparisons"].append({"signal": signal, "control": control["signal"],
                        "net_difference": result["net_return"] - control["net_return"],
                        "drawdown_difference": abs(result["max_drawdown"]) - abs(control["max_drawdown"]),
                        "fee_difference": result["fees"] - control["fees"]})
            write_json(out / "run.json", record)
            print(f"{signal}: {result['status']} {result['completed_sessions']}/{len(dates)}; net={result.get('net_return')}", flush=True)
        record["status"] = ("completed_alpha158_linear_diagnostics" if all(row["status"] == "completed" for row in record["results"])
                            else "partial_alpha158_linear_diagnostics")
    except Exception as error:
        record.update(status="failed_alpha158_linear_preparation", error=f"{type(error).__name__}: {error}")
    finally:
        if feed is not None:
            feed.close()
        record["elapsed_seconds"] = round(time.monotonic() - begin, 3)
        write_json(out / "run.json", record)
        report(out, record)
    print(str(out), flush=True)
    return out, record


def validate_saved_protocol(previous, source):
    protocol = previous["protocol"]
    if (previous.get("purpose") != ID
            or previous.get("status") not in {"partial_alpha158_linear_diagnostics", "completed_alpha158_linear_diagnostics"}
            or previous.get("strategy_validated") is not False or previous.get("execution_admitted") is not False
            or protocol.get("id") != ID or protocol.get("signals") != list(SIGNALS)
            or protocol.get("alpha") != ALPHA or protocol.get("parameters_searched") is not False
            or protocol.get("fresh_holdout_claimed") is not False or protocol.get("execution_admitted") is not False
            or protocol["portfolio"] != source["portfolio_config"] or protocol["segments"] != source["config"]["segments"]
            or protocol.get("signal_portfolio_configs") != {signal: source["portfolio_config"] for signal in SIGNALS}
            or len(previous["results"]) != len(SIGNALS)
            or [row["signal"] for row in previous["results"]] != list(SIGNALS)):
        raise ValueError("Saved continuation requires the same terminal fixed model, costs, periods and research boundary")


def run_saved(source_id, reference_view, *, event_view=None):
    source_path = (ROOT / "outputs/market_replay" / source_id / "run.json").resolve()
    source_path.parent.relative_to((ROOT / "outputs/market_replay").resolve())
    previous = json.loads(source_path.read_text(encoding="utf-8"))
    source = json.loads(Path(previous["source_run"]).read_text(encoding="utf-8"))
    validate_saved_protocol(previous, source)
    _, _, _, seed_scores = source_models(source)
    old_artifacts = (DATA_ROOT / "research/market_replay" / source_path.parent.name).resolve()
    if Path(previous["artifacts"]).resolve() != old_artifacts:
        raise ValueError("Saved prediction/account artifact root changed")
    inputs = dict(previous["protocol"]["stock_inputs"])
    inputs["reference_view"] = str(Path(reference_view).resolve())
    if event_view is not None:
        inputs["event_view"] = str(Path(event_view).resolve())
    portfolio = StrategyConfig(**previous["protocol"]["portfolio"]).validate()
    feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
    out = None
    begin = time.monotonic()
    try:
        dates = [day for day in feed.calendar if previous["start"] <= day <= previous["end"]]
        if len(dates) != previous["sessions"] or dates[0] != previous["start"] or dates[-1] != previous["end"]:
            raise ValueError("Saved account calendar changed")
        checkpoints, predictions, controls = {}, {}, []
        for signal in SIGNALS:
            checkpoint = load_replay_checkpoint(old_artifacts / signal, portfolio, dates)
            validate_prefix_inputs(previous["protocol"]["stock_manifest"], feed.manifest,
                                   checkpoint["state"]["day"], checkpoint["state"])
            score = saved_score(old_artifacts / "predictions" / signal / "historical_scores.parquet")
            if not score.index.equals(seed_scores[SEEDS[0]].index):
                raise ValueError("Saved prediction pool differs from original all-seed inference")
            checkpoints[signal], predictions[signal] = checkpoint, score
        if not predictions[SIGNALS[1]].equals(equal_seed_ranks(seed_scores)):
            raise ValueError("Saved all-seed rank ensemble changed")
        # Reuse each control only if the amended reference cannot alter its entire ledger.
        for row in source["portfolio_metrics"]:
            seed = row["seed"]
            signal = f"alpha158_seed{seed}" if row["variant"] == "B0" else portfolio.signal
            folder = Path(source["data_output"]) / (f"B0_seed{seed}/replay" if seed is not None else "rule_replay")
            checkpoint = load_replay_checkpoint(folder, portfolio, dates)
            if row["status"] != "completed" or len(checkpoint["daily"]) != len(dates):
                raise ValueError("Completed original controls required")
            validate_prefix_inputs(source["dataset_manifest"], feed.manifest,
                                   checkpoint["state"]["day"], checkpoint["state"])
            controls.append({"signal": signal, **row, "source_artifacts": str(folder), "replayed": False})
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        out, artifacts = ROOT / "outputs/market_replay" / identifier, DATA_ROOT / "research/market_replay" / identifier
        out.mkdir(parents=True, exist_ok=False)
        artifacts.mkdir(parents=True, exist_ok=False)
        for name in ("linear_model.json", "linear_preprocessing_coefficients.csv"):
            shutil.copy2(old_artifacts / name, artifacts / name)
        shutil.copy2(__file__, artifacts / "implementation.py")
        shutil.copytree(old_artifacts / "predictions", artifacts / "predictions")
        record = deepcopy(previous)
        protocol = record["protocol"]
        protocol.update(stock_inputs=inputs, stock_manifest=feed.manifest)
        record.update(id=identifier, status="running", artifacts=str(artifacts), resume_run=str(source_path.parent),
                      models_refit=False, controls=controls, results=[], comparisons=[],
                      reused_sessions={signal: len(checkpoints[signal]["daily"]) for signal in SIGNALS})
        record.pop("error", None)
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        for signal in SIGNALS:
            checkpoint = checkpoints[signal]
            active = (ScoreFeed(feed, export_scores(predictions[signal]))
                      if len(checkpoint["daily"]) < len(dates) else feed)
            result = {"signal": signal, **baseline_ledger_result(active, portfolio, dates, artifacts / signal, checkpoint=checkpoint)}
            record["results"].append(result)
            if result["status"] == "completed":
                for control in controls:
                    record["comparisons"].append({"signal": signal, "control": control["signal"],
                        "net_difference": result["net_return"] - control["net_return"],
                        "drawdown_difference": abs(result["max_drawdown"]) - abs(control["max_drawdown"]),
                        "fee_difference": result["fees"] - control["fees"]})
            write_json(out / "run.json", record)
            print(f"Saved {signal}: {result['status']} {result['completed_sessions']}/{len(dates)}; net={result.get('net_return')}", flush=True)
        record["status"] = ("completed_alpha158_linear_diagnostics" if all(row["status"] == "completed" for row in record["results"])
                            else "partial_alpha158_linear_diagnostics")
        return out, record
    except Exception as error:
        if out is None:
            raise
        record.update(status="failed_alpha158_linear_preparation", error=f"{type(error).__name__}: {error}")
        return out, record
    finally:
        feed.close()
        if out is not None:
            record["elapsed_seconds"] = round(time.monotonic() - begin, 3)
            write_json(out / "run.json", record)
            report(out, record)
            print(str(out), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-run")
    source.add_argument("--resume-run")
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--event-view", type=Path)
    args = parser.parse_args(argv)
    if args.resume_run:
        if args.reference_view is None:
            parser.error("Saved continuation requires --reference-view; no model refit")
        _, record = run_saved(args.resume_run, args.reference_view, event_view=args.event_view)
    else:
        if args.reference_view is not None or args.event_view is not None:
            parser.error("Action/reference amendments are only supported with saved predictions")
        _, record = run(args.source_run)
    return 0 if record["status"] == "completed_alpha158_linear_diagnostics" else 1


if __name__ == "__main__":
    raise SystemExit(main())
