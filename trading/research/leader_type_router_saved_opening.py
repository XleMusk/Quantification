"""Read the registered router's saved evidence and bound research decisions.

This adapter never loads a strategy, derives an owner or advances Engine. A
publication's eight-type evidence is not a four-book target until a committed
session binds it to that book's actual post-opening state.
"""
from copy import deepcopy
from pathlib import Path
from stat import S_ISREG

from ..config import StrategyConfig
from ..contracts import PortfolioIntent
from ..published_close_router import PublishedCloseRouterEnvelope, RECEIPT_SCHEMA
from .saved_leader_account_intent import _day, _stamp, _weights
from .leader_daily_signals import _calendar_view


def declared_dependency(reference):
    """Retain a producer's hash declaration plus cheap file metadata, not IO."""
    path = Path(reference.get("path") or reference["file"]).resolve()
    value = {"path": str(path), "sha256": reference["sha256"],
        "verification": "declared_hash_and_stat_only_content_checked_by_original_consumer"}
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return {**value, "exists": False, "size_bytes": None, "mtime_ns": None}
    return {**value, "exists": S_ISREG(metadata.st_mode),
        "size_bytes": metadata.st_size, "mtime_ns": metadata.st_mtime_ns}


def _saved_repair(reg, reg_ref, run_dir, current):
    """Validate the saved new generation without invoking its source loader."""
    from . import leader_type_router_paper as paper
    from . import leader_type_router_runtime_repair as repair
    from .registered_research_saved_opening import _read, _boundary
    original_ref = reg["repair_evidence"]["original_registration"]
    original, original_ref = _read(original_ref["path"], original_ref)
    _boundary(original)
    registered, definition = _stamp(reg["registered_at"]), _stamp(reg["strategy_definition_registered_at"])
    first, execution = _day(reg["first_decision_day"]), _day(reg["first_execution_day"])
    replaced = {"schema", "registered_at", "code"}
    if (original.get("schema") != paper.REGISTRATION_SCHEMA
            or any(reg.get(k) != v for k, v in original.items() if k not in replaced)
            or original.get("registered_at") != reg["strategy_definition_registered_at"]
            or reg.get("after_close_registration") is not True
            or reg.get("not_original_preregistered_route_success") is not True
            or reg.get("repair_policy") != repair.POLICY
            or reg.get("repair_identity") != repair._identity(reg)
            or Path(reg["repair_run_directory"]).resolve() != Path(run_dir).resolve()
            or not definition < _stamp(first + "T15:00:00+08:00")
            or registered.date().isoformat() != first
            or not _stamp(first + "T15:05:00+08:00") <= registered
                < _stamp(execution + "T09:30:00+08:00") or registered > current):
        raise ValueError("Saved repair must retain its exact independent identity, frozen strategy and two actual clocks")
    repair._separate(run_dir, [reg["original_run_directory"],
        reg["original_failed_publication_directory"], Path(reg_ref["path"]).parent])
    return {"marker": repair._marker(reg, reg_ref), "original_ref": original_ref,
        "generation": "explicit_after_close_runtime_repair_original_registration_and_failure_unchanged"}


