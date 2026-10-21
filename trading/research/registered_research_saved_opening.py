"""Project registered entry/seat files into the existing saved opening workflow.

Only saved metadata, sealed account traces and explicit publications are read.
An unbound rule/candidate list is evidence, never an invented account target.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import MARKET_TZ
from .native_saved_opening import FLAGS, SOURCE_BASIS, _hash, _ref, _source_path
from .saved_leader_account_intent import _day, _stamp, _weights
from .leader_daily_signals import _calendar_view

FAMILY = "registered_research"
ROUTER_KINDS = ("leader_type_router", "leader_type_router_runtime_repair")
ROUTE_KINDS = ("entry_comparison", "public_seat_static", "public_seat_append", *ROUTER_KINDS)
SOURCE_SCHEMA = "registered_research_saved_opening_source_v1"
SCHEMA = "saved_registered_research_account_intent_v1"
OPENING_SCHEMA = "registered_research_saved_account_opening_v1"


def _file_ref(path, expected=None):
    path = Path(path).resolve()
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Saved registered metadata exceeds its bounded reader")
    data = path.read_bytes()
    ref = {"path": str(path), "sha256": sha256(data).hexdigest()}
    if expected is not None and ref != _ref(expected):
        raise ValueError("Saved registered source bytes changed")
    return ref


def _read(path, expected=None):
    ref = _file_ref(path, expected)
    return json.loads(Path(ref["path"]).read_text(encoding="utf-8-sig")), ref


def _boundary(value):
    if (value.get("candidate_panel_only") is not True or any(value.get(k) is not False for k in
            ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("Registered research-only boundary changed")


def _definition(kind):
    if kind == "entry_comparison":
        from . import leader_entry_comparison_forward as core
        from . import leader_entry_comparison_publication as pub
        return core, pub.REGISTRATION_SCHEMA, pub.SCHEMA, list(pub.CASE_IDS), pub.METHOD_ID
    from . import seat_hypothesis_dated_inputs as inputs
    if kind == "public_seat_static":
        from . import seat_hypothesis_dated_forward as core
        schema = inputs.PUBLICATION_SCHEMA
    elif kind == "public_seat_append":
        from . import seat_append_forward as core
        from .seat_append_dated_inputs import PUBLICATION_SCHEMA as schema
    else:
        raise ValueError("Unknown registered research route")
    return core, core.REGISTRATION_SCHEMA, schema, list(inputs.ARMS), core.METHOD_ID


def _reference_equal(a, b):
    return isinstance(a, dict) and isinstance(b, dict) and _ref(a) == _ref(b)


def _facts(kind, registration, publications, run_dir, observation, current):
    if kind in ROUTER_KINDS:
        from .leader_type_router_saved_opening import facts
        return facts(registration, publications, run_dir, observation, current,
            runtime_repair=kind == "leader_type_router_runtime_repair")
    core, reg_schema, pub_schema, case_ids, method = _definition(kind)
    reg, reg_ref = _read(registration)
    _boundary(reg)
    if (reg.get("schema") != reg_schema or reg.get("method_id") != method
            or (reg.get("spec", {}).get("case_ids") if kind == "entry_comparison" else reg.get("arm_ids")) != case_ids):
        raise ValueError("Exact original route method and independent case identities required")
    config = StrategyConfig(**reg["config"]).validate()
    registered_at = _stamp(reg["registered_at"])
    if registered_at > current:
        raise ValueError("Future method registration cannot be consumed")
    calendar, calendar_ref = _read(reg["calendar"].get("path") or reg["calendar"]["file"], reg["calendar"])
    year = int(calendar["year"])
    sessions, _, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Original pinned exchange calendar required")
    first = _day(reg["first_decision_day"])
    if first not in sessions or sessions.index(first) + 1 >= len(sessions):
        raise ValueError("Original registered first D has no exact next session")
    deps = [reg_ref, calendar_ref]
    # These are original small contract pins, not a rerun of source assemblers.
    for key in ("implementation", "original_registration", "dated_input_method_registration"):
        if reg.get(key):
            proof = _file_ref(reg[key].get("path") or reg[key]["file"], reg[key]); deps.append(proof)
    records = []
    for path in publications:
        pub, proof = _read(path)
        _boundary(pub)
        if pub.get("schema") != pub_schema:
            raise ValueError("Publication belongs to another registered route")
        d = _day(pub["source_close_day"] if kind == "entry_comparison" else pub["decision_day"])
        e = _day(pub["proposed_execution_day"] if kind == "entry_comparison" else pub["execution_day"])
        clock = _stamp(pub["recorded_at"] if kind == "entry_comparison" else pub["published_at"])
        expected_reg = reg_ref if kind == "entry_comparison" else reg[
            "original_registration" if kind == "public_seat_static" else "dated_input_method_registration"]
        if (not _reference_equal(pub.get("registration"), expected_reg) or d not in sessions
                or sessions.index(d) + 1 >= len(sessions) or sessions[sessions.index(d) + 1] != e
                or clock.date().isoformat() != d or clock < registered_at
                or not _stamp(d + "T15:05:00+08:00") <= clock < _stamp(e + "T09:30:00+08:00")
                or clock > current):
            raise ValueError("Publication must retain its original method, actual D clock and exact E")
        bundle_key = "input_bundle" if kind == "entry_comparison" else "bundle"
        bundle, bundle_ref = _read(pub[bundle_key].get("path") or pub[bundle_key]["file"], pub[bundle_key])
        deps += [proof, bundle_ref]
        if kind == "entry_comparison":
            from .leader_entry_comparison_publication import COMMON5KEYS
            if (pub.get("method_id") != method or [r["case_id"] for r in pub["base_cases"]] != list(COMMON5KEYS)
                    or pub.get("case_count_after_account_application") != 15
                    or d not in bundle["source_days_complete"]):
                raise ValueError("Entry publication must preserve the original five unbound rules")
            if pub.get("confirmationproof"):
                cp = pub["confirmationproof"]["manifest"]
                _, extra_ref = _read(cp.get("path") or cp["file"], cp); deps.append(extra_ref)
        elif (bundle.get("decision_day") != d or bundle.get("next_execution_day") != e
                or not _reference_equal(bundle.get("calendar"), calendar_ref)
                or bundle.get("config") != reg["config"]
                or [r["arm_id"] for r in bundle["arms"]] != case_ids):
            raise ValueError("Saved seat bundle changed dates, four arms or config")
        records.append({"record": pub, "ref": proof, "bundle": bundle, "bundle_ref": bundle_ref,
                        "source_day": d, "execution_day": e, "published_at": clock.isoformat()})
    days = [p["source_day"] for p in records]
    allowed = reg.get("window") if kind == "entry_comparison" else sessions[sessions.index(first):]
    if days != list(allowed[:len(days)]):
        raise ValueError("Publications must be the exact original consecutive prefix, never mtime sorted or skipped")
    directory = Path(run_dir).resolve()
    head_path = directory / "head.json"
    head_dep = {"path": str(head_path), "exists": head_path.is_file()}
    head, seals = None, {}
    if head_dep["exists"]:
        head, head_ref = _read(head_path); deps.append(head_ref); head_dep.update(head_ref)
        _boundary(head)
        binding = head["binding"]
        if (head.get("schema") != core.SCHEMA or list(head.get("cases", {})) != case_ids
                or binding.get("method_id") != method or binding.get("config") != config.to_dict()
                or binding.get("run_directory") != str(directory)
                or not _reference_equal(binding.get("registration"), reg_ref)):
            raise ValueError("Original route account method, config, registration or cases changed")
        if kind == "entry_comparison":
            prefix = head["publications"]
            if len(prefix) > len(records) or any(not _reference_equal(p, records[i]["ref"]) for i, p in enumerate(prefix)):
                raise ValueError("Entry account consumed publication prefix changed")
            for key, case in head["cases"].items():
                if case["daily"]:
                    journal = case["journal"][-1]
                    seal, proof = _read(case["journal_files"][-1]["file"], case["journal_files"][-1])
                    Path(proof["path"]).relative_to(directory / "journal")
                    from .leader_entry_confirmation_policy import POLICY_IDS
                    from .leader_published_forward_run import RISK_POLICY_ID
                    if (len(case["daily"]) != len(case["journal"]) or len(case["journal"]) != len(case["journal_files"])
                            or seal != journal or seal["post_session_state_sha256"] != _hash(case["state"])
                            or seal["post_session_daily_row_sha256"] != _hash(case["daily"][-1])):
                        raise ValueError("Original entry saved state and latest independent seal differ")
                    if (case["state"].get("published_close_target_policy") != {
                            "schema": "replay_published_close_target_policy_v1", "context": case["context"],
                            "policy_id": POLICY_IDS[key.split(":")[1]]}
                            or case["state"].get("replay_risk_exit_policy") != {
                            "schema": "replay_risk_exit_policy_v1", "context": case["context"], "policy_id": RISK_POLICY_ID}):
                        raise ValueError("Entry account persisted target or risk policy differs from its original arm")
                    deps.append(proof); seals[key] = proof
        else:
            prefix = head["consumed_publications"]
            if not prefix or len(prefix) > len(records) or len(head["journal_files"]) != len(prefix):
                raise ValueError("Seat head requires its complete original publication and journal prefix")
            for i, p in enumerate(prefix):
                if (not _reference_equal(p["publication"], records[i]["ref"])
                        or not _reference_equal(p["bundle"], records[i]["bundle_ref"])):
                    raise ValueError("Seat account consumed publication prefix changed")
            journal, proof = _read(head["journal_files"][-1]["file"], head["journal_files"][-1])
            Path(proof["path"]).relative_to(directory / "journal")
            if (journal.get("schema") != core.JOURNAL_SCHEMA or journal["binding_sha256"] != _hash(binding)
                    or journal["publication"] != prefix[-1]
                    or journal["raw_sources_sha256"] != _hash(head["raw_sources"])):
                raise ValueError("Seat latest original session seal changed")
            deps.append(proof)
            for key, case in head["cases"].items():
                if journal["post_case_sha256"][key] != _hash(case):
                    raise ValueError("Seat case differs from its four-book seal")
                seals[key] = proof
        for key, case in head["cases"].items():
            if (case["context"] != core._context(directory, key).to_dict() or case["state"].get("broker_binding")
                    or case["state"]["day"] != (case["daily"][-1]["date"] if case["daily"] else None)):
                raise ValueError("Saved account context or exact Ledger date changed")
    observed, observation_ref = None, None
    if observation is not None:
        observed, observation_ref = _read(observation); deps.append(observation_ref)
        _boundary(observed)
        failure_schema = {"entry_comparison": "leader_entry_comparison_forward_command_failure_v1",
            "public_seat_static": "public_seat_dated_forward_command_failure_v1",
            "public_seat_append": "public_seat_append_forward_command_failure_v1"}[kind]
        if (observed.get("schema") not in {core.RESULT_SCHEMA, failure_schema}
                or (observed.get("registration") is not None and not _reference_equal(observed["registration"], reg_ref))
                or (observed.get("method_id") is not None and observed["method_id"] != method)
                or (observed.get("run_directory") is not None and Path(observed["run_directory"]).resolve() != directory)
                or (observed.get("checkpoint") is not None and Path(observed["checkpoint"]).resolve() != head_path)):
            raise ValueError("Consumer observation belongs to another route or account")
    return {"registration": reg, "registration_ref": reg_ref, "calendar_ref": calendar_ref,
        "config": config.to_dict(), "case_ids": case_ids, "method_id": method, "records": records,
        "head": head, "head_dependency": head_dep, "seals": seals,
        "observation": observed, "observation_ref": observation_ref,
        "dependencies": list({r["path"]: r for r in deps}.values()), "sessions": sessions,
        "source_day": days[-1] if days else None,
        "execution_day": records[-1]["execution_day"] if records else sessions[sessions.index(first)+1]}


def source_dependencies_current(source_index):
    """Read fixed small dependencies; a newly created account invalidates no-head."""
    try:
        source, _ = _read(_source_path(source_index))
        if source.get("schema") != SOURCE_SCHEMA:
            return False
        dep = source["head_dependency"]
        if Path(dep["path"]).is_file() != dep["exists"]:
            return False
        for ref in source["dependencies"]:
            _file_ref(ref["path"], ref)
        if source.get("declared_input_dependencies"):
            from .leader_type_router_saved_opening import declared_dependency
            if any(declared_dependency(ref) != ref for ref in source["declared_input_dependencies"]):
                return False
        return True
    except (ValueError, OSError, KeyError, TypeError):
        return False


def build_source(route_kind, registration, publications, run_dir, out, *, consumer_observation=None, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    directory, original = Path(out).resolve(), Path(run_dir).resolve()
    protected = [original, Path(registration).resolve().parent]
    protected += [Path(p).resolve().parent for p in publications]
    if consumer_observation is not None:
        protected.append(Path(consumer_observation).resolve().parent)
    if any(directory == p or directory in p.parents or p in directory.parents for p in protected):
        raise ValueError("Composite source must stay outside original accounts and inputs")
    f = _facts(route_kind, registration, publications, original, consumer_observation, current)
    latest = f["records"][-1] if f["records"] else None
    source = {"schema": SOURCE_SCHEMA, "source_family": FAMILY, "route_kind": route_kind,
        "status": "saved_registered_research_sources", "case_ids": f["case_ids"],
        "run_directory": str(directory), "original_run_directory": str(original),
        "source_day": f["source_day"], "execution_day": f["execution_day"],
        "first_decision_day": f["registration"]["first_decision_day"],
        "published_at": latest["published_at"] if latest else None,
        "source_known_at": current.isoformat(), "execution_config": f["config"],
        "method_id": f["method_id"], "account_count": (sum(bool(c["daily"]) for c in f["head"]["cases"].values())
            if route_kind in ROUTER_KINDS and f["head"] else len(f["head"]["cases"]) if f["head"] else 0),
        "head_dependency": f["head_dependency"], "dependencies": f["dependencies"],
        "refs": {"registration": f["registration_ref"], "publications": [r["ref"] for r in f["records"]],
                 "publication": latest["ref"] if latest else None, "original_head":
                 {k:v for k,v in f["head_dependency"].items() if k != "exists"} if f["head"] else None,
                 "consumer_observation": f["observation_ref"]},
        "created_at": current.isoformat(), **FLAGS}
    if route_kind in ROUTER_KINDS:
        source["declared_input_dependencies"] = deepcopy(f["declared_input_dependencies"])
    if route_kind == "leader_type_router_runtime_repair":
        source["runtime_repair"] = deepcopy(f["runtime_repair"])
        source["runtime_generation"] = f["runtime_generation"]
    source["input_id"] = _hash({k:v for k,v in source.items() if k not in {"created_at", "source_known_at"}})
    target = directory / "head.json"
    if target.exists():
        previous, _ = _read(target)
        if previous["input_id"] == source["input_id"]:
            return previous
        if any(previous.get(k) != source[k] for k in ("schema", "route_kind", "original_run_directory", "method_id")):
            raise ValueError("Composite source cannot replace its registered route or account")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "head.tmp.json"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(source, stream, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(temporary, target)
    return source


def _load(source_index, current):
    path = _source_path(source_index)
    source, ref = _read(path)
    if (source.get("schema") != SOURCE_SCHEMA or source.get("source_family") != FAMILY
            or source.get("run_directory") != str(path.parent)
            or any(source.get(k) is not v for k,v in FLAGS.items())
            or not source_dependencies_current(path)):
        raise ValueError("Registered composite source is stale, corrupt or not research-only")
    refs = source["refs"]
    f = _facts(source["route_kind"], refs["registration"]["path"],
        [r["path"] for r in refs["publications"]], source["original_run_directory"],
        refs["consumer_observation"]["path"] if refs["consumer_observation"] else None, current)
    if (source["dependencies"] != f["dependencies"] or source["head_dependency"] != f["head_dependency"]
            or source.get("declared_input_dependencies", []) != f.get("declared_input_dependencies", [])
            or source["case_ids"] != f["case_ids"] or source["source_day"] != f["source_day"]
            or source["execution_day"] != f["execution_day"] or source["execution_config"] != f["config"]
            or source["method_id"] != f["method_id"]
            or source.get("runtime_repair") != f.get("runtime_repair")
            or source.get("runtime_generation") != f.get("runtime_generation")
            or source["input_id"] != _hash({k:v for k,v in source.items() if k not in {"input_id", "created_at", "source_known_at"}})
            or not _stamp(source["source_known_at"]) <= _stamp(source["created_at"]) <= current):
        raise ValueError("Composite source metadata differs from exact registered originals")
    return source, ref, f


def _saved_decision(source, facts, key):
    if source["route_kind"] in ROUTER_KINDS:
        from .leader_type_router_saved_opening import saved_decision
        return saved_decision(source, facts, key)
    latest = facts["records"][-1] if facts["records"] else None
    if latest is None:
        return {"kind": None, "weights": None, "reason": "first_registered_publication_not_yet_saved",
                "selected_decision": None, "publication": None}
    common = {"source_day": source["source_day"], "execution_day": source["execution_day"],
        "published_at": latest["published_at"], "publication": latest["ref"]}
    if source["route_kind"] == "entry_comparison":
        family, arm = key.split(":")
        original = next(r for r in latest["record"]["base_cases"] if r["family"] == family)
        return {**common, "kind": None, "weights": None, "selected_decision": None,
            "reason": "original_five_family_rule_is_unbound_until_actual_D_post_opening_arm_application",
            "published_original_rule": {"family": family, "arm": arm,
                "portfolio_intent": deepcopy(original["portfolio_intent"]),
                "original_candidates": deepcopy(original.get("original_candidates", [])),
                "holdings_and_arm_application_account_bound": False}}
    original = next(r for r in latest["bundle"]["arms"] if r["arm_id"] == key)
    kind = original["decision_kind"]
    if kind not in {"NO_CHANGE", "RANKED_CANDIDATES"} or original.get("weights") is not None:
        raise ValueError("Saved seat decision changed its original unbound candidate semantics")
    return {**common, "kind": kind, "weights": None, "selected_decision": None,
        "reason": original.get("reason"), "ranked_candidates": deepcopy(original.get("ranked_candidates", [])),
        "targets_account_bound": False}


def _ready_decision(source, f, key):
    if source["route_kind"] in ROUTER_KINDS:
        from .leader_type_router_saved_opening import ready_decision
        return ready_decision(source, f, key)
    if not f["records"]:
        return None, "first_registered_publication_not_yet_saved"
    if f["head"] is None:
        return None, "original_research_account_not_created_no_financial_or_quantity_evidence"
    case = f["head"]["cases"][key]
    if case.get("error") or case.get("blocking_evidence") or case.get("failed_attempt"):
        return None, "saved_case_has_unresolved_failure_or_blocking_evidence"
    observation = f["observation"]
    if observation and (observation.get("execution_progress_unknown") is True or observation.get("error")
            or observation.get("blocking_evidence") or "fail" in observation.get("status", "")
            or "blocked" in observation.get("status", "")):
        return None, "latest_external_consumer_observation_is_failed_blocked_or_unknown"
    if not case["daily"] or case["state"]["day"] != source["source_day"]:
        return None, "latest_publication_has_no_exact_D_committed_account"
    d, e = source["source_day"], source["execution_day"]
    config = StrategyConfig(**f["config"]).validate()
    queued, remaining = None, []
    if source["route_kind"] == "entry_comparison":
        seal = case["journal"][-1]
        if (seal["decision_day"] != d or seal["execution_day"] != e
                or not _reference_equal(seal["publication"], f["records"][-1]["ref"])):
            return None, "entry_latest_publication_has_not_been_applied_to_this_arm"
        reduced = (seal.get("published_close_reduction") or {}).get("reduced_intent")
        bound = reduced or seal["bound_original_intent"]
        weights = _weights(case["state"].get("pending_targets"), config)
        original_weights = deepcopy(weights)
        kind = bound["kind"]
        if kind not in {"FULL_TARGET", "NO_CHANGE"} or ((kind == "NO_CHANGE") != (weights is None)):
            raise ValueError("Saved effective entry decision and original queue disagree")
        if kind == "FULL_TARGET" and bound["weights"] != weights:
            raise ValueError("Saved effective entry weights changed after the sealed reduction")
        queued = deepcopy(case["state"].get("pending_risk_exits"))
        if queued is not None:
            if (queued["decided_on"] != d or queued["execution_on"] != e or queued["context"] != case["context"]):
                raise ValueError("Saved risk exits differ from this exact D/E account")
            held = {s for s,lots in case["state"]["positions"].items() if sum(l["qty"] for l in lots)>0}
            remaining = sorted(held.intersection(queued["symbols"]))
            if weights is not None:
                for symbol in queued["symbols"]: weights[symbol] = 0
        return {"kind": kind, "weights": weights, "original_weights": original_weights, "queued_exit": queued,
            "remaining_exit_symbols": remaining, "source_known_at": seal["committed_at"]}, None
    saved = _saved_decision(source, f, key)
    if saved["kind"] != "NO_CHANGE":
        return None, "ranked_candidates_have_no_saved_account_bound_next_targets"
    seal, _ = _read(f["seals"][key]["path"], f["seals"][key])
    return {"kind": "NO_CHANGE", "weights": None, "original_weights": None, "queued_exit": None,
        "remaining_exit_symbols": [], "source_known_at": max(
            _stamp(source["published_at"]), _stamp(seal["committed_at"])).isoformat()}, None


def export_requests(source_index, execution_day=None, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    source, ref, f = _load(source_index, current)
    if execution_day is not None and _day(execution_day) != source["execution_day"]:
        raise ValueError("Registered opening must keep its exact published E")
    result = {}
    for key in source["case_ids"]:
        saved = _saved_decision(source, f, key)
        original = saved.get("published_original_rule")
        published_input = None if not f["records"] else {
            "source_day": source["source_day"], "execution_day": source["execution_day"],
            "published_at": saved["published_at"], "decision_kind":
                "UNBOUND_FAMILY_RULE" if original else saved["kind"],
            "reason": saved.get("reason"), "candidate_symbols":
                [r["symbol"] for r in original["original_candidates"]] if original else
                [r["symbol"] for r in saved.get("ranked_candidates", [])],
            "target_weights": None, "publication": deepcopy(saved["publication"])}
        ready, reason = _ready_decision(source, f, key)
        base = {"schema": SCHEMA, "source_family": FAMILY, "route_kind": source["route_kind"],
            "case_id": key, "strategy_id": source["route_kind"] + ":" + key,
            "run_directory": source["run_directory"], "original_run_directory": source["original_run_directory"],
            "source_day": source["source_day"], "execution_day": source["execution_day"],
            "saved_decision": saved, "refs": {"head": ref}, "created_at": current.isoformat(),
            "published_input": published_input,
            "source_basis": SOURCE_BASIS, "signal_publication_verified": False,
            "execution_config": deepcopy(source["execution_config"]), **FLAGS}
        if source["route_kind"] in ROUTER_KINDS:
            # The publication's all-type evidence cannot manufacture a selected
            # owner. Only an actual committed saved state can supply it.
            case = f["head"]["cases"][key] if f["head"] else None
            owner = case["state"].get("leader_type_router") if case and case["daily"] else None
            base.update(active_type=(owner or {}).get("active_type"), router_state=deepcopy(owner))
            if published_input is not None:
                published_input["decision_kind"] = "UNBOUND_ROUTING_EVIDENCE"
        if source["route_kind"] == "leader_type_router_runtime_repair":
            base["runtime_repair"] = deepcopy(source["runtime_repair"])
        if ready is None:
            result[key] = {**base, "status": "not_ready", "reason": reason,
                "kind": None, "weights": None, "cash_cents": None, "equity_cents": None,
                "target_version": None, "context": None}
        else:
            case = f["head"]["cases"][key]
            known = max(_stamp(ready["source_known_at"]), _stamp(source["published_at"]))
            if not _stamp(source["source_day"]+"T15:00:00+08:00") <= known < _stamp(source["execution_day"]+"T09:30:00+08:00") or known > current:
                raise ValueError("Saved account decision was not known before its exact E open")
            result[key] = {**base, **ready, "source_known_at": known.isoformat(),
                "account_scope": "whole_observed_account",
                "target_version": len(case["daily"]), "target_version_basis": "saved_original_session_count",
                "context": deepcopy(case["context"])}
    return result


def read_request(payload, *, current=None):
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("Exact registered saved-account intent required")
    current = _stamp(current or datetime.now(MARKET_TZ))
    expected = export_requests(payload["refs"]["head"]["path"], payload["execution_day"], current=current)[payload["case_id"]]
    expected["created_at"] = payload["created_at"]
    if (expected.get("status") == "not_ready" or expected != payload
            or not _stamp(expected["source_known_at"]) <= _stamp(payload["created_at"]) <= current):
        raise ValueError("Registered saved intent is not an exact ready account decision")
    return expected


def load_saved_case(source_index, case_id, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    source, source_ref, f = _load(source_index, current)
    if case_id not in source["case_ids"]:
        raise KeyError("Unknown original registered case")
    if f["head"] is None or not f["head"]["cases"][case_id]["daily"]:
        raise ValueError("Original registered account has no committed Ledger")
    case = f["head"]["cases"][case_id]
    refs = {"head": source_ref, "original_head": _ref(f["head_dependency"]),
            "latest_journal": f["seals"][case_id]}
    return {"state": deepcopy(case["state"]), "daily": deepcopy(case["daily"][-1]),
        "context": deepcopy(case["context"]), "config": deepcopy(f["config"]),
        "ledger_day": case["state"]["day"], "executed_decision": None,
        "executed_decision_reason": "executed_decision_not_reconstructed_from_next_publication",
        "refs": refs, "basis": "exact_registered_original_book_and_latest_independent_session_seal", **FLAGS}


def _quotes(source, f, wanted):
    """Explicit product creation alone reads the saved exact-D raw close."""
    if source["route_kind"] in ROUTER_KINDS:
        from .leader_type_router_saved_opening import quotes
        return quotes(source, f, wanted)
    from .leader_eight_type_opening import _quote_rows
    latest = f["records"][-1]
    if source["route_kind"] == "entry_comparison":
        import pyarrow.parquet as pq
        bundle = latest["bundle"]
        raw = Path(bundle["raw_bars_file"]).resolve()
        raw.relative_to(Path(latest["bundle_ref"]["path"]).parent)
        if sha256(raw.read_bytes()).hexdigest() != bundle["raw_bars_sha256"]:
            raise ValueError("Saved entry own original raw close changed")
        names = pq.read_schema(raw).names
        required = {"date", "close", "tradestatus", "isST"}
        if not required <= set(names) or not {"symbol", "code"}.intersection(names):
            raise ValueError("Saved entry own raw quote columns are unknown")
        columns = [k for k in ("date", "symbol", "code", "close", "tradestatus", "isST") if k in names]
        rows = pq.read_table(raw, columns=columns, filters=[("date", "==", source["source_day"])]).to_pylist()
        for row in rows:
            for key in set(bundle["json_encoded_object_columns"]).intersection(columns):
                row[key] = json.loads(row[key])
        return _quote_rows(rows, source["source_day"], wanted)
    import csv
    raw_ref = latest["bundle"]["D_primary"]["raw"]
    raw = Path(raw_ref.get("path") or raw_ref["file"])
    if sha256(raw.read_bytes()).hexdigest() != raw_ref["sha256"]:
        raise ValueError("Saved seat original D raw close changed")
    with raw.open(encoding="utf-8-sig", newline="") as stream:
        return _quote_rows(list(csv.DictReader(stream)), source["source_day"], wanted)


def build_opening(source_index, out, *, current=None):
    from .leader_eight_type_opening import _case_preview, _write
    from . import opening_pack
    current = _stamp(current or datetime.now(MARKET_TZ))
    source, source_ref, f = _load(source_index, current)
    directory = Path(out).resolve()
    protected = [Path(source["run_directory"]), Path(source["original_run_directory"])]
    protected += [Path(r["path"]).parent for r in source["dependencies"]]
    if directory.exists() or any(directory == p or directory in p.parents or p in directory.parents for p in protected):
        raise ValueError("Use a new opening output outside original registered sources and accounts")
    intents = export_requests(source_index, current=current)
    wanted = {s for intent in intents.values() if intent.get("status") != "not_ready" for s in (intent["weights"] or {})}
    if f["head"]:
        wanted.update(s for key,intent in intents.items() if intent.get("status") != "not_ready"
            for s,lots in f["head"]["cases"][key]["state"]["positions"].items() if sum(l["qty"] for l in lots)>0)
    quotes = _quotes(source, f, wanted) if wanted else {}
    directory.mkdir(parents=True, exist_ok=False)
    rows = []
    for key, intent in intents.items():
        if intent.get("status") == "not_ready":
            rows.append(deepcopy(intent)); continue
        child = "case_"+sha256(key.encode()).hexdigest()[:24]
        reconciliation_source = {"run_directory": source["original_run_directory"],
            "publication": f["records"][-1]["ref"], "latest_journal": f["seals"][key]}
        row = _case_preview(directory/child, intent, f["head"]["cases"][key], quotes,
            current=current, reconciliation_source=reconciliation_source)
        owner = ({"active_type": intent["active_type"], "router_state": deepcopy(intent["router_state"])}
            if source["route_kind"] in ROUTER_KINDS else {})
        rows.append({**row, **owner, "strategy_id": intent["strategy_id"], "saved_decision": deepcopy(intent["saved_decision"])})
    if not source_dependencies_current(source_index) or _read(_source_path(source_index))[1] != source_ref:
        raise ValueError("Registered originals changed before product publication")
    previews = [r["preview"] for r in rows if r["status"] == "paper_preview_only"]
    pack_dir, pack = opening_pack.build(previews, execution_day=source["execution_day"]) if previews else (None,None)
    result = {"schema": OPENING_SCHEMA, "source_family": FAMILY, "route_kind": source["route_kind"],
        "status": "paper_candidates_only" if previews else "not_ready", "generated_at": current.isoformat(),
        "source_run_directory": source["run_directory"], "source_head_sha256": source_ref["sha256"],
        "source_cutoff": source["source_day"], "execution_day": source["execution_day"],
        "case_count": len(rows), "preview_count": len(previews), "not_ready_count": len(rows)-len(previews),
        "account_count": source["account_count"], "cases": rows,
        "opening_pack": str(pack_dir/"opening_pack.json") if pack_dir else None,
        "candidate_order_count": pack["candidate_order_count"] if pack else None,
        "independent_cases_combined_into_account": False, "automatic_strategy_selection": False,
        "candidate_orders_are_fills": False, **FLAGS}
    if source["route_kind"] == "leader_type_router_runtime_repair":
        result["runtime_repair"] = deepcopy(source["runtime_repair"])
    _write(directory/"result.json", result)
    return directory, result
