"""Publish append-only public-seat candidates without touching an account.

Raw disclosure clocks determine first maturity.  Catalog/panel construction
clocks must precede the actual same-day publication, and never rewrite an
earlier first-maturity day.  The original single-snapshot modules stay frozen.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from . import seat_hypothesis_dated_inputs as original
from .exchange_calendar import load_calendar
from .leader_daily_signals import _calendar_view


METHOD_ID = "public_seat_append_first_maturity_dated_inputs_v1"
REGISTRATION_SCHEMA = "public_seat_append_dated_method_registration_v1"
SCHEMA = "public_seat_append_dated_inputs_v1"
PUBLICATION_SCHEMA = "public_seat_append_dated_publication_v1"
ARMS = original.ARMS
FLAGS = dict(original.FLAGS)
RULE_SPEC = {
    "method_id": METHOD_ID,
    "arms": list(ARMS),
    "source": "immutable_exact_event_day_batches_with_registered_catalog_ancestry",
    "maturity": "first_calendar_D_at_least_event_plus_lag_and_raw_available_by_D_1500",
    "late_append": "past_first_maturity_is_not_rolled_to_firstD_or_append_day",
    "same_symbol_collision": "latest_event_date_among_same_D_matured_events_not_max_score_no_sum",
    "suppressed_events": "retain_all_older_same_D_events_with_suppression_reason",
    "audit_scope": "all_four_arms_first_matured_deduplicated_candidates_before_top5_not_all_historical_catalog_symbols",
    "unknown": "unknown_event_does_not_veto_other_known_events",
    "empty": "verified_empty_batch_or_no_matured_events_is_explicit_NO_CHANGE_not_FULL_TARGET",
    "selection": "Engine_choose_targets_at_actual_D_book_retain20_holdings5_no_publication_weights",
    "holding": "event_update_NO_CHANGE_preserves_holdings_no_new_exit_or_forced_liquidation",
}


def _now():
    return original._now()


def _catalog_module():
    # Lazy import lets this small module and the catalog be developed without
    # introducing an import cycle; neither path has collection side effects.
    from . import seat_disclosure_catalog
    return seat_disclosure_catalog


def _refs(references):
    result, seen = [], {}
    for reference in references:
        actual = original._verify(reference)
        old = seen.get(actual["file"])
        if old is not None and old != actual:
            raise ValueError("One input path has conflicting source bindings")
        if old is None:
            result.append(actual)
            seen[actual["file"]] = actual
    return result


def load_registration(path, now=None):
    """Validate one independently registered input method; no writes or Engine."""
    now = now or _now()
    record = original._read(path)
    if (record.get("schema") != REGISTRATION_SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("rule_spec") != RULE_SPEC or record.get("arm_ids") != list(ARMS)
            or any(record.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Exact independent append input registration and boundaries are required")
    actual = original._stamp(record.get("registered_at"), now)
    implementation = original._verify(record["implementation"])
    catalog_code = original._verify(record["catalog_implementation"])
    catalog_module = _catalog_module()
    if implementation != original._ref(__file__) or catalog_code != original._ref(catalog_module.__file__):
        raise ValueError("Registered append core implementation changed")
    base_ref = original._verify(record["original_registration"])
    base, _, base_files = original._registered(base_ref["file"], now)
    if record.get("config") != base["config"] or original._stamp(base["registered_at"], now) > actual:
        raise ValueError("Original four-arm thresholds, costs or registration ancestry changed")
    calendar = original._verify(record["calendar"])
    payload = load_calendar(Path(calendar["file"]))
    if payload is None:
        raise ValueError("Pinned exchange calendar is required")
    year = int(payload["year"])
    sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    first = original._day(record["first_decision_day"])
    if status != "pinned_static_exchange_calendar" or first not in sessions or first < actual.date().isoformat():
        raise ValueError("The prospective first decision session cannot be backfilled or rolled")
    initial_ref = original._verify(record["initial_catalog"])
    initial = catalog_module.load_catalog(initial_ref["file"], now=now)
    if initial["reference"] != initial_ref or original._stamp(initial["created_at"], now) > actual:
        raise ValueError("Initial immutable source catalog must actually exist before registration")
    files = _refs([original._ref(path), implementation, catalog_code, base_ref, calendar,
        *base_files, *initial["references"]])
    return {"record": record, "reference": original._ref(path), "original": base,
        "original_reference": base_ref, "calendar": calendar, "sessions": sessions,
        "config": record["config"], "initial_catalog": initial,
        "files": [Path(r["file"]) for r in files], "references": files}


def load_registered_catalog(path, registered, now=None):
    """Require actual catalog ancestry, not an unrelated copy of its first rows."""
    now = now or _now()
    module = _catalog_module()
    catalog = module.load_catalog(path, now=now)
    module.assert_catalog_prefix(catalog, registered["initial_catalog"])
    sessions = registered["sessions"]
    if any(batch["event_day"] not in sessions for batch in catalog["batches"]):
        raise ValueError("Each disclosure event day must be on the registered calendar")
    return catalog


def mature_arms(catalog, sessions, decision_day):
    """Reconstruct all schedules and four rankings from validated batch facts.

    This deliberately has no firstD argument: an event that first matured
    before a new account starts is not made new by that account's inception.
    """
    day = original._day(decision_day)
    if day not in sessions:
        raise ValueError("Decision day is not on the fixed exchange calendar")
    results = []
    for arm_id, (recipe, lag) in ARMS.items():
        schedule, candidates, source_rows, panels = [], [], [], []
        base_id = (original.original.HYPOTHESIS_ID if recipe == "legacy_repeated_sum"
            else original.original.STOCKDAY_HYPOTHESIS_ID)
        consumer_id = (original.original.AVAILABILITY_HYPOTHESIS_ID if recipe == "legacy_repeated_sum"
            else original.original.STOCKDAY_AVAILABILITY_ID)
        for batch in catalog["batches"]:
            if batch["status"] not in {"completed_nonempty", "completed_empty"}:
                raise ValueError("Pending or failed source batches cannot mean a known no-event day")
            event = batch["event_day"]
            if event not in sessions:
                raise ValueError("Disclosure event day is outside the registered calendar")
            start = sessions.index(event) + lag
            earliest = sessions[start] if start < len(sessions) else None
            facts = batch["recipes"][recipe]
            supported = {row["symbol"] for row in facts["supported"]}
            source_rows.extend({**row, "batch_id": batch["batch_id"]} for row in facts["supported"])
            panels.extend({"batch_id": batch["batch_id"], "observation": row} for row in facts["panel_observations"])
            for row in facts["candidates"]:
                if row["event_date"] != event:
                    raise ValueError("Candidate event date differs from its immutable batch")
                clock = facts["clocks"].get((event, row["symbol"]))
                available = clock["available_at"] if clock else None
                stamp = datetime.fromisoformat(available) if available else None
                if stamp is not None and stamp.utcoffset() is None:
                    raise ValueError("Event availability clock must have an explicit offset")
                first = (next((d for d in sessions[start:]
                    if stamp <= datetime.fromisoformat(d + "T15:00:00+08:00")), None)
                    if stamp is not None and earliest is not None else None)
                status = ("outside_scope" if row["symbol"] not in supported else "unknown" if stamp is None else
                    "not_matured" if first is None or day < first else
                    "matured_on_D" if day == first else "matured_before_D")
                item = {**row, "batch_id": batch["batch_id"], "source_clock": clock,
                    "earliest_lag_calendar_day": earliest, "first_eligible_decision_day": first,
                    "status": status, "source_available_by_D_1500":
                    stamp <= datetime.fromisoformat(day + "T15:00:00+08:00") if stamp else None,
                    "suppressed_by_newer_matured_event": False}
                schedule.append(item)
                if status == "matured_on_D":
                    candidates.append(item)
        chosen = {}
        for item in candidates:
            prior = chosen.get(item["symbol"])
            if prior is None or item["event_date"] > prior["event_date"]:
                chosen[item["symbol"]] = item
            elif item["event_date"] == prior["event_date"]:
                raise ValueError("Duplicate same-symbol/event-day batch escaped catalog conflict checks")
        for item in candidates:
            if item is not chosen[item["symbol"]]:
                item["suppressed_by_newer_matured_event"] = True
                item["suppression_reason"] = "newer_event_date_matured_on_same_D_not_score_comparison"
                item["selected_event_date"] = chosen[item["symbol"]]["event_date"]
        ranked = [{key: item[key] for key in ("symbol", "score", "event_date", "batch_id")}
            for item in chosen.values()]
        ranked.sort(key=lambda row: (-row["score"], row["symbol"]))
        results.append({"arm_id": arm_id, "base_hypothesis_id": base_id,
            "consumer_method_id": consumer_id, "lag_sessions": lag,
            "source_ranked_candidates": source_rows, "ranked_candidates": ranked,
            "event_schedule": schedule, "panel_observations": panels,
            "decision_kind": "RANKED_CANDIDATES" if ranked else "NO_CHANGE",
            "reason": "known_first_maturity_events_only" if ranked else "no_matured_known_supported_event_on_D",
            "weights": None, "unknown_event_is_not_a_veto_on_other_known_events": True})
    return results


def required_audit_symbols(arms):
    """Current actionable candidate inventory; no unrelated historical union."""
    return sorted({row["symbol"] for arm in arms for row in arm["ranked_candidates"]})


def _waiting(status, day, execution, **facts):
    return {"schema": SCHEMA, "status": status, "decision_day": day,
        "next_execution_day": execution, "output_created": False, "publication": None, **facts, **FLAGS}


def build_dated_inputs(registration, catalog, calendar, close_folder, source_audit, out, *, decision_day):
    """Save a real same-D publication, or wait without a directory or account."""
    now, day = _now(), original._day(decision_day)
    registered = load_registration(registration, now)
    loaded = load_registered_catalog(catalog, registered, now)
    sessions = registered["sessions"]
    if original._ref(calendar) != registered["calendar"] or day not in sessions or day < registered["record"]["first_decision_day"]:
        raise ValueError("Exact registered calendar and first prospective D must be respected")
    index = sessions.index(day)
    if index + 1 >= len(sessions):
        raise ValueError("Exact next exchange session is required")
    execution = sessions[index + 1]
    references = _refs([*registered["references"], *loaded["references"]])
    packages = [Path(r["file"]).parent for r in references] + loaded["packages"] + [Path(close_folder).resolve()]
    target = original._protect(out, [r["file"] for r in references] + [calendar, source_audit], packages)
    folder = Path(close_folder).resolve()
    if not all((folder / name).is_file() for name in ("plan.json", "receipt.json", "raw_daily.csv")):
        return _waiting("waiting_for_exact_complete_D_close", day, execution)
    receipt = original._read(folder / "receipt.json")
    if receipt.get("status") != "complete_full_dated_close_inventory_not_admitted":
        return _waiting("waiting_for_exact_complete_D_close", day, execution,
            observed_source_status=receipt.get("status"))
    if now.date().isoformat() != day or now < datetime.fromisoformat(day + "T15:05:00+08:00"):
        raise ValueError("Publication must actually occur on D after 15:05; no cross-day backfill")
    primary, _, inventory = original._complete(folder, day, now)
    if original._read(folder / "plan.json")["calendar_sha256"] != registered["calendar"]["sha256"]:
        raise ValueError("Exact D close and registered calendar differ")
    arms = mature_arms(loaded, sessions, day)
    symbols = required_audit_symbols(arms)
    if not Path(source_audit).is_file():
        return _waiting("waiting_for_exact_bound_independent_source_audit", day, execution, required_symbols=symbols)
    audit = original.validate_exact_source_audit(source_audit, folder, decision_day=day, symbols=symbols)
    references = _refs([*references, audit["reference"],
        *[primary[key] for key in ("raw", "plan", "receipt", "roster")],
        original._ref(primary["acquisition"]["roster_receipt"]),
        audit["verification"]["comparison"]["raw"], audit["verification"]["comparison"]["metadata"]])
    original._protect(target, [r["file"] for r in references], packages + [Path(r["file"]).parent for r in references])
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "status": "completed_dated_research_inputs_only",
        "decision_day": day, "next_execution_day": execution, "registration": registered["reference"],
        "base_registration": registered["original_reference"], "catalog": loaded["reference"],
        "catalog_created_at": loaded["created_at"], "catalog_batch_ids": [b["batch_id"] for b in loaded["batches"]],
        "calendar": registered["calendar"], "config": registered["config"], "rule_spec": RULE_SPEC,
        "input_references": references, "D_primary": primary, "D_inventory": inventory,
        "source_audit": audit, "required_audit_symbols": symbols, "arms": arms,
        "source_scope": "registered_immutable_catalog_append_prefix_first_maturity_only",
        "source_availability_cutoff": day + "T15:00:00+08:00", "next_execution_raw_supplied": False,
        "next_execution_quality": "unknown_not_an_observed_raw_bar", "target_weights_generated": False,
        "metrics": None, "limitations": [
            "Raw source knowledge determines first maturity; a late catalog never rolls an old event to today.",
            "NO_CHANGE preserves holdings; this input does not introduce expiry or create account targets.",
            "D numerical price agreement does not establish source identity, PIT, IPO or corporate-action coverage.",
            "E opening tradability, holdings and account quality remain for the shared Engine consumer."], **FLAGS}
    target.mkdir(parents=True)
    original._write(target / "bundle.json", result)
    for reference in references:
        original._verify(reference)
    published = _now()
    audit_record = original._read(audit["reference"]["file"])
    clocks = [loaded["created_at"], primary["source_available_at"], audit_record["checked_at"],
        audit_record["comparison"]["declared_source"]["source_available_at"], registered["record"]["registered_at"]]
    if (published.date().isoformat() != day or published < now
            or any(original._stamp(t, published) > published for t in clocks)):
        raise ValueError("Publication crossed D or precedes actual source/derived evidence; payload remains unpublished")
    publication = {"schema": PUBLICATION_SCHEMA, "method_id": METHOD_ID, "published_at": published.isoformat(),
        "decision_day": day, "execution_day": execution, "bundle": original._ref(target / "bundle.json"),
        "registration": registered["reference"], "catalog": loaded["reference"],
        "same_D_actual_publication": True, **FLAGS}
    original._write(target / "publication.json", publication)
    return {**result, "publication": {**original._ref(target / "publication.json"), "published_at": published.isoformat()}}


def load_publication(path, registered, now=None):
    """Recheck source semantics and all exact inputs for the separate consumer."""
    now = now or _now()
    proof, pub = original._ref(path), original._read(path)
    day, execution = original._day(pub["decision_day"]), original._day(pub["execution_day"])
    sessions = registered["sessions"]
    stamp = original._stamp(pub["published_at"], now)
    if (pub.get("schema") != PUBLICATION_SCHEMA or pub.get("method_id") != METHOD_ID
            or day not in sessions or day < registered["record"]["first_decision_day"]
            or sessions.index(day) + 1 >= len(sessions) or execution != sessions[sessions.index(day) + 1]
            or pub.get("registration") != registered["reference"] or pub.get("same_D_actual_publication") is not True
            or stamp.date().isoformat() != day or stamp < datetime.fromisoformat(day + "T15:05:00+08:00")
            or stamp >= datetime.fromisoformat(execution + "T09:30:00+08:00")
            or stamp < original._stamp(registered["record"]["registered_at"], now)
            or any(pub.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Exact same-D append publication or prospective clock changed")
    bundle_ref = original._verify(pub["bundle"])
    bundle = original._read(bundle_ref["file"])
    loaded = load_registered_catalog(pub["catalog"]["file"], registered, now)
    if loaded["reference"] != pub["catalog"]:
        raise ValueError("Publication catalog bytes changed")
    arms = mature_arms(loaded, sessions, day)
    symbols = required_audit_symbols(arms)
    if (bundle.get("schema") != SCHEMA or bundle.get("method_id") != METHOD_ID
            or bundle.get("status") != "completed_dated_research_inputs_only"
            or bundle.get("decision_day") != day or bundle.get("next_execution_day") != execution
            or bundle.get("registration") != registered["reference"]
            or bundle.get("base_registration") != registered["original_reference"]
            or bundle.get("catalog") != loaded["reference"] or bundle.get("catalog_created_at") != loaded["created_at"]
            or bundle.get("catalog_batch_ids") != [b["batch_id"] for b in loaded["batches"]]
            or bundle.get("calendar") != registered["calendar"] or bundle.get("config") != registered["config"]
            or bundle.get("rule_spec") != RULE_SPEC or bundle.get("arms") != arms
            or bundle.get("required_audit_symbols") != symbols
            or bundle.get("source_scope") != "registered_immutable_catalog_append_prefix_first_maturity_only"
            or bundle.get("source_availability_cutoff") != day + "T15:00:00+08:00"
            or bundle.get("next_execution_raw_supplied") is not False
            or bundle.get("next_execution_quality") != "unknown_not_an_observed_raw_bar"
            or bundle.get("target_weights_generated") is not False or bundle.get("metrics") is not None
            or any(bundle.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Append publication schedule, ranked candidates or NO_CHANGE semantics changed")
    primary, _, inventory = original._complete(bundle["D_primary"]["source_folder"], day, now)
    if primary != bundle["D_primary"] or inventory != bundle["D_inventory"]:
        raise ValueError("Exact D complete source differs from published input")
    if original._read(Path(primary["source_folder"]) / "plan.json")["calendar_sha256"] != registered["calendar"]["sha256"]:
        raise ValueError("Published D source and registered calendar differ")
    audit = original.validate_exact_source_audit(bundle["source_audit"]["reference"]["file"],
        primary["source_folder"], decision_day=day, symbols=symbols)
    if audit != bundle["source_audit"]:
        raise ValueError("Published exact D independent comparison changed")
    required = _refs([*registered["references"], *loaded["references"], audit["reference"],
        *[primary[key] for key in ("raw", "plan", "receipt", "roster")],
        original._ref(primary["acquisition"]["roster_receipt"]),
        audit["verification"]["comparison"]["raw"], audit["verification"]["comparison"]["metadata"]])
    if bundle.get("input_references") != required:
        raise ValueError("Published exact input-reference inventory changed")
    audit_record = original._read(audit["reference"]["file"])
    clocks = [loaded["created_at"], primary["source_available_at"], audit_record["checked_at"],
        audit_record["comparison"]["declared_source"]["source_available_at"]]
    if any(original._stamp(value, now) > stamp for value in clocks) or original._ref(path) != proof:
        raise ValueError("Publication precedes its sources or changed during source validation")
    return {"reference": {"publication": proof, "bundle": bundle_ref, "catalog": loaded["reference"],
        "decision_day": day, "execution_day": execution, "published_at": pub["published_at"]},
        "pub": pub, "bundle": bundle, "catalog_loaded": loaded}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--calendar", required=True, type=Path)
    parser.add_argument("--close", required=True, type=Path, dest="close_folder")
    parser.add_argument("--source-audit", required=True, type=Path)
    parser.add_argument("--decision-day", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = build_dated_inputs(args.registration, args.catalog, args.calendar, args.close_folder,
        args.source_audit, args.out, decision_day=args.decision_day)
    print(json.dumps({key: result.get(key) for key in ("schema", "status", "decision_day",
        "next_execution_day", "publication")}, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