def facts(registration, publications, run_dir, observation, current, *, runtime_repair=False):
    from . import leader_type_router_paper as paper
    from . import leader_type_router_forward as core
    from .registered_research_saved_opening import _read, _file_ref, _ref, _hash, _boundary, _reference_equal
    reg, reg_ref = _read(registration)
    _boundary(reg)
    if runtime_repair:
        from .leader_type_router_runtime_repair import SCHEMA as reg_schema
    else:
        reg_schema = paper.REGISTRATION_SCHEMA
    if (reg.get("schema") != reg_schema or reg.get("method_id") != paper.METHOD_ID
            or reg.get("case_ids") != list(paper.CASE_IDS)
            or any(reg.get(k) is not v for k, v in paper.FLAGS.items())):
        raise ValueError("Exact registered four-book router method required")
    registered = _stamp(reg["registered_at"])
    if registered > current:
        raise ValueError("Router registration cannot be in the future")
    repair_facts = _saved_repair(reg, reg_ref, run_dir, current) if runtime_repair else None
    config = StrategyConfig(**reg["config"]).validate()
    cal = reg["references"]["calendar"]
    calendar, calendar_ref = _read(cal["path"], cal)
    year = int(calendar["year"])
    sessions, _, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
    first = _day(reg["first_decision_day"])
    if (status != "pinned_static_exchange_calendar" or first not in sessions
            or sessions.index(first)+1 >= len(sessions)
            or reg["first_execution_day"] != sessions[sessions.index(first)+1]):
        raise ValueError("Original router first D/E and pinned calendar required")
    deps = [reg_ref, calendar_ref]
    if repair_facts:
        deps.append(repair_facts["original_ref"])
    # Historical panels can exceed the bounded metadata reader. Their exact
    # hashes are producer declarations, validated by the original consumer;
    # display only tracks cheap stat changes and reads the small publication.
    declared = [declared_dependency(reg["references"]["anchor_inputs"])]
    anchor = reg["references"]["anchor_publication"]
    deps.append(_file_ref(anchor["path"], anchor))
    anchor_prefix = reg["anchor_source_prefix"]
    if not anchor_prefix or anchor_prefix[-1]["day"] >= first:
        raise ValueError("Saved router anchor source declaration must precede its first D")
    records = []
    for path in publications:
        pub, proof = _read(path)
        _boundary(pub)
        d, e, clock = _day(pub["source_close_day"]), _day(pub["proposed_execution_day"]), _stamp(pub["recorded_at"])
        if (pub.get("schema") != paper.PUBLICATION_SCHEMA or pub.get("method_id") != paper.METHOD_ID
                or not _reference_equal(pub.get("registration"), reg_ref)
                or [c["case_id"] for c in pub["cases"]] != list(paper.CASE_IDS)
                or any(c.get("target_weights") is not None or c.get("active_type") is not None
                       or c.get("account_binding_pending") is not True for c in pub["cases"])
                or d not in sessions or sessions.index(d)+1 >= len(sessions)
                or e != sessions[sessions.index(d)+1] or clock.date().isoformat() != d
                or not max(registered, _stamp(d+"T15:05:00+08:00")) <= clock
                    < _stamp(e+"T09:30:00+08:00") or clock > current
                or pub.get("publication_time_backfilled") is not False):
            raise ValueError("Saved router publication changed its exact D/E, actual clock or unbound four-book semantics")
        if repair_facts and (pub.get("runtime_repair") != repair_facts["marker"]
                or pub.get("publication_at") != pub["recorded_at"]):
            raise ValueError("Saved repair publication changed its exact runtime marker or actual publication clock")
        source = pub["source"]
        manifest_ref = {"path": source["input_manifest"], "sha256": source["input_manifest_sha256"]}
        prefix = pub["source_prefix"]
        proofs = source["source_proofs"]
        if (prefix.get("through") != d or prefix.get("sources") != proofs
                or not proofs or proofs[-1]["day"] != d
                or proofs[:len(anchor_prefix)] != anchor_prefix):
            raise ValueError("Saved router publication does not declare the exact anchored D source prefix")
        declared.append(declared_dependency(manifest_ref))
        base_ref = pub["base_publication"]
        base, base_ref = _read(base_ref.get("path") or base_ref["file"], base_ref)
        if repair_facts and d == first:
            original_source = base["source"]
            original_inputs = {"path": original_source["input_manifest"],
                "sha256": original_source["input_manifest_sha256"]}
            if (not _reference_equal(original_inputs, reg["repair_source"]["inputs"])
                    or not _reference_equal(base_ref, reg["repair_source"]["base_publication"])):
                raise ValueError("Saved repair first D must retain the original failed source pair")
        if (base.get("source_close_day") != d or base.get("proposed_execution_day") != e
                or not registered <= clock or _stamp(base["recorded_at"]) > clock):
            raise ValueError("Original eight-type publication binding changed")
        deps += [proof, base_ref]
        records.append({"record": pub, "ref": proof, "bundle": None, "bundle_ref": manifest_ref,
            "source_day": d, "execution_day": e, "published_at": clock.isoformat()})
    days = [r["source_day"] for r in records]
    if days != sessions[sessions.index(first):][:len(days)]:
        raise ValueError("Router publications require their exact first-D consecutive prefix")
    directory, head, seals = Path(run_dir).resolve(), None, {}
    head_path = directory / "head.json"
    head_dep = {"path": str(head_path), "exists": head_path.is_file()}
    if head_dep["exists"]:
        head = core.load_saved_head(directory)  # Saved journal/Ledger validation only.
        _, head_ref = _read(head_path); head_dep.update(head_ref); deps.append(head_ref)
        _boundary(head)
        binding = head["binding"]
        if (binding.get("method_id") != paper.METHOD_ID or binding.get("config") != config.to_dict()
                or binding.get("run_directory") != str(directory)
                or not _reference_equal(binding.get("registration"), reg_ref)
                or binding.get("first_decision_day") != first
                or len(head["publications"]) > len(records)
                or any(not _reference_equal(p, records[i]["ref"]) for i,p in enumerate(head["publications"]))
                or (head["publications"] and head["source_prefix"] !=
                    records[len(head["publications"])-1]["record"]["source_prefix"])):
            raise ValueError("Saved router account differs from its registered source prefix")
        for key, case in head["cases"].items():
            if not case["daily"]:
                continue
            seal = case["journal"][-1]
            reference = case["journal_files"][-1]
            _, proof = _read(reference["path"], reference)
            if (seal["post_state_sha256"] != _hash(case["state"])
                    or seal["row_sha256"] != _hash(case["daily"][-1])):
                raise ValueError("Router saved latest state and row differ from their seal")
            deps.append(proof); seals[key] = proof
    observed, observation_ref = None, None
    if observation is not None:
        observed, observation_ref = _read(observation); _boundary(observed); deps.append(observation_ref)
        if (observed.get("schema") not in {core.RESULT_SCHEMA, "leader_type_router_forward_command_failure_v1"}
                or observed.get("method_id", paper.METHOD_ID) != paper.METHOD_ID
                or (observed.get("checkpoint") is not None and Path(observed["checkpoint"]).resolve() != head_path)):
            raise ValueError("Saved router consumer observation belongs to another account")
        if repair_facts and observed.get("runtime_repair") != repair_facts["marker"]:
            raise ValueError("Saved repair consumer observation belongs to another runtime generation")
    result = {"registration": reg, "registration_ref": reg_ref, "calendar_ref": calendar_ref,
        "config": config.to_dict(), "case_ids": list(paper.CASE_IDS), "method_id": paper.METHOD_ID,
        "records": records, "head": head, "head_dependency": head_dep, "seals": seals,
        "observation": observed, "observation_ref": observation_ref,
        "dependencies": list({r["path"]: r for r in deps}.values()), "sessions": sessions,
        "declared_input_dependencies": list({r["path"]: r for r in declared}.values()),
        "source_day": days[-1] if days else None,
        "execution_day": records[-1]["execution_day"] if records else reg["first_execution_day"]}
    if repair_facts:
        result.update(runtime_repair=repair_facts["marker"], runtime_generation=repair_facts["generation"])
    return result


