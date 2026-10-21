"""Read saved native preopen plans, CLI observations and isolated checkpoints."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path

from ..contracts import MARKET_TZ, PreopenPortfolioIntent, RunContext, TradingMode
from ..settings import DATA_ROOT, ROOT
from .shortline_native_preopen import (DECISIONS_SCHEMA, FLAGS as PLAN_FLAGS, MODEL_KINDS,
    POLICY_ID, QUALIFIED_POLICY_ID, REGISTRATION_SCHEMA, SCHEMA as PLAN_SCHEMA, SPEC,
    load_preopen_plan, policy_spec)
from .shortline_native_preopen_forward import (FLAGS as FORWARD_FLAGS, JOURNAL_SCHEMA,
    IPO_APPEND_METHOD_ID, METHOD_ID, RESULT_SCHEMA, RULE_SPEC, SCHEMA as HEAD_SCHEMA,
    forward_rule_spec, load_qualification_plan_prefix)


SCHEMA = "native_shortline_preopen_status_view_v1"
SCOPE = "20261016_source1015_v1"
QUALIFIED_SCOPE = "20261016_source1015_ipoqualified_v1"
POLICY_SCOPES = {POLICY_ID: SCOPE, QUALIFIED_POLICY_ID: QUALIFIED_SCOPE}
COMPARISON_SCHEMA = "native_shortline_preopen_comparison_view_v1"
FAILURE_SCHEMA = "native_shortline_preopen_forward_command_failure_v1"
FLAGS = {**FORWARD_FLAGS, "read_only": True, "runner_called": False, "model_scored": False,
    "ledger_mutated": False, "account_created": False, "orders_generated": False,
    "fallback_to_older_success": False, "automatic_promotion": False}


def _stamp(value):
    text = str(value)
    if text.endswith(" UTC"):
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).astimezone(MARKET_TZ)
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Saved research clocks require explicit timezone offsets")
    return stamp.astimezone(MARKET_TZ)


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Canonical saved session date required")
    return value


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read(file):
    file = Path(file).resolve()
    data = file.read_bytes()
    return json.loads(data.decode("utf-8-sig")), {"file": str(file),
        "sha256": sha256(data).hexdigest(), "byte_count": len(data)}


def _bound(reference):
    value, proof = _read(reference["file"])
    if proof != reference:
        raise ValueError("Saved small-file reference bytes changed")
    return value


def _flags(value, expected):
    if any(value.get(key) is not flag for key, flag in expected.items()):
        raise ValueError("Saved research boundary flags changed")


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("Saved accounting count/money must be a nonnegative integer")
    return value


def _saved_cents(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Saved Engine money must be finite")
    cents = Decimal(str(value)) * 100
    if cents != cents.to_integral_value():
        raise ValueError("Saved Engine money is not on its original cent basis")
    return _integer(int(cents))


def _discover_plan(data_root, policy_id=POLICY_ID):
    candidates = []
    for file in (data_root / "research/shortline_native_preopen").glob("*/plan.json"):
        # Names only route the established independent scope families. They do
        # not prove a policy: the selected saved payload is still validated.
        # Route before decoding so corrupt qualified JSON cannot hide v1.
        qualified_family = "ipoqualified" in file.parent.name
        if qualified_family != (policy_id == QUALIFIED_POLICY_ID):
            continue
        record, _ = _read(file)
        if record.get("policy_id") != policy_id:
            if qualified_family or file.parent.name in POLICY_SCOPES.values() or record.get("policy_id") == QUALIFIED_POLICY_ID:
                raise ValueError("Canonical saved plan scope and its actual policy disagree")
            continue
        candidates.append((_day(record["source_day"]), _stamp(record["published_at"]), str(file), file))
    return max(candidates)[-1] if candidates else None


def _qualified_plan(file, now):
    # This is validation of an existing publication, not score computation or
    # plan generation. Reuse its original registered-time IPO projection and
    # fixed-rank comparison rather than implementing a second set of rules.
    from pypdf.errors import PyPdfError
    try:
        loaded = load_preopen_plan(file)
    except PyPdfError as error:
        raise ValueError(f"Saved qualified IPO source PDF is unreadable: {error}") from error
    plan, registration = loaded["record"], loaded["registration"]
    if _stamp(plan["published_at"]) > now:
        raise ValueError("Saved qualified plan was not published at this read frontier")
    scope_file = Path(loaded["plan_ref"]["file"]).parent / plan["qualification_scope_file"]
    scope, scope_ref = _read(scope_file)
    reference = next(item for item in plan["files"] if item["path"] == plan["qualification_scope_file"])
    if (scope_ref["sha256"] != reference["sha256"] or scope_ref["byte_count"] != reference["byte_count"]):
        raise ValueError("Qualified scope changed during read validation")
    fields = ("raw_stage_membership_complete", "raw_unknown_stage_symbols", "excluded_no_limit_symbols",
        "remaining_unknown_stage_symbols", "scored_no_limit_conflicts", "qualified_stage_membership_complete", "decision_as_of")
    stage_scope = {key: scope[key] for key in fields}
    stage_scope.update(qualification_inputs=scope["qualification_inputs"], source_ref=scope_ref)
    for item in registration["frozen_model_refs"].values():
        _, current = _read(item["manifest_file"])
        if current["sha256"] != item["manifest_sha256"] or _stamp(item["model_available_at"]) > _stamp(plan["published_at"]):
            raise ValueError("Frozen model manifest changed or was not yet available")
    keys = ("model_kind", "decision", "reason", "weights", "selected_count", "scope_complete", "target_cash_weight")
    return {"record": plan, "ref": loaded["plan_ref"], "registration": registration,
        "canonical_loaded": loaded,
        "calendar": _bound(plan["calendar"]), "models": [{key: loaded["decisions"][kind][key] for key in keys} for kind in MODEL_KINDS],
        "unknown_stage_symbols": scope["raw_unknown_stage_symbols"],
        "model_generation": registration["frozen_model_refs"], "stage_scope": stage_scope,
        "qualification_proof": {"qualification_inputs": scope["qualification_inputs"],
            "qualification_verification": scope["qualification_verification"]},
        "validation_basis": "canonical_saved_plan_registered_time_IPO_projection_and_rank_comparison_only"}


def _plan(file, now, expected_policy_id=POLICY_ID):
    plan, proof = _read(file)
    _flags(plan, PLAN_FLAGS)
    spec = policy_spec(expected_policy_id)
    if plan.get("policy_id") != expected_policy_id:
        raise ValueError("Saved plan belongs to a different independent policy")
    if expected_policy_id == QUALIFIED_POLICY_ID:
        return _qualified_plan(file, now)
    if (plan.get("schema") != PLAN_SCHEMA or plan.get("status") != "published_preopen_research_plan"
            or plan.get("spec") != spec):
        raise ValueError("Saved plan is not the fixed native preopen research method")
    d, e, published = _day(plan["source_day"]), _day(plan["execution_day"]), _stamp(plan["published_at"])
    calendar = _bound(plan["calendar"])
    from .leader_daily_signals import _calendar_view
    year = calendar["year"]
    sessions, previous, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
    if (status != "pinned_static_exchange_calendar" or previous.get(e) != d
            or not _stamp(d + "T15:00:00+08:00") < published < _stamp(e + "T09:30:00+08:00")
            or published > now or _stamp(plan["next_open_deadline"]) != _stamp(e + "T09:30:00+08:00")):
        raise ValueError("Saved plan publication/session is late, future or inconsistent")
    files = {}
    for reference in plan["files"]:
        path = (Path(file).resolve().parent / reference["path"]).resolve()
        if path.parent != Path(file).resolve().parent or path.name in files:
            raise ValueError("Saved plan payload escaped its directory or repeated")
        data = path.read_bytes()
        if len(data) != reference["byte_count"] or sha256(data).hexdigest() != reference["sha256"]:
            raise ValueError("Saved plan payload changed")
        files[path.name] = data
    if set(files) != {"registration.json", "decisions.json", "lr_ranked_preview.csv", "lgbm_ranked_preview.csv"}:
        raise ValueError("Saved plan must retain its complete four-file publication")
    registration = json.loads(files["registration.json"].decode("utf-8-sig"))
    decisions = json.loads(files["decisions.json"].decode("utf-8-sig"))
    _flags(registration, PLAN_FLAGS)
    _flags(decisions, PLAN_FLAGS)
    publication = _bound(plan["source_publication"])
    if (publication.get("schema") != "native_shortline_raw_score_publication_v1"
            or publication.get("target_generation_allowed") is not False
            or publication.get("execution_admitted") is not False):
        raise ValueError("Plan must retain its research-only raw-score publication")
    if (registration.get("schema") != REGISTRATION_SCHEMA or registration.get("spec") != SPEC
            or registration.get("policy_id") != POLICY_ID or registration["calendar"] != plan["calendar"]
            or registration["source_publication"] != plan["source_publication"]
            or not _stamp(publication["published_at"]) <= _stamp(registration["registered_at"]) <= published
            or decisions.get("schema") != DECISIONS_SCHEMA or decisions.get("policy_id") != POLICY_ID
            or any(value.get("source_day") != d or value.get("execution_day") != e for value in (registration, decisions))
            or [m["model_kind"] for m in decisions["models"]] != list(MODEL_KINDS)):
        raise ValueError("Plan registration, decisions and actual source publication differ")
    models, unknown = [], None
    for item in decisions["models"]:
        _flags(item, PLAN_FLAGS)
        scope = item["input_scope"]
        missing = scope["unknown_stage_symbols"]
        if (not isinstance(missing, list) or len(set(missing)) != len(missing)
                or item["source_day"] != d or item["execution_day"] != e
                or item["scope_complete"] is not (scope["stage_membership_complete"] is True and not missing)):
            raise ValueError("Both plan models must preserve the complete/unknown source scope")
        if unknown is not None and unknown != missing:
            raise ValueError("Model unknown membership differs")
        unknown = missing
        weights = item["weights"]
        if item["decision"] == "NO_CHANGE":
            if weights is not None or item["selected_count"] != 0:
                raise ValueError("NO_CHANGE is unknown/preserve, not an empty FULL_TARGET")
        elif item["decision"] == "FULL_TARGET":
            if (not isinstance(weights, dict) or not weights or len(weights) > SPEC["holdings"]
                    or any(type(w) not in (int, float) or not math.isfinite(w) or not 0 < w <= SPEC["single_limit"] for w in weights.values())
                    or sum(weights.values()) > SPEC["gross_limit"] + 1e-12
                    or item["scope_complete"] is not True):
                raise ValueError("Saved FULL_TARGET must preserve its fixed caps and known scope")
        else:
            raise ValueError("Unknown saved portfolio-intent kind")
        models.append({**{key: item[key] for key in ("model_kind", "decision", "reason", "weights", "selected_count", "scope_complete")},
            "target_cash_weight": item.get("target_cash_weight")})
    generation = registration["frozen_model_refs"]
    if list(generation) != list(MODEL_KINDS) or generation["lr"] != generation["lgbm"]:
        raise ValueError("Both model plans must retain the same generation")
    for item in generation.values():
        _, current = _read(item["manifest_file"])
        if current["sha256"] != item["manifest_sha256"] or _stamp(item["model_available_at"]) > published:
            raise ValueError("Frozen model manifest changed or was not yet available")
    return {"record": plan, "ref": proof, "registration": registration, "calendar": calendar,
        "models": models, "unknown_stage_symbols": unknown, "model_generation": generation,
        "stage_scope": {"raw_stage_membership_complete": scope["stage_membership_complete"],
            "raw_unknown_stage_symbols": unknown, "excluded_no_limit_symbols": [],
            "remaining_unknown_stage_symbols": unknown, "scored_no_limit_conflicts": [],
            "qualified_stage_membership_complete": None, "decision_as_of": None,
            "qualification_inputs": None, "source_ref": None},
        "validation_basis": "saved_original_policy_payloads_and_account_refs_read_only"}


def _append_qualification_history(head, plan, now):
    """Read each original plan's IPO prefix without rebinding saved accounts."""
    from pypdf.errors import PyPdfError
    bound, consumed = head["binding"], head["consumed_plans"]
    inception = bound["stage_membership_qualification"]
    inputs = inception["qualification_inputs"]
    loaded = []
    try:
        for item in consumed:
            _bound(item["plan"])
            original = load_preopen_plan(item["plan"]["file"])
            if (original["plan_ref"] != item["plan"]
                    or original["source_day"] != item["source_day"]
                    or original["execution_day"] != item["execution_day"]
                    or original["record"]["published_at"] != item["published_at"]
                    or original["record"]["policy_id"] != QUALIFIED_POLICY_ID
                    or original["calendar"] != bound["calendar"]
                    or original["registration"]["frozen_model_refs"] != bound["frozen_model_refs"]
                    or _stamp(original["record"]["published_at"]) > now):
                raise ValueError("Saved append-only IPO history changed an original consumed plan")
            loaded.append(original)
        current = plan["canonical_loaded"]
        matching = [item for item in loaded if item["source_day"] == current["source_day"]]
        if matching:
            if matching[0]["plan_ref"] != current["plan_ref"]:
                raise ValueError("Current plan replaced an already consumed source session")
        elif loaded and current["source_day"] > loaded[-1]["source_day"]:
            loaded.append(current)
        else:
            raise ValueError("Current append-only IPO plan precedes the saved account prefix")
        prefix = load_qualification_plan_prefix(loaded, inputs["ipo_evidence"]["file"],
            inputs["research_master_run"]["file"], bound["calendar"]["file"])
    except PyPdfError as error:
        raise ValueError(f"Saved append-only IPO source PDF is unreadable: {error}") from error
    if (prefix["inception"] != inception
            or bound["ipo_qualification"] != inception["qualification_verification"]):
        raise ValueError("Saved append-only IPO inception source differs from its original binding")
    for item, evidence in zip(consumed, prefix["entries"]):
        if (item.get("qualification_evidence") != evidence["proof"]
                or item.get("qualification_scope") != evidence["scope"]):
            raise ValueError("Saved per-session IPO evidence differs from its original plan prefix")


