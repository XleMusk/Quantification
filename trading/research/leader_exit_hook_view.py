"""Read saved five-family expiry diagnostics; no replay or account imports."""
from datetime import date, datetime
from hashlib import sha256
import json
import math
from pathlib import Path


SCHEMA = "leader_original_common20_independent_expiry_hook_diagnostic_v1"
METHOD = "same_saved_common20_batches_add_independent_expiry_hook_v1"
FAMILIES = ("stage1_amount_baseline", "stage2_amount_baseline", "stage3_amount_baseline",
            "trend_leader_20_60", "daily_rebound_proxy")
COMPLETED = "completed_fixed_exit_hook_diagnostic"
FAILED = "diagnostic_with_preserved_engine_failure"
CASE_COMPLETED = "completed_already_inspected_engine_diagnostic"
CASE_STATUSES = {CASE_COMPLETED, "blocked_engine_replay", "unavailable_complete_cohort_decision_batches",
                 "unavailable_verified_full_market_breadth"}
PERMISSION_FLAGS = ("execution_admitted", "selection_enabled", "strategy_validated")
OTHER_FALSE_FLAGS = ("persistent_ledger_mutated", "broker_interacted", "provider_contacted",
    "model_refit", "future_labels_used_for_score", "parameters_tuned", "signals_recomputed",
    "original_artifacts_mutated", "pit_verified", "corporate_actions_verified", "future_independent_window_run")


def _stamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Expiry diagnostic clocks require an explicit timezone")
    return result


def _boundary(record, *, panel=True):
    if (not isinstance(record, dict)
            or any(record.get(key) is not False for key in PERMISSION_FLAGS)
            or any(record.get(key, False) is not False for key in OTHER_FALSE_FLAGS)
            or (panel and record.get("candidate_panel_only") is not True)):
        raise ValueError("Saved expiry diagnostic research boundaries changed")


def _count(value):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("Expiry diagnostic counts must be nonnegative integers or null")
    return value


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Canonical saved session dates required")
    return value


def _positions(value):
    if value is None:
        return None
    if not isinstance(value, dict) or any(type(qty) is not int or qty <= 0 for qty in value.values()):
        raise ValueError("Saved open positions must contain positive share quantities")
    return len(value)


def _return(summary, name):
    if (summary.get("status") != CASE_COMPLETED or summary.get("error") is not None
            or not _count(summary.get("fills"))):
        return None
    metric = summary.get(name)
    if metric is None:
        return None
    if not isinstance(metric, dict):
        raise ValueError("Saved diagnostic metrics must be a mapping or null")
    value = metric.get("net_return")
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Saved net return must be a finite fraction or null")
    return value


def _summary_fields(summary, family, prefix):
    _boundary(summary)
    if (summary.get("family") != family or summary.get("pool") != "common_pre_window_symbol20"
            or summary.get("exit_policy") != "complete_empty_and_five_session_exit"
            or summary.get("status") not in CASE_STATUSES):
        raise ValueError("Every diagnostic row must retain its original family and common20 scope")
    error = summary.get("error")
    if error is not None and (not isinstance(error, str) or not error):
        raise ValueError("Saved replay error must be a nonempty string or null")
    coverage = summary.get("decision_coverage")
    if coverage is not None and not isinstance(coverage, dict):
        raise ValueError("Saved decision coverage must be a mapping or null")
    last = summary.get("last_successful_session")
    return {
        f"{prefix}_status": summary["status"],
        f"{prefix}_main_return": _return(summary, "metrics"),
        f"{prefix}_tail_return": _return(summary, "tail_included_diagnostic_metrics"),
        f"{prefix}_overdue_days": _count((coverage or {}).get("over_five_session_held_days")),
        f"{prefix}_open_positions": _positions(summary.get("open_positions")),
        f"{prefix}_orders": _count(summary.get("orders")),
        f"{prefix}_fills": _count(summary.get("fills")),
        f"{prefix}_blocked_exits": _count(summary.get("blocked_exit_count")),
        f"{prefix}_last_session": _day(last) if last is not None else None,
    }


