"""Saved-source, whole-pool weekly reversal and reported-turnover diagnostics.

Formation is saved before any future opening-price endpoint is read.  These
rank diagnostics do not create a strategy account or describe account returns.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import SignalUnavailable
from . import weekly_liquidity_inputs as inputs
from . import weekly_liquidity_diagnostics as diagnostics
from .saved_monthly_rank_diagnostic import endpoint_panel


METHOD_ID = "saved_weekly_reported_turnover_conditioned_reversal_rank_diagnostic_v1"
REGISTRATION_SCHEMA = METHOD_ID + "_registration"
RESULT_SCHEMA = METHOD_ID + "_result"
FLAGS = {**inputs.FLAGS, "account_backtest": False, "returns_reportable": False,
         "account_accessed": False, "ledger_mutated": False,
         "paper_orders_generated": False, "model_refit": False,
         "independent_holdout_claimed": False}
SPEC = {"method_id": METHOD_ID, "start": "2024-01-02", "end": "2026-09-30",
    "source": "completed_saved_original_weekly_pair_full_source_canonical_adjusted_panel",
    "schedule": "original_every_five_sessions_anchored_20240102_full_schedule_including_warmup_unknown",
    "pool": "original_weekly_features_and_LowRiskFeed_pool_without_sign_or_future_quality_filter",
    "turnover": "mean_last63_exact_calendar_active_reported_percent_with_min42_known_no_scaling",
    "turnover_window": 63, "minimum_known_turnover": 42,
    "turnover_groups": "ceil_average_pct_rank_times5_clip1_5_on_same_D_turnknown_pool_preserve_ties",
    "matched_pool": "all_and_T1_T5_use_same_formation_D_turnknown_members_no_future_endpoint_filter",
    "reversal": "unchanged_negative_adjusted_close_D_over_close_Dminus5_minus1",
    "endpoints": {"entry": 1, "h5_exit": 6, "h21_exit": 22},
    "h5": "adjusted_open_Dplus6_over_Dplus1_minus1",
    "h21": "adjusted_open_Dplus22_over_Dplus1_minus1",
    "late5_21": "adjusted_open_Dplus22_over_Dplus6_minus1_not_h21_minus_h5",
    "future_unknown": "exact_missing_or_inactive_nonpositive_volume_or_price_and_calendar_overflow_stay_null",
    "formation_saved_before_future_endpoints": True,
    "evaluation": deepcopy(diagnostics.SPEC),
    "scope": "2024_2025_2026_partial_and_all_date_equal_descriptive",
    "overlap": "overlapping_horizons_and_repeated_symbols_not_independent_replications",
    "not_account_rule": "weekly_reranking_with_21day_expiry_is_not_fixed21day_holding_no_account_rule_tested",
    "price_basis": "derived_adjusted_research_levels_not_independent_total_returns",
    "raw_limits_slippage_fees_midpath_halts_or_execution_evaluated": False,
    "historical_model_or_signal_publication_claimed": False, **FLAGS}


def now():
    return datetime.now().astimezone().isoformat()


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if value is pd.NA:
        return None
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def _save(path, value):
    path = Path(path)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(_clean(value), handle, ensure_ascii=False, indent=2, allow_nan=False)


def _directory(out, protected):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new independent weekly liquidity output directory")
    for path in protected:
        path = Path(path).resolve()
        if out == path or out in path.parents or path in out.parents:
            raise ValueError("Output must not overlap any source or registration directory")
    return out


def _code_files():
    root = Path(__file__).resolve().parents[1]
    return [Path(__file__).resolve(), Path(inputs.__file__).resolve(), Path(diagnostics.__file__).resolve(),
        *[root / name for name in ("research/weekly_reversal.py", "research/low_risk.py",
        "research/bulk_feed.py", "research/price_boundaries.py", "research/factors.py",
        "research/factor_families.py", "research/saved_monthly_rank_diagnostic.py",
        "rules.py", "symbols.py", "config.py")]]


def _source_directories(source):
    return sorted({str(Path(ref["path"]).resolve().parent)
                   for key, ref in source["metadata_refs"].items() if key != "turn_unit_evidence"}
                  | {str(Path(source["canonical_panel"]["path"]).resolve().parent)})


def endpoint_plan(days, calendar):
    if (calendar != sorted(set(calendar)) or days != sorted(set(days))
            or any(day not in calendar for day in days)):
        raise ValueError("Exact ordered source-calendar formation dates required")
    positions = {day: i for i, day in enumerate(calendar)}
    result = {}
    for day in days:
        i = positions[day]
        result[day] = {"source_day": day,
            **{key: calendar[i + offset] if i + offset < len(calendar) else None
               for key, offset in (("entry_day", 1), ("h5_exit_day", 6), ("h21_exit_day", 22))}}
    return result


def register(run_json, out, *, reference_note=None):
    """Save only source metadata, fixed rules and the current implementation."""
    source = inputs.inspect_saved_weekly_inputs(run_json)
    protected = _source_directories(source)
    note = Path(reference_note).resolve() if reference_note is not None else None
    note_ref = inputs._ref(note) if note is not None else None
    if note is not None:
        protected.append(note.parent)
    folder = _directory(out, protected)
    days = inputs.weekly.decision_days(source["calendar"])
    code = [inputs._ref(path) for path in _code_files()]
    folder.mkdir(parents=True, exist_ok=False)
    snapshot = folder / "code_snapshot"
    snapshot.mkdir()
    copies = []
    for i, ref in enumerate(code):
        path = snapshot / f"{i:02}_{Path(ref['path']).name}"
        content = Path(ref["path"]).read_bytes()
        with path.open("xb") as handle:
            handle.write(content)
        copied = inputs._ref(path)
        if copied["sha256"] != ref["sha256"]:
            raise ValueError("Implementation changed while its registration copy was saved")
        copies.append({"source": ref, "copy": copied})
    record = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID, "registered_at": now(),
        "spec": deepcopy(SPEC), "source": source, "decision_days": days,
        "endpoint_plan": endpoint_plan(days, source["calendar"]), "code": code,
        "code_snapshots": copies, "reference_note": note_ref,
        "future_endpoint_data_read": False, "formation_scores_computed": False, **FLAGS}
    _save(folder / "registration.json", record)
    return record


def _registration(path):
    path = Path(path).resolve()
    record = inputs._read(path)
    if (record.get("schema") != REGISTRATION_SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("spec") != SPEC
            or record.get("code") != [inputs._ref(file) for file in _code_files()]):
        raise ValueError("Frozen weekly liquidity rules or current implementation changed")
    for ref in record["source"]["metadata_refs"].values():
        if inputs._ref(ref["path"]) != ref:
            raise ValueError("Registered saved source metadata changed")
    for item in record.get("code_snapshots", []):
        if inputs._ref(item["copy"]["path"]) != item["copy"] or item["source"] not in record["code"]:
            raise ValueError("Registered implementation copy changed")
    if len(record.get("code_snapshots", [])) != len(record["code"]):
        raise ValueError("Missing registered implementation copies")
    if record.get("reference_note") is not None and inputs._ref(record["reference_note"]["path"]) != record["reference_note"]:
        raise ValueError("Registered source-reference note changed")
    if (record["decision_days"] != inputs.weekly.decision_days(record["source"]["calendar"])
            or record["endpoint_plan"] != endpoint_plan(record["decision_days"], record["source"]["calendar"])):
        raise ValueError("Registered exact cadence or endpoint plan changed")
    return record


def formation(loaded):
    """No future inputs. Keep every original pool row and its turnover coverage."""
    calendar, days = loaded["base"].calendar, loaded["decision_days"]
    active = loaded["turnover_active"].reindex(index=calendar)
    mean = active.rolling(SPEC["turnover_window"], min_periods=SPEC["minimum_known_turnover"]).mean()
    count = active.rolling(SPEC["turnover_window"], min_periods=1).count()
    rows, coverage = [], []
    for day in days:
        pool = loaded["pools"][day]
        if isinstance(pool, SignalUnavailable):
            coverage.append({"date": day, "original_pool_status": "unknown", "reason": pool.reason,
                "original_pool_members": None, "turnknown_members": None, "turnunknown_members": None,
                "turn_groups": {str(i): None for i in range(1, 6)}})
            continue
        own = []
        exact_sessions = min(SPEC["turnover_window"], calendar.index(day) + 1)
        exact_window = exact_sessions == SPEC["turnover_window"]
        for row in pool:
            symbol = row["symbol"]
            value, known = mean.at[day, symbol], int(count.at[day, symbol])
            usable = exact_window and np.isfinite(value) and known >= SPEC["minimum_known_turnover"]
            own.append({"date": day, "symbol": symbol, "reversal_score": row["reversal"],
                "momentum": row["momentum"], "volatility": row["volatility"],
                "median_amount": row["median_amount"], "turn63_percent": float(value) if usable else np.nan,
                "turn63_known_observations": known, "turn63_exact_sessions": exact_sessions,
                "turn_group": np.nan, "turnover_eligible": bool(usable)})
        part = pd.DataFrame(own)
        if not part.empty:
            valid = part.turnover_eligible
            part.loc[valid, "turn_group"] = np.ceil(part.loc[valid, "turn63_percent"].rank(method="average", pct=True) * 5).clip(1, 5)
            own = part.to_dict("records")
        known_count = sum(row["turnover_eligible"] for row in own)
        coverage.append({"date": day, "original_pool_status": "known", "reason": None,
            "original_pool_members": len(pool), "turnknown_members": known_count,
            "turnunknown_members": len(pool) - known_count,
            "turn_groups": {str(i): sum(row["turn_group"] == i for row in own) for i in range(1, 6)}})
        rows.extend(own)
    columns = ["date", "symbol", "reversal_score", "momentum", "volatility", "median_amount",
               "turn63_percent", "turn63_known_observations", "turn63_exact_sessions", "turn_group", "turnover_eligible"]
    full = pd.DataFrame(rows, columns=columns).set_index(["date", "symbol"]).sort_index()
    matched = full.loc[full.turnover_eligible.astype(bool)].copy()
    return full, matched, coverage


def _labels(frame, plans, opening, observations):
    labels = {name: [] for name in diagnostics.ENDPOINTS}
    rows = []
    def price(day, symbol):
        value = opening.at[day, symbol] if day is not None else np.nan
        return float(value) if np.isfinite(value) and value > 0 else np.nan
    def ratio(end, start):
        return end / start - 1 if np.isfinite(end) and np.isfinite(start) else np.nan
    def reason(day, symbol):
        return ("calendar_endpoint_unknown" if day is None else
                observations.get((day, symbol), {"reason": "endpoint_original_code_row_missing"})["reason"])
    for day, symbol in frame.index:
        plan = plans[day]
        entry = price(plan["entry_day"], symbol)
        h5 = price(plan["h5_exit_day"], symbol)
        h21 = price(plan["h21_exit_day"], symbol)
        values = {"h5": ratio(h5, entry), "h21": ratio(h21, entry), "late5_21": ratio(h21, h5)}
        for name, value in values.items():
            labels[name].append(value)
        rows.append({"date": day, "symbol": symbol, **plan, **values,
            "entry_adjusted_open": entry, "h5_exit_adjusted_open": h5, "h21_exit_adjusted_open": h21,
            "entry_reason": reason(plan["entry_day"], symbol),
            "h5_exit_reason": reason(plan["h5_exit_day"], symbol),
            "h21_exit_reason": reason(plan["h21_exit_day"], symbol)})
    return {name: pd.Series(value, index=frame.index, dtype=float) for name, value in labels.items()}, rows


def run(registration, out):
    registration = Path(registration).resolve()
    record = _registration(registration)
    folder = _directory(out, [registration.parent, *_source_directories(record["source"])])
    folder.mkdir(parents=True, exist_ok=False)
    phase = {"formation_sealed": False, "future_endpoint_read_started": False}
    _save(folder / "started.json", {"schema": RESULT_SCHEMA, "started_at": now(), **phase, **FLAGS})
    try:
        loaded = inputs.load_inputs_from_saved_weekly(record["source"]["metadata_refs"]["saved_run"]["path"])
        if loaded["source"] != record["source"] or loaded["decision_days"] != record["decision_days"]:
            raise ValueError("Loaded input generation differs from the registered source/cadence")
        full, frame, coverage = formation(loaded)
        observation = loaded["load_observation"]
        del loaded
        full.to_parquet(folder / "formation.parquet")
        full.to_csv(folder / "formation.csv")
        frame.to_parquet(folder / "score_frame.parquet")
        _save(folder / "coverage.json", coverage)
        formation_files = {name: inputs._ref(folder / name) for name in
                           ("formation.parquet", "formation.csv", "score_frame.parquet", "coverage.json")}
        sealed = {"schema": METHOD_ID + "_formation_seal", "score_sealed_at": now(),
            "registration": inputs._ref(registration), "registered_days": record["decision_days"],
            "original_pool_rows": len(full), "matched_score_rows": len(frame),
            "files": formation_files,
            "future_endpoint_data_read": False, **FLAGS}
        _save(folder / "formation_seal.json", sealed)
        phase["formation_sealed"] = True
        symbols = sorted(frame.index.get_level_values("symbol").unique())
        plans = {f"{day}:{endpoint}": {"entry_day": plan["entry_day"], "exit_day": plan[key]}
                 for day, plan in record["endpoint_plan"].items()
                 for endpoint, key in (("h5", "h5_exit_day"), ("h21", "h21_exit_day"))}
        phase["future_endpoint_read_started"] = True
        opening, observations, needed = endpoint_panel(
            {"calendar": record["source"]["calendar"], "panel": record["source"]["canonical_panel"]}, plans, symbols)
        labels, label_rows = _labels(frame, record["endpoint_plan"], opening, observations)
        pd.DataFrame(label_rows).to_parquet(folder / "labels.parquet", index=False)
        pd.DataFrame(label_rows).to_csv(folder / "labels.csv", index=False)
        evaluation = diagnostics.evaluate(frame, labels, record["decision_days"])
        for name in ("daily", "groups", "summaries", "paired_late_daily", "paired_late_summaries"):
            _save(folder / (name + ".json"), evaluation[name])
            if name != "groups":
                pd.DataFrame(evaluation[name]).to_csv(folder / (name + ".csv"), index=False)
        result = {"schema": RESULT_SCHEMA, "method_id": METHOD_ID,
            "status": "completed_saved_whole_pool_weekly_liquidity_rank_diagnostic", "completed_at": now(),
            "registration": inputs._ref(registration), "spec": deepcopy(SPEC),
            "source_clock": record["source"]["source_clock"], "score_sealed_at": sealed["score_sealed_at"],
            "registered_days": len(record["decision_days"]), "original_pool_rows": len(full),
            "matched_score_rows": len(frame), "unique_matched_symbols": len(symbols),
            "coverage": coverage, "load_observation": observation,
            "endpoint_rowgroups_read": len(needed), "endpoint_days_read": needed,
            "endpoint_labels_known": {name: int(value.notna().sum()) for name, value in labels.items()},
            "outputs": {name: inputs._ref(folder / name) for name in (
                "formation.parquet", "formation.csv", "score_frame.parquet", "formation_seal.json",
                "coverage.json", "labels.parquet", "labels.csv", "daily.json", "groups.json", "summaries.json",
                "paired_late_daily.json", "paired_late_summaries.json")},
            "account_return": None, "independent_replications_claimed": False,
            "source_prices_scores_and_accounts_modified": False, **phase, **FLAGS}
        _save(folder / "result.json", result)
        return result
    except Exception as error:
        _save(folder / "failure.json", {"schema": RESULT_SCHEMA + "_failure", "failed_at": now(),
            "error": f"{type(error).__name__}: {error}", **phase, **FLAGS})
        raise


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("register")
    prepare.add_argument("--weekly-run", type=Path, required=True)
    prepare.add_argument("--reference-note", type=Path)
    prepare.add_argument("--out", type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--registration", type=Path, required=True)
    execute.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = register(args.weekly_run, args.out, reference_note=args.reference_note) if args.command == "register" else run(args.registration, args.out)
    print(json.dumps({"schema": result["schema"], "output": str(args.out.resolve())}, ensure_ascii=False))
    return 0


main = cli
