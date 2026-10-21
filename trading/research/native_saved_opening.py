"""Expose saved native preopen books and their explicit next published plan.

This adapter reads sealed files only.  In particular, an already consumed
``pending_targets=None`` is not the next decision, and no model or Engine is run.
"""
import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, RunContext, TradingMode
from .leader_daily_signals import _calendar_view
from .saved_account_intent import FLAGS as INTENT_FLAGS
from .saved_leader_account_intent import _day, _stamp, _weights
from .shortline_native_preopen import MODEL_KINDS, policy_spec


SOURCE_SCHEMA = "native_saved_opening_source_v1"
SCHEMA = "saved_native_preopen_account_intent_v1"
OPENING_SCHEMA = "native_preopen_saved_account_opening_v1"
HEAD_SCHEMA = "native_shortline_preopen_shared_ledger_checkpoint_v1"
JOURNAL_SCHEMA = "native_shortline_preopen_committed_session_v1"
FLAGS = {**INTENT_FLAGS, "engine_advanced": False, "ledger_mutated": False,
    "account_created": False, "broker_interacted": False}
SOURCE_BASIS = "saved_research_account_not_a_current_broker_signal"


class _NotReady(ValueError):
    pass


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read(path, expected=None):
    path = Path(path).resolve()
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Saved native metadata exceeds its bounded reader")
    data = path.read_bytes()
    ref = {"path": str(path), "sha256": sha256(data).hexdigest()}
    if expected is not None and ref != _ref(expected):
        raise ValueError("Saved native source bytes changed")
    return json.loads(data.decode("utf-8-sig")), ref


def _ref(value):
    return {"path": str(Path(value.get("path") or value["file"]).resolve()),
            "sha256": value["sha256"]}


def _boundary(value):
    if any(value.get(k) is not v for k, v in INTENT_FLAGS.items() if k != "prefill_allowed"):
        raise ValueError("Saved native research-only boundary changed")


def _source_path(source_index):
    path = Path(source_index).resolve()
    return path / "head.json" if path.is_dir() else path


def _context(directory, kind):
    return RunContext("native-preopen:" + _hash(str(directory))[:20] + ":" + kind,
                      TradingMode.REPLAY).to_dict()