def _read_view(workspace, now=None):
    """Select by actual completion time and read only small saved JSON records."""
    read_clock = _stamp(now if now is not None else datetime.now().astimezone())
    root = Path(workspace).resolve() / "outputs/leader_exit_research"
    base = {"schema": "leader_exit_hook_read_only_view_v1", "read_at": read_clock.isoformat(),
        "created_at": None, "source_file": None, "method_id": METHOD, "start": None, "end": None,
        "execution_tail_sessions": [], "new_engine_cases": None,
        "retained_unavailable_original_cases": None, "frozen_cohort_size": None,
        "rows": [], "protocol_file": None, "recovery_evidence_file": None,
        "research_only": True, "candidate_panel_only": True,
        **{key: False for key in PERMISSION_FLAGS + OTHER_FALSE_FLAGS},
        "strategy_run_started": False, "engine_run_started": False, "engine_advanced": False,
        "account_accessed": False, "fallback_to_old_result": False, "automatic_promotion": False}
    candidates = []
    for path in root.glob("*/result.json"):
        try:
            raw = path.read_bytes()
            record = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Saved expiry diagnostic cannot be read: {path.parent.name}") from error
        if not isinstance(record, dict):
            raise ValueError("Saved expiry result must be a JSON mapping")
        if record.get("schema") == SCHEMA or record.get("method_id") == METHOD:
            candidates.append((_stamp(record.get("created_at")), str(path), path, record, raw))
    if not candidates:
        return {**base, "status": "unavailable", "reason": "no_saved_fixed_expiry_hook_diagnostic"}
    created, _, path, record, raw = max(candidates, key=lambda row: (row[0], row[1]))
    if (record.get("schema") != SCHEMA or record.get("method_id") != METHOD
            or record.get("status") not in {COMPLETED, FAILED} or created > read_clock):
        raise ValueError("Latest expiry diagnostic is incompatible, unfinished or future-dated")
    _boundary(record)
    protocol_path = Path(record["protocol"]).resolve()
    if protocol_path != path.parent.resolve() / "protocol.json":
        raise ValueError("Protocol must belong to the selected saved diagnostic")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8-sig"))
    _boundary(protocol)
    if (protocol.get("schema") != SCHEMA or protocol.get("method_id") != METHOD
            or _stamp(protocol["recorded_at"]) > created
            or protocol.get("families") != list(FAMILIES)
            or protocol.get("pool") != "common_pre_window_symbol20"):
        raise ValueError("Saved protocol does not bind the fixed five-family diagnostic")
    start, end = _day(protocol["start"]), _day(protocol["end"])
    tail = protocol["execution_tail_sessions"]
    if (start > end or not isinstance(tail, list) or not tail or tail != sorted(set(tail))
            or any(_day(day) <= end for day in tail)
            or _stamp(tail[-1] + "T15:00:00+08:00") > created):
        raise ValueError("Saved decision and execution-tail windows are inconsistent")
    symbols = protocol["frozen_symbols"]
    if (not isinstance(symbols, list) or len(symbols) != 20
            or any(not isinstance(symbol, str) or not symbol for symbol in symbols)
            or symbols != sorted(set(symbols))):
        raise ValueError("The frozen original cohort must retain all twenty distinct symbols")
    comparisons = record["comparisons"]
    if (type(record.get("new_engine_cases")) is not int or record["new_engine_cases"] != 5
            or not isinstance(comparisons, list) or any(not isinstance(row, dict) for row in comparisons)
            or [row.get("family") for row in comparisons] != list(FAMILIES)):
        raise ValueError("Exactly the five fixed families must remain in original order")
    retained = record["original_unavailable_cases_retained_without_new_replay"]
    if not isinstance(retained, list) or len(retained) != 15:
        raise ValueError("All fifteen unavailable original cases must remain separate from the five new runs")
    expected_retained = {(family, pool, policy) for family in FAMILIES
        for pool in ("common_pre_window_symbol20", "common_pre_window_uncapped")
        for policy in ("complete_empty_and_five_session_exit", "verified_full_market_breadth_defense")
        if (pool, policy) != ("common_pre_window_symbol20", "complete_empty_and_five_session_exit")}
    retained_ids = []
    for row in retained:
        _boundary(row)
        if row.get("status") not in {"unavailable_verified_full_market_breadth", "unavailable_complete_cohort_decision_batches"}:
            raise ValueError("Retained original cases cannot be promoted into completed runs")
        retained_ids.append((row.get("family"), row.get("pool"), row.get("exit_policy")))
    if set(retained_ids) != expected_retained:
        raise ValueError("The fifteen retained references must cover distinct original unavailable cases")
    rows = []
    for item in comparisons:
        old, new = item["original"], item["independent_hook"]
        for summary in (old, new):
            if (not isinstance(summary, dict) or summary.get("cohort_size", 20) != 20
                    or (summary.get("open_positions") is not None
                        and not set(summary["open_positions"]) <= set(symbols))):
                raise ValueError("Saved holdings or cohort size escape the fixed twenty-symbol scope")
        rows.append({"family": item["family"], **_summary_fields(old, item["family"], "old"),
            **_summary_fields(new, item["family"], "new"), "new_error": new.get("error")})
        if any(rows[-1][key] is not None and not start <= rows[-1][key] <= tail[-1]
               for key in ("old_last_session", "new_last_session")):
            raise ValueError("Saved last session is outside this fixed diagnostic window")
    failed = record["status"] == FAILED or any(
        row["new_error"] is not None or row["new_status"] != CASE_COMPLETED for row in rows)
    recovery_path = path.parent / "recovery_evidence.json"
    recovery_file = None
    if recovery_path.is_file():
        recovery = json.loads(recovery_path.read_text(encoding="utf-8-sig"))
        _boundary(recovery, panel=False)
        if (recovery.get("schema") != "leader_exit_hook_saved_economic_and_recovery_evidence_v1"
                or any(recovery.get(key) is not False for key in ("engine_advanced", "returns_recomputed"))
                or not created <= _stamp(recovery["created_at"]) <= read_clock
                or Path(recovery["source_result"]["file"]).resolve() != path.resolve()
                or recovery["source_result"]["sha256"] != sha256(raw).hexdigest()):
            raise ValueError("Saved recovery evidence is not bound to this actual diagnostic")
        recovery_file = str(recovery_path.resolve())
    return {**base, "status": "diagnostic_failed" if failed else "completed",
        "created_at": record["created_at"], "source_file": str(path.resolve()),
        "selected_by": "timezone_aware_actual_created_at", "start": start, "end": end,
        "execution_tail_sessions": tail, "new_engine_cases": 5,
        "retained_unavailable_original_cases": 15, "frozen_cohort_size": 20,
        "rows": rows, "protocol_file": str(protocol_path), "recovery_evidence_file": recovery_file}


def leader_exit_hook_view(workspace, now=None):
    """Expose saved evidence, translating malformed contracts into read errors."""
    try:
        return _read_view(workspace, now)
    except (KeyError, TypeError) as error:
        raise ValueError("Saved expiry diagnostic contract is incomplete or malformed") from error
