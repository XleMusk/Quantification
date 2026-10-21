"""Continue fixed full-source model ledgers without repeating model work."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sys
import uuid

import pandas as pd

from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .experiment import (ScoreFeed, baseline_ledger_result, baseline_report,
                         load_replay_checkpoint, validate_config, write_json)
from .market_replay import validate_resume_inputs


def resume_boundary(record, dates):
    manifest = record["dataset_manifest"]
    return {
        "status": "completed_full_source_diagnostic" if record["status"] == "completed" else "failed_full_source_diagnostic",
        "purpose": "saved_full_source_baseline_ledger", "scope": manifest["scope"],
        "start": dates[0], "end": dates[-1], "sessions": len(dates),
        "symbols": record["data_quality"]["symbols"], "board_counts": {},
        "protocol": {"portfolio": record["portfolio_config"],
                     "signals": [f"alpha158_seed{s}" for s in record["config"]["seeds"]] + [record["portfolio_config"]["signal"]]},
        "inputs": {key: manifest[key] for key in (
            "price_view", "raw_view", "event_view", "reference_view", "listing_master",
            "source_codes", "canonical_codes", "unsupported_instruments")}}


def run_saved(source, *, event_view=None, reference_view=None, out=None, restart_cases=()):
    source = Path(source).resolve()
    source.relative_to((ROOT / "outputs/factor_mining").resolve())
    if not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", source.name):
        raise ValueError("Expected a saved baseline run identity")
    previous = json.loads((source / "run.json").read_text(encoding="utf-8"))
    if (previous["status"] not in {"completed", "failed_ledger_comparison"}
            or previous.get("experiment_kind") != "alpha158_ingestion_ledger_comparison"
            or previous["dataset_manifest"]["scope"] != BULK_SCOPE
            or previous.get("candidate_search_performed") is not False
            or previous.get("live_ready") is not False or not previous.get("bulk_inputs")):
        raise ValueError("Saved replay requires a terminal research-only full-source baseline")
    source_data = (DATA_ROOT / "research/factor_mining" / source.name).resolve()
    if Path(previous["data_output"]).resolve() != source_data:
        raise ValueError("Baseline artifact root changed")
    expected_cases = {("B0", seed) for seed in previous["config"]["seeds"]} | {("RULE", None)}
    if (len(previous["portfolio_metrics"]) != len(expected_cases)
            or {(row["variant"], row["seed"]) for row in previous["portfolio_metrics"]} != expected_cases):
        raise ValueError("Saved baseline is missing a planned ledger")
    inputs = dict(previous["bulk_inputs"])
    for name, value in (("event_view", event_view), ("reference_view", reference_view)):
        if value is not None:
            inputs[name] = str(Path(value).resolve())
    feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
    try:
        config = deepcopy(previous["config"])
        validate_config(config, feed, baseline_only=True, ingestion_only=True)
        portfolio = StrategyConfig(**previous["portfolio_config"]).validate()
        if StrategyConfig(**config["portfolio"]).validate().to_dict() != portfolio.to_dict():
            raise ValueError("Saved strategy/cost configuration changed")
        start, end = config["segments"]["test"]
        dates = [day for day in feed.calendar if start <= day <= end]
        calendar_file = Path(inputs["event_view"]) / "source_input_calendar.csv"
        if (not dates or sha256(calendar_file.read_bytes()).hexdigest() != previous["input_hashes"]["calendar.csv"]
                or len(feed.symbols) - len(feed.unsupported_instruments) != previous["data_quality"]["symbols"]):
            raise ValueError("Saved prediction calendar/universe changed")
        current = {**previous, "dataset_manifest": {**feed.manifest, "folder": str(feed.folder)}}
        before, after = resume_boundary(previous, dates), resume_boundary(current, dates)
        cases = [(f"alpha158_seed{s}", "B0", s, f"B0_seed{s}/replay") for s in config["seeds"]]
        cases += [(portfolio.signal, "RULE", None, "rule_replay")]
        if set(restart_cases) - {case[0] for case in cases}:
            raise ValueError("Unknown explicit account restart")
        checkpoints, model_sources = {}, {}
        for signal, variant, seed, relative in cases:
            checkpoint = None if signal in restart_cases else load_replay_checkpoint(source_data / relative, portfolio, dates)
            validate_resume_inputs(before, after, checkpoint["state"]["day"] if checkpoint else None,
                                   checkpoint["state"] if checkpoint else None)
            checkpoints[signal] = checkpoint
            if variant == "B0":
                model = Path(previous.get("model_sources", {}).get(signal, source_data / f"B0_seed{seed}")).resolve()
                model.relative_to((DATA_ROOT / "research/factor_mining").resolve())
                if not all((model / name).is_file() for name in ("model.txt", "preprocessing.csv", "historical_scores.parquet")):
                    raise ValueError(f"Missing saved model/predictions: {signal}")
                model_sources[signal] = str(model)
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        out = Path(out) if out else ROOT / "outputs/factor_mining" / identifier
        data_out = DATA_ROOT / "research/factor_mining" / identifier
        out.mkdir(parents=True, exist_ok=False)
        data_out.mkdir(parents=True, exist_ok=False)
        record = deepcopy(current)
        record.update(id=identifier, status="running", stage="saved_predictions_ledger_continuation",
                      started_at=datetime.now().astimezone().isoformat(), output=str(out), data_output=str(data_out),
                      bulk_inputs=inputs, calendar_file=str(calendar_file.resolve()), resume_run=str(source),
                      model_sources=model_sources, models_refit=False, restart_cases=list(restart_cases),
                      portfolio_metrics=[], paired_deltas=[], ledger_failures=[], replay_code=str(Path(__file__).resolve()),
                      command=sys.argv, model_training_command=previous.get("model_training_command", previous["command"]))
        record.pop("finished_at", None)
        write_json(out / "run.json", record)
        for name in ("alpha158_catalog.csv", "interpretable_35.csv", "selection_frozen.json", "split_counts.csv",
                     "model_metrics.csv", "quarterly_ic.csv", "model_daily_ic.csv", "model_groups.csv",
                     "selected_and_alpha158_test_daily_ic.csv"):
            shutil.copyfile(source / name, out / name)
        for signal, variant, seed, relative in cases:
            active = feed
            if variant == "B0" and (checkpoints[signal] is None or len(checkpoints[signal]["daily"]) < len(dates)):
                predictions = pd.read_parquet(Path(model_sources[signal]) / "historical_scores.parquet")
                active = ScoreFeed(feed, predictions)
            result = baseline_ledger_result(active, portfolio, dates, data_out / relative, checkpoint=checkpoints[signal])
            record["portfolio_metrics"].append({"variant": variant, "seed": seed, **result})
            write_json(out / "run.json", record)
            print(f"replayed_saved_{signal}_{result['status']}", flush=True)
        rows = record["portfolio_metrics"]
        rule = rows[-1]
        for row in rows[:-1]:
            available = row["status"] == rule["status"] == "completed"
            record["paired_deltas"].append({"seed": row["seed"], "comparison": "B0_minus_RULE",
                "comparison_status": "available" if available else "unavailable_failed_ledger",
                **{"delta_" + key: row[key] - rule[key] if available else None for key in
                   ("net_return", "max_drawdown", "turnover_over_initial")}})
        for name in ("portfolio_metrics", "paired_deltas"):
            pd.DataFrame(record[name]).to_csv(out / (name + ".csv"), index=False)
        failures = [row for row in rows if row["status"] == "failed"]
        status = "failed_ledger_comparison" if failures else "completed"
        record.update(status=status, stage=status, ledger_failures=failures, finished_at=datetime.now().astimezone().isoformat())
        write_json(out / "run.json", record)
        baseline_report(out, record)
        return out, record
    except Exception as error:
        if "record" in locals():
            record.update(status="failed", stage="failed", error=f"{type(error).__name__}: {error}",
                          finished_at=datetime.now().astimezone().isoformat())
            write_json(out / "run.json", record)
        raise
    finally:
        feed.close()
