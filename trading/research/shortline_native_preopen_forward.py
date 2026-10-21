"""Continue two isolated native-model research books through the shared Engine.

Only already published plans and strictly completed dated closes are read. A
successor close is an execution-quality witness, never a new ranking input.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd

from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..engine import advance
from ..ledger import Ledger, new_state
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .baostock_full_close import load_complete_close
from .exchange_calendar import load_calendar
from .ipo_qualification import load_ipo_qualification
from .leader_daily_signals import _calendar_view
from .leader_forward_run import ForwardRawFeed
from .provider_close_batches import _acquisition_times, _inventory
from .shortline_decision_v3 import _known_raw
from .shortline_native_preopen import (MODEL_KINDS, POLICY_ID, QUALIFIED_POLICY_ID, SPEC as PLAN_SPEC,
    bind_preopen_plan, load_preopen_plan, policy_spec)
from .shortline_panel_replay import _config
from .research_corporate_action_view import (KNOWLEDGE_MODES as ACTION_KNOWLEDGE_MODES,
    STATE_KEY as ACTION_STATE_KEY, load_research_corporate_action_view)


SCHEMA = "native_shortline_preopen_shared_ledger_checkpoint_v1"
RESULT_SCHEMA = "native_shortline_preopen_forward_result_v1"
JOURNAL_SCHEMA = "native_shortline_preopen_committed_session_v1"
METHOD_ID = "native_shortline_two_model_preopen_shared_engine_v1"
IPO_APPEND_METHOD_ID = "native_shortline_two_model_preopen_ipo_append_only_shared_engine_v1"
ACTION_METHOD_ID = "native_shortline_two_model_preopen_fixed_action_components_shared_engine_v1"
IPO_APPEND_ACTION_METHOD_ID = "native_shortline_two_model_preopen_ipo_append_fixed_action_components_shared_engine_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "current_signal_admitted": False, "model_refit": False, "network_accessed": False,
    "user_account_mutated": False, "broker_interacted": False, "ths_payload_generated": False,
    "pit_verified": False, "corporate_actions_verified": False}
RULE_SPEC = {"method_id": METHOD_ID, "policy_id": POLICY_ID, "models": list(MODEL_KINDS),
    "plan_spec": PLAN_SPEC, "mode": "replay", "holdings_count_is_not_session_expiry": True,
    "session_execution": "original_Engine_advance_preopen_intent_opening_path",
    "commit": "both_model_trials_then_immutable_whole_session_journal_then_atomic_single_checkpoint",
    "reference_policy": "related_E_original_preclose_numerically_equals_exact_D_original_close_or_unknown",
    "source_day_prices_used_for_ranking": False, "successor_used_for_ranking_or_refill": False,
    "corporate_action_coverage": "unknown_no_component_opt_in_no_reference_cash_imputation",
    "metrics_policy": "null_accounting_only_not_verified_total_return"}


def forward_rule_spec(policy_id, *, ipo_append_only=False, action_components=False):
    """Keep the original rule bytes; explicitly bind the qualified variant."""
    spec = policy_spec(policy_id)
    if ipo_append_only and policy_id != QUALIFIED_POLICY_ID:
        raise ValueError("IPO append-only inception is available only for the qualified policy")
    if type(action_components) is not bool:
        raise ValueError("Fixed action components must be explicitly enabled or absent")
    result = (deepcopy(RULE_SPEC) if policy_id == POLICY_ID else
        {**deepcopy(RULE_SPEC), "policy_id": policy_id, "plan_spec": spec,
            "stage_membership_qualification": "only_known_no_limit_raw_unknown_exclusions_at_each_original_registration",
            "original_raw_scope_relabelled_complete": False,
            "qualification_sources": "one_fixed_verification_and_input_reference_set_for_the_entire_book"})
    if ipo_append_only:
        result.update(method_id=IPO_APPEND_METHOD_ID,
            qualification_sources="inception_anchor_then_each_original_plan_own_append_only_listing_bundle",
            qualification_append="identical_legacy_base_master_calendar_and_immutable_source_record_prefix",
            qualification_clock="bundle_created_by_original_registration_and_execution_assessment_at_E_09_30",
            previously_consumed_qualification_reprojected=False)
    if action_components:
        result.update(method_id=IPO_APPEND_ACTION_METHOD_ID if ipo_append_only else ACTION_METHOD_ID,
            corporate_action_coverage="fixed_partial_typed_ordinary_components_cash_only_inception_not_reference_exception",
            action_evidence="one_fixed_review_receipt_sourceproof_mode_context_and_tax_for_entire_book",
            action_evidence_append_supported=False, action_reference_override_supported=False)
    return result


def _now():
    return datetime.now(MARKET_TZ)


def _action_pair(action_review, action_knowledge_mode):
    if (action_review is None) != (action_knowledge_mode is None):
        raise ValueError("--action-review and --action-knowledge-mode must be supplied together")
    if action_knowledge_mode is not None and action_knowledge_mode not in ACTION_KNOWLEDGE_MODES:
        raise ValueError("An explicit supported fixed-action knowledge mode is required")


def _action_input_paths(action_review):
    if action_review is None:
        return []
    path = Path(action_review).resolve()
    receipt = path.parent / "retrieval_receipt.json"
    review, acquisition = _read(path)[0], _read(receipt)[0]
    files = [path, receipt]
    files.extend((path.parent / item["file"]).resolve() for item in acquisition["sources"])
    for event in review["events"]:
        files.extend(Path(item["source_file"]).resolve() for item in event["official_sources"])
        files.append(Path(event["official_announcement_metadata"]["file"]).resolve())
    return files


def load_action_views(action_review, action_knowledge_mode, directory, config, now):
    """Pure fixed-evidence loading; no account creation, Engine or PDF parsing."""
    _action_pair(action_review, action_knowledge_mode)
    if action_review is None:
        return {"views": {}, "binding": None, "input_paths": []}
    directory = Path(directory).resolve()
    views = {kind: load_research_corporate_action_view(action_review,
        knowledge_mode=action_knowledge_mode, context=_context(directory, kind), config=config, now=now)
        for kind in MODEL_KINDS}
    fixed = [{key: value for key, value in views[kind].binding.items() if key != "context"}
        for kind in MODEL_KINDS]
    if any(binding != fixed[0] for binding in fixed[1:]):
        raise ValueError("Both native models must bind the same fixed action review and source proofs")
    files = _action_input_paths(action_review)
    for file in files:
        package = file.parent
        if directory == package or package in directory.parents or directory in package.parents:
            raise ValueError("Action-enabled run must remain outside its review and indirect source packages")
    return {"views": views, "binding": {"knowledge_mode": action_knowledge_mode,
        "views": {kind: views[kind].binding for kind in MODEL_KINDS},
        "action_evidence_append_supported": False, "reference_override_supported": False},
        "input_paths": files}


def _stamp(value):
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("Research source clocks require explicit timezone offsets")
    return value.astimezone(MARKET_TZ)


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    return json.loads(data.decode("utf-8-sig")), {"file": str(path),
        "sha256": sha256(data).hexdigest(), "byte_count": len(data)}


@contextmanager
def _lock(path):
    # Local research lock only: importing the application worker would prepare
    # shared account directories even when its explicit lock path is supplied.
    with Path(path).open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0"); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _source(folder, calendar_ref, sessions, now):
    folder = Path(folder).resolve()
    metadata, raw, ordinary = load_complete_close(folder)
    day, plan, receipt = metadata["plan"]["day"], metadata["plan"], metadata["receipt"]
    metadata["ordinary_roster"] = ordinary.to_dict("records")
    _inventory(raw, metadata, "pinned_static_exchange_calendar")
    if day not in sessions or plan["calendar_sha256"] != calendar_ref["sha256"]:
        raise ValueError("Strict dated raw source/calendar differs from the published plan")
    clocks = _acquisition_times(metadata, folder)
    terminal, roster = clocks["terminal_completion_observed_at"], clocks["roster_receipt_completed_at"]
    if terminal is None or roster is None:
        raise ValueError("Complete close and own dated-roster completion clocks are required")
    times = [*clocks["acquisition_timestamps"].values(), *clocks["roster_acquisition_timestamps"].values()]
    available = max(_stamp(terminal), _stamp(roster))
    if available < _stamp(day + "T15:00:00+08:00") or any(_stamp(t) > now for t in times):
        raise ValueError("Strict raw completion predates its close or contains future acquisition clocks")
    proof = {"day": day, "source_folder": str(folder), "raw_file": str(folder / "raw_daily.csv"),
        "raw_sha256": receipt["raw_daily_sha256"], "plan_sha256": digest(folder / "plan.json"),
        "receipt_sha256": digest(folder / "receipt.json"), "roster_raw_sha256": plan["roster_raw_sha256"],
        "calendar_sha256": calendar_ref["sha256"], "source_available_at": available.isoformat(),
        "ordinary_inventory_rows": len(ordinary), "read_only_verified": True,
        "receipt_status": receipt["status"], "full_dated_roster_inventory_complete": True, **clocks}
    native = raw.copy()
    native["symbol"] = ordinary.symbol.to_numpy()
    canonical = native.rename(columns={"pctChg": "pct_chg"}).copy()
    for field in ("open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg"):
        canonical[field] = pd.to_numeric(canonical[field], errors="coerce")
    canonical["is_st"] = pd.to_numeric(canonical.isST, errors="raise").astype(int)
    canonical["preclose_for_limits"] = canonical.preclose
    canonical["provider_raw_observation"] = True
    return {"proof": proof, "raw": native, "canonical": canonical,
        "original_rows": native.set_index("symbol").to_dict("index")}


def _panel_link(loaded, now):
    """Pin the small producer metadata now, without rereading its 914-day raw."""
    result, result_ref = _read(loaded["publication"]["batch_result_file"])
    panel, panel_ref = _read(result["panel_result"])
    day = loaded["source_day"]
    if (panel.get("last_source_day") != day or panel.get("feature_contract_id") != PLAN_SPEC["feature_contract_id"]
            or _stamp(panel["available_at"]) > _stamp(loaded["publication"]["published_at"])
            or _stamp(panel["available_at"]) > now
            or panel.get("execution_admitted") is not False):
        raise ValueError("Native feature metadata does not describe this exact source day/contract/availability")
    for item in result["score_batches"]:
        score_proof, _ = _read(item["provenance_file"])
        source = score_proof["feature_source"]
        generation, generation_ref = _read(score_proof["manifest_file"])
        if (str(Path(panel["latest_features_path"]).resolve()) != str(Path(source["source_file"]).resolve())
                or panel["latest_features_sha256"] != source["source_sha256"]
                or panel["available_at"] != source["available_at"]
                or panel["feature_semantics"] != score_proof["feature_semantics"]
                or generation_ref["sha256"] != score_proof["manifest_sha256"]
                or generation["feature_contract_id"] != panel["feature_contract_id"]
                or generation["feature_semantics"] != panel["feature_semantics"]):
            raise ValueError("Saved raw score features and native producer metadata differ")
    daily = [p for p in panel["source_proof"]["day_sources"] if p["day"] == day]
    if len(daily) != 1:
        raise ValueError("Native metadata must identify exactly its own source day")
    components = [p for p in panel["source_proof"]["component_sources"]
        if p["component_id"] == daily[0]["component_id"]]
    if (len(components) != 1 or components[0]["component_kind"] != "strict_dated_close_csv"
            or daily[0]["component_kind"] != "strict_dated_close_csv"):
        raise ValueError("This consumer requires the plan day to originate from its strict dated close")
    proof = components[0]["source_proof"]
    if (proof["day"] != day or daily[0]["source_available_at"] != proof["source_available_at"]
            or _stamp(proof["source_available_at"]) > _stamp(panel["available_at"])):
        raise ValueError("Native per-day and component source evidence differ")
    return {"panel_metadata": panel_ref, "score_batch_result": result_ref,
        "metadata_attachment_basis": "current_consumer_input_pin_not_original_score_publication_attachment",
        "original_score_publication_bound_panel_metadata": False,
        "source_close_proof": {k: proof[k] for k in ("day", "source_folder", "raw_file", "raw_sha256",
            "plan_sha256", "receipt_sha256", "roster_raw_sha256", "calendar_sha256", "source_available_at")}}


def _qualification_link(loaded, qualification, input_refs):
    """Bind the saved daily projection without rewriting its raw feature scope."""
    record, registration = loaded["record"], loaded["registration"]
    relative = record["qualification_scope_file"]
    folder = Path(loaded["plan_ref"]["file"]).parent
    file = (folder / relative).resolve()
    if file.parent != folder or relative != "qualification_scope.json":
        raise ValueError("Qualified plan scope must remain its own saved fifth payload")
    scope, reference = _read(file)
    bindings = [item for item in record["files"] if item["path"] == relative]
    if (len(bindings) != 1 or bindings[0]["sha256"] != reference["sha256"]
            or bindings[0]["byte_count"] != reference["byte_count"]
            or scope["qualification_inputs"] != registration["qualification_inputs"]
            or scope["qualification_inputs"] != input_refs
            or scope["qualification_verification"] != qualification.verification
            or scope["policy_id"] != QUALIFIED_POLICY_ID
            or scope["source_day"] != loaded["source_day"]
            or scope["decision_as_of"] != registration["registered_at"]):
        raise ValueError("Qualified plan and caller IPO/master source, verification or registration clock differ")
    return {"qualification_inputs": deepcopy(input_refs),
        "qualification_verification": deepcopy(qualification.verification)}, reference


def load_qualification_plan_prefix(loaded_plans, ipo_evidence, research_master_run, calendar):
    """Read each canonical plan's own IPO bundle, preserving its original clock.

    The caller's evidence is the immutable inception anchor, not the latest
    bundle. Returned typed qualifications are per-plan execution inputs; the
    inception proof is the only qualification proof in the fixed book binding.
    This helper does not create plans, accounts, journals or directories.
    """
    from .ipo_listing_supplement import validate_listing_bundle_append

    if not loaded_plans:
        raise ValueError("An IPO append prefix needs its original inception plan")
    calendar = Path(calendar).resolve()
    anchor_refs = {"ipo_evidence": _read(ipo_evidence)[1],
        "research_master_run": _read(research_master_run)[1] if research_master_run is not None else None}
    calendar_sha256 = digest(calendar)
    cache, entries, previous, inception = {}, [], None, None
    for index, loaded in enumerate(loaded_plans):
        inputs = loaded["registration"].get("qualification_inputs")
        if (loaded["record"]["policy_id"] != QUALIFIED_POLICY_ID
                or not isinstance(inputs, dict) or set(inputs) != set(anchor_refs)
                or inputs["research_master_run"] != anchor_refs["research_master_run"]
                or (index == 0 and inputs != anchor_refs)
                or Path(loaded["calendar"]["file"]).resolve() != calendar
                or loaded["calendar"]["sha256"] != calendar_sha256):
            raise ValueError("IPO append must begin at the caller anchor with one fixed master and calendar")
        evidence_ref = _read(inputs["ipo_evidence"]["file"])[1]
        if evidence_ref != inputs["ipo_evidence"]:
            raise ValueError("A plan's original IPO bundle changed")
        key = _hash(inputs)
        if key not in cache:
            cache[key] = load_ipo_qualification(evidence_ref["file"], calendar,
                master_run=research_master_run)
        qualification = cache[key]
        verification = qualification.verification
        if ("bundle_created_at" not in verification
                or _stamp(verification["bundle_created_at"]) > _stamp(loaded["registration"]["registered_at"])):
            raise ValueError("Each IPO bundle must actually exist by its original plan registration")
        if previous is None:
            validate_listing_bundle_append(qualification, qualification)
        else:
            if (loaded["source_day"] not in qualification.calendar
                    or qualification.calendar.index(loaded["source_day"]) == 0
                    or qualification.calendar[qualification.calendar.index(loaded["source_day"]) - 1]
                    != loaded_plans[index - 1]["source_day"]):
                raise ValueError("IPO plan lineage must retain every exact consecutive source session")
            validate_listing_bundle_append(previous, qualification)
        proof, scope = _qualification_link(loaded, qualification, inputs)
        if inception is None:
            inception = deepcopy(proof)
        entries.append({"qualification": qualification, "proof": proof, "scope": scope})
        previous = qualification
    return {"inception": inception, "entries": entries}


def _context(directory, kind):
    return RunContext("native-preopen:" + _hash(str(directory))[:20] + ":" + kind, TradingMode.REPLAY)


def _quality(case, loaded, feed, sources, qualification):
    d, e = loaded["source_day"], loaded["execution_day"]
    held = {s for s, lots in case["state"]["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
    decision = loaded["decisions"][case["kind"]]
    required = held | {s for s, w in (decision["weights"] or {}).items() if w > 0}
    issues = []
    for symbol in sorted(required):
        previous, current = feed.rows.get(d, {}).get(symbol), feed.rows.get(e, {}).get(symbol)
        reasons = []
        if not _known_raw(previous): reasons.append("source_day_raw_status_or_activity_unknown")
        if not _known_raw(current): reasons.append("execution_day_raw_status_or_activity_unknown")
        if not reasons:
            a = Decimal(sources[d]["original_rows"][symbol]["close"])
            b = Decimal(sources[e]["original_rows"][symbol]["preclose"])
            if a != b: reasons.append("corporate_action_or_reference_change_unresolved_no_cash_imputation")
        ipo = qualification.assess(symbol, e, decision_as_of=e + "T09:30:00+08:00")
        if ipo["engine_block_reason"] is not None: reasons.append("execution_open_listing_qualification_unknown_or_excluded")
        if reasons:
            issues.append({"symbol": symbol, "source_day": d, "execution_day": e,
                "reasons": reasons, "IPO_evidence": ipo, "absence_of_company_actions_proven": False})
    return {"required_symbols": sorted(required), "issues": issues,
        "reference_equality_is_not_action_coverage_verification": True}


def _summary(kind, case, config):
    state = case["state"]
    book = Ledger(state, config, RunContext.from_dict(case["context"]))
    result = {"model_kind": kind, "context": case["context"], "last_processed_day": state["day"],
        "processed_execution_days": [r["date"] for r in case["daily"]], "daily": deepcopy(case["daily"]),
        "cash_cents": state["cash"], "cash_available_cents": book.cash_available(),
        "positions": {s: {"quantity": book.quantity(s), "available": book.available(s, state["day"])}
            for s in state["positions"] if book.quantity(s)},
        "orders": deepcopy(state["orders"]), "fills": deepcopy(state["fills"]),
        "order_count": len(state["orders"]), "fill_count": len(state["fills"]),
        "pending_targets": deepcopy(state.get("pending_targets")), "metrics": None,
        "performance_unavailable_reason": "accounting_only_action_and_PIT_coverage_unverified_no_total_return_claim"}
    if ACTION_STATE_KEY in state:
        result.update(action_component_binding=deepcopy(state[ACTION_STATE_KEY]),
            entitlements=deepcopy(state["entitlements"]), receivables=deepcopy(state["receivables"]),
            action_evidence_append_supported=False, action_reference_override_supported=False)
    return result


def _validate_head(head, binding, entries, sources, directory):
    if (head.get("schema") != SCHEMA or head.get("binding") != binding
            or any(head.get(k) is not v for k, v in FLAGS.items())
            or list(head.get("cases", {})) != list(MODEL_KINDS)):
        raise ValueError("Persistent research method/model/calendar/config/account binding changed")
    count = len(head["consumed_plans"])
    if head["consumed_plans"] != [p["reference"] for p in entries[:count]]:
        raise ValueError("Consumed plan history disappeared, changed hash, or changed its native source metadata")
    end = entries[count - 1]["loaded"]["execution_day"] if count else None
    if head["raw_sources"] != [sources[d]["proof"] for d in sorted(sources) if end and d <= end]:
        raise ValueError("Previously consumed strict raw-source prefix changed or disappeared")
    if len(head["journal_files"]) != count:
        raise ValueError("Complete two-model session journal count differs from the committed cursor")
    previous = None
    for index, reference in enumerate(head["journal_files"]):
        file = Path(reference["file"]).resolve()
        if file.parent != directory / "journal":
            raise ValueError("Session journal must remain inside its independent research run")
        entry, proof = _read(file)
        expected = entries[index]["loaded"]
        raw_prefix = [sources[d]["proof"] for d in sorted(sources) if d <= expected["execution_day"]]
        if (proof != reference or entry["schema"] != JOURNAL_SCHEMA or entry["previous_journal_sha256"] != previous
                or entry["plan"] != head["consumed_plans"][index]
                or entry["binding_sha256"] != _hash(binding)
                or entry["source_day"] != expected["source_day"]
                or entry["execution_day"] != expected["execution_day"]
                or entry["raw_sources_sha256"] != _hash(raw_prefix)):
            raise ValueError("Independent session journal or predecessor binding changed")
        if (binding.get("ipo_append_only") is True
                and entry.get("qualification_evidence") != entries[index]["reference"]["qualification_evidence"]):
            raise ValueError("Independent session journal changed its own original IPO qualification prefix")
        if "fixed_action_components" in binding:
            expected_actions = {kind: {**binding["fixed_action_components"]["views"][kind],
                "replay_start_day": entries[0]["loaded"]["execution_day"]} for kind in MODEL_KINDS}
            if entry.get("action_component_bindings") != expected_actions:
                raise ValueError("Session journal changed its fixed action review/context/start binding")
        previous = proof["sha256"]
    for kind in MODEL_KINDS:
        case = head["cases"][kind]
        expected_days = [p["loaded"]["execution_day"] for p in entries[:count]]
        if (case["kind"] != kind or case["context"] != _context(directory, kind).to_dict()
                or case["state"].get("broker_binding") or case["state"]["day"] != (expected_days[-1] if count else None)
                or [r["date"] for r in case["daily"]] != expected_days
                or (count and (_hash(case) != entry["post_case_sha256"][kind]))):
            raise ValueError("Saved full account/daily/preopen receipt differs from the sealed session journal")
        if "fixed_action_components" in binding:
            action_binding = {**binding["fixed_action_components"]["views"][kind],
                "replay_start_day": entries[0]["loaded"]["execution_day"]}
            if (case["state"].get(ACTION_STATE_KEY) != action_binding
                    or any(row.get("action_component_binding") != action_binding for row in case["daily"])):
                raise ValueError("Saved action state/daily proof differs from its fixed review/context/start")
        elif ACTION_STATE_KEY in case["state"]:
            raise ValueError("A default native account cannot silently acquire action component accounting")


def _result(status, directory, head, config, *, committed=0, created=False, new_order_count=0,
            policy_id=POLICY_ID, method_id=METHOD_ID, **facts):
    result = {"schema": RESULT_SCHEMA, "method_id": head["binding"]["method_id"] if head else method_id,
        "policy_id": head["binding"]["policy_id"] if head else policy_id,
        "status": status, "read_at": _now().isoformat(), "run_directory": str(directory),
        "checkpoint": str(directory / "head.json") if head else None,
        "research_checkpoint_created": bool(head), "research_ledger_mutated": bool(committed),
        "research_accounts_created": 2 if created else 0, "account_reset": False,
        "case_count": 2 if head else 0, "newly_committed_execution_sessions": committed,
        "newly_committed_case_sessions": committed * 2,
        "cases": [_summary(k, head["cases"][k], config) for k in MODEL_KINDS] if head else [],
        "metrics": None, **facts, **FLAGS}
    result["new_order_count"] = new_order_count
    result["orders_generated"] = new_order_count > 0
    result["orders_present"] = any(c["order_count"] for c in result["cases"])
    if result["method_id"] in {IPO_APPEND_METHOD_ID, IPO_APPEND_ACTION_METHOD_ID}:
        result["ipo_append_only"] = True
    if directory.exists():
        report = directory / "attempts" / (_now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8] + ".json")
        result["attempt_report"] = str(report)
        write_json(report, result)
    return result


def run(plans, raw_closes, ipo_evidence, research_master_run, run_dir, *, mode=TradingMode.REPLAY,
        through=None, ipo_append_only=False, action_review=None, action_knowledge_mode=None):
    """Consume a complete saved plan prefix; ``through`` limits execution days."""
    if mode != TradingMode.REPLAY:
        raise ValueError("This isolated research consumer supports REPLAY only, not a broker/PAPER worker")
    if type(ipo_append_only) is not bool:
        raise ValueError("IPO append-only inception must be an explicit boolean")
    _action_pair(action_review, action_knowledge_mode)
    if not plans:
        raise ValueError("At least one original saved preopen plan is required")
    now, directory, config = _now(), Path(run_dir).resolve(), _config(100000, 5)
    loaded = sorted([load_preopen_plan(path) for path in plans], key=lambda p: p["source_day"])
    first = loaded[0]
    plan_policy = first["record"]["policy_id"]
    plan_spec = policy_spec(plan_policy)
    rule_spec = forward_rule_spec(plan_policy, ipo_append_only=ipo_append_only,
        action_components=action_review is not None)
    method_id = rule_spec["method_id"]
    calendar_ref = first["calendar"]
    payload = load_calendar(Path(calendar_ref["file"]))
    if payload is None or digest(calendar_ref["file"]) != calendar_ref["sha256"]:
        raise ValueError("Published exchange calendar changed")
    year = payload["year"]
    sessions, previous, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    days = [p["source_day"] for p in loaded]
    if (status != "pinned_static_exchange_calendar" or len(days) != len(set(days))
            or days != [d for d in sessions if days[0] <= d <= days[-1]]):
        raise ValueError("All original plans must form one exact consecutive source-session prefix")
    models = first["registration"]["frozen_model_refs"]
    for kind in MODEL_KINDS:
        if digest(models[kind]["manifest_file"]) != models[kind]["manifest_sha256"]:
            raise ValueError("The frozen generation manifest changed")
    entries = []
    for p in loaded:
        if (p["calendar"] != calendar_ref or p["registration"]["frozen_model_refs"] != models
                or p["record"]["spec"] != plan_spec or p["record"]["policy_id"] != plan_policy
                or previous.get(p["execution_day"]) != p["source_day"]
                or sessions.index(p["execution_day"]) + 1 >= len(sessions)):
            raise ValueError("Plan calendar, exact execution session, model generation or fixed policy changed")
        link = _panel_link(p, now)
        entries.append({"loaded": p, "reference": {"plan": p["plan_ref"], "source_day": p["source_day"],
            "execution_day": p["execution_day"], "published_at": p["record"]["published_at"], **link}})
    if ipo_append_only:
        lineage = load_qualification_plan_prefix(loaded, ipo_evidence, research_master_run, calendar_ref["file"])
        qualification = lineage["entries"][0]["qualification"]
    else:
        qualification = load_ipo_qualification(ipo_evidence, Path(calendar_ref["file"]), master_run=research_master_run)
    if list(qualification.calendar) != sessions:
        raise ValueError("Typed IPO qualification and published plan calendars differ")
    qualified_binding = None
    if ipo_append_only:
        qualified_binding = lineage["inception"]
        for entry, daily_qualification in zip(entries, lineage["entries"]):
            entry["qualification"] = daily_qualification["qualification"]
            entry["reference"]["qualification_scope"] = daily_qualification["scope"]
            entry["reference"]["qualification_evidence"] = daily_qualification["proof"]
    elif plan_policy == QUALIFIED_POLICY_ID:
        input_refs = {"ipo_evidence": _read(ipo_evidence)[1],
            "research_master_run": _read(research_master_run)[1] if research_master_run is not None else None}
        for entry in entries:
            proof, reference = _qualification_link(entry["loaded"], qualification, input_refs)
            if qualified_binding is not None and qualified_binding != proof:
                raise ValueError("A continuous qualified book cannot change IPO/master evidence sources")
            qualified_binding = proof
            entry["reference"]["qualification_scope"] = reference
    sources = {}
    for folder in raw_closes:
        source = _source(folder, calendar_ref, sessions, now)
        day = source["proof"]["day"]
        if day in sources:
            raise ValueError("A strict dated raw source cannot be supplied twice")
        sources[day] = source
    for entry in entries:
        d = entry["loaded"]["source_day"]
        expected = entry["reference"]["source_close_proof"]
        if d not in sources:
            continue  # Missing source is reported as waiting, never filled.
        if any(sources[d]["proof"][key] != value for key, value in expected.items()):
            raise ValueError("Published D features and current strict D execution source are not the same source")
    binding = {"method_id": method_id, "rule_spec": rule_spec, "policy_id": plan_policy,
        "config": config.to_dict(), "calendar": calendar_ref, "calendar_payload": payload,
        "frozen_model_refs": models, "ipo_qualification": qualification.verification,
        "first_source_day": days[0], "run_directory": str(directory), "mode": TradingMode.REPLAY.value,
        "contexts": {kind: _context(directory, kind).to_dict() for kind in MODEL_KINDS}}
    if qualified_binding is not None:
        binding["stage_membership_qualification"] = qualified_binding
    if ipo_append_only:
        binding["ipo_append_only"] = True
    action_setup = load_action_views(action_review, action_knowledge_mode, directory, config, now)
    action_views = action_setup["views"]
    if action_setup["binding"] is not None:
        binding["fixed_action_components"] = action_setup["binding"]
    head_file = directory / "head.json"
    head = _read(head_file)[0] if head_file.exists() else None
    if head is not None:
        _validate_head(head, binding, entries, sources, directory)
    elif directory.exists():
        raise FileExistsError("An existing research run without its checkpoint cannot be reset")
    cutoff = through or loaded[-1]["execution_day"]
    if (cutoff not in sessions or cutoff < first["execution_day"]
            or (head and cutoff < head["cases"][MODEL_KINDS[0]]["state"]["day"])):
        raise ValueError("Through must be an exact execution session without moving the persistent cursor backwards")
    cursor = len(head["consumed_plans"]) if head else 0
    work = [entry for entry in entries[cursor:] if entry["loaded"]["execution_day"] <= cutoff]
    if not work:
        return _result("already_consumed_original_plan_prefix", directory, head, config,
            policy_id=plan_policy, method_id=method_id)
    required = [work[0]["loaded"][key] for key in ("source_day", "execution_day")]
    missing = [d for d in required if d not in sources]
    if missing:
        return _result("waiting_for_exact_complete_raw_close", directory, head, config,
            policy_id=plan_policy, method_id=method_id,
            missing_source_days=missing, required_execution_day=required[-1])
    directory.mkdir(parents=True, exist_ok=True)
    with _lock(directory / "runner.lock"):
        if (head_file.exists() and _read(head_file)[0] != head) or (head is not None and not head_file.exists()):
            raise ValueError("Research checkpoint changed during input validation")
        committed, created, new_orders = 0, False, 0
        for entry in work:
            p = entry["loaded"]
            d, e = p["source_day"], p["execution_day"]
            missing = [day for day in (d, e) if day not in sources]
            if missing:
                return _result("waiting_for_exact_complete_raw_close", directory, head, config,
                    policy_id=plan_policy, method_id=method_id,
                    committed=committed, created=created, new_order_count=new_orders,
                    missing_source_days=missing, required_execution_day=e)
            if head and head["cases"][MODEL_KINDS[0]]["state"]["day"] != d:
                raise ValueError("The account cannot skip an intervening original preopen plan/session")
            raw = pd.concat([sources[day]["canonical"] for day in sorted(sources) if day <= e], ignore_index=True)
            symbols = sorted(raw.symbol.unique())
            session_qualification = entry["qualification"] if ipo_append_only else qualification
            if action_views:
                feeds = {kind: ForwardRawFeed(raw, sessions, symbols, ipo_qualification=session_qualification,
                    corporate_action_view=action_views[kind]) for kind in MODEL_KINDS}
            else:
                feed = ForwardRawFeed(raw, sessions, symbols, ipo_qualification=session_qualification)
                feeds = {kind: feed for kind in MODEL_KINDS}
            cases = (head["cases"] if head else {kind: {"kind": kind,
                "context": _context(directory, kind).to_dict(), "state": new_state(config.initial_cash), "daily": []}
                for kind in MODEL_KINDS})
            qualities = {kind: _quality(cases[kind], p, feeds[kind], sources, session_qualification) for kind in MODEL_KINDS}
            if any(q["issues"] for q in qualities.values()):
                return _result("blocked_required_raw_reference_or_IPO_evidence", directory, head, config,
                    policy_id=plan_policy, method_id=method_id,
                    committed=committed, created=created, new_order_count=new_orders,
                    execution_day=e, blocking_evidence=qualities,
                    session_not_committed=True)
            trials = deepcopy(cases)
            try:
                for kind in MODEL_KINDS:
                    case = trials[kind]
                    context = RunContext.from_dict(case["context"])
                    envelope = bind_preopen_plan(p["plan_ref"]["file"], kind, context)
                    if envelope.intent.source != f"research:shortline_native_preopen:{p['plan_ref']['sha256']}:{kind}":
                        raise ValueError("Saved plan changed between source validation and typed context binding")
                    row = advance(case["state"], feeds[kind], e, config, len(case["daily"]),
                        context=context, preopen_intent=envelope)
                    row.update(source_day=d, execution_day=e, plan=p["plan_ref"],
                        original_plan_kind=p["decisions"][kind]["decision"],
                        preopen_published_at=p["record"]["published_at"],
                        execution_close_used_for_ranking=False, reference_and_IPO_quality=qualities[kind])
                    if ipo_append_only:
                        row["qualification_evidence"] = deepcopy(entry["reference"]["qualification_evidence"])
                    if action_views:
                        row["action_component_binding"] = deepcopy(case["state"][ACTION_STATE_KEY])
                    case["daily"].append(row)
            except (ValueError, KeyError, TypeError, PermissionError) as error:
                return _result("failed_engine_session_no_commit", directory, head, config,
                    policy_id=plan_policy, method_id=method_id,
                    committed=committed, created=created, new_order_count=new_orders, execution_day=e,
                    error=f"{type(error).__name__}: {error}", failed_model_kind=kind,
                    session_not_committed=True, both_model_trials_discarded=True)
            new_head = deepcopy(head) if head else {"schema": SCHEMA, "registered_at": _now().isoformat(),
                "binding": binding, "consumed_plans": [], "raw_sources": [], "journal_files": [], **FLAGS}
            new_head["cases"] = trials
            new_head["consumed_plans"].append(entry["reference"])
            new_head["raw_sources"] = [sources[day]["proof"] for day in sorted(sources) if day <= e]
            journal = {"schema": JOURNAL_SCHEMA, "execution_day": e, "source_day": d,
                "plan": entry["reference"], "binding_sha256": _hash(binding),
                "previous_journal_sha256": head["journal_files"][-1]["sha256"] if head else None,
                "post_case_sha256": {kind: _hash(trials[kind]) for kind in MODEL_KINDS},
                "raw_sources_sha256": _hash(new_head["raw_sources"]),
                "committed_at": _now().isoformat(), **FLAGS}
            if ipo_append_only:
                journal["qualification_evidence"] = deepcopy(entry["reference"]["qualification_evidence"])
            if action_views:
                journal["action_component_bindings"] = {kind: deepcopy(trials[kind]["state"][ACTION_STATE_KEY])
                    for kind in MODEL_KINDS}
            file = directory / "journal" / (e + "_" + _hash(journal)[:24] + ".json")
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open("x", encoding="utf-8") as handle:
                json.dump(journal, handle, ensure_ascii=False, indent=2, allow_nan=False)
            _, proof = _read(file)
            new_head["journal_files"].append(proof)
            # One atomic replacement includes both complete account states.
            write_json(head_file, new_head)
            created = created or head is None
            head = new_head
            committed += 1
            new_orders += sum(trials[kind]["daily"][-1]["new_orders"] for kind in MODEL_KINDS)
        return _result("advanced_isolated_native_research_books", directory, head, config,
            committed=committed, created=created, new_order_count=new_orders, policy_id=plan_policy,
            method_id=method_id)


def _append_qualification_input_paths(plans):
    """Locate saved IPO source packages before an observation can be exported.

    These reads only restrict output paths; canonical plan/source validation
    still runs independently before any account operation.
    """
    paths = []
    for plan in plans:
        registration, _ = _read(Path(plan).resolve().parent / "registration.json")
        inputs = registration["qualification_inputs"]
        bundle_path = Path(inputs["ipo_evidence"]["file"]).resolve()
        bundle, _ = _read(bundle_path)
        if bundle.get("schema") != "research_ipo_listing_bundle_v1":
            raise ValueError("IPO append output protection requires explicit listing bundles")
        paths.append(bundle_path)
        base_path = Path(bundle["base_evidence"]).resolve()
        base, _ = _read(base_path)
        receipt_path = Path(base["source_receipt"]).resolve()
        receipt, _ = _read(receipt_path)
        paths.extend([base_path, receipt_path])
        paths.extend(Path(source["raw_path"]).resolve() for source in receipt["sources"])
        for reference in bundle["supplements"]:
            supplement_path = Path(reference["path"]).resolve()
            supplement, _ = _read(supplement_path)
            source_receipt_path = Path(supplement["source_receipt"]).resolve()
            source_receipt, _ = _read(source_receipt_path)
            paths.extend([supplement_path, source_receipt_path, Path(source_receipt["raw_file"]).resolve()])
        if inputs["research_master_run"] is not None:
            master_run = Path(inputs["research_master_run"]["file"]).resolve()
            master, _ = _read(master_run)
            paths.extend([master_run, master_run.parent / master["master_file"]])
    return paths


def _result_export_path(path, *, run_dir, plans, raw_closes, ipo_evidence, research_master_run,
                        ipo_append_only=False, action_review=None, action_knowledge_mode=None):
    """An observation export cannot replace account or source-package files."""
    _action_pair(action_review, action_knowledge_mode)
    if path is None:
        return None
    target = Path(path).resolve()
    if target.suffix.lower() != ".json" or target.is_dir():
        raise ValueError("A result export requires an external JSON file")
    inputs = [*plans, *raw_closes, ipo_evidence, research_master_run]
    if ipo_append_only:
        inputs.extend(_append_qualification_input_paths(plans))
    inputs.extend(_action_input_paths(action_review))
    roots = [Path(run_dir).resolve()]
    roots.extend(p.resolve() if p.is_dir() else p.resolve().parent for p in map(Path, inputs))
    if any(target == root or root in target.parents for root in roots):
        raise ValueError("A result export must stay outside the account and original source packages")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="append", type=Path, required=True)
    parser.add_argument("--raw-close", action="append", type=Path, required=True)
    parser.add_argument("--ipo-proof", type=Path, required=True)
    parser.add_argument("--research-master-run", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--through")
    parser.add_argument("--ipo-append-only", action="store_true",
        help="Start a distinct qualified book with append-only IPO evidence; --ipo-proof remains its inception anchor")
    parser.add_argument("--action-review", type=Path,
        help="Explicit fixed typed ordinary-action review for a distinct cash-only research inception")
    parser.add_argument("--action-knowledge-mode", choices=ACTION_KNOWLEDGE_MODES,
        help="Required with --action-review; does not admit PIT or price-reference overrides")
    parser.add_argument("--mode", default=TradingMode.REPLAY.value, choices=[m.value for m in TradingMode])
    parser.add_argument("--result-json", type=Path,
        help="Save the full observation outside the account/source directories, including a first waiting result")
    args = parser.parse_args(argv)
    export = _result_export_path(args.result_json, run_dir=args.run, plans=args.plan,
        raw_closes=args.raw_close, ipo_evidence=args.ipo_proof, research_master_run=args.research_master_run,
        ipo_append_only=args.ipo_append_only, action_review=args.action_review,
        action_knowledge_mode=args.action_knowledge_mode)
    try:
        result = run(args.plan, args.raw_close, args.ipo_proof, args.research_master_run, args.run,
            mode=TradingMode(args.mode), through=args.through, ipo_append_only=args.ipo_append_only,
            action_review=args.action_review, action_knowledge_mode=args.action_knowledge_mode)
    except (ValueError, TypeError, OSError, RuntimeError, KeyError) as error:
        if export is not None:
            failure = {"schema": "native_shortline_preopen_forward_command_failure_v1",
                "status": "failed", "recorded_at": _now().isoformat(),
                "run_directory": str(args.run.resolve()), "error_type": type(error).__name__,
                "reason": str(error), "execution_progress_unknown": True,
                "account_reset": False, **FLAGS}
            if args.ipo_append_only:
                failure.update(method_id=IPO_APPEND_METHOD_ID, ipo_append_only=True)
            if args.action_review is not None:
                failure.update(method_id=IPO_APPEND_ACTION_METHOD_ID if args.ipo_append_only else ACTION_METHOD_ID,
                    fixed_action_review=str(args.action_review.resolve()), action_knowledge_mode=args.action_knowledge_mode)
            write_json(export, failure)
        raise
    if export is not None:
        write_json(export, result)
    summary = {key: result[key] for key in ("schema", "status", "checkpoint", "case_count",
        "newly_committed_case_sessions", "metrics", "execution_admitted")}
    summary["result_file"] = str(export) if export is not None else None
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
