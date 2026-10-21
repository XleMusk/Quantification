"""Five fixed daily leader proxies from a frozen physical-price inventory.

Only dated source observations and historical bars are read. No model is fit,
future survival path is selected, account is written or order is constructed.
Provider inventory coverage is explicitly weaker than an exchange/PIT roster.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from ..rules import stock_board
from .exchange_calendar import load_calendar
from .shortline_decision_v2 import BATCH_SCHEMA


FAMILIES = (
    "stage1_amount_baseline", "stage2_amount_baseline", "stage3_amount_baseline",
    "trend_leader_20_60", "daily_rebound_proxy",
)
SOURCE_SCOPE = "pinned_provider_observed_universe"
MIN_AMOUNT = 20_000_000.0
RAW_COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "is_st", "pre_close"]
NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "amount", "pct_chg"]


def _canonical_day(value) -> str:
    text = str(value)
    return date.fromisoformat(text).isoformat()


def read_physical_snapshot(snapshot: Path, *, end: str) -> tuple[pd.DataFrame, dict]:
    """Reuse the pinned manifest's partitions, retaining all available past history."""
    snapshot = Path(snapshot).resolve()
    end = _canonical_day(end)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False:
        raise ValueError("Leader diagnostics require the existing unadmitted complete inventory")
    if len(manifest.get("chunks", [])) != manifest.get("chunks_expected"):
        raise ValueError("Frozen inventory's declared partition count is incomplete")
    end_int = int(end.replace("-", ""))
    pieces, partitions = [], []
    for item in manifest["chunks"]:
        if int(item["lower_inclusive"]) > end_int:
            continue
        piece = pd.read_parquet(snapshot / item["parquet_file"], columns=RAW_COLUMNS)
        if len(piece) != int(item["rows"]):
            raise ValueError(f"Frozen partition row count changed: {item['parquet_file']}")
        piece["date"] = pd.to_datetime(piece["date"].astype(str), format="%Y%m%d", errors="raise").dt.strftime("%Y-%m-%d")
        piece = piece.loc[piece["date"].le(end)].copy()
        pieces.append(piece)
        partitions.append({
            "prefix": str(item["prefix"]), "lower": str(item["lower_inclusive"]),
            "upper": str(item["upper_inclusive"]), "file": item["parquet_file"],
            "observed_dates": sorted(piece["date"].unique().tolist()), "rows_loaded": len(piece),
        })
    if not pieces:
        raise ValueError("No physical source observations at or before the requested end")
    raw = pd.concat(pieces, ignore_index=True)
    metadata = {
        "snapshot": str(snapshot), "manifest": str(snapshot / "manifest.json"),
        "manifest_status": manifest["status"], "manifest_end": manifest["end"],
        "prefixes": [str(value) for value in manifest["prefixes"]], "partitions": partitions,
        "source": manifest.get("source"), "source_verified": False,
        "source_coverage_scope": SOURCE_SCOPE,
    }
    return raw, metadata


def _calendar_view(payload: dict | None, start: str, end: str, observed_dates: list[str]) -> tuple[list[str], dict[str, str | None], str]:
    if not payload or payload.get("schema") != "cn_a_share_exchange_calendar_v1":
        return [day for day in observed_dates if start <= day <= end], {}, "calendar_unavailable_observed_dates_only"
    year = int(payload["year"])
    closed = set()
    for interval in payload.get("closed_ranges", []):
        a, b = interval.split("/", 1)
        cursor, stop = date.fromisoformat(a), date.fromisoformat(b)
        while cursor <= stop:
            closed.add(cursor.isoformat())
            cursor += timedelta(days=1)
    overrides = set(payload.get("open_sessions", []))
    sessions = []
    cursor = date(year, 1, 1)
    stop = date(year, 12, 31)
    while cursor <= stop:
        text = cursor.isoformat()
        if text in overrides or (cursor.weekday() < 5 and text not in closed):
            sessions.append(text)
        cursor += timedelta(days=1)
    previous = {day: sessions[index - 1] if index else None for index, day in enumerate(sessions)}
    selected = [day for day in sessions if start <= day <= end]
    if date.fromisoformat(start).year != year or date.fromisoformat(end).year != year:
        return sorted(set(selected) | {day for day in observed_dates if start <= day <= end}), previous, "calendar_year_partially_supported"
    return selected, previous, "pinned_static_exchange_calendar"


