"""Read the saved unbound 32-case plan, without loading research producers."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path


SCHEMA = "leader_eight_type_unbound_close_paper_plan_v1"
TYPES = ("space_height_leader", "relay_continuation_leader", "trend_leader", "liquidity_capacity_proxy",
         "cross_regime_candidate", "rebound_repair_candidate", "sector_catchup_leader", "turnover_leader")
GUARDS = ("unguarded", "two_close_envguard")
CAPACITIES = ("causal_first_seen_cap20", "uncapped")
CASE_KEYS = tuple((identity, guard, capacity) for identity in TYPES for guard in GUARDS for capacity in CAPACITIES)
MODES = {"NO_CHANGE", "EXIT_ALL", "ALLOW_FIXED_CANDIDATES"}
PHASES = {"broad_strength_snapshot", "mixed_breadth_snapshot", "broad_weakness_snapshot", "unknown"}
FALSE_FLAGS = ("execution_admitted", "selection_enabled", "strategy_validated", "current_signal_admitted",
    "account_accessed", "engine_advanced", "orders_generated", "ths_payload_generated", "broker_interacted",
    "model_refit", "future_labels_used_for_score", "provider_contacted", "account_created", "ledger_mutated",
    "historical_publication_verified", "pit_verified", "corporate_actions_verified", "case_selected")
COMMON_FALSE = ("execution_admitted", "selection_enabled", "strategy_validated", "current_signal_admitted")
LOCAL = timezone(timedelta(hours=8))


def _stamp(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Saved paper plan clocks require an explicit timezone")
    return stamp.astimezone(LOCAL)


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Canonical saved paper-plan session dates required")
    return value


def _boundary(record, *, full=True):
    if (not isinstance(record, dict) or record.get("candidate_panel_only") is not True
            or (full and record.get("research_only") is not True)
            or any(record.get(key) is not False for key in (FALSE_FLAGS if full else COMMON_FALSE))):
        raise ValueError("Saved eight-type paper plan changed its unbound research boundary")


def _text(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Saved paper-plan reference or explanation is missing")
    return value


def _texts(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Saved rule descriptions must remain a list of strings")
    return value


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Saved candidate counts must be nonnegative integers")
    return value


def _fraction(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Paper weights must be finite equity fractions, not shares or unknown zero fills")
    return value


def _missing_evidence(value):
    if isinstance(value, list):
        return _texts(value)
    if isinstance(value, dict):
        for key, count in value.items():
            _text(key)
            _count(count)
        return value
    raise ValueError("Missing evidence must retain its saved string list or nonnegative count mapping")


def _candidates(values, day, recorded):
    if not isinstance(values, list) or len(values) > 5:
        raise ValueError("Saved fixed top-five candidates required")
    result = []
    for row in values:
        symbol, score = _text(row["symbol"]), row["score"]
        if (type(score) not in (int, float) or not math.isfinite(score)
                or row.get("source_day", day) != day or row.get("date", day) != day
                or (row.get("available_at") is not None and _stamp(row["available_at"]) > recorded)):
            raise ValueError("Saved candidate score, date or availability is invalid")
        result.append({"symbol": symbol, "score": score})
    if len({row["symbol"] for row in result}) != len(result):
        raise ValueError("Saved candidates cannot contain duplicate securities")
    return result


def _saved_facts(value, recorded):
    """Retain nested saved facts, without allowing them to grant admission."""
    denied = set(FALSE_FLAGS) | {"trade_ready", "paper_admitted", "reference_formula_admitted",
        "feature_chain_admitted", "reference_is_order_price", "reference_is_actual_held_entry"}
    clock_keys = {"available_at", "observed_at", "read_at", "registered_at", "derived_at",
        "source_available_at", "decision_as_of"}
    if isinstance(value, dict):
        for key, entry in value.items():
            if key in denied and entry is not None and entry is not False:
                raise ValueError("Saved candidate facts cannot grant trading, account or reference-price admission")
            if key in clock_keys and entry is not None and _stamp(entry) > recorded:
                raise ValueError("Saved candidate evidence became available after its own publication")
            _saved_facts(entry, recorded)
    elif isinstance(value, list):
        for entry in value:
            _saved_facts(entry, recorded)
    elif type(value) in (int, float) and not math.isfinite(value):
        raise ValueError("Saved candidate evidence cannot contain nonfinite numbers")
    return value


def _optional_number(value, *, positive=False, fraction=False, integer=False):
    if value is None:
        return None
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (positive and value <= 0) or (fraction and not 0 <= value <= 1)
            or (integer and (value < 0 or int(value) != value))):
        raise ValueError("Saved candidate numbers must retain their finite original units or null")
    return value


def _saved_symbol_rows(item, key, chosen, day, recorded, *, multiple=False):
    values = item.get(key)
    if values is None:
        return None
    if not isinstance(values, list):
        raise ValueError("Saved per-security candidate references must be lists or unavailable")
    result = {}
    for row in values:
        if not isinstance(row, dict) or _text(row.get("symbol")) not in chosen:
            raise ValueError("Saved candidate reference belongs to a different selected security")
        symbol = row["symbol"]
        date_key = "source_day" if key == "candidate_reference_and_setup" else "day"
        if row.get(date_key) != day or any(row[field] != day for field in ("day", "date", "source_day") if field in row):
            raise ValueError("Saved candidate reference belongs to a different source close")
        if not multiple and symbol in result:
            raise ValueError("Saved candidate setup and proof symbols must be unique")
        _saved_facts(row, recorded)
        if key == "candidate_reference_and_setup":
            for field in ("reference_close_cny", "setup_low_cny", "setup_high_cny", "sma20_observed", "sma60_observed"):
                _optional_number(row.get(field), positive=True)
        elif key == "current_reference_evidence":
            prior = row.get("previous_day_required")
            if prior is not None and _day(prior) >= day:
                raise ValueError("A saved prior-close reference cannot use a current or future session")
            _optional_number(row.get("preclose_to_previous_close_ratio_minus1"))
        elif key == "IPO_evidence" and row.get("decision_as_of") is not None:
            if _stamp(row["decision_as_of"]).date().isoformat() != day:
                raise ValueError("Saved IPO evidence must use its own source-day decision cutoff")
        if multiple:
            result.setdefault(symbol, []).append(deepcopy(row))
        else:
            result[symbol] = deepcopy(row)
    return result


def _candidate_details(item, selected, day, recorded):
    """Expose only saved candidate evidence; never evaluate a held setup."""
    chosen = {row["symbol"] for row in selected}
    setups = _saved_symbol_rows(item, "candidate_reference_and_setup", chosen, day, recorded)
    issues = _saved_symbol_rows(item, "current_raw_issues", chosen, day, recorded, multiple=True)
    references = _saved_symbol_rows(item, "current_reference_evidence", chosen, day, recorded)
    ipo = _saved_symbol_rows(item, "IPO_evidence", chosen, day, recorded)
    details = []
    for row in item["cap_candidates"]:
        _saved_facts(row, recorded)
        symbol = row["symbol"]
        for field in ("name", "board", "identity_status"):
            if row.get(field) is not None and not isinstance(row[field], str):
                raise ValueError("Saved candidate identity fields must retain strings or null")
        evidence, invalidation = row.get("evidence"), row.get("invalidation")
        if evidence is not None and not isinstance(evidence, dict):
            raise ValueError("Saved per-candidate evidence must retain its mapping or unknown")
        if invalidation is not None:
            _texts(invalidation)
        if row.get("ipo_feature_support") is not None and type(row["ipo_feature_support"]) is not bool:
            raise ValueError("Saved candidate IPO support must remain a boolean or unknown")
        amount = _optional_number(row.get("amount_cny"))
        if amount is not None and amount < 0:
            raise ValueError("Saved candidate activity cannot be negative")
        reference = references.get(symbol) if references is not None else None
        qualification = ipo.get(symbol) if ipo is not None else None
        if item["environment_mode"] == "EXIT_ALL":
            checks = "not_run_independent_exit"
        elif issues is not None and reference is not None and qualification is not None:
            checks = "saved_entry_checks"
        else:
            checks = "unavailable_in_saved_plan"
        details.append({"symbol": symbol, "score": row["score"], "name": row.get("name"),
            "source_day": row.get("source_day", row.get("date")), "board": row.get("board"),
            "stage": _optional_number(row.get("stage"), integer=True), "amount_cny": amount,
            "pct_chg": _optional_number(row.get("pct_chg")),
            "amount_rank_pct": _optional_number(row.get("amount_rank_pct"), fraction=True),
            "evidence": deepcopy(evidence), "invalidation": deepcopy(invalidation),
            "ipo_feature_support": row.get("ipo_feature_support"), "identity_status": row.get("identity_status"),
            "trade_ready": False, "setup_reference": setups.get(symbol) if setups is not None else None,
            "current_raw_issues": issues.get(symbol, []) if issues is not None else None,
            "current_reference_evidence": reference, "IPO_evidence": qualification,
            "checks_status": checks})
    return details


def _row(item, expected, record, environment, recorded, timely):
    _boundary(item)
    identity, guard, capacity = expected
    identifier = "__".join(expected)
    if (item["case_id"] != identifier or (item["type"], item["guard"], item["capacity"]) != expected
            or item["publication_status"] != ("unbound_current_close_research" if timely else "late_context_only")):
        raise ValueError("All original 8 x 2 x 2 cases must remain in their exact saved order")
    rule = environment["per_type"][identity]
    _boundary(rule, full=False)
    if rule["id"] != identity:
        raise ValueError("Saved per-type applicability has a different identity")
    entry = _texts(rule["rule"]["entry_phases_after_two_close_confirmation"])
    if not entry or any(phase not in PHASES - {"unknown"} for phase in entry):
        raise ValueError("Saved per-type environment applicability is missing")
    mode = rule["mode"] if guard == "two_close_envguard" else "ALLOW_FIXED_CANDIDATES"
    if mode not in MODES or item["environment_mode"] != mode:
        raise ValueError("Case environment mode differs from its saved environment decision")
    decision = item["decision"]
    if (decision not in {"NO_CHANGE", "EXIT_ALL", "FULL_TARGET"}
            or (mode == "EXIT_ALL" and decision != "EXIT_ALL")
            or (mode == "NO_CHANGE" and decision != "NO_CHANGE")
            or (decision == "EXIT_ALL" and mode != "EXIT_ALL")):
        raise ValueError("Saved environment and portfolio decision contradict each other")
    original = _candidates(item["original_top5"], record["source_close_day"], recorded)
    selected = _candidates(item["cap_candidates"], record["source_close_day"], recorded)
    chosen = {row["symbol"] for row in selected}
    if selected != [row for row in original if row["symbol"] in chosen]:
        raise ValueError("Capacity must preserve the saved top-five path without refill")
    intent = item["portfolio_intent"]
    if (type(intent["schema_version"]) is not int or intent["schema_version"] != 1 or intent["contract_type"] != "PortfolioIntent"
            or intent["unit"] != "equity_fraction" or intent["context"] is not None
            or _stamp(intent["decided_at"]) != recorded
            or intent["execution_not_before"] != record["execution_day_bound"]
            or intent["source"] != "research:leader_eight_type_unbound_paper:" + identifier):
        raise ValueError("Saved typed intent is bound, differently dated or has a changed execution date")
    weights, cash = item["target_weights"], item["target_cash_weight"]
    if decision == "NO_CHANGE":
        if weights is not None or cash is not None or intent["kind"] != "NO_CHANGE" or intent["weights"] != {}:
            raise ValueError("NO_CHANGE must preserve unknown weights and cash, not become an empty portfolio")
    else:
        if not isinstance(weights, dict) or intent["kind"] != "FULL_TARGET" or intent["weights"] != weights:
            raise ValueError("Saved FULL_TARGET fractions differ from the typed intent")
        values = [_fraction(value) for value in weights.values()]
        if abs(sum(values) + _fraction(cash) - 1) > 1e-9:
            raise ValueError("Saved equity weights and cash must sum to one")
        if decision == "EXIT_ALL":
            if weights != {} or cash != 1:
                raise ValueError("EXIT_ALL is only an unbound empty target with cash weight one")
        elif not weights or not set(weights) <= chosen or any(value <= 0 for value in values):
            raise ValueError("A FULL_TARGET must contain positive weights from saved capacity candidates")
    structural = _text(item["structural_invalidation_policy"])
    if structural != record["risk_policy_spec"][identity]:
        raise ValueError("Saved structural invalidation rule differs from its saved plan")
    if (item["required_evidence"] != rule["rule"]["additional_candidate_requirements"]
            or item["risk_and_invalidation"] != rule["rule"]["invalidation_signals"]):
        raise ValueError("Saved row requirements differ from its own per-type frozen rule")
    return {"case_id": identifier, "type": identity, "guard": guard, "capacity": capacity,
        "decision": decision, "environment_mode": mode, "reason": _text(item["reason"]),
        "original_candidate_count": len(original), "cap_candidate_count": len(selected),
        "unresolved_member_count": _count(item["unresolved_member_count"]),
        "target_weights": weights, "target_cash_weight": cash, "entry_phases": entry,
        "required_evidence": _texts(item["required_evidence"]),
        "risk_and_invalidation": _texts(item["risk_and_invalidation"]),
        "structural_invalidation_policy": structural, "missing_evidence": _missing_evidence(item["type_missing_evidence"]),
        "candidates": selected,
        "candidate_details": _candidate_details(item, selected, record["source_close_day"], recorded)}


def _empty_view(read_clock):
    return {"schema": "leader_eight_type_paper_read_only_view_v1", "read_at": read_clock.isoformat(),
        "source_file": None, "recorded_at": None, "source_close_day": None, "proposed_execution_day": None,
        "execution_day_bound": None, "next_open_deadline": None, "execution_window_elapsed": None,
        "case_count": None, "environment": None, "rows": [], "input_manifest": None, "protocol_file": None,
        "research_only": True, "candidate_panel_only": True, **{key: False for key in FALSE_FLAGS}}


def _read_view(workspace, now):
    read_clock = _stamp(now if now is not None else datetime.now().astimezone())
    base = _empty_view(read_clock)
    choices = []
    for path in (Path(workspace).resolve() / "outputs/leader_eight_type_paper").glob("*/paper_plan.json"):
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict):
            raise ValueError("Saved paper plan must be a JSON mapping")
        if record.get("schema") == SCHEMA:
            choices.append((_stamp(record["recorded_at"]), str(path), path, record))
    if not choices:
        return {**base, "status": "unavailable", "reason": "no_saved_eight_type_unbound_paper_plan"}
    _, _, path, record = max(choices, key=lambda row: (row[0], row[1]))
    return _plan_view(path, record, read_clock)


def _plan_view(path, record, read_clock):
    base = _empty_view(read_clock)
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError("An explicitly saved original eight-type paper plan is required")
    recorded = _stamp(record["recorded_at"])
    _boundary(record)
    day, execution = _day(record["source_close_day"]), _day(record["proposed_execution_day"])
    opening = _stamp(record["next_open_deadline"])
    timely = recorded.date().isoformat() == day and recorded < opening
    if (recorded > read_clock or recorded < _stamp(day + "T15:00:00+08:00") or execution <= day
            or opening != _stamp(execution + "T09:30:00+08:00")
            or record["status"] != ("unbound_current_close_paper_research" if timely else "late_context_only")
            or record["execution_day_bound"] != (execution if timely else None)
            or _stamp(record["publication_at"]) != recorded
            or record["publication_time_backfilled"] is not False
            or record["execution_day_rolled_forward"] is not False
            or record["same_source_day_evening_required_for_binding"] is not True
            or record["budget_is_account_cash"] is not False):
        raise ValueError("Paper recording, source close, original next open or late-context boundary changed")
    source = record["source"]
    if not _stamp(source["scope_frozen_at"]) <= _stamp(source["input_recorded_at"]) <= recorded:
        raise ValueError("Saved input scope or manifest became available after publication")
    external = source["external_fixed_protocol"]
    protocol_file, input_file = _text(external["file"]), _text(source["input_manifest"])
    fixed = record["protocol"]
    if any(container.get(key, False) is not False for container in (source, external["record"], fixed)
           for key in FALSE_FLAGS):
        raise ValueError("Saved source or protocol cannot grant trading or strategy permission")
    if (fixed["types"] != list(TYPES) or fixed["guards"] != list(GUARDS)
            or fixed["capacities"] != [{"id": CAPACITIES[0], "max_symbols": 20}, {"id": CAPACITIES[1], "max_symbols": None}]
            or type(fixed["case_count"]) is not int or fixed["case_count"] != 32
            or type(record["case_count"]) is not int or record["case_count"] != 32 or len(record["cases"]) != 32
            or external["record"]["schema"] != "leader_eight_type_fixed_comparison_protocol_v1"
            or external["record"]["case_count"] != 32
            or [item["id"] for item in external["record"]["cases"]] != ["__".join(key) for key in CASE_KEYS]):
        raise ValueError("All thirty-two original fixed case identities must be preserved")
    environment = record["environment_policy"]
    _boundary(environment, full=False)
    if (environment["day"] != day or environment["execution_not_before"] != execution
            or environment["mode"] not in MODES or environment["phase"] not in PHASES
            or not _stamp(environment["latest_inputs_available_at"]) <= _stamp(environment["as_of"]) <= recorded
            or set(environment["per_type"]) != set(TYPES)
            or any(type(environment[key]) is not bool for key in ("two_close_confirmation", "exact_previous_present"))):
        raise ValueError("Saved environment belongs to a different date, scope or future clock")
    previous = environment["previous_session_required"]
    if previous is not None and _day(previous) >= day:
        raise ValueError("Previous required session cannot be the current or a future close")
    rows = [_row(item, key, record, environment, recorded, timely) for item, key in zip(record["cases"], CASE_KEYS)]
    return {**base, "status": "saved_paper_plan" if timely else "late_context_only",
        "source_file": str(path.resolve()), "recorded_at": record["recorded_at"], "source_close_day": day,
        "proposed_execution_day": execution, "execution_day_bound": record["execution_day_bound"],
        "next_open_deadline": record["next_open_deadline"], "execution_window_elapsed": read_clock >= opening,
        "case_count": 32, "environment": {key: environment[key] for key in
            ("day", "phase", "mode", "reason", "two_close_confirmation", "exact_previous_present", "previous_session_required")},
        "rows": rows, "input_manifest": input_file, "protocol_file": protocol_file}


def leader_eight_type_paper_view(workspace, now=None):
    """Read only the small saved publication, never its replay, raw or account files."""
    try:
        return _read_view(workspace, now)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Saved eight-type paper-plan contract is incomplete or malformed") from error


def read_saved_plan(path, now=None):
    """Validate exactly this publication, never substitute a newer or older one."""
    path = Path(path).resolve()
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        read_clock = _stamp(now if now is not None else datetime.now().astimezone())
        return _plan_view(path, record, read_clock)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Explicit eight-type paper-plan contract is incomplete or malformed") from error