def saved_decision(source, facts, key):
    latest = facts["records"][-1] if facts["records"] else None
    return {"kind": None, "weights": None, "selected_decision": None,
        "active_type": None, "targets_account_bound": False,
        "reason": "router_D_evidence_needs_committed_post_opening_owner_decision" if latest else
                  "first_registered_publication_not_yet_saved",
        "source_day": source["source_day"], "execution_day": source["execution_day"],
        "published_at": latest["published_at"] if latest else None,
        "publication": latest["ref"] if latest else None}


def ready_decision(source, f, key):
    from .registered_research_saved_opening import _reference_equal
    if not f["records"]:
        return None, "first_registered_publication_not_yet_saved"
    if f["head"] is None:
        return None, "original_research_account_not_created_no_financial_or_quantity_evidence"
    case = f["head"]["cases"][key]
    if case.get("error") or case.get("blocking_evidence") or case.get("failed_attempt"):
        return None, "saved_case_has_unresolved_failure_or_blocking_evidence"
    observation = f["observation"]
    if observation and (observation.get("execution_progress_unknown") is True
            or observation.get("error") or "fail" in observation.get("status", "")):
        return None, "latest_external_consumer_observation_is_failed_or_unknown"
    if not case["daily"] or case["state"]["day"] != source["source_day"]:
        return None, "latest_publication_has_no_exact_D_committed_account"
    seal, row, state = case["journal"][-1], case["daily"][-1], case["state"]
    d, e = source["source_day"], source["execution_day"]
    if (seal["day"] != d or seal["execution_day"] != e
            or not _reference_equal(seal["publication"], f["records"][-1]["ref"])):
        return None, "router_latest_publication_has_not_been_consumed_by_this_book"
    envelope = PublishedCloseRouterEnvelope.from_dict(seal["envelope"])
    receipt = row.get("published_close_router")
    if (not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("envelope") != envelope.to_dict()
            or state.get("last_published_close_router") != receipt):
        raise ValueError("Saved router row lacks its exact bound post-opening receipt")
    bound = PortfolioIntent.from_dict(receipt["intent"])
    config = StrategyConfig(**f["config"]).validate()
    weights = _weights(state.get("pending_targets"), config)
    if (bound.context.to_dict() != case["context"] or bound.execution_not_before != e
            or envelope.source_day != d or envelope.execution_day != e
            or envelope.published_at != source["published_at"]
            or bound.kind.value not in {"FULL_TARGET", "NO_CHANGE"}
            or ((bound.kind.value == "NO_CHANGE") != (weights is None))
            or (weights is not None and dict(bound.weights) != weights)):
        raise ValueError("Saved router intent and exact D/E pending target disagree")
    if bound.kind.value == "FULL_TARGET" and state.get("pending_published_close_intent") != receipt:
        raise ValueError("Saved router FULL target lost its original pending published receipt")
    original_weights = deepcopy(weights)
    queued, remaining = deepcopy(state.get("pending_risk_exits")), []
    if queued is not None:
        if queued["decided_on"] != d or queued["execution_on"] != e or queued["context"] != case["context"]:
            raise ValueError("Saved router risk exits differ from their exact D/E book")
        held = {s for s,lots in state["positions"].items() if sum(l["qty"] for l in lots) > 0}
        remaining = sorted(held.intersection(queued["symbols"]))
        if weights is not None:
            for symbol in queued["symbols"]: weights[symbol] = 0
    return {"kind": bound.kind.value, "weights": weights, "original_weights": original_weights,
        "queued_exit": queued, "remaining_exit_symbols": remaining,
        "active_type": (state.get("leader_type_router") or {}).get("active_type"),
        "router_state": deepcopy(state.get("leader_type_router")),
        "source_known_at": seal["committed_at"]}, None


def quotes(source, facts, wanted):
    from .leader_eight_type_opening import _quotes
    # This explicit product step alone may read the saved own-D raw prices.
    return _quotes(facts["records"][-1]["record"]["source"], source["source_day"], wanted)
