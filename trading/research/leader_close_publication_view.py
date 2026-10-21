"""Read saved rule-target publications without producing targets or touching accounts."""
from datetime import date, datetime
import json
import math
from pathlib import Path


SCHEMA = "leader_close_target_publication_v1"
FALSE_BOUNDARIES = (
    "execution_admitted", "selection_enabled", "orders_generated", "ledger_mutated",
    "formal_target_admitted", "ths_payload_generated", "account_created", "broker_interacted",
    "strategy_validated", "model_refit", "future_labels_used_for_score",
)


def _stamp(value, label):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(f"{label} requires an explicit timezone")
    return stamp


def _boundaries(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FALSE_BOUNDARIES and item is not False:
                raise ValueError(f"Saved publication boundary changed: {key}")
            _boundaries(item)
    elif isinstance(value, list):
        for item in value:
            _boundaries(item)


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def leader_close_publication_view(workspace, *, now=None):
    """Choose by actual aware recording time; return every case and full saved weights."""
    read_clock = now or datetime.now().astimezone()
    read_at = read_clock.isoformat()
    _stamp(read_at, "Publication read clock")
    operation = {"research_only": True, "provider_contacted": False, "strategy_run_started": False,
                 "account_accessed": False, "selection_enabled": False, "execution_admitted": False,
                 "orders_generated": False, "ledger_mutated": False}
    paths = list((Path(workspace) / "outputs/leader_close_publication").glob("*/publication.json"))
    if not paths:
        return {"schema": "leader_close_publication_view_v1", "status": "missing", "source_file": None,
                "read_at": read_at, "cases": [], "case_count": 0, **operation}
    records = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("Saved publication must be a JSON object")
        # An unreadable clock cannot be silently ranked behind an older success.
        recorded = _stamp(payload.get("recorded_at"), "Actual recorded_at")
        records.append((recorded, str(path.resolve()), payload))
    recorded, source_file, payload = max(records, key=lambda row: (row[0], row[1]))
    if recorded > read_clock:
        raise ValueError("Actual recorded_at cannot be in the future of the read clock")
    if (payload.get("schema") != SCHEMA or payload.get("candidate_panel_only") is not True
            or any(payload.get(key) is not False for key in FALSE_BOUNDARIES)):
        raise ValueError("Expected a saved unadmitted candidate rule-target publication")
    _boundaries(payload)
    if (payload.get("timeliness_uses_actual_recording") is not True
            or payload.get("publication_time_backfilled") is not False
            or payload.get("execution_day_rolled_forward") is not False
            or _stamp(payload.get("publication_at"), "publication_at") != recorded):
        raise ValueError("Publication must retain its actual clock and original execution day")
    source_day, execution_day = (date.fromisoformat(payload[key]) for key in
                                  ("source_close_day", "proposed_execution_day"))
    deadline = _stamp(payload.get("next_open_deadline"), "Next-open deadline")
    late = recorded >= deadline
    if (source_day >= execution_day or deadline.date() != execution_day
            or payload.get("status") != ("late_context_only" if late else "close_research_target_publication_only")):
        raise ValueError("Saved publication clock, status or original day is inconsistent")
    cases, seen = payload.get("cases"), set()
    if not isinstance(cases, list) or payload.get("case_count") != len(cases):
        raise ValueError("Saved publication lost its complete case inventory")
    displayed = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each saved publication case must be an object")
        identity = case["case_id"]
        if not isinstance(identity, str) or identity in seen:
            raise ValueError("Publication case identities must be unique")
        seen.add(identity)
        if case.get("execution_admitted") is not False or case.get("formal_target_admitted") is not False:
            raise ValueError("A case cannot acquire execution or formal-target admission")
        weights, intent = case["target_weights"], case["portfolio_intent"]
        if (not isinstance(weights, dict) or not isinstance(intent, dict) or intent.get("weights") != weights
                or any(not _number(value) or value < 0 for value in weights.values())
                or sum(weights.values()) > 1 + 1e-8):
            raise ValueError("Saved complete target weights disagree with the typed intent")
        kind, cash = intent.get("kind"), case["target_cash_weight"]
        if kind == "NO_CHANGE":
            if weights or cash is not None:
                raise ValueError("NO_CHANGE cannot be rendered as a new cash target")
        elif kind == "FULL_TARGET":
            if not _number(cash) or abs(cash + sum(weights.values()) - 1) > 1e-8:
                raise ValueError("Full target cash and complete weights do not reconcile")
        else:
            raise ValueError("Unknown saved rule-target intent kind")
        displayed.append({key: case.get(key) for key in (
            "case_id", "family", "pool", "exit_policy", "status", "ledger_status", "no_change_reason",
            "target_weights", "target_cash_weight", "formal_target_admitted", "execution_admitted",
            "holdings_retention_and_expiry_account_bound", "order_sizing_binding_pending")}
            | {"intent_kind": kind, "complete_target_weights": True})
    history = payload.get("recent_source_history")
    history_available = payload.get("history_input_available_at")
    specified_at = payload.get("history_source_specified_at")
    ready_at = payload.get("source_ready_at")
    binding = payload.get("protocol_binding") or {}
    if history is not None:
        if (not isinstance(history, dict) or not isinstance(binding, dict)
                or history != binding.get("history_source_view")
                or not binding.get("history_source_supplement")
                or history_available != history.get("available_at")
                or _stamp(history_available, "History input availability") > recorded):
            raise ValueError("Recent history input lost its saved prefix binding or availability clock")
    elif history_available is not None:
        raise ValueError("History availability was saved without its recent-source view")
    if specified_at is not None or (isinstance(binding, dict) and binding.get("history_source_specified_at") is not None):
        if (history is None or not isinstance(binding, dict) or specified_at != binding.get("history_source_specified_at")
                or _stamp(specified_at, "History source specified_at") > recorded):
            raise ValueError("Saved history source specification clock differs from its binding or recording")
    if ready_at is not None:
        ready = _stamp(ready_at, "Source ready_at")
        input_clocks = [value for value in (history_available, specified_at,
                       (payload.get("source_terminal_completion") or {}).get("terminal_completion_observed_at")) if value is not None]
        if ready > recorded or any(ready < _stamp(value, "Saved source input clock") for value in input_clocks):
            raise ValueError("Source ready_at contradicts actual input availability or recording")
    return {"schema": "leader_close_publication_view_v1", "source_schema": SCHEMA,
            "status": payload["status"], "source_file": source_file, "read_at": read_at,
            "selected_by": "timezone_aware_actual_recorded_at", "source_close_day": payload["source_close_day"],
            "recorded_at": payload["recorded_at"], "proposed_execution_day": payload["proposed_execution_day"],
            "next_open_deadline": payload["next_open_deadline"],
            "timeliness": "late_context_only" if late else "recorded_before_next_open",
            "timeliness_uses_actual_recording": True, "execution_day_rolled_forward": False,
            "ledger_checkpoint": payload.get("ledger_checkpoint"),
            "ledger_binding": "unbound" if payload.get("ledger_checkpoint") is None else "saved_checkpoint_only",
            "execution_binding_pending": payload.get("execution_binding_pending"),
            "recent_source_history": history, "history_input_available_at": history_available,
            "history_source_specified_at": specified_at, "source_ready_at": ready_at,
            "history_prefix_binding": "recorded_recent_source_prefix" if history is not None else "legacy_input_path",
            "history_source_supplement": binding.get("history_source_supplement"),
            "cases": displayed, "case_count": len(displayed), "complete_case_inventory": True,
            "display_is_not_portfolio_selection": True, "candidate_panel_only": True,
            "strategy_validated": False, "formal_target_admitted": False,
            "ths_payload_generated": False, **operation}
