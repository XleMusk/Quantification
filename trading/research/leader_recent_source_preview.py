"""Derive compatible daily previews from consecutive complete recent sources.

Only this new mixed-source view prioritizes the supplied recent packages. The
original history, source packages, scores and previews are never changed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..market_dataset import write_frame, write_json
from .baostock_daily_slice import digest
from .baostock_full_close import load_complete
from .leader_close_preview import SCHEMA as PREVIEW_SCHEMA, canonical, market_tape
from .leader_daily_signals import FAMILIES, _calendar_view, build_leader_daily_batches, read_physical_snapshot
from .leader_history_repair_preview import BOUNDARY, _current_raw_identity, _source_gate
from .leader_recent_source_history import compose_recent_sources


SCHEMA = "leader_recent_source_feature_view_v1"
DEFAULT_PRODUCER_METHOD = "original_five_family_default_single_build_no_IPO_feature_opt_in"
QUALIFIED_PRODUCER_METHOD = "original_five_family_single_build_explicit_listing_feature_qualification_v1"


def _day_qualification(qualification, day):
    """Retain only this decision's evidence in its dated child preview."""
    value = deepcopy(qualification)
    value["decisions"] = {day: value["decisions"][day]}
    value["unknown_symbols_by_family_by_day"] = {
        family: {day: values[day]} for family, values in value["unknown_symbols_by_family_by_day"].items()}
    value["known_excluded_symbols_by_day"] = {day: value["known_excluded_symbols_by_day"][day]}
    return value