def _head(file, directory, plan, now):
    head, proof = _read(file)
    _flags(head, FORWARD_FLAGS)
    bound = head["binding"]
    policy_id = plan["record"]["policy_id"]
    spec = policy_spec(policy_id)
    ipo_append_only = bound.get("method_id") == IPO_APPEND_METHOD_ID
    expected_method = IPO_APPEND_METHOD_ID if ipo_append_only else METHOD_ID
    if (head.get("schema") != HEAD_SCHEMA or bound.get("method_id") != expected_method
            or bound.get("rule_spec") != forward_rule_spec(policy_id, ipo_append_only=ipo_append_only)
            or (ipo_append_only and bound.get("ipo_append_only") is not True)
            or (not ipo_append_only and "ipo_append_only" in bound)
            or bound.get("policy_id") != policy_id
            or bound.get("config") != spec["config"] or bound.get("calendar") != plan["record"]["calendar"]
            or bound.get("calendar_payload") != plan["calendar"]
            or bound.get("frozen_model_refs") != plan["model_generation"]
            or bound.get("mode") != TradingMode.REPLAY.value or Path(bound["run_directory"]).resolve() != directory
            or list(head["cases"]) != list(MODEL_KINDS) or _stamp(head["registered_at"]) > now):
        raise ValueError("Saved checkpoint context, model, policy, config or calendar differs from this plan")
    if (not ipo_append_only and policy_id == QUALIFIED_POLICY_ID and bound.get("stage_membership_qualification") != plan["qualification_proof"]
            or policy_id == POLICY_ID and "stage_membership_qualification" in bound):
        raise ValueError("Saved checkpoint changed its original qualification-source policy")
    consumed, references, raw = head["consumed_plans"], head["journal_files"], head["raw_sources"]
    if not consumed or len(consumed) != len(references) or [r["day"] for r in raw] != sorted({r["day"] for r in raw}):
        raise ValueError("Saved checkpoint has no complete paired account/journal prefix")
    if ipo_append_only:
        _append_qualification_history(head, plan, now)
    from .leader_daily_signals import _calendar_view
    year = plan["calendar"]["year"]
    _, previous, _ = _calendar_view(plan["calendar"], f"{year}-01-01", f"{year}-12-31", [])
    if (bound["first_source_day"] != consumed[0]["source_day"]
            or any(previous.get(item["execution_day"]) != item["source_day"] for item in consumed)
            or any(a["execution_day"] != b["source_day"] for a, b in zip(consumed, consumed[1:]))):
        raise ValueError("Checkpoint consumed plans no longer form an exact adjacent session chain")
    prior, latest = None, None
    for item, reference in zip(consumed, references):
        journal_file = Path(reference["file"]).resolve()
        if journal_file.parent != directory / "journal":
            raise ValueError("Checkpoint journal escaped this isolated research run")
        latest = _bound(reference)
        if (latest.get("schema") != JOURNAL_SCHEMA or latest["plan"] != item
                or latest["source_day"] != item["source_day"] or latest["execution_day"] != item["execution_day"]
                or latest["binding_sha256"] != _hash(bound) or latest["previous_journal_sha256"] != prior
                or (ipo_append_only and latest.get("qualification_evidence") != item["qualification_evidence"])
                or latest["raw_sources_sha256"] != _hash([r for r in raw if r["day"] <= item["execution_day"]])
                or _stamp(latest["committed_at"]) > now):
            raise ValueError("Saved checkpoint independent journal/source prefix changed")
        prior = reference["sha256"]
    accounts = []
    for kind in MODEL_KINDS:
        case, state = head["cases"][kind], head["cases"][kind]["state"]
        context = RunContext.from_dict(case["context"])
        expected_context = RunContext("native-preopen:" + _hash(str(directory))[:20] + ":" + kind, TradingMode.REPLAY)
        days = [r["execution_day"] for r in consumed]
        if (context != expected_context or bound["contexts"][kind] != context.to_dict()
                or case["kind"] != kind or _hash(case) != latest["post_case_sha256"][kind]
                or state.get("broker_binding") or state["day"] != days[-1]
                or [row["date"] for row in case["daily"]] != days
                or state.get("preopen_execution_binding") != {"schema": "simulation_preopen_execution_binding_v1",
                    "context": context.to_dict(), "config": bound["config"]}):
            raise ValueError("Saved complete account/state/cursor changed from its independently sealed journal")
        receipt = state["last_preopen_intent"]
        envelope = PreopenPortfolioIntent.from_dict(receipt["envelope"])
        if (envelope.intent.context != context or envelope.source_day != consumed[-1]["source_day"]
                or envelope.execution_day != state["day"] or envelope.published_at != consumed[-1]["published_at"]):
            raise ValueError("Saved preopen receipt no longer matches its processed plan")
        cash, positions = _integer(state["cash"]), {}
        for symbol, lots in state["positions"].items():
            qty = sum(_integer(lot["qty"]) for lot in lots)
            if not qty: continue
            available = sum(lot["qty"] for lot in lots if _day(lot["available"]) <= state["day"])
            sell_reserved = any(o["symbol"] == symbol and o["side"] == "sell"
                and o["status"] in {"submitted", "partial", "cancel_pending"} for o in state["orders"].values())
            positions[symbol] = {"quantity": qty, "available": None if sell_reserved else available,
                "availability_basis": "saved_lots_at_processed_cursor_reservations_not_recomputed"}
        last = case["daily"][-1]
        if _saved_cents(last["cash"]) != cash or _integer(last["holdings"]) != len(positions):
            raise ValueError("Saved last Engine daily cash/holdings and checkpoint cursor differ")
        equity, receivables = _saved_cents(last["equity"]), _saved_cents(last["receivables"])
        blocked = [event for event in state["events"] if event.get("kind") == "order_blocked"]
        accounts.append({"model_kind": kind, "context": context.to_dict(), "last_processed_day": state["day"],
            "cash_cents": cash, "cash_available_cents": None, "equity_cents": equity,
            "receivables_cents": receivables, "positions": positions, "order_count": len(state["orders"]),
            "fill_count": len(state["fills"]), "orders": state["orders"],
            "fills": state["fills"], "daily": case["daily"], "metrics": None,
            "blocked_orders": blocked, "blocked_order_count": len(blocked),
            "pending_targets": state.get("pending_targets"), "pending_risk_exits": state.get("pending_risk_exits"),
            "money_unit": "cent", "daily_money_unit": "CNY_yuan", "simulated_account_only": True,
            "financial_summary_basis": "last_original_Engine_daily_not_recomputed_accounting",
            "cash_available_basis": "not_saved_as_scalar_in_checkpoint"})
    return head, proof, accounts, latest["committed_at"]


