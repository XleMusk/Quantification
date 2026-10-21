"""Explicit posthoc missing-row classification; no price or feature rebuilding.

The frozen universe remains intact.  Only an absent own-day row at/after its
reported out date becomes a known research exclusion.  Existing holding and
order execution checks are not changed by this saved candidate-input overlay.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from .baostock_daily_slice import digest
from .ipo_qualification import _time


METHOD_ID = "posthoc_reported_out_date_missing_native_row_classification_v1"
SCHEMA = "leader_historical_listing_classification_v1"
DESCRIPTOR = "historical_listing_classification"
LISTING_METHOD = "posthoc_listing_metadata_preserved_actual_clock_v1"
EXCLUSION = "posthoc_reported_outside_listing_period_and_exact_native_row_missing"
CONFLICT = "active_native_row_conflicts_with_posthoc_reported_out_date"
FLAGS = {"candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "pit_verified": False,
    "returns_reportable": False, "historical_publication_backdated": False,
    "raw_rows_fabricated": False, "features_recomputed": False,
    "frozen_scope_members_removed": False, "engine_execution_checks_relaxed": False}


def _canonical_day(day):
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("One canonical exact classification day is required")
    return day


def _raw_records(day, raw):
    if not isinstance(raw, pd.DataFrame) or "date" not in raw or not raw.date.eq(day).all():
        raise ValueError("Classification requires exact own-day native rows, including an explicit empty group")
    frame = raw.copy()
    if "symbol" not in frame:
        if "code" not in frame or not frame.code.str.match(r"^(sh|sz)\.\d{6}$").all():
            raise ValueError("Original canonical native symbol/code is required")
        frame["symbol"] = frame.code.str[3:] + frame.code.str[:2].map({"sh": ".SSE", "sz": ".SZSE"})
    if frame.symbol.isna().any() or frame.symbol.duplicated().any():
        raise ValueError("Exact native classification rows must have unique symbols")
    return {row["symbol"]: row for row in frame.to_dict("records")}


def classify_day(day, raw, scope_members, qualification):
    """Use only original listing records and D rows; never remove future exits."""
    _canonical_day(day)
    scope = list(scope_members)
    if not scope or len(scope) != len(set(scope)):
        raise ValueError("The unchanged complete fixed scope is required")
    proof = qualification.verification
    if (proof.get("method_id") != LISTING_METHOD
            or proof.get("original_availability_clocks_backdated") is not False):
        raise ValueError("Only explicit posthoc listing metadata with its original clocks is supported")
    _time(proof["posthoc_observed_at"])
    actual = _raw_records(day, raw)
    known, conflicts = {}, {}
    for symbol in scope:
        record = qualification.records.get(symbol, {})
        out = record.get("reported_out_date")
        if not out or day < _canonical_day(out):
            continue
        evidence = {"symbol": symbol, "day": day, "reported_out_date": out,
            "original_record_available_at": record.get("available_at"),
            "posthoc_observed_at": proof["posthoc_observed_at"],
            "method_id": METHOD_ID, "historical_PIT_knowledge_claimed": False}
        row = actual.get(symbol)
        if row is None:
            known[symbol] = {**evidence, "reason": EXCLUSION, "raw_row_present": False}
        elif str(row.get("tradestatus")) == "1":
            conflicts[symbol] = {**evidence, "reason": CONFLICT,
                "raw_row_present": True, "native_tradestatus": row["tradestatus"]}
        # An actual halted/unknown-status row is not this missing-row method.
    return {"day": day, "known_exclusions": known, "conflicts": conflicts}


def apply_day(panel, classification, *, available_at, scope_members):
    """Copy saved rules and candidates; change only the proved membership issue."""
    day = classification["day"]
    if panel.get("day", day) != day or _time(available_at) < _time(panel["available_at"]):
        raise ValueError("New classification cannot change the day or backdate saved observations")
    result = deepcopy(panel)
    known, conflicts = classification["known_exclusions"], classification["conflicts"]
    if set(known) & set(conflicts) or not (set(known) | set(conflicts)) <= set(scope_members):
        raise ValueError("Classification must belong to the unchanged scope without contradictory statuses")
    for item in result["types"]:
        if any(row["symbol"] in known for row in item.get("candidates", [])):
            raise ValueError("A saved candidate without its own raw row cannot be silently removed")
        original_unknown = set(item.get("unresolved_symbols", []))
        removed = original_unknown & set(known)
        unknown = original_unknown - set(known)
        unknown.update(conflicts)
        exclusions = dict(item.get("known_exclusions", {}))
        exclusions.update({symbol: EXCLUSION for symbol in known})
        for symbol in conflicts:
            exclusions.pop(symbol, None)
        item.update(unresolved_symbols=sorted(unknown), known_exclusions=exclusions,
            scope_complete=not bool(set(scope_members) & unknown))
        if unknown != original_unknown:
            item["status"] = "matched" if item.get("candidates") else "unknown" if unknown else "not_matched"
            item["historical_listing_membership_changed"] = True
        missing = item.get("missing_evidence")
        reason = "fixed_member_missing_from_current_own_roster"
        if removed and isinstance(missing, dict) and reason in missing:
            remaining = missing[reason] - len(removed)
            if remaining < 0:
                raise ValueError("Original missing-row reason count cannot cover the classified exact missing rows")
            if remaining:
                missing[reason] = remaining
            else:
                missing.pop(reason)
        if conflicts:
            item["historical_listing_active_raw_conflicts"] = sorted(conflicts)
    result["available_at"] = available_at
    result["historical_listing_classification_method"] = METHOD_ID
    return result


@dataclass(frozen=True)
class VerifiedHistoricalListingDay:
    day: str
    scope_members: tuple
    classification: dict
    qualification_verification: dict


def verify_day(classification, day, raw, scope_members, qualification):
    """Typed hook is created only after recomputing the declared own-day facts."""
    expected = classify_day(day, raw, scope_members, qualification)
    if classification != expected:
        raise ValueError("Declared historical listing classification differs from exact raw/listing records")
    return VerifiedHistoricalListingDay(day, tuple(scope_members), deepcopy(expected),
        deepcopy(qualification.verification))


def eligibility(view, symbol, day):
    """Dedicated historical hook; arbitrary eligibility maps are never trusted."""
    from .leader_missing_type_candidates import _eligibility
    hook = view.get(DESCRIPTOR)
    if hook is None:
        return _eligibility(view, symbol)
    if not isinstance(hook, VerifiedHistoricalListingDay) or hook.day != day:
        raise ValueError("Historical listing exclusions require a verified exact-day typed hook")
    if symbol in hook.classification["known_exclusions"]:
        return "not_matched", EXCLUSION, None
    if symbol in hook.classification["conflicts"]:
        return "unknown", CONFLICT, None
    return _eligibility(view, symbol)


def load_classification(manifest, qualification):
    """Read only the small pinned sidecar; do not reload parent raw or panels."""
    from .leader_eight_type_historical_native import _read
    descriptor = manifest[DESCRIPTOR]
    if (descriptor.get("method_id") != METHOD_ID
            or digest(descriptor["sidecar_file"]) != descriptor["sidecar_sha256"]
            or digest(descriptor["parent_manifest"]) != descriptor["parent_manifest_sha256"]):
        raise ValueError("Historical listing sidecar or original parent pin changed")
    sidecar = _read(descriptor["sidecar_file"])
    parent = _read(descriptor["parent_manifest"])
    if (parent.get("identity_research") is not None or parent.get(DESCRIPTOR) is not None
            or sidecar.get("schema") != SCHEMA or sidecar.get("method_id") != METHOD_ID
            or sidecar.get("created_at") != descriptor["classified_at"]
            or sidecar.get("parent_manifest") != descriptor["parent_manifest"]
            or sidecar.get("parent_manifest_sha256") != descriptor["parent_manifest_sha256"]
            or sidecar.get("scope_members") != parent["scope_members"]
            or sidecar.get("source_days") != parent["source_days"]
            or sidecar.get("qualification_verification") != qualification.verification
            or _time(sidecar["created_at"]) < _time(parent["created_at"])
            or _time(sidecar["created_at"]) > _time(manifest["created_at"])
            or any(sidecar.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Historical listing method, scope, source dates or original clocks changed")
    for key, value in parent.items():
        if key not in {"created_at", "panels_file", "panels_sha256"} and manifest.get(key) != value:
            raise ValueError("Listing classification cannot replace source/qualification/protocol fields")
    if sorted(sidecar["days"]) != parent["source_days"]:
        raise ValueError("Every original source day must retain an explicit classification")
    return sidecar


def validate(manifest, panels, raw, qualification):
    """Check the overlay, including a later explicitly registered identity layer."""
    sidecar = load_classification(manifest, qualification)
    by_day = {day: group for day, group in raw.groupby("date", sort=False)}
    derived = manifest.get("identity_research") is not None
    identity_types = {"cross_regime_candidate", "sector_catchup_leader"}
    for day in manifest["source_days"]:
        classification = sidecar["days"][day]
        verify_day(classification, day, by_day[day], manifest["scope_members"], qualification)
        panel = panels[day]
        if (panel.get("historical_listing_classification_method") != METHOD_ID
                or _time(panel["available_at"]) < _time(sidecar["created_at"])):
            raise ValueError("Classified observations lost their actual new method/clock")
        for item in panel["types"]:
            unknown = set(item.get("unresolved_symbols", []))
            exclusions = item.get("known_exclusions", {})
            if (set(classification["known_exclusions"]) & unknown
                    or not set(classification["conflicts"]) <= unknown
                    or any(exclusions.get(symbol) != EXCLUSION for symbol in classification["known_exclusions"])
                    or set(classification["conflicts"]) & set(exclusions)):
                raise ValueError("Saved classification was dropped or a raw/listing conflict hidden")
            if item.get("scope_complete") is not (not bool(set(manifest["scope_members"]) & unknown)):
                raise ValueError("Classified scope completeness must use the original fixed-scope intersection")
            # Identity derivation has its own exact-parent validator and changes
            # two type labels; it must still retain the same exclusions above.
            if derived and item["id"] in identity_types:
                continue
            expected_status = "matched" if item.get("candidates") else "unknown" if unknown else "not_matched"
            if item.get("historical_listing_membership_changed") is True and item.get("status") != expected_status:
                raise ValueError("Classified status cannot erase remaining genuine unknowns")
    return sidecar


def run(inputs, out):
    from .leader_eight_type_historical_native import load_inputs, _now, _save
    path, folder = Path(inputs).resolve(), Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Listing classification requires a new separate output directory")
    # Reject derived inputs before reading their large saved panels.
    from .leader_eight_type_historical_native import _read
    header = _read(path)
    if header.get("identity_research") is not None or header.get(DESCRIPTOR) is not None:
        raise ValueError("Classify one original native input before registering a new identity overlay")
    loaded, parent = load_inputs(path)
    actual = _now()
    by_day = {day: group for day, group in loaded["raw"].groupby("date", sort=False)}
    classifications, counts = {}, Counter()
    panels = loaded["panel_by_day"]
    for day in parent["source_days"]:
        item = classify_day(day, by_day[day], parent["scope_members"], loaded["qualification"])
        panels[day] = apply_day(panels[day], item, available_at=actual, scope_members=parent["scope_members"])
        classifications[day] = item
        counts["known_exclusion_symbol_days"] += len(item["known_exclusions"])
        counts["active_raw_conflict_symbol_days"] += len(item["conflicts"])
    folder.mkdir(parents=True, exist_ok=False)
    sidecar_path = folder / "listing_classification.json"
    _save(sidecar_path, {"schema": SCHEMA, "method_id": METHOD_ID, "created_at": actual,
        "parent_manifest": str(path), "parent_manifest_sha256": digest(path),
        "scope_members": parent["scope_members"], "source_days": parent["source_days"],
        "qualification_verification": loaded["qualification"].verification,
        "days": classifications, "counts": dict(counts), **FLAGS})
    child = deepcopy(parent)
    child.update(created_at=_now(), panels_file=str(folder / "panels.json"))
    _save(child["panels_file"], panels)
    child["panels_sha256"] = digest(child["panels_file"])
    child[DESCRIPTOR] = {"method_id": METHOD_ID, "classified_at": actual,
        "parent_manifest": str(path), "parent_manifest_sha256": digest(path),
        "sidecar_file": str(sidecar_path), "sidecar_sha256": digest(sidecar_path)}
    _save(folder / "manifest.json", child)
    summary = {"schema": SCHEMA + "_summary", "method_id": METHOD_ID, "created_at": _now(),
        "manifest": str(folder / "manifest.json"), "manifest_sha256": digest(folder / "manifest.json"),
        "source_days": len(parent["source_days"]), "scope_count": len(parent["scope_members"]),
        **dict(counts), **FLAGS}
    _save(folder / "summary.json", summary)
    return {"manifest": child, "summary": summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = run(args.inputs, args.out)
    from .leader_eight_type_historical_native import _clean
    import json
    print(json.dumps(_clean(result["summary"]), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