def _physical_frame(raw: pd.DataFrame, end: str) -> pd.DataFrame:
    missing = sorted(set(RAW_COLUMNS) - set(raw.columns))
    if missing:
        raise ValueError(f"Physical raw frame missing columns: {missing}")
    frame = raw.copy()
    frame["date"] = pd.to_datetime(frame["date"].astype(str), errors="raise").dt.strftime("%Y-%m-%d")
    frame = frame.loc[frame["date"].le(end)].copy()
    frame["code"] = frame["code"].astype(str).str.split(".").str[0].str.zfill(6)
    if frame.duplicated(["code", "date"]).any():
        raise ValueError("Duplicate physical code/date observations")
    frame["ordinary_equity"] = frame["code"].str.match(r"(?:60\d{4}|00\d{4}|30\d{4}|688\d{3})$")
    frame = frame.loc[frame["ordinary_equity"]].copy()
    frame["symbol"] = frame["code"] + np.where(frame["code"].str.startswith(("60", "688")), ".SSE", ".SZSE")
    st_text = frame["is_st"].astype(str).str.strip().str.lower()
    frame["is_st_unknown"] = ~st_text.isin({"true", "false", "1", "0", "1.0", "0.0"})
    frame["is_st"] = st_text.isin({"true", "1", "1.0"})
    frame["board"] = frame["symbol"].map(stock_board)
    for field in NUMERIC_COLUMNS:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame["preclose_for_limits"] = frame["close"] / (1 + frame["pct_chg"] / 100).where(frame["pct_chg"].gt(-100))
    finite = np.isfinite(frame[NUMERIC_COLUMNS + ["preclose_for_limits"]].to_numpy()).all(axis=1)
    positive = frame[["open", "high", "low", "close", "preclose_for_limits"]].gt(0).all(axis=1)
    shape = frame["high"].ge(frame[["open", "close"]].max(axis=1)) & frame["low"].le(frame[["open", "close"]].min(axis=1)) & frame["high"].ge(frame["low"])
    frame["source_row_valid"] = finite & positive & shape & frame["volume"].ge(0) & frame["amount"].ge(0) & ~frame["is_st_unknown"]
    frame["active_non_st"] = frame["source_row_valid"] & ~frame["is_st"] & frame["volume"].gt(0)
    return frame.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def _features(frame: pd.DataFrame, previous_session: dict) -> pd.DataFrame:
    history = frame.loc[frame["active_non_st"]].copy()
    group = history.groupby("symbol", sort=False)
    history["previous_observed_day"] = group["date"].shift(1)
    history["previous_close"] = group["close"].shift(1)
    history["previous_open"] = group["open"].shift(1)
    history["previous_pct_chg"] = group["pct_chg"].shift(1)
    history["calendar_contiguous"] = history["previous_observed_day"].eq(history["date"].map(previous_session)) & history["previous_observed_day"].notna()
    rates = np.where(history["board"].eq("main"), .10, .20)
    history["limit_up"] = np.floor(history["close"] * 100 + .5).ge(np.floor(history["preclose_for_limits"] * (1 + rates) * 100 + .5))
    prior_limit = history.groupby("symbol", sort=False)["limit_up"].shift(1).eq(True)
    reference_known = (history["preclose_for_limits"] / history["previous_close"] - 1).abs().le(.01)
    symbol_changed = history["symbol"].ne(history["symbol"].shift(1))
    run_start = symbol_changed | ~history["limit_up"] | ~prior_limit | ~history["calendar_contiguous"] | ~reference_known
    run_id = run_start.cumsum()
    stage = history.groupby(run_id, sort=False).cumcount() + 1
    start_known = ~history["limit_up"] | (history["calendar_contiguous"] & reference_known & ~prior_limit)
    run_known = start_known.groupby(run_id, sort=False).transform("first")
    history["stage"] = np.where(~history["limit_up"], 0, np.where(run_known, stage, np.nan))
    history["stage_reference_known"] = ~history["limit_up"] | run_known
    # Observed-row histories are causal. Return lag60 needs61 closes even
    # though the SMA60 itself needs only60; no shorter-history substitution.
    history["observed_history_rows"] = group.cumcount() + 1
    history["sma20"] = group["close"].transform(lambda values: values.rolling(20, min_periods=20).mean())
    history["sma60"] = group["close"].transform(lambda values: values.rolling(60, min_periods=60).mean())
    history["ret20"] = history["close"] / group["close"].shift(20) - 1
    history["ret60"] = history["close"] / group["close"].shift(60) - 1
    history["amount_rank_pct"] = history.groupby("date", sort=False)["amount"].rank(method="average", pct=True)
    two_back_limit = history.groupby("symbol", sort=False)["limit_up"].shift(2).eq(True)
    previous_contiguous = history.groupby("symbol", sort=False)["calendar_contiguous"].shift(1).eq(True)
    history["daily_rebound"] = two_back_limit & history["calendar_contiguous"] & previous_contiguous & history["previous_pct_chg"].lt(0) & history["close"].gt(history["previous_open"]) & history["pct_chg"].gt(0)
    return history