def _observation(file, directory, now, *, legacy=False, policy_id=POLICY_ID):
    record, proof = _read(file)
    if legacy:
        if policy_id != POLICY_ID or record.get("schema") != "native_preopen_forward_cli_observation_v1":
            raise ValueError("Unknown saved CLI observation schema")
        item, when = record["summary"], _stamp(record["observed_before_utc"])
        if item.get("schema") != RESULT_SCHEMA or item.get("execution_admitted") is not False or item.get("metrics") is not None:
            raise ValueError("Legacy observation must remain a research-only partial CLI summary")
        missing = ([record["planned_execution_day"]] if item["status"] == "waiting_for_exact_complete_raw_close"
            and record["planned_execution_day"] not in record["provided_source_days"] else [])
        data = {"status": item["status"], "observed_at": when.isoformat(),
            "observed_at_basis": "saved_before_CLI_call_clock_not_exact_finish",
            "evidence_level": "legacy_cli_summary_only", "missing_source_days": missing,
            "required_execution_day": record["planned_execution_day"], "error": None, "blocking_evidence": None,
            "checkpoint_claimed": item.get("checkpoint") is not None,
            "execution_progress_unknown": False, "full_result_saved": False}
    else:
        _flags(record, FORWARD_FLAGS)
        failure = record.get("schema") == FAILURE_SCHEMA
        if (record.get("schema") not in {RESULT_SCHEMA, FAILURE_SCHEMA}
                or Path(record["run_directory"]).resolve() != directory
                or (not failure and (record.get("metrics") is not None
                    or record.get("policy_id", POLICY_ID) != policy_id))):
            raise ValueError("Saved observation belongs to another run or changed its research boundary")
        when = _stamp(record["recorded_at"] if failure else record["read_at"])
        data = {"status": record["status"], "observed_at": when.isoformat(), "observed_at_basis": "saved_result_recording",
            "evidence_level": "command_failure_progress_unknown" if failure else "full_saved_runner_result",
            "missing_source_days": record.get("missing_source_days", []),
            "required_execution_day": record.get("required_execution_day", record.get("execution_day")),
            "error": record.get("error", record.get("reason")), "blocking_evidence": record.get("blocking_evidence"),
            "checkpoint_claimed": record.get("checkpoint") is not None or (not failure
                and (record.get("research_checkpoint_created") is True or record.get("case_count", 0) > 0)),
            "execution_progress_unknown": record.get("execution_progress_unknown", False), "full_result_saved": not failure}
    if when > now:
        raise ValueError("Saved observation clock is in the future")
    return {**data, "source_ref": proof, "stale_for_current_head": False}