def derive_previews(packages, history_snapshot, calendar, out, *, ipo_qualification=None):
    """Build recent features once, with an optional explicit listing policy."""
    packages = [Path(path).resolve() for path in packages]
    out, history_snapshot, calendar = (Path(path).resolve() for path in (out, history_snapshot, calendar))
    if out.exists():
        raise FileExistsError("A recent-source view requires a new output directory")
    if len(packages) < 3 or len(packages) != len(set(packages)):
        raise ValueError("At least three distinct complete dated packages are required")
    gates = [gate for package in packages if (gate := _source_gate(package)) is not None]
    if gates:
        return {"schema": SCHEMA,
            "status": "blocked_failed_source" if any(gate["status"] == "blocked_failed_source" for gate in gates)
                      else "waiting_source_incomplete",
            "checked_at": datetime.now().astimezone().isoformat(), "sources": gates,
            "output": str(out), "output_directory_created": False, **BOUNDARY}
    # Each loader verifies its own full roster and exact raw bytes. Today's
    # survivors never define an earlier source's population.
    loaded = [load_complete(package) for package in packages]
    recorded_time = datetime.now().astimezone()
    composed = compose_recent_sources(loaded, history_snapshot, calendar, history_reader=read_physical_snapshot,
                                      observed_at=recorded_time.isoformat())
    loaded, days, sources = composed["loaded_sources"], composed["days"], composed["sources"]
    payload, cutoff, calendar_hash = composed["calendar_payload"], composed["base_history_end"], composed["calendar_sha256"]
    base, metadata, combined = composed["base_history"], composed["history_metadata"], composed["combined"]
    options = {"ipo_qualification": ipo_qualification} if ipo_qualification is not None else {}
    bundle = build_leader_daily_batches(combined, metadata, start=days[0], end=days[-1],
                                        calendar_payload=payload, retain_window_features=True, **options)
    qualification = bundle.get("feature_qualification")
    if (ipo_qualification is None) != (qualification is None):
        raise ValueError("Recent feature policy must match the explicit qualification request")
    recorded = datetime.now().astimezone().isoformat()
    identity = {"derived_at": recorded, "sources": sources, "base_end": cutoff}
    if qualification is not None:
        identity["feature_method_id"] = qualification["method_id"]
        identity["feature_verification"] = qualification["verification"]
    view_id = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    common = {"schema": SCHEMA, "shared_view_id": view_id, "derived_at": recorded,
        "derivation": "new_view_recent_complete_sources_after_explicit_original_history_cutoff",
        "view_root": str(out), "root_lineage_file": str(out / "recent_source_lineage.json"),
        "original_history_snapshot": str(history_snapshot),
        "original_history_manifest_sha256": digest(history_snapshot / "manifest.json"),
        "base_history_requested_end": cutoff, "base_history_max_observed_day": str(base.date.max()),
        "base_history_min_observed_day": str(base.date.min()), "base_history_included_rows": len(base),
        "original_history_manifest_end": metadata.get("manifest_end"),
        "original_rows_after_cutoff_loaded_into_view": 0,
        "original_reader_rows_after_cutoff_ignored": composed["original_reader_rows_after_cutoff_ignored"],
        "conflict_rule": "Recent source priority exists only in this new view; original rows after the cutoff are excluded, not overwritten",
        "view_sessions": days, "calendar": str(calendar), "calendar_sha256": calendar_hash,
        "producer_method": QUALIFIED_PRODUCER_METHOD if qualification is not None else DEFAULT_PRODUCER_METHOD,
        "producer_pass_count": 1, "history_inventory_complete": False, "continuous_history_claimed": False,
        "remaining_symbol_gaps_retained": True, "source_verified": False, "pit_verified": False,
        "company_actions_verified": False, "provider_contacted": False,
        "actual_signal_publication_verified": False, "historical_publication_verified": False,
        "stockdb_modified": False, "native_features_modified": False, "prior_scores_modified": False,
        "prior_previews_modified": False, "future_bars_used": False, **BOUNDARY}
    root_lineage = {**common, "recent_sources": sources,
        "raw_rows_by_recent_session": {day: len(raw) for day, (raw, _) in zip(days, loaded)}}
    if qualification is not None:
        root_lineage["feature_qualification"] = qualification
    previews = []
    for day, (raw, source) in zip(days, loaded):
        features = bundle["window_features"].loc[lambda frame: frame.date.eq(day)].copy()
        candidates = bundle["candidate_table"].loc[lambda frame: frame.date.eq(day)].copy()
        _current_raw_identity(features, raw)
        tape = market_tape(raw)
        tape.update(limit_up_proxy_count=int(features.limit_up.sum()),
                    limit_up_stage_unknown_count=int((features.limit_up & features.stage.isna()).sum()),
                    stages={str(stage): int(features.stage.eq(stage).sum()) for stage in range(1, 6)})
        if qualification is not None:
            # The environment's raw count must still include price-only IPO
            # hits, while ordinary qualified ladder counts use the new policy.
            tape.update(limit_up_proxy_count=int(features.unqualified_limit_up.sum()),
                limit_up_stage_unknown_count=int((features.unqualified_limit_up & features.stage.isna()).sum()),
                listing_qualified_limit_up_proxy_count=int(features.limit_up.sum()),
                listing_qualification_unknown_rows=int(features.listing_qualification_status.eq("unknown").sum()),
                feature_qualification_method_id=qualification["method_id"],
                limit_up_proxy_basis="unqualified_original_price_only_proxy_not_listing_qualified",
                stages_basis="explicit_listing_qualified_features_same_decision_cutoff")
        families = []
        for family in FAMILIES:
            rows = candidates.loc[candidates.family.eq(family)]
            entry = {"family": family, "candidate_count": len(rows),
                "display_top5": json.loads(rows.head(5).to_json(orient="records")),
                "producer_batch_status": bundle["batches"][family][day]["status"]}
            if qualification is not None:
                batch = bundle["batches"][family][day]
                entry["feature_qualification"] = deepcopy(batch["feature_qualification"])
                if batch.get("incomplete_reason"):
                    entry["incomplete_reason"] = batch["incomplete_reason"]
            families.append(entry)
        lineage = {**common, "day": day, "latest_source_day_used": day,
            "source_days_used": [value for value in days if value <= day],
            "source_proofs": [item for item in sources if item["day"] <= day],
            "raw_rows_by_recent_session": {value: count for value, count in root_lineage["raw_rows_by_recent_session"].items() if value <= day},
            "current_raw_fields_unchanged": True,
            "feature_time_semantics": "Original producer past-only shifts and rolling windows; current output is filtered to this exact source day"}
        result = {"schema": PREVIEW_SCHEMA, "created_at": recorded, "day": day,
            "status": "complete_source_close_research_preview_only", "source": source,
            "recent_source_lineage": lineage, "history_snapshot": str(history_snapshot),
            "history_inventory_complete": False, "calendar": str(calendar), "calendar_sha256": calendar_hash,
            "source_inventory_complete": True, "tape": tape, "families": families,
            "producer_coverage": [item for item in bundle["coverage_facts"] if item["day"] == day],
            "candidate_count": len(candidates), "history_semantics": bundle["protocol"],
            "successor_raw_bar_used_for_features": False, "execution_successor_proof_admitted": False,
            "source_verified": False, "pit_verified": False, "company_actions_verified": False,
            "provider_contacted": False, **BOUNDARY,
            "limitations": ["Recent complete provider sources form a new mixed-source research view, not an admitted continuous snapshot",
                "Original snapshot rows after the exact base cutoff are excluded only from this new view; no original file is changed",
                "Every recent day retains its own complete dated roster; missing or suspended names are not survivor-pruned",
                "Original observed-row trend windows and unknown stages remain; no IPO feature correction is opted in",
                "Actual derivation time is not historical signal publication; PIT, company actions and execution stay unverified"]}
        if qualification is not None:
            day_qualification = _day_qualification(qualification, day)
            lineage["feature_qualification"] = day_qualification
            lineage["feature_time_semantics"] = "Same fixed producer with explicit listing qualification; every decision uses its own 15:05 cutoff for historical and current qualification; unqualified feature columns retained"
            result["feature_qualification"] = day_qualification
            result["limitations"][3] = "Original observed-row trend windows remain; explicit listing-window qualification retains unknown membership and four original unqualified feature columns"
        previews.append((out / day.replace("-", ""), features, candidates, result))
    # Source/clock/raw-identity checks and all calculations precede reserving
    # the final output. A partial package produces no directory or final files.
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "recent_source_lineage.json", root_lineage)
    entries = []
    for folder, features, candidates, result in previews:
        folder.mkdir()
        write_frame(folder / "current_features.csv", features)
        write_frame(folder / "candidates.csv", candidates)
        write_json(folder / "recent_source_lineage.json", result["recent_source_lineage"])
        write_json(folder / "result.json", result)
        entries.append({"day": result["day"], "preview_folder": str(folder),
                        "candidate_count": result["candidate_count"], "active_non_st_rows": len(features)})
    result = {"schema": SCHEMA, "status": "completed_recent_source_feature_view", "created_at": recorded,
        "shared_view_id": view_id, "base_history_end": cutoff, "days": days, "previews": entries,
        "recent_source_lineage": root_lineage, "output_directory_created": True, **BOUNDARY}
    write_json(out / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, action="append", required=True)
    parser.add_argument("--history-snapshot", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--ipo-evidence", type=Path, help="Explicit typed listing-policy evidence; omission preserves the original feature method")
    parser.add_argument("--research-master-run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.research_master_run and not args.ipo_evidence:
        raise ValueError("Research master must be bound to explicit IPO evidence")
    proof = None
    if args.ipo_evidence:
        from .ipo_qualification import load_ipo_qualification
        proof = load_ipo_qualification(args.ipo_evidence, args.calendar, master_run=args.research_master_run)
    result = derive_previews(args.package, args.history_snapshot, args.calendar, args.out, ipo_qualification=proof)
    print(json.dumps({key: result.get(key) for key in ("status", "days", "previews", "sources", "output_directory_created")}
                     | {"output": str(args.out.resolve()), "execution_admitted": False}, ensure_ascii=False))
    return 2 if result["status"] == "blocked_failed_source" else 0


if __name__ == "__main__":
    raise SystemExit(main())
