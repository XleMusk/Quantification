"""Opt-in past-only symbol projections for the fixed ret60 observation buffer.

No reader is opened here. The caller owns the already pinned NativeHistorySource
instances, full original warmup, first-day scope and explicit fixed calendars.
"""
from copy import deepcopy
from datetime import datetime

import pandas as pd

from ..contracts import MARKET_TZ


METHOD_ID = "native_first_decision_ret60_valid_observation_prefix_restoration_v1"
REQUIRED_OBSERVATIONS = 61  # D's close and its 60th prior valid observed close.


def restore_valid_warmup(history, *, first_day, warmup_days, scope_members,
                        qualification, fixed_sessions, source_by_day, observed_at=None, physical_native=None):
    """Return (unaltered projected prefix rows, explicit recovery evidence).

    `history` may include later replay days, which are excluded before counting.
    Only the first day's frozen scope and reported listing metadata select work.
    Missing exact whole-source days stop the search and remain unresolved.
    """
    scope = sorted(scope_members)
    if physical_native is None:
        from .leader_eight_type_historical_native import _physical_native
        physical_native = _physical_native
    sessions = list(fixed_sessions)
    warmup = list(warmup_days)
    if (not scope or len(scope) != len(set(scope_members)) or sessions != sorted(set(sessions))
            or first_day not in sessions or len(warmup) != 61 or warmup != sorted(set(warmup))
            or warmup[-1] >= first_day
            or [day for day in sessions if warmup[0] <= day < first_day] != warmup):
        raise ValueError("Unchanged first-day scope and exact original 61-session full warmup required")
    past = history.loc[history.date.le(first_day)].copy()
    if (past.empty or past.date.min() != warmup[0] or past.duplicated(["date", "code"]).any()
            or sorted(past.loc[past.date.eq(first_day), "symbol"].unique()) != scope):
        raise ValueError("Original full warmup and frozen first-day native rows required; no future scope selection")
    physical = physical_native(past)
    counts = physical.loc[physical.active_non_st].groupby("symbol").size().to_dict()
    initial = {symbol: int(counts.get(symbol, 0)) for symbol in scope}
    counts = dict(initial)
    outcomes, remaining = {}, set()
    for symbol in scope:
        record = qualification.records.get(symbol)
        if counts[symbol] >= REQUIRED_OBSERVATIONS:
            outcomes[symbol] = "original_buffer_complete"
        elif not record or not record.get("ipo_date"):
            outcomes[symbol] = "unknown_listing_metadata_not_restored"
        elif record["ipo_date"] > first_day:
            outcomes[symbol] = "posthoc_not_yet_listed_retains_unknown"
        elif (record["ipo_date"] >= warmup[0]
                or (record["ipo_date"] >= sessions[0]
                    and sum(record["ipo_date"] <= day <= first_day for day in sessions) < REQUIRED_OBSERVATIONS)):
            outcomes[symbol] = "known_insufficient_posthoc_listing_history_retains_unknown"
        else:
            remaining.add(symbol)
    original_targets = sorted(remaining)
    frames, reads, missing_source = [], [], None
    for day in reversed([day for day in sessions if day < warmup[0]]):
        # A reported IPO is a boundary, never permission to consume pre-IPO bars.
        for symbol in list(remaining):
            if day < qualification.records[symbol]["ipo_date"]:
                outcomes[symbol] = "reported_listing_boundary_reached_retains_unknown"
                remaining.remove(symbol)
        if not remaining:
            break
        required = sorted(remaining)
        source = source_by_day.get(day)
        if source is None:
            missing_source = {"day": day, "symbols": required, "reason": "exact_fixed_calendar_source_missing"}
            break
        frame = source.frame(day, symbols=required)
        proof = deepcopy(frame.attrs.get("provenance"))
        if (not isinstance(proof, dict) or proof.get("day") != day or proof.get("row_group") is None
                or not frame.date.eq(day).all() or not set(frame.symbol) <= set(required)
                or frame.duplicated(["date", "code"]).any()):
            raise ValueError("Exact projected native source rows and original row-group provenance required")
        frame.attrs.clear()
        # Retain every returned raw row, including halts/ST/empty quantities.
        # The shared feature predicate alone determines which observations count.
        physical = physical_native(frame)
        valid = physical.loc[physical.active_non_st].symbol.tolist()
        for symbol in valid:
            counts[symbol] += 1
            if counts[symbol] >= REQUIRED_OBSERVATIONS:
                outcomes[symbol] = "restored_61_valid_observations"
                remaining.remove(symbol)
        frames.append(frame)
        reads.append({"day": day, "symbols_requested": required,
            "scope": "explicit_symbol_projection_not_full_market_history",
            "projected_rows_returned": len(frame), "projected_rows_included": len(frame),
            "valid_observations_added": len(valid), "valid_symbols": valid,
            "missing_requested_symbols": sorted(set(required) - set(frame.symbol)),
            "row_group_provenance": proof, "original_source_provenance": deepcopy(source.provenance)})
    for symbol in remaining:
        outcomes[symbol] = ("exact_source_missing_retains_unknown" if missing_source
            else "explicit_calendar_prefix_exhausted_retains_unknown")
    prefix = (pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
        if frames else history.iloc[0:0].copy())
    if prefix.duplicated(["date", "code"]).any() or (not prefix.empty and prefix.date.max() >= warmup[0]):
        raise ValueError("Recovered rows must be a distinct strictly earlier native prefix")
    evidence = {"schema": "leader_valid_warmup_restoration_v1", "method_id": METHOD_ID,
        "created_at": observed_at or datetime.now(MARKET_TZ).isoformat(), "first_decision_day": first_day,
        "original_full_market_warmup_days": warmup, "original_full_market_warmup_unchanged": True,
        "required_valid_observations": REQUIRED_OBSERVATIONS, "first_day_scope_members": scope,
        "recovery_targets_selected_using_first_day_or_earlier_only": original_targets,
        "initial_valid_count": initial, "final_valid_count": counts, "outcomes": outcomes,
        "projection_day_sources": reads, "missing_source": missing_source,
        "projected_prefix_rows": len(prefix), "future_rows_used_for_selection_or_counting": False,
        "original_raw_rows_imputed_or_replaced": False, "halts_or_ST_counted_as_valid": False,
        "physical_rowgroup_decoding_still_required": True,
        "listing_metadata_method": "posthoc_reported_listing_boundaries_not_historical_PIT",
        "all_historical_observations_seen": False, "pit_verified": False, "returns_reportable": False,
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
        "execution_admitted": False}
    return prefix, evidence
