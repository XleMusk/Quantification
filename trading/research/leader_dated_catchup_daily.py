"""Daily command recipe for the already registered four catch-up research books.

This module reuses the existing producers, real publication clock and shared
account runner. The caller supplies its existing supervised ``run_step``;
there is no scheduler, data acquisition, registration or account migration here.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from ..settings import DATA_ROOT, ROOT
from .leader_daily_signals import _calendar_view


SCHEMA = "registered_dated_catchup_daily_profile_v1"
PLAN_SCHEMA = "registered_dated_catchup_daily_plan_v1"
RESULT_SCHEMA = "registered_dated_catchup_daily_result_v1"
PAPER_REGISTRATION = ROOT / "outputs/leader_dated_catchup/20261017_paper_method_v1/registration.json"
IPO_EVIDENCE = ROOT / "outputs/ipo_qualification/20261015_301660/evidence.json"
MASTER_RUN = ROOT / "outputs/instrument_masters/20261008_120957_1b0e36f3/run.json"
RUN_DIRECTORY = DATA_ROOT / "research/leader_dated_catchup_forward/20261019_v1"
FLAGS = {"candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False,
    "user_account_mutated": False, "model_refit": False}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()).hexdigest()


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _sessions(calendar):
    payload = _read(calendar)
    year = payload["year"]
    sessions, previous, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("The registered fixed exchange calendar is required")
    return sessions, previous


def bootstrap_profile(*, paper_registration=PAPER_REGISTRATION, ipo_evidence=IPO_EVIDENCE,
        research_master_run=MASTER_RUN, run_dir=RUN_DIRECTORY):
    """Read existing registration/catalog metadata; do not invoke producers."""
    from . import leader_dated_catchup_paper as paper
    registration, paper_ref = paper._registered(paper_registration, datetime.now(MARKET_TZ))
    candidate_ref = registration["candidate_registration"]
    candidate = _read(candidate_ref["file"])
    catalog_ref = candidate["catalog_anchor"]
    if _ref(catalog_ref["file"]) != catalog_ref:
        raise ValueError("The original dated-identity anchor changed")
    catalog = _read(catalog_ref["file"])
    catalog_receipt = Path(catalog_ref["file"]).parent / "receipt.json"
    receipt = _read(catalog_receipt)
    if (catalog.get("schema") != "leader_dated_identity_catalog_v1"
            or receipt.get("catalog_sha256") != catalog_ref["sha256"]
            or catalog.get("catalog_file") != str(Path(catalog_ref["file"]).resolve())):
        raise ValueError("The actually persisted original identity catalog is required")
    anchor = catalog["anchor"]
    calendar, registry = anchor["calendar"], anchor["registry_file"]
    sessions, previous = _sessions(calendar)
    first = registration["first_decision_day"]
    prior = previous.get(first)
    if (first not in sessions or registration["first_execution_day"] != sessions[sessions.index(first) + 1]
            or not catalog.get("entries") or catalog["entries"][-1]["source_day"] != prior):
        raise ValueError("The registered first decision and exact prior catalog must agree")
    if not (paper.original._stamp(receipt["persisted_at"]) <= datetime.now(MARKET_TZ)
            < paper.original._stamp(first + "T15:00:00+08:00")):
        raise ValueError("Initial daily attachment must precede the original first decision close")
    fixed = [paper_ref, candidate_ref, catalog_ref, _ref(catalog_receipt),
        _ref(calendar), _ref(registry), _ref(ipo_evidence), _ref(research_master_run)]
    return {"schema": SCHEMA, "paper_registration": paper_ref,
        "candidate_registration": deepcopy(candidate_ref), "fixed_refs": fixed,
        "first_decision_day": first, "first_execution_day": registration["first_execution_day"],
        "case_ids": deepcopy(registration["case_ids"]), "last_source_day": prior,
        "calendar": _ref(calendar), "registry": str(Path(registry).resolve()),
        "catalog": deepcopy(catalog_ref), "ipo_evidence": str(Path(ipo_evidence).resolve()),
        "research_master_run": str(Path(research_master_run).resolve()),
        "run_directory": str(Path(run_dir).resolve()), "candidate_inputs": [], "publications": [],
        "operations": [], **FLAGS}


def _validate_profile(profile):
    if profile.get("schema") != SCHEMA or any(profile.get(k) is not v for k, v in FLAGS.items()):
        raise ValueError("The original research-only catch-up daily profile is required")
    for reference in [*profile["fixed_refs"], profile["catalog"]]:
        if _ref(reference["file"]) != reference:
            raise ValueError("Registered catch-up input or catalog changed")
    return _sessions(profile["calendar"]["file"])


def input_directories(profile):
    """Protect declared source packages without reading account/daily history."""
    paths = [profile["run_directory"], *[Path(r["file"]).resolve().parent for r in profile["fixed_refs"]],
        Path(profile["catalog"]["file"]).resolve().parent]
    candidate = _read(profile["candidate_registration"]["file"])
    paths.append(Path(candidate["base_panel"]["file"]).resolve().parent)
    manifest = _read(candidate["base_panel"]["file"])
    paths.extend(row["source_folder"] for row in manifest.get("source_proofs", [])
        if row.get("source_folder"))
    for row in manifest.get("preview_sources", []):
        paths.extend(Path(row[key]).resolve().parent for key in ("preview", "feature_file") if row.get(key))
    paths.extend(Path(p).resolve().parent for p in [*profile["candidate_inputs"], *profile["publications"]])
    return list(dict.fromkeys(str(Path(p).resolve()) for p in paths))


def _separate(out, profile, panel, analysis):
    roots = [Path(p) for p in input_directories(profile)]
    roots.extend(Path(p).resolve().parent for p in [panel, analysis,
        *[r["file"] for r in profile["fixed_refs"]], profile["catalog"]["file"],
        *profile["candidate_inputs"], *profile["publications"]])
    if any(out == root or root in out.parents or out in root.parents for root in roots):
        raise ValueError("Daily catch-up output must be separate from original inputs and books")


def next_plan(profile, day, panel, analysis, out):
    """Plan only one exact new close from saved eight-type/environment outputs."""
    sessions, previous = _validate_profile(profile)
    if (day not in sessions or previous[day] != profile["last_source_day"]
            or day < profile["first_decision_day"] or sessions.index(day) + 1 >= len(sessions)):
        raise ValueError("Only the exact next registered catch-up decision session can be planned")
    panel, analysis, out = Path(panel).resolve(), Path(analysis).resolve(), Path(out).resolve()
    _separate(out, profile, panel, analysis)
    manifest, observed = _read(panel), _read(analysis)
    if (not manifest.get("source_days") or manifest["source_days"][-1] != day
            or observed.get("day") != day
            or manifest.get("calendar_sha256") != profile["calendar"]["sha256"]):
        raise ValueError("Exact current saved panel, environment and registered calendar are required")
    folder = out / "artifacts"
    candidate_file = folder / "candidate/input.json"
    publication_file = folder / "publication/paper_plan.json"
    catalog_file = folder / "catalog/catalog.json"
    shared = ["--ipo-evidence", profile["ipo_evidence"],
        "--research-master-run", profile["research_master_run"]]
    repeat = lambda flag, values: [token for value in values for token in (flag, str(value))]
    steps = [
        {"id": "catchup_input", "requires": [], "command": ["leader-dated-catchup-inputs", "build",
            "--base-panel", str(panel), "--catalog", profile["catalog"]["file"], *shared,
            "--registration", profile["candidate_registration"]["file"], "--out", str(candidate_file.parent)],
            "expected_files": [str(candidate_file)]},
        {"id": "catchup_publication", "requires": ["catchup_input"], "command": [
            "leader-dated-catchup-paper", "publish",
            *repeat("--candidate-input", [*profile["candidate_inputs"], candidate_file]),
            "--panel", str(panel), *shared, "--registration", profile["paper_registration"]["file"],
            "--out", str(publication_file.parent)], "expected_files": [str(publication_file)]},
        {"id": "catchup_catalog", "requires": ["catchup_publication"], "command": [
            "leader-dated-identity", "--registry", profile["registry"],
            "--analysis", str(analysis), "--parent", profile["catalog"]["file"],
            "--out", str(catalog_file.parent)], "expected_files": [str(catalog_file), str(catalog_file.parent / "receipt.json")]},
    ]
    consumer_due = day >= profile["first_execution_day"]
    if consumer_due:
        if profile["first_execution_day"] not in manifest["source_days"]:
            raise ValueError("The registered first execution's real raw source is absent")
        steps.append({"id": "catchup_forward", "requires": ["catchup_publication"], "command": [
            "leader-dated-catchup-forward", *repeat("--publication", [*profile["publications"], publication_file]),
            "--panel", str(panel), *shared, "--registration", profile["paper_registration"]["file"],
            "--run-dir", profile["run_directory"], "--through", day,
            "--result-json", str(out / "forward_result.json")], "expected_files": [str(out / "forward_result.json")]})
    return {"schema": PLAN_SCHEMA, "source_day": day, "execution_day": sessions[sessions.index(day) + 1],
        "parent_profile_sha256": _hash(profile), "before_profile": deepcopy(profile),
        "source_refs": [_ref(panel), _ref(analysis)],
        "steps": steps, "consumer_due": consumer_due,
        "next_values": {"last_source_day": day, "catalog_file": str(catalog_file),
            "candidate_inputs": [*profile["candidate_inputs"], str(candidate_file)],
            "publications": [*profile["publications"], str(publication_file)]}, **FLAGS}


def validate_plan(plan, profile, day, panel, analysis, out):
    """Reconstruct the exact saved command plan for the existing recovery path."""
    before = plan.get("before_profile")
    if not isinstance(before, dict) or _hash(before) != plan.get("parent_profile_sha256"):
        raise ValueError("Catch-up plan's original profile changed")
    if profile != before:
        expected = {**deepcopy(before),
            **{key: value for key, value in plan["next_values"].items() if key != "catalog_file"},
            "catalog": _ref(plan["next_values"]["catalog_file"]),
            "operations": [*before["operations"], str(Path(out).resolve() / "receipt.json")]}
        if profile != expected:
            raise ValueError("Catch-up recovery profile differs from its exact saved before/after states")
    if plan != next_plan(before, day, panel, analysis, out):
        raise ValueError("An existing catch-up daily plan cannot change its source or command")
    return plan


def run_profile(profile, day, panel, analysis, out, *, run_step):
    """Save one recipe and use the caller's existing idempotent command runner."""
    out = Path(out).resolve()
    plan_file, receipt_file = out / "plan.json", out / "receipt.json"
    if plan_file.exists():
        plan = validate_plan(_read(plan_file), profile, day, panel, analysis, out)
        before = plan["before_profile"]
    else:
        before = profile
        proposed = next_plan(profile, day, panel, analysis, out)
        _atomic(plan_file, proposed)
        plan = proposed
    saved = _read(receipt_file) if receipt_file.exists() else {"schema": RESULT_SCHEMA,
        "day": day, "execution_day": plan["execution_day"], "plan": _ref(plan_file),
        "steps": {}, "consumer_called": False, "consumer_complete": False,
        "consumer_required": plan["consumer_due"], "case_count": len(before["case_ids"]),
        "first_decision_day": before["first_decision_day"], "first_execution_day": before["first_execution_day"],
        "account_count": None, "research_accounts_created": None,
        "newly_committed_case_sessions": None, "metrics": None, **FLAGS}
    if saved.get("plan") != _ref(plan_file):
        raise ValueError("Saved catch-up receipt no longer binds its original plan")
    saved["consumer_complete"] = False
    for step in plan["steps"]:
        if any(saved["steps"].get(parent, {}).get("status") != "completed" for parent in step["requires"]):
            saved["steps"][step["id"]] = {"status": "blocked_by_prior_stage"}
        else:
            saved["steps"][step["id"]] = run_step(step)
            if step["id"] == "catchup_forward":
                saved["consumer_called"] = True
        _atomic(receipt_file, saved)
    producers_complete = all(saved["steps"].get(key, {}).get("status") == "completed"
        for key in ("catchup_input", "catchup_publication", "catchup_catalog"))
    next_profile = None
    if producers_complete:
        next_profile = {**deepcopy(before),
            **{key: value for key, value in plan["next_values"].items() if key != "catalog_file"},
            "catalog": _ref(plan["next_values"]["catalog_file"]),
            "operations": [*before["operations"], str(receipt_file)]}
        saved.update(producer_complete=True, adopted_profile_sha256=_hash(next_profile),
            latest_publication=_ref(plan["next_values"]["publications"][-1]))
    else:
        saved["producer_complete"] = False
    if not producers_complete:
        saved["status"] = "blocked_catchup_production"
    elif not plan["consumer_due"]:
        saved["status"] = "published_first_decision_execution_source_not_yet_available"
    elif saved["steps"].get("catchup_forward", {}).get("status") != "completed":
        saved["status"] = "catchup_publication_saved_consumer_unfinished"
    else:
        result = _read(out / "forward_result.json")
        complete = (result.get("case_count") == len(before["case_ids"])
            and {row.get("case_id") for row in result.get("cases", [])} == set(before["case_ids"])
            and all(row.get("last_processed_day") == day and not row.get("error")
                and not row.get("blocking_evidence") for row in result.get("cases", [])))
        saved.update(status="completed_exact_catchup_research_day" if complete else "catchup_publication_saved_consumer_blocked_or_pending",
            consumer_complete=complete,
            consumer_result=_ref(out / "forward_result.json"),
            consumer_status=result.get("status"), account_count=result.get("case_count"),
            research_accounts_created=result.get("research_accounts_created"),
            newly_committed_case_sessions=result.get("newly_committed_case_sessions"),
            cases=deepcopy(result.get("cases")))
    _atomic(receipt_file, saved)
    return {"result": saved, "next_profile": next_profile}
