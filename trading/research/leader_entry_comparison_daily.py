"""Continue the existing fifteen confirmation books from their real D16 publication.

The enclosing daily driver supplies its already saved own input and extras.
This recipe does not collect data, rebuild old publications or create accounts.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from ..settings import DATA_ROOT, ROOT
from .leader_daily_signals import _calendar_view


SCHEMA = "registered_entry_comparison_daily_profile_v1"
PLAN_SCHEMA = "registered_entry_comparison_daily_plan_v1"
RESULT_SCHEMA = "registered_entry_comparison_daily_result_v1"
REGISTRATION = ROOT / "outputs/leader_entry_comparison/20261016_prospective_v1/registration.json"
FIRST_PUBLICATION = ROOT / "outputs/leader_entry_comparison/20261016_actual_publication_v1/publication.json"
CURRENT_EXTRAS = DATA_ROOT / "research/dated_bulk_extras/20261016_official_bulk_v1/result.json"
RUN_DIRECTORY = DATA_ROOT / "research/leader_entry_comparison/20261016_prospective_v1"
MUTABLE_PROFILE_KEYS = {"last_source_day", "publications", "publication_bundles", "confirmation_manifests",
    "last_extras", "operations"}
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "user_account_mutated": False, "model_refit": False}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _stamp(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Saved entry-comparison clocks require explicit timezones")
    return stamp


def _sessions(calendar):
    payload = _read(calendar)
    sessions, previous, status = _calendar_view(payload, f"{payload['year']}-01-01",
        f"{payload['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("The original entry-comparison fixed calendar is required")
    return sessions, previous


def _checked(reference):
    if _ref(reference["file"]) != {"file": reference["file"], "sha256": reference["sha256"]}:
        raise ValueError("Saved entry-comparison source reference changed")
    return _read(reference["file"])


def _extras(path, day, calendar_ref):
    value = _read(path)
    if (value.get("schema") != "baostock_dated_bulk_extras_research_v1"
            or value.get("status") != "completed_dated_bulk_extras_research_only"
            or value.get("day") != day or value.get("calendar_sha256") != calendar_ref["sha256"]
            or value.get("ordinary_roster_complete") is not True
            or value.get("raw_strings_preserved") is not True or value.get("turn_numeric_unit") != "percent"
            or value.get("turn_scaled") is not False or value.get("execution_admitted") is not False
            or _stamp(value["available_at"]) > datetime.now(MARKET_TZ)):
        raise ValueError("Exact dated saved extras, units and calendar are required")
    return _ref(path)


def _bundle(path, day):
    value = _read(path)
    if (value.get("schema") != "leader_published_close_saved_input_bundle_v1"
            or not value.get("source_days_complete") or value["source_days_complete"][-1] != day
            or value.get("candidate_panel_only") is not True
            or any(value.get(key) is not False for key in
                ("execution_admitted", "selection_enabled", "strategy_validated", "account_created"))
            or _stamp(value["recorded_at"]) > datetime.now(MARKET_TZ)):
        raise ValueError("The exact current saved own-input bundle is required")
    return value


def bootstrap_profile(current_source_day="2026-10-16", *, registration=REGISTRATION,
        first_publication=FIRST_PUBLICATION, current_extras=CURRENT_EXTRAS, run_dir=RUN_DIRECTORY):
    """Adopt saved pre-account evidence; never republish or invoke a consumer."""
    from .leader_entry_comparison_publication import load_registration
    loaded = load_registration(registration)
    record, proof = loaded["record"], loaded["proof"]
    publication = _read(first_publication)
    first, execution = record["first_decision_day"], record["first_execution_day"]
    if (current_source_day != first or publication.get("schema") != "leader_entry_comparison_close_publication_v1"
            or publication.get("method_id") != record["method_id"] or publication.get("registration") != proof
            or publication.get("source_close_day") != first or publication.get("proposed_execution_day") != execution
            or publication.get("publication_time_backfilled") is not False
            or publication.get("execution_day_rolled_forward") is not False
            or publication.get("case_count_after_account_application") != len(record["case_ids"])
            or publication.get("candidate_panel_only") is not True
            or any(publication.get(key) is not False for key in
                ("execution_admitted", "selection_enabled", "strategy_validated", "account_created"))):
        raise ValueError("The original first actual entry-comparison publication is required")
    published = _stamp(publication["recorded_at"])
    if (published.date().isoformat() != first or published < _stamp(first + "T15:05:00+08:00")
            or published >= _stamp(execution + "T09:30:00+08:00") or published > datetime.now(MARKET_TZ)):
        raise ValueError("The first real publication cannot be backfilled or rolled")
    bundle_ref = publication["input_bundle"]
    own = _checked(bundle_ref)
    _bundle(bundle_ref["file"], first)
    if _stamp(own["recorded_at"]) > published:
        raise ValueError("The original own input must precede its actual publication")
    confirmation_ref = (publication.get("confirmationproof") or {}).get("manifest")
    if confirmation_ref is None:
        raise ValueError("The existing first publication's saved confirmation is required")
    confirmation = _checked(confirmation_ref)
    _, previous = _sessions(record["calendar"]["file"])
    if (confirmation.get("day") != first or confirmation.get("previous_day") != previous[first]
            or confirmation.get("status") != "completed_research_confirmation_inputs"
            or _stamp(confirmation["available_at"]) > published):
        raise ValueError("The first exact previous-day confirmation must precede publication")
    extras_ref = _extras(current_extras, first, record["calendar"])
    fixed = [_ref(registration), record["protocol"], record["calendar"], _ref(first_publication),
        _ref(bundle_ref["file"]), _ref(confirmation_ref["file"]), extras_ref]
    return {"schema": SCHEMA, "registration": _ref(registration), "fixed_refs": fixed,
        "calendar": deepcopy(record["calendar"]), "first_decision_day": first,
        "first_execution_day": execution, "window": deepcopy(record["window"]),
        "case_ids": deepcopy(record["case_ids"]), "last_source_day": current_source_day,
        "run_directory": str(Path(run_dir).resolve()), "publications": [str(Path(first_publication).resolve())],
        "publication_bundles": [bundle_ref["file"]], "confirmation_manifests": [confirmation_ref["file"]],
        "last_extras": extras_ref, "operations": [], **FLAGS}


def _validate_profile(profile):
    if profile.get("schema") != SCHEMA or any(profile.get(key) is not value for key, value in FLAGS.items()):
        raise ValueError("The original fifteen-case research-only daily profile is required")
    for ref in profile["fixed_refs"]:
        _checked(ref)
    _checked(profile["last_extras"])
    days = [_read(path)["source_close_day"] for path in profile["publications"]]
    if (len(days) != len(profile["publication_bundles"]) or days != profile["window"][:len(days)]
            or not days or days[-1] != profile["last_source_day"]):
        raise ValueError("The original actual-publication prefix cannot skip, reset or disappear")
    return _sessions(profile["calendar"]["file"])


def input_directories(profile):
    from .leader_entry_comparison_forward import input_directories as declared
    return declared(publications=profile["publications"],
        publication_bundles=profile["publication_bundles"], latest_bundle=profile["publication_bundles"][-1],
        registration=profile["registration"]["file"], run_dir=profile["run_directory"],
        extra_inputs=[profile["last_extras"]["file"], *profile["confirmation_manifests"]])


def next_plan(profile, day, input_bundle, current_extras, out):
    sessions, previous = _validate_profile(profile)
    if (day not in profile["window"][:46] or previous.get(day) != profile["last_source_day"]
            or day not in sessions[:-1]):
        raise ValueError("Only the exact next registered entry-comparison close may be planned")
    bundle, extras, out = Path(input_bundle).resolve(), Path(current_extras).resolve(), Path(out).resolve()
    own = _bundle(bundle, day)
    extras_ref = _extras(extras, day, profile["calendar"])
    prior_extras = _read(profile["last_extras"]["file"])
    if prior_extras.get("day") != previous[day]:
        raise ValueError("Confirmation requires the accurate previous exchange-session extras")
    if (profile["first_execution_day"] not in own["source_days_complete"]
            or any(date not in own["source_days_complete"] for date in profile["window"][:profile["window"].index(day)+1])):
        raise ValueError("The cumulative own source must retain the original first execution and all published days")
    from .leader_entry_comparison_forward import input_directories as declared
    roots = [Path(path) for path in declared(publications=profile["publications"],
        publication_bundles=[*profile["publication_bundles"], str(bundle)], latest_bundle=str(bundle),
        registration=profile["registration"]["file"], run_dir=profile["run_directory"],
        extra_inputs=[str(extras), profile["last_extras"]["file"], *profile["confirmation_manifests"]])]
    if any(out == root or root in out.parents or out in root.parents for root in roots):
        raise ValueError("Entry-comparison daily outputs must remain separate from inputs and accounts")
    tail = profile["window"].index(day) >= 40
    confirmation = out / "artifacts/confirmation/manifest.json"
    publication = out / "artifacts/publication/publication.json"
    repeat = lambda flag, values: [token for value in values for token in (flag, str(value))]
    common = ["--registration", profile["registration"]["file"]]
    steps = []
    if not tail:
        steps.append({"id": "entry_confirmation_inputs", "requires": [], "command": [
            "leader-entry-confirmation-inputs", "--input-bundle", str(bundle), *common,
            "--current-extras", str(extras), "--previous-extras", profile["last_extras"]["file"],
            "--out", str(confirmation.parent)], "expected_files": [str(confirmation)]})
    steps.append({"id": "entry_comparison_publication",
        "requires": [] if tail else ["entry_confirmation_inputs"], "command": [
            "leader-entry-comparison", "publish", "--input-bundle", str(bundle), *common,
            *([] if tail else ["--confirmation-manifest", str(confirmation)]),
            "--out", str(publication.parent)], "expected_files": [str(publication)]})
    steps.append({"id": "entry_comparison_forward", "requires": ["entry_comparison_publication"], "command": [
        "leader-entry-comparison-forward", *repeat("--publication", [*profile["publications"], publication]),
        *repeat("--publication-bundle", [*profile["publication_bundles"], bundle]), "--bundle", str(bundle), *common,
        "--run-dir", profile["run_directory"], "--through", day, "--result-json", str(out / "forward_result.json")],
        "expected_files": [str(out / "forward_result.json")]})
    return {"schema": PLAN_SCHEMA, "source_day": day, "execution_day": sessions[sessions.index(day)+1],
        "parent_profile_sha256": _hash(profile), "before_profile": deepcopy(profile),
        "source_refs": [_ref(bundle), extras_ref], "steps": steps, "consumer_due": True,
        "next_values": {"last_source_day": day, "publications": [*profile["publications"], str(publication)],
            "publication_bundles": [*profile["publication_bundles"], str(bundle)],
            "confirmation_manifests": [*profile["confirmation_manifests"], *([] if tail else [str(confirmation)])],
            "last_extras": extras_ref}, **FLAGS}


def _adopt(before, plan, out):
    return {**deepcopy(before), **deepcopy(plan["next_values"]),
        "operations": [*before["operations"], str(Path(out).resolve() / "receipt.json")]}


def validate_plan(plan, profile, day, input_bundle, current_extras, out):
    before = plan.get("before_profile")
    if not isinstance(before, dict) or _hash(before) != plan.get("parent_profile_sha256"):
        raise ValueError("The entry-comparison saved plan's original profile changed")
    if profile != before and profile != _adopt(before, plan, out):
        raise ValueError("Daily recovery requires the exact saved before/after profile")
    if plan != next_plan(before, day, input_bundle, current_extras, out):
        raise ValueError("A saved daily confirmation plan cannot change its source or command")
    return plan


def run_profile(profile, day, input_bundle, current_extras, out, *, run_step):
    out = Path(out).resolve()
    plan_file, receipt_file = out / "plan.json", out / "receipt.json"
    if plan_file.exists():
        plan = validate_plan(_read(plan_file), profile, day, input_bundle, current_extras, out)
    else:
        plan = next_plan(profile, day, input_bundle, current_extras, out)
        _atomic(plan_file, plan)
    before = plan["before_profile"]
    saved = _read(receipt_file) if receipt_file.exists() else {"schema": RESULT_SCHEMA,
        "day": day, "execution_day": plan["execution_day"], "plan": _ref(plan_file), "steps": {},
        "consumer_called": False, "consumer_required": True, "consumer_complete": False,
        "case_count": len(before["case_ids"]), "first_decision_day": before["first_decision_day"],
        "first_execution_day": before["first_execution_day"], "account_count": None,
        "research_accounts_created": None, "newly_committed_case_sessions": None, "metrics": None, **FLAGS}
    if saved.get("plan") != _ref(plan_file):
        raise ValueError("The saved daily observation no longer binds its exact plan")
    saved["consumer_complete"] = False
    for step in plan["steps"]:
        if any(saved["steps"].get(key, {}).get("status") != "completed" for key in step["requires"]):
            saved["steps"][step["id"]] = {"status": "blocked_by_prior_stage"}
        else:
            saved["steps"][step["id"]] = run_step(step)
            if step["id"] == "entry_comparison_forward":
                saved["consumer_called"] = True
        _atomic(receipt_file, saved)
    produced = saved["steps"].get("entry_comparison_publication", {}).get("status") == "completed"
    saved["producer_complete"] = produced
    next_profile = _adopt(before, plan, out) if produced else None
    if produced:
        saved.update(adopted_profile_sha256=_hash(next_profile),
            latest_publication=_ref(plan["next_values"]["publications"][-1]))
    saved["status"] = "blocked_entry_comparison_production" if not produced else "entry_publication_saved_consumer_unfinished"
    if produced and saved["steps"].get("entry_comparison_forward", {}).get("status") == "completed":
        result = _read(out / "forward_result.json")
        valid = (result.get("schema") == "leader_entry_comparison_forward_result_v1"
            and result.get("candidate_panel_only") is True
            and all(result.get(key) is False for key in ("execution_admitted", "selection_enabled", "strategy_validated")))
        complete = (valid and result.get("execution_progress_unknown") is False
            and result.get("case_count") == len(before["case_ids"])
            and len(result.get("cases", [])) == len(before["case_ids"])
            and {row.get("case_id") for row in result.get("cases", [])} == set(before["case_ids"])
            and all(row.get("last_processed_day") == day and not row.get("error")
                and not row.get("blocking_evidence") for row in result.get("cases", [])))
        saved.update(status="completed_exact_entry_comparison_research_day" if complete
            else "entry_publication_saved_consumer_blocked_or_pending", consumer_complete=complete,
            consumer_result=_ref(out / "forward_result.json"), consumer_status=result.get("status"),
            execution_progress_unknown=result.get("execution_progress_unknown", True),
            account_count=result.get("case_count"), research_accounts_created=result.get("research_accounts_created"),
            newly_committed_case_sessions=result.get("newly_committed_case_sessions"), cases=deepcopy(result.get("cases")))
    _atomic(receipt_file, saved)
    return {"result": saved, "next_profile": next_profile}