def _facts(run_dir, plan_path, current):
    directory = Path(run_dir).resolve()
    head_path = directory / "head.json"
    path = Path(plan_path).resolve()
    path = path / "plan.json" if path.is_dir() else path
    if not head_path.is_file() or not path.is_file():
        raise _NotReady("exact_native_account_head_or_explicit_next_plan_missing")
    head, head_ref = _read(head_path)
    plan, plan_ref = _read(path)
    _boundary(head); _boundary(plan)
    if (head.get("schema") != HEAD_SCHEMA or list(head.get("cases", {})) != list(MODEL_KINDS)
            or plan.get("schema") != "native_shortline_preopen_research_plan_v1"
            or plan.get("status") != "published_preopen_research_plan"):
        raise ValueError("An exact saved native two-model account and published plan are required")
    binding = head["binding"]
    policy = plan["policy_id"]
    config = StrategyConfig(**binding["config"]).validate()
    if (binding["policy_id"] != policy or plan["spec"] != policy_spec(policy)
            or plan["spec"]["config"] != config.to_dict()
            or plan.get("publication_requires_saved_plan") is not True
            or plan.get("raw_score_target_generation_allowed") is not False
            or plan.get("raw_score_payloads_modified") is not False):
        raise ValueError("Native next plan cannot change policy, config or fixed score contract")
    d, e = _day(plan["source_day"]), _day(plan["execution_day"])
    calendar, calendar_ref = _read(plan["calendar"]["file"], plan["calendar"])
    sessions, _, status = _calendar_view(calendar, d, str(int(d[:4])) + "-12-31", [])
    if (plan["calendar"] != binding["calendar"] or calendar != binding["calendar_payload"]
            or status != "pinned_static_exchange_calendar" or d not in sessions
            or next((s for s in sessions if s > d), None) != e):
        raise ValueError("Native plan must use its account's exact calendar and next session")
    published = _stamp(plan["published_at"])
    opening = _stamp(e + "T09:30:00+08:00")
    if (plan["next_open_deadline"] != opening.isoformat()
            or not _stamp(d + "T15:00:00+08:00") <= published < opening or published > current):
        raise ValueError("Native next plan was not actually available before its exact opening")
    payloads, payload_refs = {}, []
    for item in plan["files"]:
        payload = (path.parent / item["path"]).resolve()
        if payload.parent != path.parent or payload.name in payloads:
            raise ValueError("Native plan payload must be unique and inside its published directory")
        data = payload.read_bytes()
        if len(data) != item["byte_count"] or sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Native plan payload changed after publication")
        payload_refs.append({"path": str(payload), "sha256": item["sha256"]})
        if payload.suffix == ".json":
            payloads[payload.name] = json.loads(data.decode("utf-8-sig"))
    expected_files = {"registration.json", "decisions.json", "lr_ranked_preview.csv", "lgbm_ranked_preview.csv"}
    if plan.get("qualification_scope_file") is not None:
        expected_files.add("qualification_scope.json")
    if ({Path(r["path"]).name for r in payload_refs} != expected_files
            or plan["decisions_file"] != "decisions.json" or plan["registration_file"] != "registration.json"):
        raise ValueError("Native plan must retain its exact published closed payload set")
    registration, decisions = payloads["registration.json"], payloads["decisions.json"]
    _boundary(registration); _boundary(decisions)
    if (registration["spec"] != plan["spec"] or registration["policy_id"] != policy
            or registration["source_day"] != d or registration["execution_day"] != e
            or registration["calendar"] != plan["calendar"]
            or registration["source_publication"] != plan["source_publication"]
            or registration["frozen_model_refs"] != binding["frozen_model_refs"]
            or not _stamp(d + "T15:00:00+08:00") <= _stamp(registration["registered_at"]) <= published
            or decisions.get("schema") != "native_shortline_preopen_research_decisions_v1"
            or decisions["policy_id"] != policy or decisions["source_day"] != d or decisions["execution_day"] != e
            or [r["model_kind"] for r in decisions["models"]] != list(MODEL_KINDS)):
        raise ValueError("Native decisions and registration do not belong to this exact account plan")
    score_pub, score_ref = _read(plan["source_publication"]["file"], plan["source_publication"])
    if _stamp(score_pub["published_at"]) > _stamp(registration["registered_at"]):
        raise ValueError("Native plan registration predates its actual score publication")
    consumed, journals = head["consumed_plans"], head["journal_files"]
    if not consumed or len(consumed) != len(journals):
        raise _NotReady("native_account_has_no_complete_committed_execution_session")
    if consumed[-1]["execution_day"] != d:
        raise _NotReady("explicit_next_plan_does_not_follow_current_native_account_day")
    latest = Path(journals[-1]["file"]).resolve()
    if latest.parent != directory / "journal":
        raise ValueError("Native session seal must stay inside its original run")
    journal, journal_ref = _read(latest, journals[-1])
    _boundary(journal)
    if (journal.get("schema") != JOURNAL_SCHEMA or journal["plan"] != consumed[-1]
            or journal["execution_day"] != d or journal["source_day"] != consumed[-1]["source_day"]
            or journal["binding_sha256"] != _hash(binding)
            or journal["raw_sources_sha256"] != _hash(head["raw_sources"])
            or journal["previous_journal_sha256"] != (journals[-2]["sha256"] if len(journals) > 1 else None)):
        raise ValueError("Native latest session seal no longer binds its exact account inputs")
    committed = _stamp(journal["committed_at"])
    if not _stamp(d + "T15:00:00+08:00") <= committed < opening or committed > current:
        raise ValueError("Native saved session was not known before the planned opening")
    for kind in MODEL_KINDS:
        case = head["cases"][kind]
        if (case["kind"] != kind or case["context"] != _context(directory, kind)
                or binding["contexts"][kind] != case["context"] or case["state"].get("broker_binding")
                or case["state"]["day"] != d or len(case["daily"]) != len(consumed)
                or [row["date"] for row in case["daily"]] != [row["execution_day"] for row in consumed]
                or journal["post_case_sha256"][kind] != _hash(case)):
            raise ValueError("Native saved model book differs from its sealed session/context")
        row = next(r for r in decisions["models"] if r["model_kind"] == kind)
        weights = _weights(row["weights"], config)
        if (row["source_day"] != d or row["execution_day"] != e
                or row["decision"] not in {"FULL_TARGET", "NO_CHANGE"}
                or (row["decision"] == "NO_CHANGE") != (weights is None)):
            raise ValueError("Native next model decision must explicitly preserve FULL versus NO_CHANGE")
    refs = {"native_head": head_ref, "plan": plan_ref,
        "decisions": next(r for r in payload_refs if Path(r["path"]).name == "decisions.json"),
        "latest_journal": journal_ref, "calendar": calendar_ref, "score_publication": score_ref}
    dependencies = list(refs.values()) + payload_refs
    return {"head": head, "plan": plan, "decisions": decisions, "journal": journal,
        "refs": refs, "dependencies": list({r["path"]: r for r in dependencies}.values()),
        "source_known_at": max(published, committed).isoformat()}