def _coverage(day: str, current: pd.DataFrame, metadata: dict, calendar_status: str, declared_cohort: Sequence[str] | None) -> dict:
    day_int = int(day.replace("-", ""))
    required = set(metadata.get("prefixes", []))
    covered = {item["prefix"] for item in metadata.get("partitions", []) if int(item["lower"]) <= day_int <= int(item["upper"]) and day in item["observed_dates"]}
    observed = set(current["symbol"])
    valid = set(current.loc[current["source_row_valid"], "symbol"])
    missing = sorted(set(declared_cohort or []) - observed)
    invalid = sorted((set(declared_cohort or []) & observed) - valid)
    query_complete = bool(required) and required <= covered and not current.empty
    quality_complete = bool(len(current)) and bool(current["source_row_valid"].all())
    calendar_complete = calendar_status == "pinned_static_exchange_calendar"
    source_complete = query_complete and quality_complete and calendar_complete
    if not source_complete:
        status = "incomplete"
    elif declared_cohort is None:
        status = "observed_source_complete"
    elif not declared_cohort or missing or invalid:
        status = "incomplete"
    else:
        status = "complete"
    return {
        "day": day, "status": status, "source_coverage_scope": SOURCE_SCOPE,
        "source_verified": False, "exchange_universe_verified": False, "pit_verified": False,
        "required_query_prefixes": sorted(required), "observed_query_prefixes": sorted(covered),
        "source_query_partitions_complete": query_complete,
        "raw_ordinary_rows": int(len(current)), "finite_valid_ordinary_rows": int(current["source_row_valid"].sum()),
        "non_st_rows": int((~current["is_st"]).sum()), "active_non_st_rows": int(current["active_non_st"].sum()),
        "invalid_raw_rows": int((~current["source_row_valid"]).sum()), "raw_quality_complete": quality_complete,
        "unknown_st_rows": int(current["is_st_unknown"].sum()),
        "calendar_status": calendar_status, "declared_cohort_count": len(declared_cohort) if declared_cohort is not None else None,
        "declared_cohort_missing_current_raw": missing, "declared_cohort_invalid_current_raw": invalid,
        "research_source_complete": source_complete,
        "completion_basis": "Loaded physical decision-day partitions/finite rows, plus explicit declared-cohort coverage when supplied; not an exchange/PIT roster",
    }


