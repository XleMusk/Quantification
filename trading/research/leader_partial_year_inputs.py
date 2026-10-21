"""Saved native partial-year source -> unchanged fixed eight-type research.

The source begins in 2023. Exactly 61 observed sessions are reserved for the
original features, so this is an April--December study, never a full-year or
252-session momentum claim. Registration precedes source derivation and PnL.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..rules import stock_board
from . import leader_eight_type_historical_native as native
from .baostock_daily_slice import digest
from .leader_eight_type_panel_replay import PROTOCOL, TYPE_IDS, GUARDS, CAPS
from .leader_historical_turnover_inputs import UNIT_EVIDENCE
from .native_history_source import NativeHistorySource
from .shortline_panel_replay import _config


METHOD_ID = "saved_native_first_61_warmup_partial_2023_fixed_eight_type_inputs_v1"
START, END, THROUGH = "2023-04-06", "2023-12-21", "2023-12-29"
CASE_IDS = tuple(f"{kind}__{guard}__{cap}" for kind in TYPE_IDS for guard in GUARDS for cap, _ in CAPS)
SPEC = {"id": METHOD_ID, "start": START, "end": END, "through": THROUGH,
    "source_sessions": 181, "decision_sessions": 175, "tail_sessions": 6, "warmup_sessions": 61,
    "cohort": "same_annual_first_D_supported_native_active_nonST_61_valid_observations_including_D_and_listing_supported",
    "cohort_count": "past_native_active_nonST_valid_count_saturated_at61_not_future_membership",
    "environment_denominator": "all_first_D_supported_native_identities_not_only_eligible_candidate_cohort",
    "unknown_types": "original_crossing_and_catchup_unknown_without_historical_identity_industry_no_backfill",
    "features": "unchanged_native_derive_panels_original_features_and_fixed_calendar",
    "cohort_replacement": False, "thresholds_changed": False, "original_32_protocol_changed": False,
    "calendar_or_prices_before_2023_fabricated": False, "full_2023_or_252_momentum_claimed": False,
    "new_action_collection_or_notice_review": False, "source_failure_or_null_removed": False,
    "independent_holdout_claimed": False, "parameters_selected_by_return": False}


def _pin(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": digest(path)}


def _check(pin):
    if _pin(pin["path"]) != pin:
        raise ValueError("Registered partial-year source metadata changed")
    return native._read(pin["path"]) if Path(pin["path"]).suffix == ".json" else None


def _save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(native._clean(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _schedule(payload, source_days):
    sessions, _, status = native._calendar_view(payload, "2023-01-01", "2023-12-31", [])
    days = [day for day in sessions if START <= day <= THROUGH]
    warmup = [day for day in sessions if day < START]
    if (payload.get("year") != 2023 or status != "pinned_static_exchange_calendar"
            or len(warmup) != 61 or len(days) != 181 or len([d for d in days if d <= END]) != 175
            or len([d for d in days if d > END]) != 6 or sessions[-1] != THROUGH
            or [day for day in source_days if day.startswith("2023-")] != sessions):
        raise ValueError("Exact 2023 source/calendar, 61 warmup and 175+6 study sessions required")
    return sessions, warmup, days


def register(native_view, calendar, master_run, action_event_view, action_reference_view, out):
    folder, source = Path(out).resolve(), Path(native_view).resolve()
    if folder.exists():
        raise FileExistsError("Partial-year registration requires a new output")
    payload, plan = native._read(calendar), native._read(source / "plan.json")
    result = native._read(source / "result.json")
    sessions, warmup, days = _schedule(payload, plan["days"])
    event_root, reference_root = Path(action_event_view).resolve(), Path(action_reference_view).resolve()
    event, reference = native._read(event_root / "result.json"), native._read(reference_root / "result.json")
    with (event_root / "source_input_calendar.csv").open(encoding="utf-8-sig", newline="") as handle:
        action_days = [row["date"] for row in csv.DictReader(handle)]
    if (result.get("status") != "source_cache_complete" or result.get("panel_file") != "raw_daily.parquet"
            or event.get("status") != "research_action_events_projected"
            or reference.get("status") != "research_reference_presence_diagnostic"
            or Path(reference["event_view"]).resolve() != event_root
            or not set([warmup[-1], *days]) <= set(action_days)
            or event.get("start", "9999") > warmup[-1] or event.get("end", "") < THROUGH
            or event.get("execution_admitted") is not False or reference.get("execution_admitted") is not False):
        raise ValueError("Existing native and action/reference pair must cover exact 2023 context and study dates")
    pins = {"source_result": _pin(source / "result.json"), "source_plan": _pin(source / "plan.json"),
        "source_receipt": _pin(source / "receipts.json"), "calendar": _pin(calendar),
        "master_run": _pin(master_run), "action_event": _pin(event_root / "result.json"),
        "action_reference": _pin(reference_root / "result.json"),
        "action_calendar": _pin(event_root / "source_input_calendar.csv"),
        "turnover_unit_evidence": _pin(UNIT_EVIDENCE),
        "source_adapter_implementation": _pin(__file__),
        "feature_implementation": _pin(Path(native.__file__)),
        "annual_cohort_semantics_implementation": _pin(Path(__file__).with_name("leader_momentum_year.py"))}
    record = {"schema": METHOD_ID + "_registration", "method_id": METHOD_ID,
        "registered_at": native._now(), "spec": deepcopy(SPEC), "protocol": deepcopy(PROTOCOL),
        "config": _config(100000, 5).to_dict(), "case_ids": list(CASE_IDS), "source_pins": pins,
        "native_view": str(source), "source_raw_sha256_from_saved_receipt": result["panel_sha256"],
        "action_event_view": str(event_root), "action_reference_view": str(reference_root),
        "source_days": days, "warmup_source_days": warmup, "all_official_year_sessions": sessions,
        "action_pair_coverage": {"source_start": event["start"], "source_end": event["end"],
            "calendar_sessions": len(action_days), "exact_context_plus_181_days_present": True,
            "reference_calendar_already_compiled": True, "reference_prices_rescanned": False,
            "unknown_terms_stop_only_affected_case": True, "new_source_fallback": False},
        "cohort_generated": False, "panels_generated": False, "new_PnL_computed": False,
        "limits": ["2023-04-06..12-21 plus six execution-tail sessions, not a full calendar year.",
            "Uses original first-day annual cohort semantics with only observed 2023 history; no 2022 backfill.",
            "Listing metadata are posthoc; missing historical industry/identity remains unknown.",
            "Source/cache completion does not certify exchange dated-roster/PIT or economic action completeness.",
            "No threshold, capacity or case selection by returns; all 32 outcomes and failures remain."],
        **native.FLAGS}
    _save_new(folder / "registration.json", record)
    return record


def _anchor_cohort(first_raw, counts, qualification):
    """The unchanged annual first-D filter, with its full excluded inventory."""
    physical = native._physical_native(first_raw)
    active = set(physical.loc[physical.active_non_st, "symbol"])
    members, excluded = [], []
    for symbol in sorted(first_raw.symbol):
        reasons = []
        if symbol not in active:
            reasons.append("first_D_not_valid_active_non_ST")
        if counts[symbol] < 61:
            reasons.append("past_valid_observation_count_below61")
        proof = qualification.assess(symbol, START, decision_as_of=START + "T15:05:00+08:00")
        if proof["engine_block_reason"] is not None:
            reasons.append("first_D_listing_qualification_unsupported")
        if reasons:
            excluded.append({"symbol": symbol, "reasons": reasons, "known_count_lower_bound": counts[symbol]})
        else:
            members.append(symbol)
    return members, excluded


def build_inputs(registration, out):
    record = native._read(registration)
    if (record.get("schema") != METHOD_ID + "_registration" or record.get("spec") != SPEC
            or record.get("protocol") != PROTOCOL or record.get("case_ids") != list(CASE_IDS)
            or record.get("config") != _config(100000, 5).to_dict()
            or any(record.get(key) is not value for key, value in native.FLAGS.items())):
        raise ValueError("Original explicit partial-year registration required")
    pins = record["source_pins"]
    for key, reference in pins.items():
        if key == "action_calendar":
            if _pin(reference["path"]) != reference:
                raise ValueError("Registered action calendar changed")
        else:
            _check(reference)
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Partial-year inputs require a new directory; no automatic retry")
    roots = [Path(record["native_view"]), Path(registration).resolve().parent,
        Path(record["action_event_view"]), Path(record["action_reference_view"])]
    if any(folder == root or root in folder.parents or folder in root.parents for root in roots):
        raise ValueError("Partial-year inputs must stay separate from registered sources")
    observed = native._now()
    qualification = native.load_posthoc_listing(pins["master_run"]["path"], pins["calendar"]["path"], observed_at=observed)
    folder.mkdir(parents=True, exist_ok=False)
    _save_new(folder / "attempt.json", {"status": "building", "started_at": observed,
        "registration": _pin(registration), **native.FLAGS})
    try:
        with NativeHistorySource(record["native_view"]) as source:
            payload = native._read(pins["calendar"]["path"])
            sessions, warmup, days = _schedule(payload, source.calendar)
            if source.provenance["raw_sha256"] != record["source_raw_sha256_from_saved_receipt"]:
                raise ValueError("Registered original native raw source changed")
            first = source.frame(START)
            supported, outside = [], []
            for symbol in first.symbol:
                try:
                    stock_board(symbol)
                    supported.append(symbol)
                except ValueError:
                    outside.append(symbol)
            first = first.loc[first.symbol.isin(supported)].copy()
            first.attrs.clear()
            counts, witness = dict.fromkeys(supported, 0), {symbol: [] for symbol in supported}
            frames = []
            for day in sessions:
                frame = first if day == START else source.frame(day, supported)
                frame = frame.copy()
                frame.attrs.clear()
                if day <= START:
                    unresolved = [symbol for symbol, count in counts.items() if count < 61]
                    valid = native._physical_native(frame.loc[frame.symbol.isin(unresolved)]) if unresolved else None
                    if valid is not None:
                        for symbol in valid.loc[valid.active_non_st, "symbol"]:
                            counts[symbol] += 1
                    if day < START:
                        close = pd.to_numeric(frame.close, errors="coerce")
                        for symbol in frame.loc[frame.tradestatus.eq("1") & np.isfinite(close) & close.gt(0), "symbol"]:
                            known = qualification.records.get(symbol)
                            if known and day >= known["ipo_date"] and len(witness[symbol]) < 5:
                                witness[symbol].append(day)
                frames.append(frame)
            witness = {symbol: values for symbol, values in witness.items() if len(values) == 5}
            qualification = native.with_prior_listing_witness(qualification, witness, START)
            members, excluded = _anchor_cohort(first, counts, qualification)
            if not members:
                raise ValueError("No first-day qualified61-observation cohort; not an all-cash result")
            history = pd.concat(frames, ignore_index=True)
            raw = history.loc[history.date.isin(days)].copy()
            context = history.loc[history.date.eq(warmup[-1])].copy()
            panels = native.derive_panels(history, days, members, payload, qualification,
                observed_at=observed, rolling_observation_cache=True)
            source._unchanged()
            proof = source.provenance
        raw_path, context_path, panels_path = folder / "raw_daily.parquet", folder / "context.parquet", folder / "panels.json"
        raw.to_parquet(raw_path, index=False)
        context.to_parquet(context_path, index=False)
        _save_new(panels_path, panels)
        _save_new(folder / "cohort.json", {"first_D": START, "scope_members": members,
            "observed_supported_count": len(supported), "excluded": excluded, "outside_supported_codes": outside,
            "valid_count_saturated_at61": counts, "future_membership_used": False,
            "raw_identity_aliases_merged": False})
        _save_new(folder / "listing_witness.json", witness)
        positive = pd.to_numeric(context.close, errors="coerce")
        manifest = {"schema": native.INPUT_SCHEMA, "method_id": native.METHOD_ID, "created_at": native._now(),
            "start": START, "end": END, "through": THROUGH, "source_days": days,
            "calendar": pins["calendar"]["path"], "calendar_sha256": pins["calendar"]["sha256"],
            "master_run": pins["master_run"]["path"], "IPO_qualification": qualification.verification,
            "native_prior_listing_witness": witness, "raw_file": str(raw_path), "raw_sha256": digest(raw_path),
            "raw_rows": len(raw), "context_raw_file": str(context_path), "context_raw_sha256": digest(context_path),
            "panels_file": str(panels_path), "panels_sha256": digest(panels_path), "scope_members": members,
            "scope_basis": "saved_annual_first_D_known_61_valid_observation_cohort_not_future_survivors",
            "scope_frozen_at": observed, "source_proofs": [proof], "warmup_source_days": warmup,
            "context_anchor": {"day": warmup[-1], "closes": dict(zip(
                context.loc[positive.gt(0), "symbol"], positive.loc[positive.gt(0)]))},
            "fixed_protocol": deepcopy(PROTOCOL), "prior_year_calendar_independently_verified": False,
            "turnover_unit_evidence": pins["turnover_unit_evidence"],
            "partial_year_source_registration": _pin(registration), "partial_year_source_method": METHOD_ID,
            "partial_year_semantics": deepcopy(SPEC), "native_source_groups_read": len(sessions),
            "environment_denominator": "all_first_D_supported_native_identities_not_only_eligible_candidate_cohort",
            "excluded_reason_counts": dict(Counter(reason for row in excluded for reason in row["reasons"])),
            "source_contract": "observed_native_rows_without_historical_complete_dated_roster",
            "feature_formula_source": "unchanged_leader_daily_signals._features_exact_native_preclose",
            "old_strict_input_schema_used": False, "future_path_survivors_used_for_scope": False,
            "identity_or_historical_sector_membership_backfilled": False,
            "initial_previous_year_positions_consumed": False, **native.FLAGS}
        _save_new(folder / "manifest.json", manifest)
        (folder / "attempt.json").write_text(json.dumps({"status": "completed", "completed_at": native._now(),
            "source_sessions": len(days), "cohort_count": len(members), "raw_rows": len(raw),
            "manifest": str(folder / "manifest.json"), "strategy_run": False, **native.FLAGS}, indent=2), encoding="utf-8")
        return manifest
    except BaseException as error:
        (folder / "attempt.json").write_text(json.dumps({"status": "failed", "failed_at": native._now(),
            "error": f"{type(error).__name__}: {error}", "automatic_retry": False, **native.FLAGS}, indent=2), encoding="utf-8")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reg = commands.add_parser("register")
    for name in ("native-view", "calendar", "master-run", "action-event-view", "action-reference-view", "out"):
        reg.add_argument("--" + name, type=Path, required=True)
    build = commands.add_parser("build")
    build.add_argument("--registration", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    value = (register if command == "register" else build_inputs)(**args)
    print(json.dumps({key: value.get(key) for key in ("method_id", "registered_at", "start", "end", "through", "raw_rows")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
