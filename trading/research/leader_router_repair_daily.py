"""Daily continuation of the explicitly registered router runtime generation."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from ..contracts import MARKET_TZ
from .leader_known_identity_daily import _read, _ref, _hash, _atomic, _sessions, _stamp
from . import leader_type_router_runtime_repair as repair


SCHEMA = "leader_router_runtime_repair_daily_profile_v1"
PLAN_SCHEMA = "leader_router_runtime_repair_daily_plan_v1"
RESULT_SCHEMA = "leader_router_runtime_repair_daily_result_v1"
ROUTE_KIND = "leader_type_router_runtime_repair"
MUTABLE_PROFILE_KEYS = {"last_source_day", "panels", "base_publications", "publications", "operations", "consumer_result"}
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "user_account_mutated": False,
    "model_refit": False, "network_accessed": False}


def _checked(reference):
    if _ref(reference["file"]) != reference:
        raise ValueError("Saved repair daily registration or fixed reference changed")
    return _read(reference["file"])


def bootstrap_profile(*, registration, publication, panel, consumer_observation, current_source_day):
    """Adopt only a genuinely published and consumed first repaired D."""
    from . import leader_type_router_forward as core
    record = repair.registration(registration)
    pub, reg_ref = _read(publication), _ref(registration)
    marker = repair._marker(record, repair.paper._pin(registration))
    day, execution = record["first_decision_day"], record["first_execution_day"]
    if (current_source_day != day or pub.get("runtime_repair") != marker
            or pub.get("source_close_day") != day or pub.get("proposed_execution_day") != execution
            or repair.paper._pin(panel) != record["repair_source"]["inputs"]
            or {"path": pub.get("base_publication", {}).get("file"),
                "sha256": pub.get("base_publication", {}).get("sha256")}
                != record["repair_source"]["base_publication"]):
        raise ValueError("Adopt only the exact already published repaired first D/source")
    head = core.load_saved_head(record["repair_run_directory"])
    observed = _read(consumer_observation)
    if (head is None or head["binding"].get("registration") != repair.paper._pin(registration)
            or [p["file"] for p in head["publications"]] != [str(Path(publication).resolve())]
            or observed.get("runtime_repair") != marker
            or observed.get("schema") != "leader_type_router_daily_forward_result_v1"
            or observed.get("checkpoint") != str(Path(record["repair_run_directory"]) / "head.json")
            or observed.get("case_count") != 4 or observed.get("error")
            or any(c["state"].get("day") != day or c.get("error") or c.get("blocking_evidence")
                for c in head["cases"].values())):
        raise ValueError("Repair adoption requires its actually committed four-book first session")
    calendar = {"file": record["references"]["calendar"]["path"],
        "sha256": record["references"]["calendar"]["sha256"]}
    return {"schema": SCHEMA, "registration": reg_ref,
        "fixed_refs": [reg_ref, calendar, _ref(__file__)],
        "calendar": calendar, "repair_identity": record["repair_identity"],
        "original_registration": deepcopy(record["repair_evidence"]["original_registration"]),
        "first_decision_day": day, "first_execution_day": execution,
        "case_ids": list(record["case_ids"]), "run_directory": record["repair_run_directory"],
        "last_source_day": day, "panels": [str(Path(panel).resolve())],
        "base_publications": [pub["base_publication"]["file"]],
        "publications": [str(Path(publication).resolve())], "operations": [],
        "consumer_result": _ref(consumer_observation), **FLAGS}


def _validate_profile(profile):
    if profile.get("schema") != SCHEMA or any(profile.get(k) is not v for k, v in FLAGS.items()):
        raise ValueError("Exact saved repair daily profile required")
    for reference in profile["fixed_refs"]:
        _checked(reference)
    record = _checked(profile["registration"])
    if (record.get("schema") != repair.SCHEMA or record.get("repair_identity") != profile["repair_identity"]
            or record.get("repair_run_directory") != profile["run_directory"]
            or record.get("case_ids") != profile["case_ids"]
            or record.get("first_decision_day") != profile["first_decision_day"]
            or record.get("first_execution_day") != profile["first_execution_day"]
            or record.get("repair_evidence", {}).get("original_registration") != profile["original_registration"]
            or not len(profile["panels"]) == len(profile["base_publications"]) == len(profile["publications"])):
        raise ValueError("Repair daily route cannot replace its registration, original identity or continuous prefix")
    return _sessions(profile["calendar"]["file"])


def input_directories(profile):
    _validate_profile(profile)
    return sorted({profile["run_directory"], *[str(Path(r["file"]).parent) for r in profile["fixed_refs"]],
        *[str(Path(p).parent) for field in ("panels", "base_publications", "publications") for p in profile[field]]})


def next_plan(profile, day, panel, base_publication, out):
    sessions, previous = _validate_profile(profile)
    if day not in sessions or previous.get(day) != profile["last_source_day"]:
        raise ValueError("Repair daily source must be the exact next original exchange session")
    out, panel, base_publication = Path(out).resolve(), Path(panel).resolve(), Path(base_publication).resolve()
    repair._separate(out, [*input_directories(profile), panel.parent, base_publication.parent])
    base = _read(base_publication)
    execution = sessions[sessions.index(day) + 1]
    if (base.get("source_close_day") != day or base.get("proposed_execution_day") != execution
            or Path(base.get("source", {}).get("input_manifest", "")).resolve() != panel
            or base["source"].get("input_manifest_sha256") != _ref(panel)["sha256"]
            or _stamp(base["recorded_at"]).date().isoformat() != day
            or not _stamp(day + "T15:05:00+08:00") <= _stamp(base["recorded_at"])
                <= datetime.now(MARKET_TZ)):
        raise ValueError("Repair daily publisher requires the exact original eight-type D input/publication pair")
    publication = out / "artifacts/publication/publication.json"
    common = ["--registration", profile["registration"]["file"]]
    steps = [{"id": "router_repair_publication", "requires": [], "command": [
        "leader-type-router-runtime-repair", "publish", *common, "--inputs", str(panel),
        "--base-publication", str(base_publication), "--out", str(publication.parent)],
        "expected_files": [str(publication)]},
        {"id": "router_repair_forward", "requires": ["router_repair_publication"], "command": [
        "leader-type-router-runtime-repair", "forward", *common,
        *[token for p in [*profile["publications"], str(publication)] for token in ("--publication", p)],
        "--panel", str(panel), "--run-dir", profile["run_directory"], "--through", day,
        "--result-json", str(out / "forward_result.json")], "expected_files": [str(out / "forward_result.json")]}]
    return {"schema": PLAN_SCHEMA, "source_day": day, "execution_day": execution,
        "parent_profile_sha256": _hash(profile), "before_profile": deepcopy(profile),
        "source_refs": [_ref(panel), _ref(base_publication)], "steps": steps,
        "next_values": {"last_source_day": day, "panels": [*profile["panels"], str(panel)],
            "base_publications": [*profile["base_publications"], str(base_publication)],
            "publications": [*profile["publications"], str(publication)]}, **FLAGS}


def _adopt(before, plan, out, consumer_result=None):
    result = {**deepcopy(before), **deepcopy(plan["next_values"]),
        "operations": [*before["operations"], str(Path(out).resolve() / "receipt.json")]}
    if consumer_result is not None:
        result["consumer_result"] = consumer_result
    return result


def validate_plan(plan, profile, day, panel, base_publication, out):
    before = plan.get("before_profile")
    if not isinstance(before, dict) or _hash(before) != plan.get("parent_profile_sha256"):
        raise ValueError("Original repair daily profile changed")
    if profile != before:
        saved = _read(Path(out) / "receipt.json")
        if saved.get("adopted_profile_sha256") != _hash(profile):
            raise ValueError("Repair continuation requires its exact saved before/after profile")
    if plan != next_plan(before, day, panel, base_publication, out):
        raise ValueError("Saved repair daily source/command plan cannot change")
    return plan


def run_profile(profile, day, panel, base_publication, out, *, run_step):
    out = Path(out).resolve()
    plan_file, receipt_file = out / "plan.json", out / "receipt.json"
    plan = (validate_plan(_read(plan_file), profile, day, panel, base_publication, out)
        if plan_file.exists() else next_plan(profile, day, panel, base_publication, out))
    if not plan_file.exists():
        _atomic(plan_file, plan)
    saved = _read(receipt_file) if receipt_file.exists() else {"schema": RESULT_SCHEMA,
        "day": day, "plan": _ref(plan_file), "steps": {}, "case_count": 4, **FLAGS}
    if saved.get("plan") != _ref(plan_file):
        raise ValueError("Repair daily receipt lost its original immutable command plan")
    for step in plan["steps"]:
        saved["steps"][step["id"]] = ({"status": "blocked_by_prior_stage"}
            if any(saved["steps"].get(parent, {}).get("status") != "completed" for parent in step["requires"])
            else run_step(step))
        _atomic(receipt_file, saved)
    producer = saved["steps"]["router_repair_publication"]["status"] == "completed"
    complete, consumer = False, None
    if saved["steps"].get("router_repair_forward", {}).get("status") == "completed":
        consumer = _ref(out / "forward_result.json")
        observed = _read(consumer["file"])
        complete = (observed.get("case_count") == 4 and len(observed.get("cases", [])) == 4
            and {c.get("case_id") for c in observed["cases"]} == set(profile["case_ids"])
            and all(c.get("last_processed_day") == day and not c.get("error")
                and not c.get("blocking_evidence") for c in observed["cases"]))
    after = _adopt(plan["before_profile"], plan, out, consumer) if producer else None
    saved.update(status="completed_exact_repaired_router_day" if producer and complete else "partial_repaired_router_day",
        producer_complete=producer, consumer_required=True, consumer_complete=complete,
        daily_complete=bool(producer and complete), adopted_profile_sha256=_hash(after) if after else None,
        consumer_result=consumer, original_failed_route_rewritten=False)
    _atomic(receipt_file, saved)
    return {"result": saved, "next_profile": after}
