"""Explicit mixed-source Qlib research proxy, with raw dated inventory retained.

This adapter never changes the old provider builder or any source package.
Its cumulative vendor-return normalization differs from qfq/factor training
prices, so frozen-model consumers must identify it as a cross-lineage diagnostic.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .baostock_full_close import load_complete
from .factors import catalog
from .full_market_alpha158 import provider_rows, qcode
from .leader_daily_signals import read_physical_snapshot
from .leader_history_repair_preview import BOUNDARY, _source_gate
from .leader_recent_source_history import _stamp, compose_recent_sources


SCHEMA = "a_share_recent_complete_source_research_provider_v1"
METHOD_ID = "recent_complete_sources_native_prefix_pct_return_provider_v1"
FIELDS = ["open", "high", "low", "close", "volume", "vwap", "factor"]
FLAGS = {**BOUNDARY, "model_trained": False, "provider_contacted": False,
         "account_created": False, "research_only": True, "pit_verified": False,
         "company_actions_verified": False, "source_verified": False}


def _provider_input(composed):
    """Exclude unsupported observations without repairing their values."""
    frame = composed["combined"].copy()
    original_days = sorted(frame.date.unique())
    days = composed["days"]
    recent = frame.date.isin(days)
    status_map = {(code[3:], str(day)): status
        for raw, _ in composed["loaded_sources"] for code, day, status in
        raw[["code", "date", "tradestatus"]].itertuples(index=False, name=None)}
    current_keys = pd.MultiIndex.from_frame(frame.loc[recent, ["code", "date"]])
    states = pd.Series(status_map).reindex(current_keys).to_numpy()
    reason = pd.Series("included", index=frame.index, dtype=object)
    def exclude(mask, name):
        reason.loc[mask & reason.eq("included")] = name
    suspended = pd.Series(False, index=frame.index)
    unknown = suspended.copy()
    suspended.loc[recent] = states == "0"
    unknown.loc[recent] = ~pd.Series(states).isin(["0", "1"]).to_numpy()
    exclude(suspended, "recent_explicit_suspension")
    exclude(unknown, "recent_trade_status_unknown")
    numeric = ["open", "high", "low", "close", "volume", "amount", "pct_chg"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    prices = frame[["open", "high", "low", "close"]]
    positive_prices = np.isfinite(prices).all(axis=1) & prices.gt(0).all(axis=1)
    coherent = prices.high.ge(prices.max(axis=1)) & prices.low.le(prices.min(axis=1))
    exclude(~positive_prices | ~coherent, "OHLC_missing_nonpositive_or_inconsistent")
    exclude(~np.isfinite(frame.volume) | frame.volume.le(0), "volume_missing_or_nonpositive")
    exclude(~np.isfinite(frame.amount) | frame.amount.le(0), "amount_missing_or_nonpositive")
    exclude(~np.isfinite(frame.pct_chg) | frame.pct_chg.le(-100), "return_missing_or_nonpositive_gross_factor")
    included = reason.eq("included")
    accepted = frame.loc[included].copy()
    if accepted.empty:
        raise ValueError("No positive finite activity observations for a research provider")
    if sorted(accepted.date.unique()) != original_days:
        raise ValueError("Activity filtering would silently shorten the global observed calendar")
    def counts(mask):
        values = reason.loc[mask]
        return {"source_rows": int(mask.sum()), "included_rows": int(values.eq("included").sum()),
            "excluded_rows": int(values.ne("included").sum()),
            "exclusion_reason_counts": {key: int(value) for key, value in values.loc[values.ne("included")].value_counts().sort_index().items()}}
    quality = {"historical_native_prefix": {**counts(~recent), "explicit_trade_status_available": False,
            "activity_basis": "finite_positive_OHLC_volume_amount_observed_bar_proxy_not_verified_historical_status"},
        "recent_sources": {day: {**counts(frame.date.eq(day)), "explicit_trade_status_available": True,
            "activity_basis": "own_exact_dated_raw_tradestatus_1_and_finite_positive_activity"} for day in days},
        "composed_source_rows": len(frame), "provider_input_rows": len(accepted),
        "adapter_excluded_rows": int((~included).sum()), "missing_values_filled": False,
        "ST_rows_removed_at_feature_stage": False, "units_rescaled": False}
    cohort = frame.groupby("code", sort=True).agg(source_observed_rows=("date", "size"),
        first_source_day=("date", "min"), last_source_day=("date", "max")).reset_index()
    accepted_counts = accepted.groupby("code").size()
    cohort["provider_input_rows"] = cohort.code.map(accepted_counts).fillna(0).astype(int)
    cohort["symbol"] = cohort.code + np.where(cohort.code.str.startswith("6"), ".SSE", ".SZSE")
    accepted["date"] = pd.to_datetime(accepted.date, format="%Y-%m-%d", errors="raise").dt.strftime("%Y%m%d").astype(int)
    return accepted, quality, cohort


def _retain_nan_only_codes(provider, dates, cohort):
    """Retain source members lacking every usable observation as empty grids."""
    missing = cohort.loc[cohort.provider_input_rows.eq(0), "code"].tolist()
    empty = np.r_[np.float32(0), np.full(len(dates), np.nan, dtype="<f4")].astype("<f4")
    for code in missing:
        target = provider / "features" / qcode(code).lower()
        target.mkdir()
        for field in FIELDS:
            empty.tofile(target / (field + ".day.bin"))
    # Native extraction sorts canonical numeric symbols, not SH/SZ prefixes.
    first, last = (pd.Timestamp(str(day)).date() for day in (dates[0], dates[-1]))
    (provider / "instruments" / "all.txt").write_text("".join(
        f"{qcode(code)}\t{first}\t{last}\n" for code in cohort.code), encoding="utf-8")
    return missing


def build(packages, history_snapshot, calendar, out):
    """Create a new provider_view directory compatible with native extraction."""
    packages = [Path(path).resolve() for path in packages]
    history_snapshot, calendar, out = (Path(path).resolve() for path in (history_snapshot, calendar, out))
    if out.exists():
        raise FileExistsError("A recent-source Qlib provider requires a new output directory")
    if len(packages) < 3 or len(packages) != len(set(packages)):
        raise ValueError("At least three distinct complete dated packages are required")
    gates = [gate for package in packages if (gate := _source_gate(package)) is not None]
    if gates:
        return out, {"schema": SCHEMA, "provider_method_id": METHOD_ID,
            "status": "blocked_failed_source" if any(gate["status"] == "blocked_failed_source" for gate in gates)
                      else "waiting_source_incomplete",
            "checked_at": datetime.now().astimezone().isoformat(), "sources": gates,
            "output_directory_created": False, **FLAGS}
    started = datetime.now().astimezone().isoformat()
    loaded = [load_complete(package) for package in packages]
    observed = datetime.now().astimezone().isoformat()
    composed = compose_recent_sources(loaded, history_snapshot, calendar,
        history_reader=read_physical_snapshot, observed_at=observed)
    for proof in composed["sources"]:
        terminal = _stamp(proof.get("source_completed_at") or proof.get("source_updated_at"))
        if not _stamp(proof["day"] + "T15:00:00+08:00") <= terminal <= _stamp(observed):
            raise ValueError("Recent source completion clock contradicts actual provider derivation")
    frame, quality, cohort = _provider_input(composed)
    definitions = catalog()
    if len(definitions) != 158 or definitions.name.duplicated().any() or definitions.formula.eq("").any():
        raise ValueError("The complete pinned native Alpha158 catalog is required")
    recent_raw, original_fields, rows_by_day = [], {}, {}
    for raw, source in composed["loaded_sources"]:
        day = source["plan"]["day"]
        original_fields[day], rows_by_day[day] = list(raw.columns), len(raw)
        copy = raw.copy()
        copy["symbol"] = copy.code.str[3:] + np.where(copy.code.str.startswith("sh."), ".SSE", ".SZSE")
        recent_raw.append(copy)
    raw_inventory = pd.concat(recent_raw, ignore_index=True)
    out.mkdir(parents=True, exist_ok=False)
    provider, dates, provider_quality = provider_rows(frame, out)
    missing_codes = _retain_nan_only_codes(provider, dates, cohort)
    definitions.to_csv(out / "alpha158_catalog.csv", index=False)
    raw_path = out / "recent_raw_observations.parquet"
    raw_inventory.to_parquet(raw_path, index=False)
    cohort_path = out / "source_observed_cohort.csv"
    cohort.to_csv(cohort_path, index=False)
    completed = datetime.now().astimezone().isoformat()
    result = {"schema": SCHEMA, "status": "research_provider_completed", "provider_method_id": METHOD_ID,
        "started_at": started, "created_at": completed, "artifacts": str(out), "provider_folder": str(provider.resolve()),
        "price_view": str(out), "fields": FIELDS, "calendar": dates, "dates": len(dates),
        "symbols": len(cohort), "rows": len(cohort) * len(dates),
        "binary_files": len(cohort) * len(FIELDS), "alpha158_definitions": len(definitions),
        "alpha158_catalog_sha256": digest(out / "alpha158_catalog.csv"),
        "source": "explicit_native_history_prefix_and_own_dated_complete_provider_sources_pct_return_research_proxy",
        "normalization": "unchanged_full_market_alpha158.provider_rows_cumulative_vendor_pct_return_index_OHLC_relative_to_close_volume_unchanged",
        "normalization_matches_original_qfq_factor_training": False,
        "normalization_comparison": "cross_lineage_diagnostic_not_training_normalization_reproduction",
        "price_values_are_raw_RMB": False, "volume_unit": "source_shares_unchanged", "amount_unit": "source_RMB_yuan_unchanged",
        "source_quality": {"adapter_activity_filter": quality, "provider_input_quality": provider_quality},
        "calendar_basis": "union_of_composed_observed_dates_preserved_exactly_after_filtering_not_full_historical_exchange_calendar_claim",
        "missing_symbol_calendar_cells": "NaN_from_existing_provider_rows_no_zero_return_or_price_fill",
        "source_proofs": composed["sources"], "recent_source_days": composed["days"],
        "recent_source_inventory_complete": True, "history_inventory_complete": False,
        "continuous_history_claimed": False, "remaining_symbol_gaps_retained": True,
        "calendar_file": str(calendar), "calendar_sha256": composed["calendar_sha256"],
        "history_snapshot": str(history_snapshot), "original_history_manifest_sha256": digest(history_snapshot / "manifest.json"),
        "base_history_end": composed["base_history_end"], "base_history_rows": len(composed["base_history"]),
        "original_rows_after_cutoff_loaded_into_view": 0,
        "original_reader_rows_after_cutoff_ignored": composed["original_reader_rows_after_cutoff_ignored"],
        "recent_raw_observations_file": str(raw_path), "recent_raw_observations_sha256": digest(raw_path),
        "recent_raw_observation_rows": len(raw_inventory), "recent_raw_rows_by_day": rows_by_day,
        "original_raw_fields_by_day": original_fields, "recent_raw_original_fields_unchanged": True,
        "recent_raw_semantics": "local_derived_copy_of_all_own_dated_ordinary_rows_including_halt_ST_nullable_not_admitted_raw_feed",
        "source_cohort_file": str(cohort_path), "source_cohort_sha256": digest(cohort_path),
        "source_cohort_symbols": len(cohort), "source_cohort_scope": "all_composed_observed_codes_not_PIT_membership_not_latest_survivors",
        "fully_excluded_source_codes": missing_codes, "nan_only_provider_symbols": len(missing_codes),
        "nan_only_code_policy": "all_seven_fields_NaN_on_full_grid_no_source_observation_not_eligible_for_dated_scoring",
        "binary_zero_header_is_format_only": True,
        "raw_prices_imputed": False, "stockdb_modified": False, "native_features_modified": False,
        "prior_scores_modified": False, "factor_selection_performed": False,
        "output_directory_created": True, **FLAGS,
        "limitations": ["Cumulative vendor-return levels are mixed-source research feature proxies, not raw RMB execution prices or qfq training-input reproduction",
            "Recent own dated source inventories are complete; historical listing/trade-status/PIT and company actions remain unverified",
            "Suspended/unknown/invalid activity is excluded only from feature-provider inputs; original recent rows and full observed source cohort are retained",
            "Provider grid includes unobserved symbol/date cells; median filling by later models cannot establish a real active observation",
            "No model fitting, score, label, account, order, Ledger or THS operation is performed"]}
    write_json(out / "result.json", result)
    return out, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, action="append", required=True)
    parser.add_argument("--history-snapshot", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="New provider-view artifact directory, normally outside the repository")
    args = parser.parse_args(argv)
    out, result = build(args.package, args.history_snapshot, args.calendar, args.out)
    print(json.dumps({"output": str(out), **{key: result.get(key) for key in
        ("status", "provider_method_id", "dates", "symbols", "recent_source_days", "execution_admitted")}}, ensure_ascii=False))
    return 0 if result["status"] == "research_provider_completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