def native_preopen_status(data_root=DATA_ROOT, workspace=ROOT, *, plan_file=None,
        observation_file=None, result_file=None, run_dir=None, now=None, policy_id=POLICY_ID):
    """Read one fixed current scope; never initialize or advance its accounts."""
    actual = _stamp(now if now is not None else datetime.now(MARKET_TZ))
    data_root, workspace = Path(data_root).resolve(), Path(workspace).resolve()
    policy_spec(policy_id)
    scope_name = POLICY_SCOPES[policy_id]
    plan_file = Path(plan_file).resolve() if plan_file else None
    directory = Path(run_dir).resolve() if run_dir else data_root / "research/shortline_native_preopen_forward" / scope_name
    observation_file = Path(observation_file).resolve() if observation_file else workspace / "outputs/shortline_native_preopen_forward" / scope_name / "actual_cli_observation.json"
    result_file = Path(result_file).resolve() if result_file else data_root / "research/shortline_native_preopen_forward_status" / (scope_name + ".json")
    base = {"schema": SCHEMA, "read_at": actual.isoformat(), "checked_at": actual.isoformat(),
        "status": "unavailable", "source_day": None, "execution_day": None, "plan_published_at": None,
        "policy_id": policy_id, "model_generation": None, "unknown_stage_symbols": None, "stage_scope": None,
        "validation_basis": None, "execution_method_id": None, "ipo_append_only": None,
        "models": [], "account_count": None, "accounts": [], "cash_cents": None, "equity_cents": None,
        "return_metrics": None, "observation": None, "head_committed_at": None, "money_unit": "cent",
        "accounts_are_independent_not_a_combined_user_account": True, "source_raw_reaudited": False,
        "source_refs": {"plan": None, "head": None, "observation": None}, **FLAGS}
    try:
        plan_file = plan_file if plan_file is not None else _discover_plan(data_root, policy_id)
        if plan_file is None or not plan_file.exists():
            return {**base, "reason": "saved_current_scope_plan_unavailable"}
        plan = _plan(plan_file, actual, policy_id)
        accounts, head, head_ref, committed = [], None, None, None
        head_file = directory / "head.json"
        if head_file.exists():
            head, head_ref, accounts, committed = _head(head_file, directory, plan, actual)
        observations = []
        if observation_file.exists(): observations.append(_observation(observation_file, directory, actual, legacy=True, policy_id=policy_id))
        if result_file.exists(): observations.append(_observation(result_file, directory, actual, policy_id=policy_id))
        attempts = sorted((directory / "attempts").glob("*.json"))
        if attempts: observations.append(_observation(attempts[-1], directory, actual, policy_id=policy_id))
        precedence = {"legacy_cli_summary_only": 0, "full_saved_runner_result": 1, "command_failure_progress_unknown": 2}
        observation = max(observations, key=lambda item: (item["observed_at"], precedence[item["evidence_level"]])) if observations else None
        if observation and observation["checkpoint_claimed"] and head is None:
            raise ValueError("Saved execution result claims a checkpoint whose account evidence is missing")
        cursor = accounts[0]["last_processed_day"] if accounts else None
        if observation and cursor:
            required = observation["required_execution_day"]
            observation["stale_for_current_head"] = (observation["observed_at"] < _stamp(committed).isoformat()
                or (required is not None and required <= cursor and observation["status"].startswith("waiting")))
        current_observation = observation if observation and not observation["stale_for_current_head"] else None
        failed = current_observation and (current_observation["status"].startswith(("blocked", "failed"))
            or current_observation["execution_progress_unknown"])
        waiting = current_observation and current_observation["status"].startswith("waiting")
        status = ("blocked_or_failed_research_books" if failed else "waiting_for_execution_source" if waiting else
            "active_research_books" if head else "saved_preopen_plan_only")
        return {**base, "status": status, "source_day": plan["record"]["source_day"],
            "execution_day": plan["record"]["execution_day"], "plan_published_at": plan["record"]["published_at"],
            "model_generation": plan["model_generation"], "unknown_stage_symbols": plan["unknown_stage_symbols"],
            "stage_scope": plan["stage_scope"], "validation_basis": plan["validation_basis"],
            "execution_method_id": head["binding"]["method_id"] if head else None,
            "ipo_append_only": head["binding"].get("ipo_append_only", False) if head else None,
            "models": plan["models"], "account_count": (None if head is None and observation
                and observation["execution_progress_unknown"] else len(accounts)), "accounts": accounts,
            "observation": observation, "head_committed_at": committed,
            "source_refs": {"plan": plan["ref"], "head": head_ref,
                "observation": observation["source_ref"] if observation else None}}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
        return {**base, "status": "unknown_saved_evidence", "error": f"{type(error).__name__}: {error}",
            "reason": "current_saved_evidence_missing_or_inconsistent_no_older_success_fallback"}


def native_preopen_comparison(data_root=DATA_ROOT, workspace=ROOT, *, policy_paths=None, now=None):
    """Show both independent saved hypotheses without selecting either one."""
    actual = _stamp(now if now is not None else datetime.now(MARKET_TZ))
    policy_paths = policy_paths or {}
    policies = []
    labels = {POLICY_ID: "原始阶段范围方案", QUALIFIED_POLICY_ID: "已知不限价新股排除方案"}
    for policy_id, scope in POLICY_SCOPES.items():
        view = native_preopen_status(data_root, workspace, now=actual,
            policy_id=policy_id, **policy_paths.get(policy_id, {}))
        policies.append({"policy_id": policy_id, "label": labels[policy_id], "scope": scope, "view": view})
    return {"schema": COMPARISON_SCHEMA, "status": "read_only_policy_comparison",
        "checked_at": actual.isoformat(), "read_at": actual.isoformat(), "policy_count": 2,
        "policies": policies, "selected_policy_id": None, "automatic_policy_selection": False,
        "independent_accounts_not_combined": True, "comparison_is_not_strategy_validation": True, **FLAGS}
