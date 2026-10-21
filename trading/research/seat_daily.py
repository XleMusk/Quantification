"""Daily wiring for the two already registered public-seat methods.

The old single snapshot has one final maturity on 19 October and its final
execution on 20 October.  It is then explicitly right censored, never sold or
kept alive by invented publications.  The append method is the ongoing route.
Only a supervised runner may execute the existing source/publisher/consumer
commands.  Bootstrap and planning read saved metadata, not account/raw files.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from ..settings import DATA_ROOT, ROOT
from .leader_daily_signals import _calendar_view

SCHEMA = "registered_public_seat_daily_profile_v1"
PLAN_SCHEMA = "registered_public_seat_daily_plan_v1"
RESULT_SCHEMA = "registered_public_seat_daily_result_v1"
MUTABLE_PROFILE_KEYS = ("last_source_day", "old_seat", "append_seat", "operations")
OLD_REGISTRATION = ROOT / "outputs/seat_public_probe/20261016_forward_v1/registration.json"
APPEND_REGISTRATION = ROOT / "outputs/seat_append/20261016_forward_prospective_v1/registration.json"
OLD_PUBLICATION = DATA_ROOT / "research/seat_hypothesis_dated_inputs/20261016_source1015_v1/publication.json"
OLD_CLOSE = DATA_ROOT / "research/bulk_close_cycles/20261016_v1/full_close"
OLD_RUN = DATA_ROOT / "research/seat_hypothesis_dated_forward/20261016_source1015_v1"
APPEND_RUN = DATA_ROOT / "research/seat_append_forward/20261019_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "model_refit": False, "user_account_mutated": False}


def _now():
    return datetime.now(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _verify(reference):
    if _ref(reference["file"]) != reference:
        raise ValueError("Saved public-seat metadata changed")
    return reference


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _sessions(calendar):
    payload = _read(calendar)
    sessions, previous, status = _calendar_view(payload, f"{payload['year']}-01-01",
        f"{payload['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Public-seat daily uses the original fixed exchange calendar")
    return sessions, previous


def _stamp(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None or stamp > _now():
        raise ValueError("A saved actual source clock must be aware and not future")
    return stamp


def _catalog(path, current=None):
    """Small append ancestry/coverage metadata; publishers validate full facts."""
    records, seen = [], set()
    def visit(file):
        reference = _ref(file)
        if reference["file"] in seen:
            raise ValueError("Catalog ancestry is cyclic")
        seen.add(reference["file"])
        record = _read(file)
        if record.get("schema") != "public_seat_disclosure_catalog_v1":
            raise ValueError("An immutable disclosure catalog is required")
        _stamp(record["created_at"])
        parent = record.get("parent_ref")
        if parent:
            _verify(parent)
            prior = visit(parent["file"])
            if record["batches"][:len(prior["batches"])] != prior["batches"]:
                raise ValueError("Catalog removed or replaced its original batch prefix")
        records.append(reference)
        return record
    record = visit(path)
    if current and current not in records:
        raise ValueError("New catalog must descend from the exact saved catalog")
    batches, days, refs = [], [], list(records)
    for item in record["batches"]:
        _verify(item["reference"])
        batch = _read(item["reference"]["file"])
        if (batch.get("schema") != "public_seat_disclosure_batch_v1"
                or batch.get("event_day") != item["event_day"]
                or batch.get("batch_id") != item["batch_id"]
                or batch.get("status") not in {"completed_nonempty", "completed_empty"}):
            raise ValueError("Every event day needs its actual complete batch")
        _stamp(batch["created_at"])
        _stamp(batch["source_available_at"])
        days.append(batch["event_day"])
        batches.append({"event_day": batch["event_day"], "status": batch["status"],
            "source_available_at": batch["source_available_at"]})
        refs.append(item["reference"])
    if days != sorted(set(days)):
        raise ValueError("Catalog event days must be unique and ordered")
    return {"reference": _ref(path), "event_days": days, "batches": batches, "refs": refs}


def bootstrap_profile(current_source_day="2026-10-16", *, old_registration=OLD_REGISTRATION,
        append_registration=APPEND_REGISTRATION, old_publication=OLD_PUBLICATION,
        old_close=OLD_CLOSE, old_run=OLD_RUN, append_run=APPEND_RUN):
    """Pin existing small artifacts; never register, collect, or create books."""
    old, append = _read(old_registration), _read(append_registration)
    if (old.get("schema") != "public_seat_single_snapshot_forward_method_registration_v1"
            or append.get("schema") != "public_seat_append_forward_method_registration_v1"
            or old.get("first_decision_day") != "2026-10-16"
            or append.get("first_decision_day") != "2026-10-19"
            or old.get("arm_ids") != append.get("arm_ids") or len(old["arm_ids"]) != 4
            or old.get("config") != append.get("config") or old["calendar"] != append["calendar"]
            or current_source_day != "2026-10-16"):
        raise ValueError("The original two independent registrations and source16 anchor are required")
    for record in (old, append):
        _stamp(record["registered_at"])
        if any(record.get(key) is not False for key in ("execution_admitted", "selection_enabled", "strategy_validated")):
            raise ValueError("Existing methods must remain research only")
    refs = [_ref(old_registration), _ref(append_registration), _ref(old_publication), _ref(__file__)]
    for record in (old, append):
        refs += [value for value in record.values() if isinstance(value, dict) and "file" in value and "sha256" in value]
        refs += record.get("shared_helper_implementations", [])
    publication = _read(old_publication)
    _verify(publication["bundle"])
    bundle = _read(publication["bundle"]["file"])
    refs.append(publication["bundle"])
    sessions, previous = _sessions(old["calendar"]["file"])
    if (publication.get("decision_day") != current_source_day or publication.get("execution_day") != "2026-10-19"
            or publication.get("same_D_actual_publication") is not True
            or _stamp(publication["published_at"]).date().isoformat() != current_source_day
            or bundle.get("D_primary", {}).get("source_folder") != str(Path(old_close).resolve())
            or bundle.get("decision_day") != current_source_day or bundle.get("next_execution_day") != "2026-10-19"
            or previous.get(append["first_decision_day"]) != current_source_day):
        raise ValueError("The real16 publication/raw anchor and exact19/20 sessions must agree")
    catalog = _catalog(append["initial_catalog"]["file"], append["initial_catalog"])
    refs += catalog["refs"]
    unique = {r["file"]: _verify(r) for r in refs}
    profile = {"schema": SCHEMA, "fixed_refs": list(unique.values()), "calendar": old["calendar"],
        "arm_ids": old["arm_ids"], "last_source_day": current_source_day,
        "old_seat": {"registration": _ref(old_registration), "input_registration": old["original_registration"],
            "first_decision_day": "2026-10-16", "first_execution_day": "2026-10-19",
            "last_decision_day": "2026-10-19", "last_execution_day": "2026-10-20",
            "publications": [str(Path(old_publication).resolve())], "raw_closes": [str(Path(old_close).resolve())],
            "last_publication_day": current_source_day, "last_processed_execution_day": None,
            "consumer_result": None, "run_directory": str(Path(old_run).resolve()), "status": "actual16_publication_pending19_execution"},
        "append_seat": {"registration": _ref(append_registration), "input_registration": append["dated_input_method_registration"],
            "first_decision_day": "2026-10-19", "first_execution_day": "2026-10-20",
            "catalog": catalog["reference"], "publications": [], "raw_closes": [],
            "last_publication_day": None, "last_processed_execution_day": None,
            "consumer_result": None, "run_directory": str(Path(append_run).resolve()), "status": "registered_first19_not_yet_published"},
        "operations": [], **FLAGS}
    profile["catalog_anchor"] = deepcopy(catalog["reference"])
    identity_keys = ("registration", "input_registration", "first_decision_day", "first_execution_day", "run_directory")
    profile["branch_identities"] = {key: {field: deepcopy(profile[key][field]) for field in identity_keys}
        for key in ("old_seat", "append_seat")}
    profile["branch_identities"]["old_seat"].update(last_decision_day="2026-10-19", last_execution_day="2026-10-20",
        anchor_publication=_ref(old_publication), anchor_close=str(Path(old_close).resolve()))
    return profile


def input_directories(profile):
    paths = [Path(r["file"]).resolve().parent for r in profile["fixed_refs"]]
    for key in ("old_seat", "append_seat"):
        branch = profile[key]
        paths += [Path(branch["run_directory"]), *map(Path, branch["raw_closes"])]
        paths += [Path(file).parent for file in branch["publications"]]
    paths.append(Path(profile["append_seat"]["catalog"]["file"]).parent)
    return list(dict.fromkeys(str(p.resolve()) for p in paths))


def _validate_profile(profile):
    if profile.get("schema") != SCHEMA or any(profile.get(k) is not v for k, v in FLAGS.items()):
        raise ValueError("The registered public-seat daily research profile is required")
    for reference in profile["fixed_refs"]:
        _verify(reference)
    sessions, previous = _sessions(profile["calendar"]["file"])
    for key in ("old_seat", "append_seat"):
        branch, identity = profile[key], profile["branch_identities"][key]
        for field, value in identity.items():
            if field not in {"anchor_publication", "anchor_close"} and branch.get(field) != value:
                raise ValueError("A mutable branch cannot change its original registered identity")
        registration = _read(identity["registration"]["file"])
        if (registration.get("first_decision_day") != identity["first_decision_day"]
                or registration.get("calendar") != profile["calendar"]
                or registration.get("arm_ids") != profile["arm_ids"]):
            raise ValueError("Branch identity differs from its fixed original registration")
        pubs = [_read(file) for file in branch["publications"]]
        days = [pub.get("decision_day") for pub in pubs]
        expected = sessions[sessions.index(identity["first_decision_day"]):][:len(days)]
        if (days != expected or branch["last_publication_day"] != (days[-1] if days else None)
                or any(pub.get("execution_day") != sessions[sessions.index(d) + 1] for pub, d in zip(pubs, days))):
            raise ValueError("Actual publication prefix cannot be skipped, reordered or replaced")
        if key == "old_seat" and (not branch["publications"]
                or _ref(branch["publications"][0]) != identity["anchor_publication"]
                or not branch["raw_closes"] or branch["raw_closes"][0] != identity["anchor_close"]
                or any(d > identity["last_decision_day"] for d in days)):
            raise ValueError("The original static publication/raw anchor and finite endpoint cannot change")
    _catalog(profile["append_seat"]["catalog"]["file"], profile["catalog_anchor"])
    return sessions, previous


def _repeat(flag, values):
    return [token for value in values for token in (flag, str(value))]


def next_plan(profile, day, package, out, *, catalog=None):
    sessions, previous = _validate_profile(profile)
    if day not in sessions or previous.get(day) != profile["last_source_day"]:
        raise ValueError("Only the exact next public-seat source day is allowed; no skip or backfill")
    package, out = Path(package).resolve(), Path(out).resolve()
    for file in input_directories(profile) + [str(package)]:
        source = Path(file).resolve()
        if out == source or out in source.parents or source in out.parents:
            raise ValueError("Daily outputs must be outside sources and the original research accounts")
    plan, receipt = _read(package / "plan.json"), _read(package / "receipt.json")
    source_day = plan.get("day", plan.get("date"))
    if source_day != day or receipt.get("status") != "complete_full_dated_close_inventory_not_admitted":
        raise ValueError("The exact successful complete D package is required")
    current_catalog = profile["append_seat"]["catalog"]
    loaded = _catalog(catalog or current_catalog["file"], current_catalog)
    needed = sessions[sessions.index("2026-10-15"):sessions.index(day)]
    missing = [d for d in needed if d not in loaded["event_days"]]
    source_refs = [_ref(package / "plan.json"), _ref(package / "receipt.json"), loaded["reference"]]
    execution = sessions[sessions.index(day) + 1] if sessions.index(day) + 1 < len(sessions) else None
    if execution is None:
        raise ValueError("An exact next execution session is required")
    steps, blocked = [], {}
    old, app = profile["old_seat"], profile["append_seat"]
    expected_app = app["first_decision_day"] if not app["last_publication_day"] else sessions[sessions.index(app["last_publication_day"]) + 1]
    app_publish = day == expected_app and not missing
    if day != expected_app:
        blocked["append_seat"] = {"status": "missed_actual_publication_prefix", "required_decision_day": expected_app}
    elif missing:
        blocked["append_seat"] = {"status": "unknown_disclosure_coverage", "missing_event_days": missing}
    append_audit = out / "artifacts/append_audit"
    append_publication = out / "artifacts/append_publication/publication.json"
    old_audit = out / "artifacts/old_audit"
    old_publication = out / "artifacts/old_publication/publication.json"
    if app_publish:
        steps += [{"id": "append_seat_audit", "requires": [], "command": ["seat-append-source-audit",
            "--registration", app["input_registration"]["file"], "--catalog", loaded["reference"]["file"],
            "--close", str(package), "--decision-day", day, "--out", str(append_audit), "--execute"],
            "expected_files": [str(append_audit / "outcome.json"), str(append_audit / "audit.json")]},
            {"id": "append_seat_publication", "requires": ["append_seat_audit"], "command": ["seat-append-dated-inputs",
            "--registration", app["input_registration"]["file"], "--catalog", loaded["reference"]["file"],
            "--calendar", profile["calendar"]["file"], "--close", str(package),
            "--source-audit", str(append_audit / "audit.json"), "--decision-day", day,
            "--out", str(append_publication.parent)], "expected_files": [str(append_publication)]}]
    old_publish = day == old["last_decision_day"] and old["last_publication_day"] == "2026-10-16"
    if old_publish and app_publish:
        steps += [{"id": "old_seat_audit_projection", "requires": ["append_seat_audit"], "command": ["seat-daily",
            "project-static-audit", "--forward-registration", old["registration"]["file"],
            "--append-audit", str(append_audit / "audit.json"), "--close", str(package), "--decision-day", day,
            "--out", str(old_audit)], "expected_files": [str(old_audit / "projection.json"), str(old_audit / "audit.json")]},
            {"id": "old_seat_publication", "requires": ["old_seat_audit_projection"], "command": ["seat-hypothesis-dated-inputs",
            "--registration", old["input_registration"]["file"], "--calendar", profile["calendar"]["file"],
            "--close", str(package), "--source-audit", str(old_audit / "audit.json"), "--decision-day", day,
            "--out", str(old_publication.parent)], "expected_files": [str(old_publication)]}]
    elif old_publish:
        blocked["old_seat"] = {"status": "missing_shared_current_source_audit"}
    for key, command in (("old_seat", "seat-hypothesis-dated-forward"), ("append_seat", "seat-append-forward")):
        branch = profile[key]
        due = day >= branch["first_execution_day"] and bool(branch["publications"])
        if key == "old_seat":
            due = due and day <= branch["last_execution_day"]
            if day == branch["last_execution_day"] and branch["last_publication_day"] != branch["last_decision_day"]:
                due = False
                blocked[key] = {"status": "missed_final_static_publication_no_backfill"}
        if due:
            steps.append({"id": key + "_forward", "requires": [], "command": [command,
                "--registration", branch["registration"]["file"], *_repeat("--publication", branch["publications"]),
                *_repeat("--raw-close", [*branch["raw_closes"], str(package)]),
                "--run-dir", branch["run_directory"], "--through", day,
                "--result-json", str(out / (key + "_forward_result.json"))],
                "expected_files": [str(out / (key + "_forward_result.json"))]})
    return {"schema": PLAN_SCHEMA, "source_day": day, "execution_day": execution,
        "parent_profile_sha256": _hash(profile), "before_profile": deepcopy(profile),
        "package": str(package), "catalog": loaded["reference"]["file"],
        "source_refs": source_refs, "steps": steps, "blocked": blocked,
        "append_catalog": loaded["reference"], "missing_event_days": missing,
        "publication_files": {"old_seat": str(old_publication), "append_seat": str(append_publication)}, **FLAGS}


def validate_plan(plan, profile, day, package, out, *, catalog=None):
    before = plan.get("before_profile")
    if not isinstance(before, dict) or _hash(before) != plan.get("parent_profile_sha256"):
        raise ValueError("Public-seat plan's original profile changed")
    if profile != before:
        saved = _read(Path(out) / "receipt.json")
        if saved.get("adopted_profile_sha256") != _hash(profile):
            raise ValueError("Public-seat recovery requires the exact original before/after profile")
    if plan != next_plan(before, day, package, out, catalog=catalog):
        raise ValueError("Public-seat saved source/catalog/commands cannot change")
    return plan


def run_profile(profile, day, package, out, *, run_step, catalog=None):
    out = Path(out).resolve()
    plan_file, receipt_file = out / "plan.json", out / "receipt.json"
    if plan_file.exists():
        plan = validate_plan(_read(plan_file), profile, day, package, out, catalog=catalog)
    else:
        plan = next_plan(profile, day, package, out, catalog=catalog)
        _atomic(plan_file, plan)
    before = plan["before_profile"]
    result = _read(receipt_file) if receipt_file.exists() else {"schema": RESULT_SCHEMA,
        "day": day, "execution_day": plan["execution_day"], "plan": _ref(plan_file),
        "steps": {}, "branches": {}, "metrics": None, **FLAGS}
    if result.get("plan") != _ref(plan_file):
        raise ValueError("Public-seat receipt no longer binds its original plan")
    for step in plan["steps"]:
        if any(result["steps"].get(k, {}).get("status") != "completed" for k in step["requires"]):
            result["steps"][step["id"]] = {"status": "blocked_by_prior_stage"}
        else:
            try:
                result["steps"][step["id"]] = run_step(step)
            except Exception as error:
                result["steps"][step["id"]] = {"status": "runner_failed_progress_unknown",
                    "error": type(error).__name__ + ": " + str(error), "execution_progress_unknown": True}
        _atomic(receipt_file, result)
    after = deepcopy(before)
    after["last_source_day"] = day
    after["operations"].append(str(receipt_file))
    after["append_seat"]["catalog"] = deepcopy(plan["append_catalog"])
    for key in ("old_seat", "append_seat"):
        branch = after[key]
        active = key == "append_seat" or day <= branch["last_execution_day"]
        if active and plan["package"] not in branch["raw_closes"]:
            branch["raw_closes"].append(plan["package"])
        publication_done = result["steps"].get(key + "_publication", {}).get("status") == "completed"
        if publication_done:
            file = plan["publication_files"][key]
            pub = _read(file)
            if pub.get("decision_day") != day or pub.get("execution_day") != plan["execution_day"]:
                raise ValueError("Completed publisher did not save the exact real D/E publication")
            branch["publications"].append(file)
            branch["last_publication_day"] = day
        producer_required = key == "append_seat" or day == branch["last_decision_day"]
        consumer_required = day >= branch["first_execution_day"] and (key == "append_seat" or day <= branch["last_execution_day"])
        summary = {"producer_required": producer_required, "producer_complete": publication_done,
            "consumer_required": consumer_required, "consumer_called": key + "_forward" in result["steps"],
            "consumer_complete": False, "account_count": None, "metrics": None,
            "publication": _ref(plan["publication_files"][key]) if publication_done else None,
            "blocking_evidence": deepcopy(plan["blocked"].get(key))}
        if result["steps"].get(key + "_forward", {}).get("status") == "completed":
            file = out / (key + "_forward_result.json")
            observed = _read(file)
            cases = observed.get("cases", [])
            expected_schema = ("public_seat_four_arm_dated_forward_result_v1" if key == "old_seat"
                else "public_seat_append_four_arm_forward_result_v1")
            complete = (observed.get("schema") == expected_schema
                and observed.get("status") == "advanced_four_isolated_dated_research_books"
                and not observed.get("error") and not observed.get("blocking_evidence")
                and observed.get("account_count") == 4 and len(cases) == 4
                and {c.get("arm_id") for c in cases} == set(before["arm_ids"])
                and all(c.get("last_processed_execution_day") == day
                    and not c.get("error") and not c.get("blocking_evidence") for c in cases))
            branch["consumer_result"] = _ref(file)
            if complete:
                branch["last_processed_execution_day"] = day
            summary.update(consumer_complete=complete, consumer_status=observed.get("status"),
                consumer_result=_ref(file), account_count=observed.get("account_count"),
                research_accounts_created=observed.get("research_accounts_created"),
                new_order_count=observed.get("new_order_count"), newly_committed_execution_sessions=observed.get("newly_committed_execution_sessions"))
        if key == "old_seat" and day >= branch["last_execution_day"]:
            branch["status"] = ("static_source_exhausted_holdings_right_censored" if branch["last_processed_execution_day"] == branch["last_execution_day"]
                else "static_source_exhausted_execution_incomplete_right_censored")
        elif summary["blocking_evidence"]:
            branch["status"] = summary["blocking_evidence"]["status"]
        elif publication_done:
            branch["status"] = "actual_publication_saved_execution_separately_observed"
        else:
            branch["status"] = "publication_or_consumer_unfinished"
        summary["status"] = branch["status"]
        summary["daily_complete"] = ((not producer_required or publication_done)
            and (not consumer_required or summary["consumer_complete"])
            and not summary["blocking_evidence"])
        if key == "old_seat" and day > branch["last_execution_day"]:
            summary["daily_complete"] = branch["last_processed_execution_day"] == branch["last_execution_day"]
        result["branches"][key] = summary
    audit_outcome = out / "artifacts/append_audit/outcome.json"
    audit_called = result["steps"].get("append_seat_audit", {}).get("status") not in (None, "blocked_by_prior_stage")
    result["network_accessed"] = _read(audit_outcome).get("network_accessed") if audit_outcome.is_file() else (None if audit_called else False)
    result["network_access_unknown"] = result["network_accessed"] is None
    result.update(status="saved_independent_public_seat_daily_outcomes", adopted_profile_sha256=_hash(after),
        daily_complete=all(b["daily_complete"] for b in result["branches"].values()),
        producer_complete=all(not b["producer_required"] or b["producer_complete"] for b in result["branches"].values()),
        consumer_complete=all(not b["consumer_required"] or b["consumer_complete"] for b in result["branches"].values()),
        last_source_day_is_input_observation_not_all_routes_completed=True)
    _atomic(receipt_file, result)
    return {"result": result, "next_profile": after}


def project_static_audit(forward_registration, append_audit, close, decision_day, out):
    """Exact string subset of one saved comparison; no HTTP or market run."""
    from . import seat_hypothesis_dated_inputs as inputs
    from . import seat_hypothesis_dated_forward as legacy
    now = _now()
    if now.date().isoformat() != decision_day:
        raise ValueError("Static audit projection belongs to the actual same D; no backfill")
    registered = legacy._registration(forward_registration, now)
    symbols = sorted({r["symbol"] for recipe in registered["recipes"].values() for r in recipe["supported"]})
    saved = _read(append_audit)
    available = [r["symbol"] for r in saved["scope_keys"]]
    parent = inputs.validate_exact_source_audit(append_audit, close, decision_day=decision_day, symbols=available)
    comparison = saved["comparison"]
    files = [forward_registration, append_audit, comparison["raw"]["file"], comparison["metadata"]["file"]]
    target = inputs._protect(out, files, [close, *[Path(p).resolve().parent for p in files]])
    target.mkdir(parents=True, exist_ok=False)
    record = {"schema": "public_seat_static_exact_comparison_projection_v1", "decision_day": decision_day,
        "source_audit": parent["reference"], "source_comparison": comparison["raw"],
        "source_metadata": comparison["metadata"], "required_symbols": symbols,
        "missing_symbols": sorted(set(symbols) - set(available)), "network_accessed": False,
        "new_quote_request_count": 0, **FLAGS}
    if record["missing_symbols"]:
        record["status"] = "blocked_old_scope_not_in_shared_comparison_no_second_request"
        _atomic(target / "projection.json", record)
        return record
    with Path(comparison["raw"]["file"]).open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fields, rows = list(reader.fieldnames), [row for row in reader if row["symbol"] in symbols]
    rows.sort(key=lambda r: r["symbol"])
    comparison_folder = target / "comparison_source"
    comparison_folder.mkdir()
    projected = comparison_folder / "comparison.csv"
    with projected.open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    metadata = deepcopy(_read(comparison["metadata"]["file"]))
    metadata.update(projection_of_audit=parent["reference"], projection_of_comparison=comparison["raw"],
        projection_exact_symbols=symbols, projection_created_at=now.isoformat(),
        projection_semantics="exact_original_string_rows_same_response_clock_units_no_new_quote")
    _atomic(comparison_folder / "metadata.json", metadata)
    audit = inputs.build_exact_source_audit(close, projected, comparison_folder / "metadata.json", target / "audit.json",
        decision_day=decision_day, symbols=symbols)
    record.update(status="completed_exact_static_scope_projection", audit=_ref(target / "audit.json"),
        comparison=_ref(projected), all_rows_match=audit["all_rows_match"],
        original_response=metadata.get("raw_response"), original_source_available_at=metadata["source_available_at"])
    _atomic(target / "projection.json", record)
    return record


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    project = sub.add_parser("project-static-audit")
    for flag in ("forward-registration", "append-audit", "close", "out"):
        project.add_argument("--" + flag, required=True, type=Path)
    project.add_argument("--decision-day", required=True)
    args = cli.parse_args(argv)
    result = project_static_audit(args.forward_registration, args.append_audit,
        args.close, args.decision_day, args.out)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["status"].startswith("completed_") else 2


if __name__ == "__main__":
    raise SystemExit(main())
