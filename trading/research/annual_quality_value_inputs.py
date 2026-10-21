"""Retrospective annual quality/value inputs from the saved monthly value pool.

Current retrieved financial versions remain current retrieved versions.  Only a
separately named annual June assumption maps them to past formation dates.
This adapter neither changes the prospective AnnualQualityFeed nor reads prices.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .monthly_timing import month_ends
from .profitability_portfolios import rank_z
from .value_portfolios import CASES as ORIGINAL_CASES, ID as ORIGINAL_METHOD


SCHEMA = "retrospective_annual_quality_saved_value_pool_inputs_v1"
CASES = ("matched_momentum", "value_only", "annual_quality_only", "quality_value_half_half")
QUALITY_FIELDS = ("revenue_cost_spread_assets", "operating_cashflow_assets")
STATEMENTS = ("balance", "income", "cashflow")
POOL_COLUMNS = ("symbol", "date", "momentum", "volatility", "median_amount", "peTTM", "pbMRQ",
                "earnings_yield", "book_yield", "value_rank")
QUALITY_COLUMNS = ("symbol", "report_day", "asof", "pit_verified", "units_verified", "execution_admitted",
    *QUALITY_FIELDS, *[field + suffix for field in QUALITY_FIELDS for suffix in ("_status", "_received_at")],
    *[statement + suffix for statement in STATEMENTS
      for suffix in ("_statement_observed", "_notice_at", "_retrieved_at")])
RANK_COLUMNS = ("date", "rank", "symbol", "score", "momentum", "volatility", "median_amount",
                "quality_score", "value_score", *QUALITY_FIELDS)
FLAGS = {"research_only": True, "candidate_panel_only": True, "retrospective": True,
    "pit_verified": False, "historical_publication_verified": False, "independent_holdout_claimed": False,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "returns_reportable": False, "account_created": False, "engine_advanced": False,
    "orders_generated": False, "network_accessed": False, "source_snapshots_modified": False}
SPEC = {"schema": SCHEMA, "cases": list(CASES),
    "parent_pool": "saved_completed_value_monthly_value_pool_all_rows_no_price_feature_rebuild",
    "formation": "exact_original_source_calendar_month_ends_only_in_original_order",
    "fiscal_year": "D_year_minus1_at_June_last_session_or_later_otherwise_D_year_minus2_exact_year_no_fallback",
    "notice_rule": "balance_income_cashflow_reported_notice_day_each_strictly_before_D",
    "availability": "original_available_numeric_two_fields_and_recorded_receipts_by_original_feature_asof",
    "receipt_rule": "actual_received_and_retrieved_times_preserved_not_required_before_historical_D",
    "historical_effective": "separate_derived_assumed_historical_effective_last_source_June_session_next_year_not_actual_receipt",
    "common_pool": "same_original_value_pool_intersect_exact_FY_joint_quality_complete_for_all_four_cases",
    "quality_score": "(existing_rank_z(revenue_cost_spread_assets)+existing_rank_z(operating_cashflow_assets))/2",
    "value_score": "existing_rank_z(original_saved_value_rank)_in_the_joint_pool_no_EP_BP_recompute",
    "matched_momentum": "original_saved_momentum",
    "value_only": "value_score", "annual_quality_only": "quality_score",
    "quality_value_half_half": ".5*quality_score+.5*value_score",
    "ranking": "descending_fixed_score_then_symbol_ascending_top30",
    "insufficient_pool": "all_cases_NO_CHANGE_if_joint_pool_below30_never_empty_FULL_or_replacement",
    "negative_and_zero_quality_values": "retained_without_clipping_positive_denominators_already_source_feature_contract",
    "asset_growth_used": False, "future_labels_or_action_quality_used": False, **FLAGS}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _digest(path):
    result = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": _digest(path)}


def inspect_saved_inputs(value_run, quality_result):
    """Inspect saved JSON/CSV metadata and hash only the two small payloads."""
    value_run, quality_result = Path(value_run).resolve(), Path(quality_result).resolve()
    value, quality = _read(value_run), _read(quality_result)
    if (value.get("research_family") != ORIGINAL_METHOD
            or value.get("status") != "completed_economic_value_diagnostics"
            or value.get("protocol", {}).get("signals") != list(ORIGINAL_CASES)
            or value.get("execution_admitted") is not False or value.get("strategy_validated") is not False
            or [(r.get("signal"), r.get("status"), r.get("completed_sessions")) for r in value.get("results", [])]
            != [(case, "completed", value.get("sessions")) for case in ORIGINAL_CASES]):
        raise ValueError("Require the original completed four-account fixed value result")
    if (quality.get("status") != "completed_annual_quality_features"
            or quality.get("execution_admitted") is not False or quality.get("pit_verified") is not False
            or not set(QUALITY_FIELDS) <= set(quality.get("definitions", {}))):
        raise ValueError("The original unadmitted saved annual quality feature result is required")
    asof = pd.Timestamp(quality["asof"])
    if asof.tzinfo is None:
        raise ValueError("Original annual quality as-of must retain its timezone")
    periods = sorted({row["report_day"] for row in quality["profiles"]})
    if periods != ["2023-12-31", "2024-12-31", "2025-12-31"]:
        raise ValueError("Use the existing three fiscal years without manufacturing FY2022")
    stock_inputs = value["protocol"]["stock_inputs"]
    calendar_file = Path(stock_inputs["event_view"]) / "source_input_calendar.csv"
    table = pd.read_csv(calendar_file, dtype=str, keep_default_na=False)
    if table.columns.tolist() != ["date"]:
        raise ValueError("The original value source calendar schema changed")
    calendar = table.date.tolist()
    replay = [d for d in calendar if value["start"] <= d <= value["end"]]
    decisions = [d for d in month_ends(calendar, value["end"]) if d >= value["start"]]
    if (calendar != sorted(set(calendar)) or not replay
            or replay[0] != value["start"] or replay[-1] != value["end"]
            or len(replay) != value["sessions"]
            or len(decisions) != value["pool_summary"]["decision_months"]):
        raise ValueError("Saved value dates do not equal the original full monthly study calendar")
    pool_file = Path(value["rankings"]).parent / "monthly_value_pool.parquet"
    quality_file = quality_result.parent / "annual_quality_features.parquet"
    return {"schema": SCHEMA, "spec": deepcopy(SPEC), "source_run": str(value_run),
        "source_refs": {"value_run": _ref(value_run), "quality_result": _ref(quality_result),
                        "calendar": _ref(calendar_file)},
        "payload_refs": {"value_pool": _ref(pool_file), "annual_quality": _ref(quality_file)},
        "stock_inputs": deepcopy(stock_inputs), "config": deepcopy(value["protocol"]["portfolio"]),
        "start": value["start"], "end": value["end"], "calendar": calendar, "replay_days": replay,
        "decision_days": decisions, "pool_summary": deepcopy(value["pool_summary"]),
        "quality_rows": quality["rows"], "quality_securities": quality["securities"],
        "quality_periods": periods, "quality_asof": quality["asof"],
        "source_clocks": {"quality_asof": quality["asof"],
            "basis": "current_retrieved_financial_version_plus_separate_retrospective_June_assumption_not_PIT",
            "actual_received_at_columns_preserved": True, "actual_statement_retrieved_at_columns_preserved": True}, **FLAGS}


inspect_inputs = inspect_saved_inputs


def _notice_day(values):
    def local_day(value):
        if pd.isna(value) or value == "":
            return pd.NaT
        try:
            stamp = pd.Timestamp(value)
        except (TypeError, ValueError):
            return pd.NaT
        if pd.isna(stamp):
            return pd.NaT
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("Asia/Shanghai")
        return pd.Timestamp(stamp.date())
    return pd.Series([local_day(value) for value in values], index=values.index, dtype="datetime64[ns]")


def score_monthly_pool(original, annual, meta):
    """Pure small-panel transform; no reads, output writes or future variables."""
    if (not set(POOL_COLUMNS) <= set(original) or not set(QUALITY_COLUMNS) <= set(annual)
            or original.duplicated(["date", "symbol"]).any()
            or annual.duplicated(["report_day", "symbol"]).any()):
        raise ValueError("Distinct original monthly pool and exact fiscal-year source rows required")
    observed_days = original.date.drop_duplicates().tolist()
    expected = meta["decision_days"]
    month_last = {d[:7]: d for d in meta["calendar"]}
    if (observed_days != expected or any(month_last.get(day[:7]) != day for day in observed_days)):
        raise ValueError("Every original pool formation must be the exact source-calendar month end in original order")
    numeric = original[["momentum", "volatility", "median_amount", "peTTM", "pbMRQ", "value_rank"]]
    if (not np.isfinite(numeric.to_numpy(dtype=float)).all()
            or original.peTTM.eq(0).any() or original.pbMRQ.le(0).any()
            or original.volatility.le(0).any()):
        raise ValueError("Saved original value pool has unknown/zero PE, nonpositive PB or invalid original features")
    if (not annual["asof"].eq(meta["quality_asof"]).all()
            or any(not value.endswith("-12-31") for value in annual.report_day.unique())
            or sorted(annual.report_day.unique()) != meta["quality_periods"]):
        raise ValueError("Annual source periods/as-of differ from the original saved feature result")
    source_asof = pd.Timestamp(meta["quality_asof"])
    if source_asof.tzinfo is None:
        raise ValueError("Original source as-of must retain its actual timezone")
    full, joint, coverage = [], [], []
    ranks = {case: [] for case in CASES}
    statuses = {}
    for day in observed_days:
        year = int(day[:4]) - (1 if int(day[5:7]) >= 6 else 2)
        report = f"{year}-12-31"
        assumed_effective = month_last.get(f"{year+1}-06")
        part = original.loc[original.date.eq(day)].copy()
        own = annual.loc[annual.report_day.eq(report)].copy()
        current = part.merge(own, on="symbol", how="left", validate="one_to_one")
        present = current.report_day.notna()
        available = present.copy()
        feature_counts = {}
        for field in QUALITY_FIELDS:
            field_numeric = np.isfinite(pd.to_numeric(current[field], errors="coerce"))
            status_ready = current[field + "_status"].eq("available_numeric")
            received = pd.to_datetime(current[field + "_received_at"], utc=True, errors="coerce")
            received_ready = received.notna() & received.le(source_asof)
            available &= field_numeric & status_ready & received_ready
            feature_counts[field + "_numeric_status_available"] = int((field_numeric & status_ready).sum())
            feature_counts[field + "_receipt_unknown_or_after_asof"] = int((present & ~received_ready).sum())
            current[field + "_actual_received_after_D"] = received.ge(pd.Timestamp(day, tz="Asia/Shanghai"))
        notices_ready = pd.Series(True, index=current.index)
        notice_unknown = pd.Series(False, index=current.index)
        notice_late = pd.Series(False, index=current.index)
        for statement in STATEMENTS:
            notice = _notice_day(current[statement + "_notice_at"])
            unknown = notice.isna()
            late = notice.ge(pd.Timestamp(day))
            notices_ready &= notice.notna() & notice.lt(pd.Timestamp(day))
            notice_unknown |= unknown
            notice_late |= late
            current[statement + "_derived_notice_day"] = notice.dt.strftime("%Y-%m-%d")
            retrieved = pd.to_datetime(current[statement + "_retrieved_at"], utc=True, errors="coerce")
            available &= current[statement + "_statement_observed"].eq(True) & retrieved.notna() & retrieved.le(source_asof)
        eligible = available & notices_ready
        current["required_fiscal_year"] = year
        current["required_report_day"] = report
        current["derived_assumed_historical_effective"] = assumed_effective
        current["historical_effective_is_assumption_not_received_at"] = True
        current["exact_fiscal_year_row_present"] = present
        current["joint_quality_eligible"] = eligible
        current["reported_notice_unknown"] = notice_unknown
        current["reported_notice_not_strictly_before_D"] = notice_late
        current["quality_score"] = np.nan
        current["value_score"] = np.nan
        selected = current.loc[eligible].copy().set_index("symbol").sort_index()
        if len(selected):
            selected["quality_score"] = sum(rank_z(selected[field]) for field in QUALITY_FIELDS) / 2
            selected["value_score"] = rank_z(selected.value_rank)
            for column in ("quality_score", "value_score"):
                current.loc[eligible, column] = current.loc[eligible, "symbol"].map(selected[column])
        status = "FULL_TARGET" if len(selected) >= 30 else "NO_CHANGE"
        reason = ("joint_quality_pool_ready" if status == "FULL_TARGET" else
                  "exact_required_fiscal_year_missing" if not len(own) else "joint_quality_pool_below30_or_unknown")
        statuses[day] = {"status": status, "reason": reason, "original_pool_members": len(part),
            "joint_pool_members": len(selected), "fiscal_year": year, "report_day": report}
        coverage.append({"date": day, "required_fiscal_year": year, "required_report_day": report,
            "derived_assumed_historical_effective": assumed_effective, "original_pool_members": len(part),
            "exact_fiscal_year_source_rows": len(own), "exact_fiscal_year_rows_in_original_pool": int(present.sum()),
            "joint_pool_members": len(selected), "excluded_or_unknown_members": len(part) - len(selected),
            "reported_notice_unknown_members": int((present & notice_unknown).sum()),
            "reported_notice_not_strictly_before_D_members": int((present & notice_late).sum()),
            "quality_values_negative": {field: int(selected[field].lt(0).sum()) for field in QUALITY_FIELDS},
            "quality_values_zero": {field: int(selected[field].eq(0).sum()) for field in QUALITY_FIELDS},
            "status": status, "reason": reason, **feature_counts})
        full.append(current)
        if len(selected):
            selected["matched_momentum_score"] = selected.momentum
            selected["value_only_score"] = selected.value_score
            selected["annual_quality_only_score"] = selected.quality_score
            selected["quality_value_half_half_score"] = .5 * selected.quality_score + .5 * selected.value_score
            joint.append(selected.reset_index())
        if status == "FULL_TARGET":
            for case in CASES:
                ordered = selected.assign(score=selected[case + "_score"]).reset_index().sort_values(
                    ["score", "symbol"], ascending=[False, True], kind="stable").head(30)
                ordered["rank"] = np.arange(1, len(ordered) + 1)
                ranks[case].append(ordered[list(RANK_COLUMNS)])
    return {"full_pool": pd.concat(full, ignore_index=True),
        "pool": pd.concat(joint, ignore_index=True) if joint else pd.DataFrame(),
        "ranks": {case: pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=RANK_COLUMNS)
                  for case, parts in ranks.items()}, "coverage": coverage, "status_by_day": statuses,
        "source_clocks": deepcopy(meta["source_clocks"]), "spec": deepcopy(SPEC), **FLAGS}


def build_inputs(value_run, quality_result, *, expected_meta=None):
    meta = inspect_saved_inputs(value_run, quality_result)
    if expected_meta is not None and meta != expected_meta:
        raise ValueError("Saved annual quality/value input generation changed after registration")
    original = pd.read_parquet(meta["payload_refs"]["value_pool"]["path"])
    annual = pd.read_parquet(meta["payload_refs"]["annual_quality"]["path"], columns=list(QUALITY_COLUMNS))
    expected = meta["pool_summary"]
    if (len(original) != expected["common_rows"] or original.symbol.nunique() != expected["distinct_pool_symbols"]
            or original.date.nunique() != expected["decision_months"]
            or len(annual) != meta["quality_rows"] or annual.symbol.nunique() != meta["quality_securities"]):
        raise ValueError("Saved complete pool or annual source inventory changed")
    return {"meta": meta, **score_monthly_pool(original, annual, meta)}
