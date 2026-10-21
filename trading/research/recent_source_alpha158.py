"""Dated Alpha158 inference from a new complete-source research provider.

Only requested dates are materialized, over EVERY pinned provider symbol. The
binary provider retains the full historical prefix for Qlib expression lookback.
Frozen models score exact-day active non-ST observations; unobserved calendar
grid rows cannot turn into scored ghost stocks through median imputation.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..market_dataset import write_json
from ..symbols import normalize_symbol
from .baostock_daily_slice import digest
from .factors import features_from_provider
from .stockdb_alpha158_lgbm_diagnostic import _resolve_model, _frozen_predictions, SEEDS

METHOD_ID = "complete_recent_source_native_alpha158_active_scope_datewise_frozen_rank_v1"
FLAGS = {"candidate_panel_only": True, "model_refit": False, "model_trained": False,
         "future_labels_used_for_score": False, "strategy_validated": False,
         "selection_enabled": False, "current_signal_admitted": False,
         "execution_admitted": False, "ledger_mutated": False,
         "target_holdings_generated": False, "paper_orders_generated": False,
         "ths_payload_generated": False, "provider_contacted": False}


def _aware(value):
    value = datetime.fromisoformat(str(value))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Actual computation and provider clocks require a timezone")
    return value


def _provider(folder, start, end, now):
    from .recent_source_qlib_provider import SCHEMA, METHOD_ID as PROVIDER_METHOD
    folder = Path(folder).resolve()
    path = folder / "result.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if (record.get("schema") != SCHEMA or record.get("provider_method_id") != PROVIDER_METHOD
            or record.get("status") != "research_provider_completed"
            or any(record.get(key) is not False for key in ("model_trained", "model_refit", "execution_admitted",
                "strategy_validated", "selection_enabled", "future_labels_used_for_score"))
            or _aware(record["created_at"]) > now):
        raise ValueError("Complete new-source research provider with actual clock required")
    provider = Path(record["provider_folder"]).resolve()
    provider.relative_to(folder)
    calendar = (provider / "calendars/day.txt").read_text(encoding="utf-8").splitlines()
    if (calendar != sorted(set(calendar)) or len(calendar) != record["dates"]
            or [int(day.replace("-", "")) for day in calendar] != record["calendar"]):
        raise ValueError("Provider full calendar identity changed")
    requested = [day for day in calendar if str(start) <= day <= str(end)]
    if not requested or not set(requested) <= set(record["recent_source_days"]):
        raise ValueError("Dated scores require explicit completed recent source dates")
    if any(_aware(day + "T15:00:00+08:00") > now for day in requested):
        raise ValueError("Future closes cannot enter dated scoring")
    instruments = [line.split("\t") for line in (provider / "instruments/all.txt").read_text(encoding="utf-8").splitlines()]
    symbols = [normalize_symbol(row[0]).vt_symbol for row in instruments]
    if (symbols != sorted(set(symbols)) or len(symbols) != record["symbols"]
            or any(len(row) != 3 or row[1:] != [calendar[0], calendar[-1]] for row in instruments)):
        raise ValueError("Full provider symbol/calendar identities changed")
    raw_path = Path(record["recent_raw_observations_file"]).resolve()
    raw_path.relative_to(folder)
    if digest(raw_path) != record["recent_raw_observations_sha256"]:
        raise ValueError("Dated raw observation proof changed")
    raw = pd.read_parquet(raw_path)
    required = {"date", "code", "tradestatus", "isST", "open", "high", "low", "close", "volume", "amount", "pctChg"}
    if (not required <= set(raw) or len(raw) != record["recent_raw_observation_rows"]
            or raw.duplicated(["date", "code"]).any()
            or sorted(raw.date.unique().tolist()) != record["recent_source_days"]):
        raise ValueError("Complete dated raw observation identity changed")
    proof_counts = {p["day"]: p["ordinary_inventory_rows"] for p in record["source_proofs"]}
    if raw.groupby("date").size().to_dict() != proof_counts:
        raise ValueError("Raw observations must retain each full dated source roster")
    definitions = pd.read_csv(folder / "alpha158_catalog.csv", keep_default_na=False)
    if (len(definitions) != 158 or definitions.name.duplicated().any() or definitions.formula.eq("").any()
            or digest(folder / "alpha158_catalog.csv") != record["alpha158_catalog_sha256"]
            or record["fields"] != ["open", "high", "low", "close", "volume", "vwap", "factor"]):
        raise ValueError("Pinned 158-definition seven-field provider required")
    # Check exact binary grid length once, not a full historical feature rebuild.
    for symbol in instruments:
        for field in record["fields"]:
            file = provider / "features" / symbol[0].lower() / (field + ".day.bin")
            if file.stat().st_size != (len(calendar) + 1) * 4:
                raise ValueError("Provider binary full-calendar shape changed")
    return folder, record, provider, calendar, requested, symbols, raw, definitions


def _active_scope(raw, requested):
    raw = raw.loc[raw.date.isin(requested)].copy()
    raw["symbol"] = raw.code.str[3:] + np.where(raw.code.str.startswith("sh."), ".SSE", ".SZSE")
    numeric = ["open", "high", "low", "close", "volume", "amount", "pctChg"]
    values = raw[numeric].apply(pd.to_numeric, errors="coerce")
    valid = np.isfinite(values).all(axis=1) & values[numeric[:-1]].gt(0).all(axis=1)
    active = raw.tradestatus.eq("1") & raw.isST.eq("0") & valid
    rows = raw.loc[active, ["date", "symbol"]]
    index = pd.MultiIndex.from_arrays([pd.to_datetime(rows.date), rows.symbol], names=["date", "symbol"])
    coverage = [{"day": day, "full_dated_roster_rows": int(group.shape[0]),
        "active_non_ST_valid_observations": int(active.loc[group.index].sum()),
        "suspended_rows_retained_unscored": int(group.tradestatus.eq("0").sum()),
        "ST_rows_retained_unscored": int(group.isST.eq("1").sum()),
        "not_scored_count": int((~active.loc[group.index]).sum())}
        for day, group in raw.groupby("date", sort=True)]
    return index, coverage


def _datewise_predictions(frame, model_paths, columns):
    # _frozen_predictions ranks all rows it receives. Calling it once per date
    # fixes the scope without changing older stored batch results or weights.
    pieces = [_frozen_predictions(day, model_paths, columns) for _, day in frame.groupby(level="date", sort=True)]
    if not pieces:
        raise ValueError("No exact-date active rows remain for frozen inference")
    return pd.concat(pieces).sort_index()


def run(provider_view, model_run, *, start, end, out, batch_size=256, _feature_builder=None):
    if not 1 <= batch_size <= 512 or str(start) > str(end):
        raise ValueError("Ordered dates and a memory batch size 1..512 required")
    started = datetime.now().astimezone()
    folder, provider_record, provider, calendar, requested, symbols, raw, definitions = _provider(provider_view, start, end, started)
    model_path, model_record, models = _resolve_model(model_run)
    frozen_catalog_path = model_path.parent / "alpha158_catalog.csv"
    frozen_catalog = pd.read_csv(frozen_catalog_path, keep_default_na=False)
    if (not {"name", "formula"} <= set(frozen_catalog)
            or not definitions[["name", "formula"]].equals(frozen_catalog[["name", "formula"]])):
        raise ValueError("Provider Alpha158 names/formulas differ from the frozen training definitions")
    if model_record.get("models_refit") is not False:
        raise ValueError("Completed wrapper must point to the previously frozen seed artifacts")
    segments = model_record["config"]["segments"]
    if max(segments["train"][-1], segments["valid"][-1]) >= requested[0]:
        raise ValueError("Frozen training/validation labels overlap requested score dates")
    model_proof = {str(seed): {"folder": str(models[seed]), "model_sha256": digest(models[seed] / "model.txt"),
        "preprocessing_sha256": digest(models[seed] / "preprocessing.csv")} for seed in SEEDS}
    scope, coverage = _active_scope(raw, requested)
    if not set(scope.get_level_values("symbol")) <= set(symbols):
        raise ValueError("Current observed symbols are absent from the prepared full provider")
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    feature_dir = out / "dated_features"
    feature_dir.mkdir()
    record = {"schema": "a_share_recent_source_alpha158_frozen_score_batch_v1", "status": "building_dated_native_features",
        "method_id": METHOD_ID, "started_at": started.isoformat(), "provider_view": str(folder),
        "provider_result_sha256": digest(folder / "result.json"), "provider_method_id": provider_record["provider_method_id"],
        "provider_calendar_start": calendar[0], "provider_calendar_end": calendar[-1],
        "provider_dates": len(calendar), "requested_feature_dates": requested,
        "score_scope": "exact_dated_provider_active_non_ST_positive_observed_OHLCV_amount_not_PIT",
        "native_grid_scope": "every_pinned_provider_symbol_times_requested_dates_full_prefix_for_expression_lookback",
        "score_aggregation": "within_each_date_seed_percentile_mean_no_cross_date_ranking",
        "source_available_at": provider_record["created_at"], "source_proofs": provider_record["source_proofs"],
        "raw_observations": provider_record["recent_raw_observations_file"],
        "raw_observations_sha256": provider_record["recent_raw_observations_sha256"],
        "model_run": str(model_path), "model_run_sha256": digest(model_path), "seed_models": model_proof,
        "training_segments": segments, "feature_count": 158, "catalog_sha256": digest(folder / "alpha158_catalog.csv"),
        "frozen_training_catalog_sha256": digest(frozen_catalog_path),
        "partitions": [], "native_symbols_completed": 0, "native_feature_rows": 0, "coverage": coverage,
        "historical_publication_verified": False, "pit_verified": False, "company_actions_verified": False,
        "cross_lineage_diagnostic": True, "engine_ledger_replay_completed": False, **FLAGS}
    write_json(out / "result.json", record)
    columns = definitions.name.tolist()
    build = _feature_builder or features_from_provider
    try:
        pieces = []
        for offset in range(0, len(symbols), batch_size):
            batch = symbols[offset:offset + batch_size]
            features = build(provider, batch, requested, definitions, [])
            expected = pd.MultiIndex.from_product([pd.to_datetime(requested), batch], names=["date", "symbol"]).sort_values()
            if not features.index.equals(expected) or features.columns.tolist() != columns:
                raise ValueError("Dated native extraction omitted symbol/date/definition identities")
            file = feature_dir / f"features_{offset // batch_size:04d}.parquet"
            features.to_parquet(file)
            record["partitions"].append({"file": str(file), "rows": len(features), "symbols": batch, "sha256": digest(file)})
            record["native_symbols_completed"] += len(batch)
            record["native_feature_rows"] += len(features)
            active_features = features.loc[features.index.isin(scope)]
            pieces.append(active_features)
            write_json(out / "result.json", record)
            print(f"dated-native {record['native_symbols_completed']}/{len(symbols)} symbols", flush=True)
        frame = pd.concat(pieces).sort_index()
        if frame.index.has_duplicates or set(frame.index) != set(scope):
            raise ValueError("Active score scope differs from full dated raw proof")
        predictions = _datewise_predictions(frame, models, columns)
        predictions.index = predictions.index.set_names(["date", "symbol"])
        scores = predictions.reset_index()
        scores["date"] = scores.date.dt.strftime("%Y-%m-%d")
        scores = scores.sort_values(["date", "rank_mean", "symbol"], ascending=[True, False, True], kind="stable")
        scores.to_parquet(out / "dated_scores.parquet", index=False)
        for entry in coverage:
            day = pd.Timestamp(entry["day"])
            d = frame.loc[frame.index.get_level_values("date") == day]
            entry.update(native_grid_rows=len(symbols), unobserved_or_out_of_scope_grid_rows=len(symbols) - len(d),
                missing_feature_cells=int(d.isna().sum().sum()), all_missing_active_feature_rows=int(d.isna().all(axis=1).sum()))
        finished = datetime.now().astimezone()
        record.update(status="completed_research_only_dated_score_batch", actual_computed_at=finished.isoformat(),
            finished_at=finished.isoformat(), score_path=str(out / "dated_scores.parquet"), score_dates=sorted(scores.date.unique()),
            score_rows=len(scores), native_symbols=len(symbols), coverage=coverage,
            limitations=["Frozen weights use older adjusted BulkResearchFeed lineage; new cumulative-pct proxy is cross-lineage research only",
                "Only active exact-day observations are scored, but listing/PIT/action/tradability gates remain separate",
                "Requested recent dates are computed now; this does not establish historical signal publication or untouched validation",
                "Seed ranks are normalized within each date; older cross-date batch artifacts are preserved and are not directly comparable returns",
                "Scores do not create a portfolio, order, account, THS payload or execution admission"])
        write_json(out / "result.json", record)
        return record
    except Exception as error:
        record.update(status="failed_recent_source_alpha158_scores", finished_at=datetime.now().astimezone().isoformat(),
                      error=f"{type(error).__name__}: {error}")
        write_json(out / "result.json", record)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-view", type=Path, required=True)
    parser.add_argument("--model-run", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args(argv)
    value = run(args.provider_view, args.model_run, start=args.start, end=args.end, out=args.out, batch_size=args.batch_size)
    print(json.dumps({k: value[k] for k in ("status", "score_dates", "score_rows", "actual_computed_at", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
