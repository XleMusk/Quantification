"""Daily recipe for the separately registered residual-exit leader books.

The original daily producer owns the candidates and immutable input panel.
This recipe only wraps that actual publication with the new method and uses
the caller's supervised command runner. It never builds candidates or sources.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from ..settings import DATA_ROOT, ROOT
from .leader_daily_signals import _calendar_view


SCHEMA = "registered_eight_type_residual_daily_profile_v1"
PLAN_SCHEMA = "registered_eight_type_residual_daily_plan_v1"
RESULT_SCHEMA = "registered_eight_type_residual_daily_result_v1"
PAPER_REGISTRATION = ROOT / "outputs/leader_residual_exit/20261018_v1/registration/registration.json"
IPO_EVIDENCE = ROOT / "outputs/ipo_qualification/20261015_301660/evidence.json"
MASTER_RUN = ROOT / "outputs/instrument_masters/20261008_120957_1b0e36f3/run.json"
RUN_DIRECTORY = DATA_ROOT / "research/leader_eight_type_residual_forward/20261019_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "user_account_mutated": False, "model_refit": False}


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


def _sessions(calendar):
    payload = _read(calendar)
    year = payload["year"]
    sessions, previous, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("The registered fixed exchange calendar is required")
    return sessions, previous


def _stamp(value):
    result = datetime.fromisoformat(str(value))
    if result.utcoffset() is None:
        raise ValueError("Actual publication and registration clocks require a timezone")
    return result


def bootstrap_profile(*, paper_registration=PAPER_REGISTRATION, ipo_evidence=IPO_EVIDENCE,
        research_master_run=MASTER_RUN, run_dir=RUN_DIRECTORY):
    """Read the existing registration only; do not register or create books."""
    from . import leader_eight_type_residual_paper as paper
    now = datetime.now(MARKET_TZ)
    registration, reference = paper._registered(paper_registration, now)
    anchor_pub = registration["anchor_base_publication"]
    anchor_input = registration["anchor_input"]
    calendar = registration["calendar"]
    for value in (reference, anchor_pub, anchor_input, calendar):
        if _ref(value["file"]) != value:
            raise ValueError("Registered residual-exit anchor or calendar changed")
    sessions, previous = _sessions(calendar["file"])
    first = registration["first_decision_day"]
    prior = previous.get(first)
    publication, manifest = _read(anchor_pub["file"]), _read(anchor_input["file"])
    if (first not in sessions or sessions.index(first) + 1 >= len(sessions)
            or registration["first_execution_day"] != sessions[sessions.index(first) + 1]
            or publication.get("source_close_day") != prior
            or not manifest.get("source_days") or manifest["source_days"][-1] != prior
            or manifest.get("calendar_sha256") != calendar["sha256"]):
        raise ValueError("Original first decision, exact prior source and calendar must agree")
    if not (_stamp(registration["registered_at"]) <= now
            < _stamp(first + "T15:00:00+08:00")):
        raise ValueError("Initial daily attachment must precede the original first decision close")
    fixed = [reference, anchor_pub, anchor_input, calendar, _ref(ipo_evidence), _ref(research_master_run)]
    return {"schema": SCHEMA, "paper_registration": reference, "fixed_refs": fixed,
        "anchor_input": deepcopy(anchor_input), "anchor_base_publication": deepcopy(anchor_pub),
        "calendar": deepcopy(calendar), "first_decision_day": first,
        "first_execution_day": registration["first_execution_day"],
        "case_ids": deepcopy(registration["case_ids"]), "last_source_day": prior,
        "ipo_evidence": str(Path(ipo_evidence).resolve()),
        "research_master_run": str(Path(research_master_run).resolve()),
        "run_directory": str(Path(run_dir).resolve()), "panels": [],
        "base_publications": [], "publications": [], "operations": [], **FLAGS}


def _validate_profile(profile):
    if profile.get("schema") != SCHEMA or any(profile.get(k) is not v for k, v in FLAGS.items()):
        raise ValueError("The original research-only residual-exit daily profile is required")
    for reference in profile["fixed_refs"]:
        if _ref(reference["file"]) != reference:
            raise ValueError("Registered residual-exit input changed")
    return _sessions(profile["calendar"]["file"])


def _panel_directories(path):
    manifest = _read(path)
    paths = [Path(path).resolve().parent]
    paths.extend(Path(manifest[key]).resolve().parent for key in
        ("raw_file", "calendar", "protocol_file") if manifest.get(key))
    if manifest.get("history_snapshot"):
        paths.append(Path(manifest["history_snapshot"]).resolve())
    paths.extend(Path(row["source_folder"]).resolve() for row in manifest.get("source_proofs", [])
        if row.get("source_folder"))
    for row in manifest.get("preview_sources", []):
        paths.extend(Path(row[key]).resolve().parent for key in ("preview", "feature_file") if row.get(key))
    for key in ("identity_report", "industry_source"):
        row = manifest.get(key, {})
        paths.extend(Path(row[field]).resolve().parent for field in ("file", "registry", "source_file", "receipt_file")
            if row.get(field))
    for row in manifest.get("turnover_sources", []):
        proof = row.get("sourceproof", {})
        paths.extend(Path(proof[field]).resolve().parent for field in ("manifest_file", "extras_result") if proof.get(field))
    return paths


def input_directories(profile):
    """Protect the registered sources, saved publications and separate books."""
    paths = [Path(profile["run_directory"]).resolve(),
        *[Path(row["file"]).resolve().parent for row in profile["fixed_refs"]]]
    # Each new manifest is already cumulative. Its declared roots cover the
    # entire source prefix; rereading every old large manifest adds no proof.
    latest_panel = profile["panels"][-1] if profile["panels"] else profile["anchor_input"]["file"]
    paths.extend(_panel_directories(latest_panel))
    paths.extend(Path(path).resolve().parent for path in
        [*profile["base_publications"], *profile["publications"]])
    return list(dict.fromkeys(str(path) for path in paths))


def _separate(out, profile, panel, base_publication):
    roots = [Path(path) for path in input_directories(profile)]
    roots.extend(_panel_directories(panel))
    roots.append(base_publication.parent)
    if any(out == root or root in out.parents or out in root.parents for root in roots):
        raise ValueError("Daily residual-exit output must be separate from original inputs and books")


def next_plan(profile, day, panel, base_publication, out):
    """Wrap exactly one new original close publication; never rebuild its signal."""
    sessions, previous = _validate_profile(profile)
    if (day not in sessions or previous[day] != profile["last_source_day"]
            or day < profile["first_decision_day"] or sessions.index(day) + 1 >= len(sessions)):
        raise ValueError("Only the exact next registered residual-exit decision session can be planned")
    panel, base_publication, out = Path(panel).resolve(), Path(base_publication).resolve(), Path(out).resolve()
    _separate(out, profile, panel, base_publication)
    manifest, original = _read(panel), _read(base_publication)
    execution = sessions[sessions.index(day) + 1]
    source = original.get("source", {})
    if (not manifest.get("source_days") or manifest["source_days"][-1] != day
            or manifest.get("calendar_sha256") != profile["calendar"]["sha256"]
            or original.get("source_close_day") != day or original.get("proposed_execution_day") != execution
            or str(Path(source.get("input_manifest", "")).resolve()) != str(panel)
            or source.get("input_manifest_sha256") != _ref(panel)["sha256"]):
        raise ValueError("Exact current original publication, its own input and registered calendar are required")
    published = _stamp(original["recorded_at"])
    if not (_stamp(day + "T15:00:00+08:00") <= published <= datetime.now(MARKET_TZ)
            and published.date().isoformat() == day
            and published < _stamp(execution + "T09:30:00+08:00")):
        raise ValueError("The source publication must have its real same-day close clock")
    publication_file = out / "artifacts/publication/paper_plan.json"
    repeat = lambda flag, values: [token for value in values for token in (flag, str(value))]
    common = ["--panel", str(panel), "--ipo-evidence", profile["ipo_evidence"],
        "--research-master-run", profile["research_master_run"],
        "--registration", profile["paper_registration"]["file"]]
    steps = [{"id": "residual_publication", "requires": [], "command": [
        "leader-eight-type-residual", "publish", "--base-publication", str(base_publication),
        *common, "--out", str(publication_file.parent)], "expected_files": [str(publication_file)]}]
    consumer_due = day >= profile["first_execution_day"]
    if consumer_due:
        if profile["first_execution_day"] not in manifest["source_days"]:
            raise ValueError("The registered first execution's real raw source is absent")
        steps.append({"id": "residual_forward", "requires": ["residual_publication"], "command": [
            "leader-eight-type-residual", "forward",
            *repeat("--publication", [*profile["publications"], publication_file]), *common,
            "--run-dir", profile["run_directory"], "--through", day,
            "--result-json", str(out / "forward_result.json")], "expected_files": [str(out / "forward_result.json")]})
    return {"schema": PLAN_SCHEMA, "source_day": day, "execution_day": execution,
        "parent_profile_sha256": _hash(profile), "before_profile": deepcopy(profile),
        "source_refs": [_ref(panel), _ref(base_publication)], "steps": steps,
        "consumer_due": consumer_due, "next_values": {"last_source_day": day,
            "panels": [*profile["panels"], str(panel)],
            "base_publications": [*profile["base_publications"], str(base_publication)],
            "publications": [*profile["publications"], str(publication_file)]}, **FLAGS}


def _adopt(before, plan, out):
    return {**deepcopy(before), **deepcopy(plan["next_values"]),
        "operations": [*before["operations"], str(Path(out).resolve() / "receipt.json")]}


def validate_plan(plan, profile, day, panel, base_publication, out):
    before = plan.get("before_profile")
    if not isinstance(before, dict) or _hash(before) != plan.get("parent_profile_sha256"):
        raise ValueError("Residual-exit plan's original profile changed")
    if profile != before and profile != _adopt(before, plan, out):
        raise ValueError("Residual-exit recovery differs from its exact saved before/after profiles")
    if plan != next_plan(before, day, panel, base_publication, out):
        raise ValueError("An existing residual-exit plan cannot change its source or command")
    return plan


def run_profile(profile, day, panel, base_publication, out, *, run_step):
    """Use only the enclosing daily driver's supervised, idempotent child runner."""
    out = Path(out).resolve()
    plan_file, receipt_file = out / "plan.json", out / "receipt.json"
    if plan_file.exists():
        plan = validate_plan(_read(plan_file), profile, day, panel, base_publication, out)
    else:
        plan = next_plan(profile, day, panel, base_publication, out)
        _atomic(plan_file, plan)
    before = plan["before_profile"]
    saved = _read(receipt_file) if receipt_file.exists() else {"schema": RESULT_SCHEMA,
        "day": day, "execution_day": plan["execution_day"], "plan": _ref(plan_file),
        "steps": {}, "consumer_called": False, "consumer_complete": False,
        "consumer_required": plan["consumer_due"], "case_count": len(before["case_ids"]),
        "first_decision_day": before["first_decision_day"], "first_execution_day": before["first_execution_day"],
        "account_count": None, "research_accounts_created": None,
        "newly_committed_case_sessions": None, "metrics": None, **FLAGS}
    if saved.get("plan") != _ref(plan_file):
        raise ValueError("Saved residual-exit receipt no longer binds its original plan")
    saved["consumer_complete"] = False
    for step in plan["steps"]:
        if any(saved["steps"].get(key, {}).get("status") != "completed" for key in step["requires"]):
            saved["steps"][step["id"]] = {"status": "blocked_by_prior_stage"}
        else:
            saved["steps"][step["id"]] = run_step(step)
            if step["id"] == "residual_forward":
                saved["consumer_called"] = True
        _atomic(receipt_file, saved)
    complete = saved["steps"].get("residual_publication", {}).get("status") == "completed"
    saved["producer_complete"] = complete
    next_profile = _adopt(before, plan, out) if complete else None
    if complete:
        saved.update(adopted_profile_sha256=_hash(next_profile),
            latest_publication=_ref(plan["next_values"]["publications"][-1]))
    if not complete:
        saved["status"] = "blocked_residual_publication"
    elif not plan["consumer_due"]:
        saved["status"] = "published_first_decision_execution_source_not_yet_available"
    elif saved["steps"].get("residual_forward", {}).get("status") != "completed":
        saved["status"] = "residual_publication_saved_consumer_unfinished"
    else:
        result = _read(out / "forward_result.json")
        finished = (result.get("case_count") == len(before["case_ids"])
            and len(result.get("cases", [])) == len(before["case_ids"])
            and {row.get("case_id") for row in result.get("cases", [])} == set(before["case_ids"])
            and all(row.get("last_processed_day") == day and not row.get("error")
                and not row.get("blocking_evidence") for row in result.get("cases", [])))
        saved.update(status="completed_exact_residual_research_day" if finished
            else "residual_publication_saved_consumer_blocked_or_pending", consumer_complete=finished,
            consumer_result=_ref(out / "forward_result.json"), consumer_status=result.get("status"),
            account_count=result.get("case_count"), research_accounts_created=result.get("research_accounts_created"),
            newly_committed_case_sessions=result.get("newly_committed_case_sessions"),
            cases=deepcopy(result.get("cases")))
    _atomic(receipt_file, saved)
    return {"result": saved, "next_profile": next_profile}