def build_leader_daily_batches(raw: pd.DataFrame, metadata: dict, *, start: str, end: str,
                              calendar_payload: dict | None, declared_cohort: Sequence[str] | None = None,
                              retain_window_features: bool = False, ipo_qualification=None) -> dict:
    """Build all-source dated candidates; cohort filtering/top5 belongs to caller."""
    start, end = _canonical_day(start), _canonical_day(end)
    if start > end:
        raise ValueError("Start exceeds end")
    if declared_cohort is not None and len(set(declared_cohort)) != len(declared_cohort):
        raise ValueError("Declared cohort contains duplicates")
    frame = _physical_frame(raw, end)
    observed_dates = sorted(frame["date"].unique().tolist())
    calendar, previous, calendar_status = _calendar_view(calendar_payload, start, end, observed_dates)
    history = _features(frame, previous)
    feature_qualification = None
    decision_features = history
    if ipo_qualification is not None:
        # Import only on the explicit new path. Default historical features,
        # rules, outputs and previously frozen protocols stay unchanged.
        from .leader_listing_features import qualify_leader_window
        decision_features, feature_qualification = qualify_leader_window(history, qualification=ipo_qualification,
            calendar_payload=calendar_payload, decision_days=calendar)
    window_features = decision_features.loc[decision_features["date"].between(start, end) & decision_features["amount"].ge(MIN_AMOUNT)].copy()
    masks = {f"stage{stage}_amount_baseline": window_features["stage"].eq(stage) for stage in (1, 2, 3)}
    masks["trend_leader_20_60"] = window_features["close"].gt(window_features["sma20"]) & window_features["sma20"].gt(window_features["sma60"]) & window_features["ret20"].gt(0) & window_features["ret60"].notna()
    if feature_qualification is not None:
        masks["trend_leader_20_60"] &= window_features["listing_qualification_status"].eq("supported")
    masks["daily_rebound_proxy"] = window_features["daily_rebound"]
    selected = {}
    table_rows = []
    for family in FAMILIES:
        values = window_features.loc[masks[family]].copy()
        fields = ["date", "ret60", "amount_rank_pct", "symbol"] if family == "trend_leader_20_60" else ["date", "amount_rank_pct", "symbol"]
        ascending = [True, False, False, True] if family == "trend_leader_20_60" else [True, False, True]
        values = values.sort_values(fields, ascending=ascending, kind="stable")
        values["rank"] = values.groupby("date", sort=False).cumcount() + 1
        daily_sizes = values.groupby("date", sort=False)["symbol"].transform("size")
        values["score"] = 1 - (values["rank"] - 1) / daily_sizes
        selected[family] = {}
        for row in values.itertuples(index=False):
            candidate = {"symbol": row.symbol, "score": float(row.score), "rank": int(row.rank),
                         "amount_rank_pct": float(row.amount_rank_pct), "amount_cny": float(row.amount),
                         "pct_chg": float(row.pct_chg)}
            for field in ["stage", "ret20", "ret60"]:
                value = getattr(row, field)
                if pd.notna(value) and np.isfinite(value):
                    candidate[field] = float(value)
            selected[family].setdefault(row.date, []).append(candidate)
            table_rows.append({"family": family, "date": row.date, **candidate})
    by_day = {day: piece for day, piece in frame.groupby("date", sort=False)}
    empty = frame.iloc[:0]
    coverage_facts, batches = [], {family: {} for family in FAMILIES}
    for day in calendar:
        current = by_day.get(day, empty)
        coverage = _coverage(day, current, metadata, calendar_status, declared_cohort)
        coverage_facts.append(coverage)
        active = current.loc[current["active_non_st"]]
        advance_count, decline_count = int(active["pct_chg"].gt(0).sum()), int(active["pct_chg"].lt(0).sum())
        flat_count = int(active["pct_chg"].eq(0).sum())
        breadth = {
            "day": day, "scope": SOURCE_SCOPE, "complete": coverage["research_source_complete"],
            "verified": False, "source_verified": False, "raw_counts_verified": coverage["research_source_complete"],
            "verification_basis": "Physical active non-ST ordinary-equity rows from all pinned provider query partitions; exchange/PIT universe unknown",
            "source": metadata.get("manifest") or metadata.get("snapshot") or "provided_physical_inventory",
            "advance_count": advance_count, "decline_count": decline_count, "flat_count": flat_count,
            "expected_active_count": int(len(active)), "observed_active_count": int(len(active)),
            "breadth": (advance_count - decline_count) / len(active) if len(active) else None,
        }
        for family in FAMILIES:
            batches[family][day] = {
                "schema": BATCH_SCHEMA, "day": day, "status": coverage["status"],
                "source": f"research:leader_daily_physical:{family}", "family": family,
                "candidates": selected[family].get(day, []), "coverage": coverage,
                "market_breadth": breadth, "source_coverage_scope": SOURCE_SCOPE, "source_verified": False,
                "candidate_scope": "all_source_signal_date_candidates_not_filtered_to_declared_cohort",
                "score_semantics": "deterministic_rank_only_not_probability_or_expected_return",
                "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
                "signal_admitted": False, "execution_admitted": False, "model_refit": False,
                "future_labels_used_for_score": False,
            }
            if feature_qualification is not None:
                view = dict(feature_qualification["decisions"][day][family])
                view["source_coverage_status_before_qualification"] = coverage["status"]
                batches[family][day]["feature_qualification"] = view
                if not view["qualification_complete"]:
                    batches[family][day].update(status="incomplete", incomplete_reason="candidate_listing_feature_membership_unknown")
    first_observed = frame.groupby("symbol", sort=False)["date"].min().to_dict()
    raw_bars = frame.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
    candidates = pd.DataFrame(table_rows, columns=["family", "date", "symbol", "score", "rank", "amount_rank_pct", "amount_cny", "pct_chg", "stage", "ret20", "ret60"])
    result = {
        "schema": "leader_daily_physical_signal_batches_v1", "raw_bars": raw_bars,
        "calendar": calendar, "batches": batches, "candidate_table": candidates,
        "coverage_facts": coverage_facts, "first_observed_date": first_observed,
        "families": list(FAMILIES), "source_metadata": metadata,
        "protocol": {"min_amount_cny": MIN_AMOUNT, "gross_limit": .8, "holdings": 5,
                     "selection": "No cohort/cap/top5/future survival path selected by producer",
                     "trend_history": "SMA20/60 on valid observed closes; return lag60 requires61 closes",
                     "stage_history": "Pinned calendar continuity and existing1% raw price-reference quality bound; unresolved leading/gapped limit-up runs stay unknown"},
        "candidate_panel_only": True, "source_verified": False, "pit_verified": False,
        "strategy_validated": False, "selection_enabled": False, "signal_admitted": False,
        "execution_admitted": False, "model_refit": False, "future_labels_used_for_score": False,
    }
    if retain_window_features:
        # Retain only the requested dates. A close preview must not calculate
        # the entire multi-year feature frame twice just to show market counts.
        result["window_features"] = decision_features.loc[decision_features.date.between(start, end)].copy()
    if feature_qualification is not None:
        result["feature_qualification"] = feature_qualification
        result["protocol"]["feature_qualification_method_id"] = feature_qualification["method_id"]
        result["protocol"]["qualified_stage_history"] = "Explicitly verified first-five no-limit sessions excluded; session6 may start stage1 with exact previous raw/reference and explicit prior unrestricted-regime proof; generic unsupported-window guard alone is insufficient; metadata assessed separately at each decision cutoff"
    return result


def produce_leader_daily_batches(snapshot: Path, start: str, end: str, *, calendar_path: Path | None = None,
                                declared_cohort: Sequence[str] | None = None, ipo_qualification=None) -> dict:
    raw, metadata = read_physical_snapshot(snapshot, end=end)
    return build_leader_daily_batches(raw, metadata, start=start, end=end,
                                      calendar_payload=load_calendar(calendar_path), declared_cohort=declared_cohort,
                                      ipo_qualification=ipo_qualification)
