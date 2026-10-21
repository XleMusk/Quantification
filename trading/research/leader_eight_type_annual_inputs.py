"""Reuse saved annual raw/cohort inputs for the unchanged historical eight types.

This is an input adapter, not a replay runner. Only the missing pre-inception
warmup is read from the original native view; annual prices stay in their saved
file. Identity overlays and all32 replay use the existing explicit entrypoints.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd

from . import leader_eight_type_historical_native as native
from .baostock_daily_slice import digest
from .leader_daily_signals import _calendar_view
from .leader_eight_type_panel_replay import PROTOCOL
from .leader_historical_turnover_inputs import UNIT_EVIDENCE
from .leader_momentum_year import METHOD as ANNUAL_SCHEMA
from .leader_valid_warmup import METHOD_ID as RESTORE_METHOD, restore_valid_warmup as restore_prefix
from .native_history_source import NativeHistorySource
from .shortline_panel_replay import _config


METHOD = "saved_annual_native_raw_first_day_cohort_to_original_eight_types_v1"
CACHE_FILES = ("raw_daily.parquet", "context.parquet", "cohort.json", "listing_witness.json")


def _checked_pin(reference):
    path = Path(reference["path"]).resolve()
    if digest(path) != reference["sha256"]:
        raise ValueError("Saved annual source changed: " + str(path))
    return path


def _annual_cache(folder):
    folder = Path(folder).resolve()
    manifest = native._read(folder / "manifest.json")
    plan_path = _checked_pin(manifest["study_plan"])
    plan = native._read(plan_path)
    if (manifest.get("schema") != ANNUAL_SCHEMA or plan.get("schema") != ANNUAL_SCHEMA
            or manifest.get("status") != "completed_annual_inputs"
            or plan.get("config") != _config(100000, 5).to_dict()
            or any(manifest.get(key) is not False for key in
                   ("execution_admitted", "selection_enabled", "strategy_validated", "pit_verified"))
            or any(manifest.get(key) != plan.get(key) for key in ("start", "end", "through"))):
        raise ValueError("An unchanged completed annual native research cache is required")
    files = {}
    for name in CACHE_FILES:
        files[name] = _checked_pin(manifest["files"][name])
        if files[name] != folder / name:
            raise ValueError("Annual cache files must retain their original local identities")
    calendar = _checked_pin(plan["calendars"][0])
    _checked_pin(plan["master"])
    payload = native._read(calendar)
    sessions, _, status = _calendar_view(payload, f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])
    days = manifest["source_days"]
    if (status != "pinned_static_exchange_calendar" or not days or days != sorted(set(days))
            or days != [day for day in sessions if manifest["start"] <= day <= manifest["through"]]
            or len([day for day in days if day > manifest["end"]]) != 6
            or not manifest["start"] <= manifest["end"] < manifest["through"]
            or manifest["context_day"] >= manifest["start"]):
        raise ValueError("Annual cache needs its exact calendar prefix and six original tail sessions")
    cohort = native._read(files["cohort.json"])
    scope = cohort["scope_members"]
    if (not scope or len(scope) != len(set(scope)) or len(scope) != manifest["cohort_count"]
            or cohort.get("first_D") != manifest["start"]
            or cohort.get("future_membership_used") is not False
            or any(cohort["valid_count_saturated_at61"].get(symbol, 0) < 61 for symbol in scope)):
        raise ValueError("Original first-D past-only 61-observation cohort required")
    # The shared warmup helper and saved-payload loader use canonical order.
    # The membership is identical to the original annual cohort; rankings are
    # still produced by the original score/tie rules, not this list order.
    return folder, manifest, plan, files, calendar, payload, sorted(scope)


def repair_scope_order(inputs, out):
    """Publish one metadata-only child of an already built unsorted cohort.

    No annual prices, panels or native source are read. The original producer
    bytes must have been archived before fixing this adapter; the child retains
    every original payload pointer/hash and every panel's actual clock.
    """
    path, out = Path(inputs).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Scope-order repair requires a new input directory")
    value = native._read(path)
    reuse, restore = value.get("annual_cache_reuse", {}), value.get("warmup_restoration", {})
    if (value.get("schema") != native.INPUT_SCHEMA or value.get("method_id") != native.METHOD_ID
            or reuse.get("method_id") != METHOD or value.get("fixed_protocol") != PROTOCOL
            or any(value.get(key) is not flag for key, flag in native.FLAGS.items())
            or value.get("identity_research") is not None
            or value.get("historical_listing_classification") is not None
            or restore.get("enabled") is not True or restore.get("method_id") != RESTORE_METHOD
            or restore.get("required_valid_observations") != 61
            or restore.get("first_decision_day") != value.get("start")
            or restore.get("reuse_from") is not None):
        raise ValueError("Only the original annual input's scope-order mismatch can be repaired")
    archive = path.parent / "source_implementation_before_order_fix.py"
    old_implementation = reuse["implementation"]
    if digest(archive) != old_implementation["sha256"]:
        raise ValueError("Exact original adapter bytes must be archived before scope-order repair")
    diagnostic_path = _checked_pin({"path": restore["diagnostic_file"], "sha256": restore["diagnostic_sha256"]})
    restored_path = _checked_pin({"path": restore["raw_file"], "sha256": restore["raw_sha256"]})
    diagnostic = native._read(diagnostic_path)
    projected = pd.read_parquet(restored_path, columns=["date", "code"])
    before, canonical = value["scope_members"], diagnostic.get("first_day_scope_members")
    if (not before or len(before) != len(set(before)) or canonical != sorted(before) or before == canonical
            or diagnostic.get("method_id") != RESTORE_METHOD
            or diagnostic.get("first_decision_day") != value["start"]
            or diagnostic.get("original_full_market_warmup_days") != value["warmup_source_days"]
            or diagnostic.get("future_rows_used_for_selection_or_counting") is not False
            or diagnostic.get("projected_prefix_rows") != len(projected)
            or projected.duplicated(["date", "code"]).any()
            or (not projected.empty and projected.date.max() >= value["warmup_source_days"][0])):
        raise ValueError("Repair requires exactly the same unique scope set and otherwise valid saved warmup")
    actual = native._now()
    child = deepcopy(value)
    child.update(created_at=actual, scope_members=canonical)
    child["annual_scope_order_repair"] = {
        "method_id": METHOD + "_canonical_scope_order_repair_v1", "actual_repaired_at": actual,
        "parent_manifest": {"path": str(path), "sha256": digest(path)},
        "original_manifest_created_at": value["created_at"],
        "original_adapter_archive": {"path": str(archive), "sha256": digest(archive)},
        "repair_implementation": {"path": str(Path(__file__).resolve()), "sha256": digest(__file__)},
        "changed_fields": ["created_at", "scope_members", "annual_scope_order_repair"],
        "scope_count": len(before), "scope_membership_unchanged": True,
        "panel_actual_clocks_unchanged": True, "panels_recomputed": False,
        "warmup_recomputed": False, "annual_raw_read": False, "native_source_opened": False,
        "large_payload_hashes_recomputed": False}
    out.mkdir(parents=True, exist_ok=False)
    native._save(out / "manifest.json", child)
    return child


def build_inputs(annual_inputs, native_view, out, *, prior_calendars=(), restore_valid_warmup=False):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Annual eight-type inputs require a new output")
    folder, annual, plan, files, calendar, payload, scope = _annual_cache(annual_inputs)
    view = Path(native_view).resolve()
    if view != Path(plan["native_source"]["source_view"]).resolve():
        raise ValueError("Warmup must use the annual cache's exact original native view")
    if any(out == source or source in out.parents or out in source.parents for source in (folder, view)):
        raise ValueError("Annual eight-type output must be outside its original source trees")
    observed = native._now()
    qualification = native.load_posthoc_listing(plan["master"]["path"], calendar,
        observed_at=observed, prior_calendars=prior_calendars)
    witness = native._read(files["listing_witness.json"])
    qualification = native.with_prior_listing_witness(qualification, witness, annual["start"])
    # Reuse the reported listing facts and actual availability, never infer IPO
    # dates from bars or replace their original retrieval time with historical D.
    for key in ("master_run", "master_run_sha256", "master_file", "master_sha256",
                "original_available_at", "calendar", "calendar_sha256",
                "native_prior_listing_witness_sha256"):
        if qualification.verification.get(key) != annual["qualification"].get(key):
            raise ValueError("Annual listing evidence changed: " + key)
    if restore_valid_warmup and not qualification.assessment_calendar:
        raise ValueError("Valid-observation recovery requires the explicit prior official calendar")
    units = native._read(UNIT_EVIDENCE)
    if (units.get("status") != "official_provider_document_read" or units.get("turn_unit") != "percent"
            or native._time(units["retrieved_at"]) > native._time(observed)):
        raise ValueError("Original reported-turnover percentage evidence required")
    raw = pd.read_parquet(files["raw_daily.parquet"])
    context = pd.read_parquet(files["context.parquet"])
    if (len(raw) != annual["raw_rows"] or sorted(raw.date.unique()) != annual["source_days"]
            or sorted(context.date.unique()) != [annual["context_day"]]
            or raw.duplicated(["date", "code"]).any() or context.duplicated(["date", "code"]).any()
            or not set(scope) <= set(raw.loc[raw.date.eq(annual["start"]), "symbol"])):
        raise ValueError("Annual raw/context dates, row identities or first-D cohort changed")
    restored, diagnostic = None, None
    with NativeHistorySource(view) as source:
        identity = lambda proof: {key: value for key, value in proof.items() if key != "opened_at"}
        if identity(source.provenance) != identity(plan["native_source"]):
            raise ValueError("Warmup native source differs from the original annual source")
        calendar_days = source.calendar
        at = calendar_days.index(annual["start"])
        if (at < 61 or calendar_days[at - 1] != annual["context_day"]
                or [day for day in calendar_days if annual["start"] <= day <= annual["through"]]
                != annual["source_days"]):
            raise ValueError("Exact native predecessor and complete annual source coverage required")
        warmup = calendar_days[at - 61:at]
        if prior_calendars and [day for day in qualification.assessment_calendar
                                if warmup[0] <= day < annual["start"]] != warmup:
            raise ValueError("Official prior calendar and exact native warmup sessions disagree")
        # Dec31 is already the saved exact context; do not reread it or 243
        # annual groups from the ancestor. Other first-D-supported rows retain
        # their denominator role; candidate scope remains the saved cohort.
        supported = set(native._read(files["cohort.json"])["valid_count_saturated_at61"])
        prefix = []
        for day in warmup[:-1]:
            frame = source.frame(day, symbols=sorted(supported))
            frame.attrs.clear()
            prefix.append(frame)
        history = pd.concat(prefix + [context, raw], ignore_index=True)
        if restore_valid_warmup:
            restored, diagnostic = restore_prefix(
                history.loc[history.symbol.isin(scope)].copy(), first_day=annual["start"],
                warmup_days=warmup, scope_members=scope, qualification=qualification,
                fixed_sessions=qualification.assessment_calendar,
                source_by_day={day: source for day in calendar_days}, observed_at=observed,
                physical_native=native._physical_native)
            if not restored.empty:
                history = pd.concat([restored, history], ignore_index=True)
        feature_qualification = (native.PosthocListingQualification(qualification.records,
            qualification.assessment_calendar, qualification.verification, qualification.assessment_calendar)
            if prior_calendars else None)
        panels = native.derive_panels(history, annual["source_days"], scope, payload, qualification,
            observed_at=observed, feature_qualification=feature_qualification, rolling_observation_cache=True)
        source._unchanged()
        source_proof = source.provenance
    out.mkdir(parents=True, exist_ok=False)
    panels_path = out / "panels.json"
    native._save(panels_path, panels)
    positive = pd.to_numeric(context.close, errors="coerce")
    manifest = {"schema": native.INPUT_SCHEMA, "method_id": native.METHOD_ID, "created_at": native._now(),
        "start": annual["start"], "end": annual["end"], "through": annual["through"],
        "source_days": annual["source_days"], "calendar": str(calendar), "calendar_sha256": digest(calendar),
        "master_run": str(Path(plan["master"]["path"]).resolve()), "IPO_qualification": qualification.verification,
        "native_prior_listing_witness": witness, "raw_file": str(files["raw_daily.parquet"]),
        "raw_sha256": annual["files"]["raw_daily.parquet"]["sha256"], "raw_rows": len(raw),
        "context_raw_file": str(files["context.parquet"]),
        "context_raw_sha256": annual["files"]["context.parquet"]["sha256"],
        "panels_file": str(panels_path), "panels_sha256": digest(panels_path), "scope_members": scope,
        "scope_basis": "saved_annual_first_D_known_61_valid_observation_cohort_not_future_survivors",
        "scope_frozen_at": annual["created_at"], "source_proofs": [source_proof], "warmup_source_days": warmup,
        "context_anchor": {"day": annual["context_day"], "closes": dict(zip(
            context.loc[positive.gt(0), "symbol"], positive.loc[positive.gt(0)]))},
        "fixed_protocol": deepcopy(PROTOCOL), "prior_year_calendar_independently_verified": False,
        "turnover_unit_evidence": {"path": str(UNIT_EVIDENCE.resolve()), "sha256": digest(UNIT_EVIDENCE)},
        "annual_cache_reuse": {"method_id": METHOD, "manifest": str(folder / "manifest.json"),
            "implementation": {"path": str(Path(__file__).resolve()), "sha256": digest(__file__)},
            "manifest_sha256": digest(folder / "manifest.json"), "study_plan": annual["study_plan"],
            "files": {name: annual["files"][name] for name in CACHE_FILES},
            "annual_native_groups_reread": 0, "missing_full_warmup_groups_read": len(warmup) - 1,
            "environment_denominator": "all_saved_first_D_supported_native_rows_not_only_candidate_cohort",
            "monthly_momentum_batches_used_as_eight_type_features": False},
        "source_contract": "saved_observed_native_rows_without_historical_complete_dated_roster",
        "feature_formula_source": "unchanged_leader_daily_signals._features_exact_native_preclose",
        "old_strict_input_schema_used": False, "future_path_survivors_used_for_scope": False,
        "identity_or_historical_sector_membership_backfilled": False,
        "initial_previous_year_positions_consumed": False, **native.FLAGS}
    if prior_calendars:
        manifest.update(prior_calendars=qualification.verification["prior_calendars"],
            feature_calendar_scope="explicit_complete_prior_years_plus_current_year",
            feature_previous_day_from_fixed_calendar_prefix=True, prior_calendar_official_schedule_supplied=True)
    if diagnostic is not None:
        restored_path, diagnostic_path = out / "restore_history.parquet", out / "restore_diagnostic.json"
        restored.to_parquet(restored_path, index=False)
        native._save(diagnostic_path, diagnostic)
        manifest["warmup_restoration"] = {"enabled": True, "method_id": diagnostic["method_id"],
            "required_valid_observations": 61, "first_decision_day": annual["start"],
            "raw_file": str(restored_path), "raw_sha256": digest(restored_path),
            "diagnostic_file": str(diagnostic_path), "diagnostic_sha256": digest(diagnostic_path),
            "original_full_market_warmup_unchanged": True,
            "scope": "explicit_past_symbol_projections_not_full_market",
            "selection_uses_first_day_or_earlier_only": True,
            "rolling_observation_cache": "exact_current_and_two_previous_fixed_sessions"}
    native._save(out / "manifest.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--annual-inputs", type=Path)
    source.add_argument("--repair-scope-order-from", type=Path)
    parser.add_argument("--native-view", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--prior-calendar", type=Path, action="append", default=[])
    parser.add_argument("--restore-valid-warmup", action="store_true")
    args = parser.parse_args(argv)
    if args.repair_scope_order_from:
        if args.native_view or args.prior_calendar or args.restore_valid_warmup:
            parser.error("Scope-order repair cannot request native reads, calendars or warmup reconstruction")
        result = repair_scope_order(args.repair_scope_order_from, args.out)
    else:
        if args.native_view is None:
            parser.error("Annual input construction requires its explicit --native-view")
        result = build_inputs(args.annual_inputs, args.native_view, args.out,
            prior_calendars=args.prior_calendar, restore_valid_warmup=args.restore_valid_warmup)
    print(json.dumps({"manifest": str(args.out.resolve() / "manifest.json"),
        "source_days": len(result["source_days"]), "scope_count": len(result["scope_members"]),
        "annual_native_groups_reread": 0, **native.FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
