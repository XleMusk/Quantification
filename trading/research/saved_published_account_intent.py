"""Read saved twenty-case and frozen Alpha158 books into the shared consumer.

Only source layouts differ here. Complete weights and queued reductions reuse
the eight-type saved-intent conversion, without reranking or advancing books.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, PortfolioIntent, RunContext, TradingMode
from ..engine import PUBLISHED_CLOSE_INTENT_SCHEMA, PUBLISHED_CLOSE_REDUCED_INTENT_SCHEMA
from ..ledger import Ledger
from . import leader_published_forward_run as leader
from . import recent_alpha158_forward_run as alpha
from .leader_daily_signals import _calendar_view
from .leader_eight_type_position_view import _intent
from .saved_account_intent import FLAGS
from .saved_leader_account_intent import SOURCE_BASIS, _case_facts, _day, _stamp


LEADER_SCHEMA = "saved_leader_published_account_intent_v1"
ALPHA_SCHEMA = "saved_alpha158_account_intent_v1"
SCHEMAS = {LEADER_SCHEMA, ALPHA_SCHEMA}
NOT_READY_SCHEMA = "saved_published_account_intent_not_ready_v1"
EMBEDDED_SCOPE = "head_pinned_embedded_journal_not_independent_seal"


def _read(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    value = json.loads(data.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Saved published-account source must be an object")
    return value, {"file": str(path), "sha256": sha256(data).hexdigest()}


def _publication_records(head, family, case_ids, calendar, config, current):
    sessions, _, status = _calendar_view(calendar, f"{calendar['year']}-01-01", f"{calendar['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Saved published-account calendar is not an exact fixed year")
    result = {}
    for reference in head["publications"]:
        record, actual = _read(reference["file"])
        day, execution = reference["day"], reference["execution_day"]
        stamp = _stamp(reference["recorded_at"])
        if (actual["sha256"] != reference["sha256"] or day in result or day not in sessions
                or next((d for d in sessions if d > day), None) != execution
                or record.get("source_close_day") != day or record.get("proposed_execution_day") != execution
                or record.get("recorded_at") != reference["recorded_at"] or stamp > current
                or stamp.date().isoformat() != day
                or not _stamp(day + "T15:05:00+08:00") <= stamp < _stamp(execution + "T09:30:00+08:00")
                or record.get("next_open_deadline") != execution + "T09:30:00+08:00"
                or record.get("status") != "close_research_target_publication_only"
                or record.get("execution_binding_pending") is not False
                or record.get("publication_time_backfilled") is not False
                or record.get("execution_day_rolled_forward") is not False
                or record.get("successor_raw_used") is not False):
            raise ValueError("Saved publication bytes, actual clock or exact D/E changed")
        if family == "leader20":
            leader._boundaries(record)
            if (record.get("schema") != leader.PUBLICATION_SCHEMA or record.get("case_count") != len(case_ids)
                    or [row["case_id"] for row in record["cases"]] != list(case_ids)
                    or record.get("protocol_binding") != head["binding"]["original_protocol"]
                    or record.get("publication_at") != reference["recorded_at"]
                    or record.get("timeliness_uses_actual_recording") is not True
                    or record.get("candidate_panel_only") is not True):
                raise ValueError("Saved twenty-case publication binding changed")
        elif (record.get("schema") != alpha.PUBLICATION_SCHEMA
                or record.get("protocol") != alpha.PROTOCOL or record.get("config") != config.to_dict()
                or record.get("holding_policy") != alpha.HOLDING_POLICY
                or [row["case"] for row in record["cases"]] != list(case_ids)
                or any(record.get(key) is not value for key, value in alpha.PUBLICATION_FLAGS.items())
                or record.get("calendar_sha256") != head["binding"]["calendar_sha256"]
                or str(Path(record["calendar"]).resolve()) != str(Path(head["binding"]["calendar"]).resolve())
                or record.get("publication_origin") != "local_recording_now"
                or record.get("frozen_model_contract") != head["binding"]["frozen_model_contract"]):
            raise ValueError("Saved Alpha158 publication/model/calendar binding changed")
        result[day] = record
    days = list(result)
    if days and (days != sorted(set(days)) or [d for d in sessions if days[0] <= d <= days[-1]] != days):
        raise ValueError("Saved actual publications are not a consecutive original prefix")
    if family == "leader20" and days != head["binding"]["window"][:len(days)]:
        raise ValueError("Saved twenty-case decision window changed")
    return result


def _pending(receipt, context, bound, day, execution, family):
    if receipt is None:
        return None
    if (receipt.get("decision_day") != day or receipt.get("execution_not_before") != execution
            or receipt.get("decision_at") != bound["decided_at"]):
        raise ValueError("Saved target receipt date/clock differs from its original journal")
    if receipt.get("schema") == PUBLISHED_CLOSE_REDUCED_INTENT_SCHEMA and family == "leader20":
        if (receipt.get("original_intent") != bound or receipt.get("policy_id") != leader.TARGET_POLICY_ID
                or receipt.get("reduced_intent", {}).get("kind") != "FULL_TARGET"):
            raise ValueError("Saved reduced target no longer belongs to the original journal/policy")
        return _intent(receipt, context)
    if receipt.get("schema") != PUBLISHED_CLOSE_INTENT_SCHEMA or receipt.get("intent") != bound:
        raise ValueError("Saved original target receipt differs from the bound publication")
    expected = {"schema": PUBLISHED_CLOSE_INTENT_SCHEMA, "decision_day": day,
        "decision_at": bound["decided_at"], "execution_not_before": execution, "intent": bound,
        "input_kind": "explicit_published_close_PortfolioIntent_not_SignalFrame",
        "source_availability_verified_by_engine": False, "source_verified": False,
        "pit_verified": False, "execution_admitted": False}
    if receipt != expected:
        raise ValueError("Saved original target receipt contract changed")
    return _intent(receipt["intent"], context)


def _row(directory, head, reference, key, family, config, published, current):
    case = head["cases"][key]
    prefix = "leader-published-forward:" if family == "leader20" else "recent-alpha158-forward:"
    context = RunContext(prefix + leader._hash(str(directory))[:16] + ":" + key, TradingMode.REPLAY)
    processed = [row["date"] for row in case["daily"]]
    if (case["context"] != context.to_dict() or case["state"].get("broker_binding")
            or processed != list(published)[:len(processed)]
            or case["state"]["day"] != (processed[-1] if processed else None)
            or len(case["journal"]) != len(processed)
            or (processed and round(case["daily"][-1]["equity"] * 100) != Ledger(case["state"], config, context).equity())):
        raise ValueError("Saved published case context/cursor/equity changed")
    if family == "leader20":
        if len(case["journal_files"]) != len(processed):
            raise ValueError("Saved independent case journal length changed")
        if processed and (case["state"].get("replay_risk_exit_policy") != {
                "schema": "replay_risk_exit_policy_v1", "policy_id": leader.RISK_POLICY_ID, "context": context.to_dict()}
                or case["state"].get("published_close_target_policy") != {
                "schema": "replay_published_close_target_policy_v1", "policy_id": leader.TARGET_POLICY_ID, "context": context.to_dict()}):
            raise ValueError("Saved twenty-case risk/target policies changed")
        leader._validate_case_journal(case, directory)
    elif (case["state"].get("replay_risk_exit_policy") is not None
            or case["state"].get("published_close_target_policy") is not None
            or case["state"].get("pending_risk_exits") is not None):
        raise ValueError("Alpha158 saved holding policy cannot acquire another exit policy")
    journal_ref = None
    last = None
    for index, entry in enumerate(case["journal"]):
        pub_ref = head["publications"][index]
        pub = published[processed[index]]
        original_row = next(row for row in pub["cases"] if row["case_id" if family == "leader20" else "case"] == key)
        original = PortfolioIntent.from_dict(original_row["portfolio_intent"])
        expected_bound = (replace(original, context=context, decided_at=pub["recorded_at"],
            execution_not_before=pub_ref["execution_day"],
            source=original.source or "research:leader_close_publication:" + key.split(":")[1])
            if family == "leader20" else replace(original, context=context))
        bound_key = "bound_original_intent" if family == "leader20" else "bound_intent"
        if (entry["decision_day"] != pub_ref["day"] or entry["execution_day"] != pub_ref["execution_day"]
                or entry["publication"] != pub_ref or entry["unbound_original_intent"] != original.to_dict()
                or entry[bound_key] != expected_bound.to_dict()
                or not _stamp(pub_ref["recorded_at"]) <= _stamp(entry["committed_at"]) <= current):
            raise ValueError("Saved journal intent/publication/context or actual commit changed")
        last = entry
    if last:
        journal_ref = case["journal_files"][-1] if family == "leader20" else reference
    day = processed[-1] if processed else None
    bound = last["bound_original_intent" if family == "leader20" else "bound_intent"] if last else None
    pending = _pending(case["state"].get("pending_published_close_intent"), context.to_dict(), bound,
                       day, last["execution_day"], family) if last else None
    if family == "alpha158" and last:
        expected_weights = PortfolioIntent.from_dict(bound).to_legacy_pending()
        if (case["state"].get("pending_targets") != expected_weights
                or ((expected_weights is None) != (pending is None))):
            raise ValueError("Embedded Alpha158 journal and actual target receipt disagree")
    positions = []
    for symbol, lots in case["state"]["positions"].items():
        if any(type(lot["qty"]) is not int or lot["qty"] < 0 for lot in lots):
            raise ValueError("Saved published-account share lots are invalid")
        quantity = sum(lot["qty"] for lot in lots)
        if quantity:
            positions.append({"symbol": symbol, "quantity_shares": quantity})
    return {"case_id": key, "processed_day": day, "context": context.to_dict(),
        "positions": positions, "latest_journal": journal_ref,
        "last_commit_at": last["committed_at"] if last else None,
        "pending_decision": pending, "last_published_decision": _intent(bound, context.to_dict()) if last else None,
        "error": case.get("error"), "blocking_evidence": case.get("blocking_evidence")}


def export_requests(run_dir, execution_day, *, current=None):
    current, execution_day = _stamp(current or datetime.now(MARKET_TZ)), _day(execution_day)
    directory = Path(run_dir).resolve()
    if not (directory / "head.json").is_file():
        raise ValueError("No saved published research head")
    head, reference = _read(directory / "head.json")
    family = "leader20" if head.get("schema") == leader.SCHEMA else "alpha158" if head.get("schema") == alpha.SCHEMA else None
    if family is None:
        raise ValueError("Unsupported saved published-account family")
    expected_flags = leader.FLAGS if family == "leader20" else alpha.FLAGS
    if (any(head.get(key) is not value for key, value in expected_flags.items())
            or _stamp(head["registered_at"]) > current
            or str(Path(head["binding"]["run_directory"]).resolve()) != str(directory)):
        raise ValueError("Saved published-account research boundary/directory/clock changed")
    ids = leader.CASE_IDS if family == "leader20" else tuple(case["id"] for case in alpha.PROTOCOL["cases"])
    if list(head["cases"]) != list(ids):
        raise ValueError("Saved published-account original case order changed")
    binding = head["binding"]
    config = StrategyConfig(**binding["config"]).validate()
    if family == "leader20":
        if (binding["risk_policy"] != leader.RISK_RULE_SPEC or binding["published_target_policy_id"] != leader.TARGET_POLICY_ID
                or binding["case_ids"] != list(ids) or binding["original_protocol"]["config"] != config.to_dict()):
            raise ValueError("Saved twenty-case method/config binding changed")
        calendar = binding["calendar"]
    else:
        calendar, calendar_ref = _read(binding["calendar"])
        if (calendar_ref["sha256"] != binding["calendar_sha256"] or binding["protocol"] != alpha.PROTOCOL
                or binding["holding_policy"] != alpha.HOLDING_POLICY or config.to_dict() != alpha._config(100000, 5).to_dict()):
            raise ValueError("Saved Alpha158 fixed calendar/model/holding binding changed")
    published = _publication_records(head, family, ids, calendar, config, current)
    normalized = {**head, "binding": {**binding, "calendar": calendar}}
    view = {"head_ref": reference, "saved_publications": head["publications"]}
    result = {}
    for key in ids:
        try:
            row = _row(directory, head, reference, key, family, config, published, current)
            facts = _case_facts(directory, view, normalized, row, execution_day, current)
            facts.update(schema=LEADER_SCHEMA if family == "leader20" else ALPHA_SCHEMA,
                source_family=family, strategy_id=key if family == "leader20" else "alpha158:" + key,
                created_at=current.isoformat(), source_journal_independently_sealed=family == "leader20",
                original_head_research_only_flag_present="research_only" in head)
            if family == "alpha158":
                facts["refs"]["latest_journal"].update(embedded_case=key, embedded_index=len(head["cases"][key]["journal"]) - 1,
                    evidence_scope=EMBEDDED_SCOPE)
                facts["refs"]["calendar"] = {"path": calendar_ref["file"], "sha256": calendar_ref["sha256"]}
            else:
                facts["refs"]["latest_journal"]["evidence_scope"] = "independently_sealed_original_case_journal"
            result[key] = facts
        except ValueError as error:
            result[key] = {"schema": NOT_READY_SCHEMA, "status": "not_ready", "source_family": family,
                "case_id": key, "run_directory": str(directory), "execution_day": execution_day,
                "reason": str(error), "refs": {"run_directory": str(directory),
                    "head": {"path": reference["file"], "sha256": reference["sha256"]}},
                "created_at": current.isoformat(), **FLAGS}
    if sha256((directory / "head.json").read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("Saved published-account head changed during export")
    return result


def export_request(run_dir, case_id, execution_day, *, current=None):
    result = export_requests(run_dir, execution_day, current=current)
    if case_id not in result:
        raise ValueError("Unknown original published-account case")
    if result[case_id]["schema"] == NOT_READY_SCHEMA:
        raise ValueError(result[case_id]["reason"])
    return result[case_id]


def read_request(payload, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    if (not isinstance(payload, dict) or payload.get("schema") not in SCHEMAS
            or any(payload.get(key) is not value for key, value in FLAGS.items())
            or payload.get("signal_publication_verified") is not False or payload.get("source_basis") != SOURCE_BASIS):
        raise ValueError("Saved published-account research-only boundary changed")
    created = _stamp(payload["created_at"])
    if not _stamp(payload["source_known_at"]) <= created <= current:
        raise ValueError("Saved published-account creation/knowledge clock changed")
    expected = export_request(payload["run_directory"], payload["case_id"], payload["execution_day"], current=current)
    expected["created_at"] = payload["created_at"]
    if payload != expected:
        raise ValueError("Saved published-account intent no longer equals its original source")
    return expected