def source_dependencies_current(source_index):
    """Cheap exact-ref freshness; it never discovers a different dated plan."""
    try:
        source, _ = _read(_source_path(source_index))
        if source.get("schema") != SOURCE_SCHEMA:
            return False
        for ref in source["dependencies"]:
            path = Path(ref["path"])
            if path.stat().st_size > 10 * 1024 * 1024 or sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def build_source(run_dir, plan, out, *, current=None):
    """Write only a separate composite index, never an original research book."""
    current = _stamp(current or datetime.now(MARKET_TZ))
    directory = Path(out).resolve()
    original, plan_path = Path(run_dir).resolve(), Path(plan).resolve()
    for protected in (original, plan_path if plan_path.is_dir() else plan_path.parent):
        if directory == protected or directory in protected.parents or protected in directory.parents:
            raise ValueError("Native composite source must not overlap original account or plan directories")
    try:
        facts = _facts(original, plan, current)
    except _NotReady as error:
        return {"schema": SOURCE_SCHEMA, "status": "waiting_for_exact_account_and_next_plan",
            "reason": str(error), "native_run_directory": str(original), "head": None, **FLAGS}
    record = facts["plan"]
    source = {"schema": SOURCE_SCHEMA, "status": "saved_native_account_and_next_plan",
        "source_family": "native_preopen", "case_ids": list(MODEL_KINDS),
        "native_run_directory": str(original), "run_directory": str(directory),
        "source_day": record["source_day"], "execution_day": record["execution_day"],
        "published_at": record["published_at"], "source_known_at": facts["source_known_at"],
        "policy_id": record["policy_id"], "spec": deepcopy(record["spec"]),
        "execution_config": deepcopy(facts["head"]["binding"]["config"]),
        "contexts": deepcopy(facts["head"]["binding"]["contexts"]),
        "refs": facts["refs"], "dependencies": facts["dependencies"],
        "created_at": current.isoformat(), **FLAGS}
    source["input_id"] = _hash({k: v for k, v in source.items() if k != "created_at"})
    head_path = directory / "head.json"
    if head_path.exists():
        previous, _ = _read(head_path)
        if previous.get("input_id") == source["input_id"]:
            return previous
        if (previous.get("schema") != SOURCE_SCHEMA or previous.get("native_run_directory") != str(original)
                or previous.get("policy_id") != source["policy_id"]):
            raise ValueError("Composite source cannot switch its original run or native policy")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "head.tmp.json"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(source, stream, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(temporary, head_path)
    return source


def _load(source_index, current):
    path = _source_path(source_index)
    source, ref = _read(path)
    if (source.get("schema") != SOURCE_SCHEMA or source.get("run_directory") != str(path.parent)
            or source.get("case_ids") != list(MODEL_KINDS)
            or any(source.get(k) is not v for k, v in FLAGS.items())
            or not source_dependencies_current(path)):
        raise ValueError("Native composite source is stale, corrupt or not research-only")
    facts = _facts(source["native_run_directory"], source["refs"]["plan"]["path"], current)
    if (source["refs"] != facts["refs"] or source["dependencies"] != facts["dependencies"]
            or source["source_known_at"] != facts["source_known_at"]
            or source["source_day"] != facts["plan"]["source_day"]
            or source["execution_day"] != facts["plan"]["execution_day"]
            or source["policy_id"] != facts["plan"]["policy_id"]
            or source["spec"] != facts["plan"]["spec"]
            or source["execution_config"] != facts["head"]["binding"]["config"]
            or source["contexts"] != facts["head"]["binding"]["contexts"]
            or not _stamp(source["source_known_at"]) <= _stamp(source["created_at"]) <= current):
        raise ValueError("Native composite source no longer describes its exact saved originals")
    expected = {k: v for k, v in source.items() if k not in {"input_id", "created_at"}}
    if source["input_id"] != _hash(expected):
        raise ValueError("Native composite source metadata changed")
    return source, ref, facts


def export_requests(source_index, execution_day=None, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    source, ref, facts = _load(source_index, current)
    if execution_day is not None and _day(execution_day) != source["execution_day"]:
        raise ValueError("Native export must retain the exact published execution day")
    result = {}
    for kind in MODEL_KINDS:
        row = next(r for r in facts["decisions"]["models"] if r["model_kind"] == kind)
        result[kind] = {"schema": SCHEMA, "source_family": "native_preopen", "case_id": kind,
            "strategy_id": "native_preopen:" + source["policy_id"] + ":" + kind,
            "run_directory": source["run_directory"], "native_run_directory": source["native_run_directory"],
            "source_day": source["source_day"], "execution_day": source["execution_day"],
            "kind": row["decision"], "weights": deepcopy(row["weights"]),
            "original_weights": deepcopy(row["weights"]), "queued_exit": None, "remaining_exit_symbols": [],
            "target_version": len(facts["head"]["consumed_plans"]),
            "target_version_basis": "processed_native_execution_session_count_not_plan_lifecycle_version",
            "source_known_at": source["source_known_at"], "created_at": current.isoformat(),
            "account_scope": "whole_observed_account", "execution_config": deepcopy(source["execution_config"]),
            "context": deepcopy(source["contexts"][kind]), "policy_id": source["policy_id"],
            "plan_decision": deepcopy(row), "refs": {"head": ref, **deepcopy(source["refs"])},
            "signal_publication_verified": False, "source_basis": SOURCE_BASIS, **FLAGS}
    return result


def read_request(payload, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("An exact saved native research intent is required")
    expected = export_requests(payload["refs"]["head"]["path"], payload["execution_day"], current=current)[payload["case_id"]]
    created = _stamp(payload["created_at"])
    if not _stamp(expected["source_known_at"]) <= created <= current:
        raise ValueError("Saved native intent creation clock changed")
    expected["created_at"] = payload["created_at"]
    if expected != payload:
        raise ValueError("Saved native intent differs from its exact next plan and account")
    return expected


def load_saved_case(source_index, case_id, *, current=None):
    """Return the sealed original Ledger and its prior executed preopen receipt."""
    current = _stamp(current or datetime.now(MARKET_TZ))
    source, source_ref, facts = _load(source_index, current)
    if case_id not in MODEL_KINDS:
        raise ValueError("Unknown native model case")
    case = facts["head"]["cases"][case_id]
    prior = facts["head"]["consumed_plans"][-1]
    old, old_ref = _read(prior["plan"]["file"], prior["plan"])
    row = case["daily"][-1]
    receipt = row.get("preopen_intent_frontier")
    envelope = receipt.get("envelope") if isinstance(receipt, dict) else None
    bound = envelope.get("intent") if isinstance(envelope, dict) else None
    executed, reason = None, "latest_daily_has_no_exact_consumed_preopen_receipt"
    if (isinstance(bound, dict) and row.get("plan") == prior["plan"]
            and old["source_day"] == prior["source_day"] and old["execution_day"] == source["source_day"]
            and old["published_at"] == prior["published_at"] and old["policy_id"] == source["policy_id"]
            and envelope.get("source_day") == prior["source_day"]
            and envelope.get("execution_day") == source["source_day"]
            and envelope.get("published_at") == prior["published_at"]
            and bound.get("context") == case["context"]
            and bound.get("source") == f"research:shortline_native_preopen:{old_ref['sha256']}:{case_id}"):
        executed = {"source_day": prior["source_day"], "execution_day": source["source_day"],
            "kind": bound["kind"], "weights": deepcopy(bound["weights"]),
            "published_at": prior["published_at"], "ref": old_ref,
            "context": deepcopy(case["context"]), "consumed_receipt": deepcopy(receipt)}
        reason = None
    return {"state": deepcopy(case["state"]), "daily": deepcopy(row),
        "context": deepcopy(case["context"]), "config": deepcopy(source["execution_config"]),
        "ledger_day": source["source_day"], "executed_decision": executed,
        "executed_decision_reason": reason,
        "refs": {"head": source_ref, **deepcopy(source["refs"]), "executed_plan": old_ref},
        "basis": "exact_native_head_and_independent_two_model_session_seal", **FLAGS}


def build_opening(source_index, out, *, current=None):
    from .leader_eight_type_opening import _case_preview, _write
    from . import opening_pack
    current = _stamp(current or datetime.now(MARKET_TZ))
    source, source_ref, facts = _load(source_index, current)
    intents = export_requests(source_index, current=current)
    quotes, price_evidence = {}, None
    if any(intent["kind"] == "FULL_TARGET" for intent in intents.values()):
        from .native_saved_opening_prices import read_quotes
        wanted = {symbol for intent in intents.values() for symbol in (intent["weights"] or {})}
        wanted.update(symbol for case in facts["head"]["cases"].values()
                      for symbol, lots in case["state"]["positions"].items() if sum(lot["qty"] for lot in lots) > 0)
        quotes, price_evidence = read_quotes(source, facts, wanted, current=current)
    directory = Path(out).resolve()
    for protected in (Path(source["run_directory"]), Path(source["native_run_directory"]), Path(source["refs"]["plan"]["path"]).parent):
        if directory == protected or directory in protected.parents or protected in directory.parents:
            raise ValueError("Native opening output must not overlap the source, account or published plan")
    if directory.exists():
        raise FileExistsError("Use a new native opening artifact directory")
    directory.mkdir(parents=True, exist_ok=False)
    price_ref = None
    if price_evidence is not None:
        _write(directory / "price_evidence.json", price_evidence)
        _, price_ref = _read(directory / "price_evidence.json")
    rows = []
    for kind, intent in intents.items():
        # An exact-D raw close sizes the original target; it is never a broker
        # quote. NO_CHANGE needs no price to retain the original exact shares.
        row = _case_preview(directory / kind, intent, facts["head"]["cases"][kind], quotes, current=current)
        if price_ref is not None and row["status"] == "paper_preview_only":
            preview, _ = _read(row["preview"])
            preview["price_evidence"] = price_ref
            preview["source_quote_status_by_symbol"] = {symbol: {key: quote.get(key) for key in
                ("trading_date", "tradestatus", "is_st")} for symbol, quote in quotes.items()}
            _write(row["preview"], preview)
        rows.append({**row, "strategy_id": intent["strategy_id"]})
    if not source_dependencies_current(source_index) or _read(_source_path(source_index))[1] != source_ref:
        raise ValueError("Native originals changed before opening pack publication")
    previews = [r["preview"] for r in rows if r["status"] == "paper_preview_only"]
    pack_dir, pack = opening_pack.build(previews, execution_day=source["execution_day"]) if previews else (None, None)
    if price_evidence is not None:
        from .native_saved_opening_prices import validate_quote_evidence
        validate_quote_evidence(price_evidence, current=current)
    result = {"schema": OPENING_SCHEMA, "source_family": "native_preopen",
        "status": "paper_candidates_only" if previews else "not_ready", "generated_at": current.isoformat(),
        "source_run_directory": source["run_directory"], "source_head_sha256": source_ref["sha256"],
        "source_cutoff": source["source_day"], "execution_day": source["execution_day"],
        "policy_id": source["policy_id"], "case_count": len(rows), "preview_count": len(previews),
        "price_evidence": price_ref,
        "not_ready_count": len(rows)-len(previews), "cases": rows,
        "opening_pack": str(pack_dir / "opening_pack.json") if pack_dir else None,
        "candidate_order_count": pack["candidate_order_count"] if pack else None,
        "independent_cases_combined_into_account": False, "automatic_strategy_selection": False,
        "candidate_orders_are_fills": False, **FLAGS}
    _write(directory / "result.json", result)
    return directory, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    source = commands.add_parser("source", help="Bind an explicit original account and next published plan")
    source.add_argument("--run-dir", required=True)
    source.add_argument("--plan", required=True)
    source.add_argument("--out", required=True)
    opening = commands.add_parser("build", help="Build copied-Ledger paper previews from one saved composite source")
    opening.add_argument("--source", required=True)
    opening.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "source":
        value = build_source(args.run_dir, args.plan, args.out)
        print(json.dumps({key: value.get(key) for key in ("status", "run_directory", "source_day", "execution_day", "reason")}, ensure_ascii=False))
    else:
        directory, value = build_opening(args.source, args.out)
        print(json.dumps({"directory": str(directory), **{key: value.get(key) for key in
            ("status", "source_cutoff", "execution_day", "case_count", "preview_count", "candidate_order_count")}}, ensure_ascii=False))
    return 0
