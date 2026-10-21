"""Source-native historical environment contexts, separate from qualified closes.

Reuses fixed phase thresholds and type applicability rules. Observed historical
coverage is not a complete dated exchange roster, candidate list or signal.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from .baostock_daily_slice import digest
from .leader_daily_signals import RAW_COLUMNS, _calendar_view, _physical_frame
from .leader_environment import _clean
from .leader_environment_policy import TYPE_RULES, classify_snapshot


SCHEMA = "leader_historical_observed_environment_v1"
PROTOCOL_SCHEMA = "leader_historical_observed_environment_protocol_v1"
METHOD_ID = "native_active_observed_2026_fixed_phase_context_v1"
PROTOCOL = {
    "id": METHOD_ID,
    "scope": "source_observed_SH_SZ_ordinary_valid_positive_volume_known_nonST_not_exchange_roster",
    "phase_rule": "unchanged_classify_snapshot_breadth_0.2_minus0.2_and_same_sign_median",
    "confirmation": "two_exact_adjacent_pinned_calendar_closes_same_nonunknown_phase_diagnostic_only",
    "sample": "first_two_confirmed_dates_per_phase_chronological_no_outcome_filter",
    "context_offsets": [-2, -1, 0, 1, 2],
    "source_fields": RAW_COLUMNS,
    "missing_days": "unknown_never_next_available",
    "native_units_adjustment_trade_status_not_inferred": True,
    "source_pre_close_used_for_execution": False,
    "current_complete_close_scope_equivalence": False,
    "historical_identity_sector_membership_backfilled": False,
    "individual_types_or_candidates_computed": False,
    "strategy_parameters_selected_by_returns": False,
}
FLAGS = {"candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "orders_generated": False,
    "engine_advanced": False, "ledger_mutated": False, "provider_contacted": False,
    "historical_publication_verified": False, "pit_verified": False,
    "corporate_actions_verified": False, "complete_dated_roster_verified": False,
    "price_adjustment_basis_verified": False, "units_verified": False}


def _now():
    return datetime.now().astimezone().isoformat()


def _time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Actual timezone-aware recording time required")
    return result


def read_history(snapshot, *, start, end, expected_manifest_sha256):
    """Read only overlapping original partitions; never call the source service."""
    snapshot = Path(snapshot).resolve()
    manifest_file = snapshot / "manifest.json"
    if digest(manifest_file) != expected_manifest_sha256:
        raise ValueError("Pinned source manifest changed")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if (manifest.get("status") != "complete_inventory_only"
            or manifest.get("execution_admitted") is not False
            or len(manifest.get("chunks", [])) != manifest.get("chunks_expected")):
        raise ValueError("Complete inventory-only source required")
    lower, upper = int(start.replace("-", "")), int(end.replace("-", ""))
    chosen = [item for item in manifest["chunks"]
              if int(item["lower_inclusive"]) <= upper and int(item["upper_inclusive"]) >= lower]
    pieces, proofs, intervals = [], [], []
    for item in chosen:
        bounds = (str(item["prefix"]), int(item["lower_inclusive"]), int(item["upper_inclusive"]))
        if any(p == bounds[0] and a <= bounds[2] and b >= bounds[1] for p, a, b in intervals):
            raise ValueError("Ambiguous overlapping source partitions")
        intervals.append(bounds)
        file = (snapshot / item["parquet_file"]).resolve()
        file.relative_to(snapshot)
        if digest(file) != item["parquet_sha256"]:
            raise ValueError("Pinned partition bytes changed")
        arrow = pq.ParquetFile(file)
        if (arrow.metadata.num_rows != int(item["rows"])
                or not set(RAW_COLUMNS) <= set(arrow.schema_arrow.names)):
            raise ValueError("Original partition row count or native columns changed")
        frame = pd.read_parquet(file, columns=RAW_COLUMNS)
        dates = pd.to_numeric(frame.date, errors="raise")
        if (not frame.code.astype(str).str.fullmatch(r"\d{6}").all()
                or not frame.code.astype(str).str.startswith(bounds[0]).all()
                or not dates.between(bounds[1], bounds[2]).all()):
            raise ValueError("Partition code/date disagrees with original query")
        frame = frame.loc[dates.between(lower, upper)].copy()
        frame["date"] = pd.to_datetime(frame.date.astype(str), format="%Y%m%d", errors="raise").dt.strftime("%Y-%m-%d")
        pieces.append(frame)
        proofs.append({"file": str(file), "sha256": item["parquet_sha256"],
                       "original_rows": item["rows"], "selected_rows": len(frame),
                       "prefix": bounds[0], "lower": bounds[1], "upper": bounds[2]})
    if not pieces:
        raise ValueError("No original partitions overlap requested history")
    raw = pd.concat(pieces, ignore_index=True)
    frame = _physical_frame(raw, end)
    source = {"snapshot": str(snapshot), "manifest": str(manifest_file),
        "manifest_sha256": expected_manifest_sha256, "partitions": proofs,
        "snapshot_created_at_field_not_historical_availability": manifest.get("created_at"),
        "source_limitations": manifest.get("limitations", []),
        "native_rows_read": len(raw), "unsupported_nonordinary_rows": len(raw) - len(frame),
        "units": "unknown_source_native", "price_adjustment_basis": "unknown_source_native",
        "source_pre_close_used_for_distribution": False}
    return frame, source


def summarize_days(frame, sessions):
    """Keep exact missing sessions unknown and separate source/active counts."""
    groups = {day: rows for day, rows in frame.groupby("date", sort=False)}
    result, previous = [], None
    for index, day in enumerate(sessions):
        rows = groups.get(day, frame.iloc[:0])
        active = rows.loc[rows.active_non_st]
        up, down, flat = (int(active.pct_chg.gt(0).sum()), int(active.pct_chg.lt(0).sum()), int(active.pct_chg.eq(0).sum()))
        breadth = float((up - down) / len(active)) if len(active) else None
        median = float(active.pct_chg.median()) if len(active) else None
        phase = classify_snapshot(breadth, median)
        confirmed = bool(index and previous["phase"] == phase and phase != "unknown")
        row = {"day": day, "source_observed_rows": len(rows),
            "source_observed_prefix_counts": {str(key): int(value) for key, value in rows.code.str[0].value_counts().items()},
            "source_valid_rows": int(rows.source_row_valid.sum()),
            "source_invalid_rows": int((~rows.source_row_valid).sum()),
            "source_ST_unknown_rows": int(rows.is_st_unknown.sum()),
            "source_known_ST_rows": int(rows.is_st.sum()),
            "source_zero_volume_rows": int(rows.volume.eq(0).sum()),
            "active_observed_non_ST_rows": len(active), "advance_count": up,
            "decline_count": down, "flat_count": flat, "breadth": breadth,
            "median_return_pct": median, "phase": phase,
            "diagnostic_two_close_phase_agreement": confirmed,
            "previous_exact_session": sessions[index-1] if index else None,
            "complete_dated_roster_verified": False,
            "phase_change": bool(index and previous["phase"] != phase
                                 and previous["phase"] != "unknown" and phase != "unknown")}
        row["phase_only_type_conditions"] = {identity: bool(confirmed and phase in rule["entry_phases_after_two_close_confirmation"])
                                              for identity, rule in TYPE_RULES.items()}
        result.append(row)
        previous = row
    return result


def select_contexts(days, full_calendar):
    counts, selected = Counter(), []
    by_day = {row["day"]: row for row in days}
    for row in days:
        phase = row["phase"]
        if not row["diagnostic_two_close_phase_agreement"] or counts[phase] >= 2:
            continue
        counts[phase] += 1
        index = full_calendar.index(row["day"])
        slots = []
        for offset in PROTOCOL["context_offsets"]:
            target = index + offset
            day = full_calendar[target] if 0 <= target < len(full_calendar) else None
            slots.append({"offset_sessions": offset, "day": day,
                          "status": "observed_context" if day in by_day and by_day[day]["source_observed_rows"] else "unknown_not_replaced",
                          "environment": by_day.get(day)})
        selected.append({"phase": phase, "day": row["day"], "selection_rank_in_phase": counts[phase],
                         "slots": slots, "individual_stocks_selected": False,
                         "historical_signal_or_complete_environment_claimed": False})
    return selected


def build(*, snapshot, calendar, start, end, protocol, out):
    out, protocol, calendar = Path(out).resolve(), Path(protocol).resolve(), Path(calendar).resolve()
    if out.exists():
        raise FileExistsError("Historical environment must use a new output directory")
    registered = json.loads(protocol.read_text(encoding="utf-8"))
    recorded, started = _time(registered["recorded_at"]), _now()
    if (registered.get("schema") != PROTOCOL_SCHEMA or registered.get("method_id") != METHOD_ID
            or registered.get("protocol") != PROTOCOL or registered.get("status") != "late_registered_historical_diagnostic_only"
            or Path(registered["snapshot"]).resolve() != Path(snapshot).resolve()
            or Path(registered["calendar"]).resolve() != calendar
            or registered.get("start") != start or registered.get("end") != end
            or end > registered["history_cutoff"] or recorded > _time(started)
            or recorded < _time(end + "T15:00:00+08:00")
            or any(registered.get(k) is not False for k in ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("Exact late-recorded fixed diagnostic protocol required")
    if digest(calendar) != registered["calendar_sha256"]:
        raise ValueError("Pinned calendar changed")
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    year = str(payload["year"])
    sessions, _, status = _calendar_view(payload, start, end, [])
    full_calendar, _, _ = _calendar_view(payload, year + "-01-01", year + "-12-31", [])
    if status != "pinned_static_exchange_calendar" or not sessions or sessions[0] != start or sessions[-1] != end:
        raise ValueError("Exact supported-year exchange-session bounds required")
    frame, source = read_history(snapshot, start=start, end=end,
        expected_manifest_sha256=registered["snapshot_manifest_sha256"])
    days = summarize_days(frame, sessions)
    contexts = select_contexts(days, full_calendar)
    created = _now()
    transitions = Counter(f"{a['phase']} -> {b['phase']}" for a, b in zip(days, days[1:]))
    result = {"schema": SCHEMA, "method_id": METHOD_ID,
        "status": "completed_observed_history_environment_context_not_complete_close_or_strategy",
        "created_at": created, "read_started_at": started, "available_at": created,
        "start": start, "end": end, "source": source,
        "protocol": {"file": str(protocol), "sha256": digest(protocol), "recorded_at": registered["recorded_at"]},
        "calendar": {"file": str(calendar), "sha256": digest(calendar), "sessions": sessions},
        "counts": {"expected_sessions": len(sessions),
            "observed_sessions": sum(row["source_observed_rows"] > 0 for row in days),
            "missing_sessions": sum(row["source_observed_rows"] == 0 for row in days),
            "phase_counts": dict(Counter(row["phase"] for row in days)),
            "confirmed_phase_counts": dict(Counter(row["phase"] for row in days if row["diagnostic_two_close_phase_agreement"])),
            "transitions": dict(transitions), "learning_contexts": len(contexts),
            "source_observed_non_calendar_days": sorted(set(frame.date) - set(sessions))},
        "days": days, "learning_contexts": contexts,
        "fixed_type_applicability_rules": TYPE_RULES,
        "individual_types_or_candidates_computed": False,
        "historical_identity_or_sector_backfilled": False,
        "actual_trade_status_inferred": False,
        "same_scope_as_recent_complete_dated_closes": False, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    (out / "result.json").write_text(json.dumps(_clean(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    table = pd.DataFrame([{k: v for k, v in row.items() if k not in ("phase_only_type_conditions", "source_observed_prefix_counts")} for row in days])
    table.to_csv(out / "daily.csv", index=False)
    (out / "learning_contexts.json").write_text(json.dumps(_clean(contexts), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "calendar", "start", "end", "protocol", "out"):
        parser.add_argument("--" + name, required=True)
    result = build(**vars(parser.parse_args(argv)))
    print(json.dumps({"status": result["status"], "created_at": result["created_at"], **result["counts"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
