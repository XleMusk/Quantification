"""Attach the registered four-book router to the existing daily supervisor.

Only the original eight-type producer builds source inputs. This adapter binds
its saved D publication and invokes the registered publisher/consumer; status
and profile inspection never execute them.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from ..contracts import MARKET_TZ
from ..settings import DATA_ROOT
from .leader_known_identity_daily import _read, _ref, _hash, _atomic, _sessions, _stamp, _panel_directories


SCHEMA = "registered_leader_type_router_daily_profile_v1"
PLAN_SCHEMA = "registered_leader_type_router_daily_plan_v1"
RESULT_SCHEMA = "registered_leader_type_router_daily_result_v1"
REGISTRATION = DATA_ROOT / "research/leader_type_router_paper/20261019_v1_registration/registration.json"
RUN_DIRECTORY = DATA_ROOT / "research/leader_type_router_forward/20261019_v1"
MUTABLE_PROFILE_KEYS = {"last_source_day", "panels", "base_publications", "publications", "operations"}
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "user_account_mutated": False,
    "model_refit": False, "network_accessed": False}


def _normal(reference):
    return {"file": str(Path(reference.get("file") or reference["path"]).resolve()),
            "sha256": reference["sha256"]}


def _checked(reference):
    expected = _normal(reference)
    if _ref(expected["file"]) != expected:
        raise ValueError("Saved router daily reference changed")
    return _read(expected["file"])


def bootstrap_profile(current_source_day="2026-10-16", *, registration=REGISTRATION,
                      run_dir=RUN_DIRECTORY):
    """Adopt an existing registration and anchors without creating an account."""
    from . import leader_type_router_paper as paper
    record = _read(registration)
    if (record.get("schema") != paper.REGISTRATION_SCHEMA or record.get("method_id") != paper.METHOD_ID
            or record.get("case_ids") != list(paper.CASE_IDS)
            or any(record.get(k) is not v for k, v in paper.FLAGS.items())):
        raise ValueError("Exact registered four-case research-only router required")
    references = record["references"]
    calendar = _normal(references["calendar"])
    sessions, previous = _sessions(calendar["file"])
    first = record["first_decision_day"]
    prior = previous.get(first)
    if (prior != current_source_day or first not in sessions
            or sessions.index(first) + 1 >= len(sessions)
            or record["first_execution_day"] != sessions[sessions.index(first) + 1]):
        raise ValueError("Router attachment must retain its original prior D and first D/E")
    current = datetime.now(MARKET_TZ)
    if not _stamp(record["registered_at"]) <= current < _stamp(first + "T15:00:00+08:00"):
        raise ValueError("Initial router attachment must precede its registered first decision close")
    anchor = _normal(references["anchor_inputs"])
    base = _normal(references["anchor_publication"])
    manifest, publication = _checked(anchor), _checked(base)
    if (not manifest.get("source_days") or manifest["source_days"][-1] != prior
            or manifest.get("calendar_sha256") != calendar["sha256"]
            or publication.get("source_close_day") != prior
            or _stamp(publication["recorded_at"]) > current):
        raise ValueError("Original router anchor inputs and actual eight-type publication disagree")
    fixed = [_ref(registration), *[_normal(r) for r in references.values()]]
    return {"schema": SCHEMA, "registration": _ref(registration), "fixed_refs": fixed,
        "calendar": calendar, "anchor_input": anchor, "anchor_base_publication": base,
        "first_decision_day": first, "first_execution_day": record["first_execution_day"],
        "case_ids": list(record["case_ids"]), "run_directory": str(Path(run_dir).resolve()),
        "last_source_day": prior, "panels": [], "base_publications": [], "publications": [],
        "operations": [], **FLAGS}


def _validate_profile(profile):
    if profile.get("schema") != SCHEMA or any(profile.get(k) is not v for k, v in FLAGS.items()):
        raise ValueError("The registered research-only router daily profile is required")
    for reference in profile["fixed_refs"]:
        _checked(reference)
    registration = _checked(profile["registration"])
    references = registration["references"]
    if (profile["case_ids"] != registration["case_ids"]
            or profile["calendar"] != _normal(references["calendar"])
            or profile["anchor_input"] != _normal(references["anchor_inputs"])
            or profile["anchor_base_publication"] != _normal(references["anchor_publication"])
            or profile["first_decision_day"] != registration["first_decision_day"]
            or profile["first_execution_day"] != registration["first_execution_day"]):
        raise ValueError("Router profile changed its original method or anchor identity")
    if not (len(profile["panels"]) == len(profile["publications"]) == len(profile["base_publications"])):
        raise ValueError("Router source and publication prefixes must stay aligned")
    return _sessions(profile["calendar"]["file"])


def input_directories(profile):
    _validate_profile(profile)
    paths = [Path(r["file"]).parent for r in profile["fixed_refs"]]
    paths += [Path(profile["run_directory"])]
    for file in [profile["anchor_input"]["file"], *profile["panels"]]:
        paths += _panel_directories(file)
    paths += [Path(p).parent for p in profile["publications"] + profile["base_publications"]]
    return sorted(set(paths))


def next_plan(profile, day, panel, base_publication, out):
    sessions, previous = _validate_profile(profile)
    if (day not in sessions or previous[day] != profile["last_source_day"]
            or day < profile["first_decision_day"] or sessions.index(day) + 1 >= len(sessions)):
        raise ValueError("Only the exact next registered router D may be planned")
    panel, base_publication, out = Path(panel).resolve(), Path(base_publication).resolve(), Path(out).resolve()
    roots = [*input_directories(profile), panel.parent, base_publication.parent]
    if any(out == p or p in out.parents or out in p.parents for p in roots):
        raise ValueError("Daily router outputs must remain separate from original inputs and books")
    manifest, base = _read(panel), _read(base_publication)
    execution = sessions[sessions.index(day) + 1]
    if (not manifest.get("source_days") or manifest["source_days"][-1] != day
            or manifest.get("calendar_sha256") != profile["calendar"]["sha256"]
            or base.get("source_close_day") != day or base.get("proposed_execution_day") != execution):
        raise ValueError("Exact D input and actual original eight-type D/E publication required")
    clock = _stamp(base["recorded_at"])
    if (clock.date().isoformat() != day or clock > datetime.now(MARKET_TZ)
            or not _stamp(day + "T15:00:00+08:00") <= clock < _stamp(execution + "T09:30:00+08:00")):
        raise ValueError("The original eight-type publication needs its real same-D close clock")
    publication = out / "artifacts/publication/publication.json"
    common = ["--registration", profile["registration"]["file"]]
    steps = [{"id": "leader_type_router_publication", "requires": [], "command": [
        "leader-type-router-paper", "publish", *common, "--inputs", str(panel),
        "--base-publication", str(base_publication), "--out", str(publication.parent)],
        "expected_files": [str(publication)]}]
    # The explicit published-close router consumes D raw and queues E. It does
    # not wait for E raw like the older preopen consumers.
    due = day >= profile["first_decision_day"]
    if due:
        repeat = [token for p in [*profile["publications"], str(publication)] for token in ("--publication", p)]
        steps.append({"id": "leader_type_router_forward", "requires": ["leader_type_router_publication"],
            "command": ["leader-type-router-forward", "run", *common, *repeat,
                "--panel", str(panel), "--run-dir", profile["run_directory"], "--through", day,
                "--result-json", str(out / "forward_result.json")],
            "expected_files": [str(out / "forward_result.json")]})
    return {"schema": PLAN_SCHEMA, "source_day": day, "execution_day": execution,
        "parent_profile_sha256": _hash(profile), "before_profile": deepcopy(profile),
        "source_refs": [_ref(panel), _ref(base_publication)], "steps": steps, "consumer_due": due,
        "next_values": {"last_source_day": day, "panels": [*profile["panels"], str(panel)],
            "base_publications": [*profile["base_publications"], str(base_publication)],
            "publications": [*profile["publications"], str(publication)]}, **FLAGS}


def _adopt(before, plan, out):
    return {**deepcopy(before), **deepcopy(plan["next_values"]),
        "operations": [*before["operations"], str(Path(out).resolve() / "receipt.json")]}


def validate_plan(plan, profile, day, panel, base_publication, out):
    before = plan.get("before_profile")
    if not isinstance(before, dict) or _hash(before) != plan.get("parent_profile_sha256"):
        raise ValueError("Router plan's original profile changed")
    if profile != before and profile != _adopt(before, plan, out):
        raise ValueError("Router recovery differs from its exact saved before/after profile")
    if plan != next_plan(before, day, panel, base_publication, out):
        raise ValueError("Saved router plan cannot change source or command")
    return plan


def run_profile(profile, day, panel, base_publication, out, *, run_step):
    out = Path(out).resolve()
    plan_file, receipt_file = out / "plan.json", out / "receipt.json"
    plan = (validate_plan(_read(plan_file), profile, day, panel, base_publication, out)
            if plan_file.exists() else next_plan(profile, day, panel, base_publication, out))
    if not plan_file.exists():
        _atomic(plan_file, plan)
    before = plan["before_profile"]
    saved = _read(receipt_file) if receipt_file.exists() else {"schema": RESULT_SCHEMA,
        "day": day, "execution_day": plan["execution_day"], "plan": _ref(plan_file), "steps": {},
        "consumer_called": False, "consumer_required": plan["consumer_due"], "consumer_complete": False,
        "case_count": len(before["case_ids"]), "account_count": None, "metrics": None, **FLAGS}
    if saved.get("plan") != _ref(plan_file):
        raise ValueError("Saved router receipt differs from its original plan")
    saved["consumer_complete"] = False
    for step in plan["steps"]:
        saved["steps"][step["id"]] = ({"status": "blocked_by_prior_stage"}
            if any(saved["steps"].get(k, {}).get("status") != "completed" for k in step["requires"])
            else run_step(step))
        if step["id"] == "leader_type_router_forward" and saved["steps"][step["id"]]["status"] != "blocked_by_prior_stage":
            saved["consumer_called"] = True
        _atomic(receipt_file, saved)
    complete = saved["steps"]["leader_type_router_publication"]["status"] == "completed"
    saved["producer_complete"] = complete
    after = _adopt(before, plan, out) if complete else None
    if complete:
        saved["latest_publication"] = _ref(plan["next_values"]["publications"][-1])
    finished = False
    if plan["consumer_due"] and saved["steps"].get("leader_type_router_forward", {}).get("status") == "completed":
        observed = _read(out / "forward_result.json")
        cases = observed.get("cases", [])
        finished = (observed.get("case_count") == len(before["case_ids"])
            and len(cases) == len(before["case_ids"])
            and {c.get("case_id") for c in cases} == set(before["case_ids"])
            and all(c.get("last_processed_day") == day and not c.get("error")
                    and not c.get("blocking_evidence") for c in cases))
        saved.update(consumer_complete=finished, consumer_result=_ref(out / "forward_result.json"),
            consumer_status=observed.get("status"), account_count=observed.get("case_count"), cases=deepcopy(cases))
    saved["daily_complete"] = bool(complete and (not plan["consumer_due"] or finished))
    saved["status"] = ("completed_exact_router_research_day" if saved["daily_complete"] else
        "router_publication_saved_consumer_unfinished" if complete else "blocked_router_publication")
    _atomic(receipt_file, saved)
    return {"result": saved, "next_profile": after}
