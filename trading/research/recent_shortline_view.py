"""Read saved recent-source shortline counts; never train, replay or select."""
from datetime import date, datetime
import json
from pathlib import Path


SCHEMA = "recent_complete_source_shortline_diagnostic_panel_v1"
METHOD = "qualified_recent_stage123_exact_calendar_witness_active_nonST_v1"
COMPLETED = "completed_recent_shortline_diagnostics"
BATCH_SCHEMA = "recent_shortline_fixed_rule_diagnostic_batches_v1"
FALSE_FLAGS = (
    "strategy_validated", "selection_enabled", "execution_admitted", "model_refit",
    "future_labels_used_for_score", "orders_generated", "ledger_mutated",
    "provider_contacted", "ths_payload_generated",
)
EXTRA_FALSE_FLAGS = (
    "old_panel_overwritten", "old_scores_recomputed", "fitted_model_loaded",
    "future_labels_exported_to_candidate_file", "historical_publication_verified",
    "pit_verified", "corporate_actions_verified", "current_signal_admitted", "engine_advanced",
)
HISTORY_POLICY = "shift1_20_observed_nonST_positive_reconstructed_reference_rows_median_min5_not_20_exact_sessions"
MARKET_SCOPE = "complete_source_active_nonST_SH_SZ_explicitly_different_from_old_nonST_population"
STAGE_SCOPE = "conditional_on_known_stage_and_completed_run_not_full_population_win_rate"


def _stamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Shortline actual clocks require an explicit timezone")
    return parsed


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Shortline saved counts must be nonnegative integers")
    return value


def _boundary(record):
    if (record.get("candidate_panel_only") is not True
            or any(record.get(key) is not False for key in FALSE_FLAGS)):
        raise ValueError("Recent shortline research boundaries changed")


def recent_shortline_view(workspace, *, now=None):
    """Expose the latest actual new-method receipt, not old model/replay outputs."""
    read_clock = now or datetime.now().astimezone()
    _stamp(read_clock.isoformat())
    root = Path(workspace).resolve() / "outputs/shortline_research"
    base = {"schema": "recent_shortline_read_only_view_v1", "read_at": read_clock.isoformat(),
        "source_file": None, "source_days": [], "by_stage": [], "stage_day_batches": [],
        "event_rows": None, "complete_labels": None, "unresolved_labels": None,
        "unresolved_membership_rows": None, "known_rule_match_rows": None,
        "incomplete_stage_day_batches": None, "return_metrics": None,
        "candidate_panel_only": True, **{key: False for key in FALSE_FLAGS},
        "research_only": True, "strategy_run_started": False, "account_accessed": False,
        "rankings_exposed": False, "fallback_to_old_batch": False}
    candidates = []
    for path in root.glob("*/result.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            if "recent_source_increment" in path.parent.name:
                raise
            continue
        if not isinstance(record, dict):
            continue
        if record.get("schema") == SCHEMA or record.get("method_id") == METHOD:
            candidates.append((_stamp(record["created_at"]), str(path), path, record))
    if not candidates:
        return {**base, "status": "unavailable", "reason": "no_saved_recent_method_diagnostic"}
    created, _, path, record = max(candidates, key=lambda item: (item[0], item[1]))
    if (record.get("schema") != SCHEMA or record.get("method_id") != METHOD
            or record.get("status") != COMPLETED or created > read_clock):
        raise ValueError("Latest recent shortline receipt is incomplete, incompatible or future-dated")
    _boundary(record)
    if (any(record.get(key) is not False for key in EXTRA_FALSE_FLAGS)
            or record.get("return_metrics") is not None
            or record.get("label_completion_requires_exact_nonlimit_witness") is not True
            or record.get("history_policy") != HISTORY_POLICY or record.get("market_scope") != MARKET_SCOPE):
        raise ValueError("Expected the new-source diagnostic scope, without fitted models or returns")
    days = record["source_days"]
    if not isinstance(days, list) or not days or days != sorted(set(days)):
        raise ValueError("Recent shortline dates must be distinct and ordered")
    for day in days:
        date.fromisoformat(day)
        if _stamp(day + "T15:00:00+08:00") > created:
            raise ValueError("Shortline receipt precedes its source close")
    cutoff = record["base_history_cutoff"]
    date.fromisoformat(cutoff)
    if cutoff >= days[0]:
        raise ValueError("Historical prefix must end before recent complete sources")
    stages = record["by_stage"]
    if not isinstance(stages, list) or [row["stage"] for row in stages] != [1, 2, 3]:
        raise ValueError("All three fixed stages must remain in original order")
    for row in stages:
        events, complete, unknown, hit4, hit5 = (_count(row[key]) for key in
            ("known_stage_events", "complete_labels", "unresolved_labels",
             "hit4_complete_known_stage", "hit5_complete_known_stage"))
        if (complete + unknown != events or not hit5 <= hit4 <= complete
                or row.get("scope") != STAGE_SCOPE):
            raise ValueError("Shortline stage labels contradict their conditional denominator")
    events = _count(record["event_rows"])
    if sum(row["known_stage_events"] for row in stages) != events:
        raise ValueError("Shortline stages do not reconcile to saved events")
    matches = _count(record["fixed_rule_pass_rows"])
    unknown_members = _count(record["unresolved_membership_rows"])
    if matches > events or record.get("fixed_rule") != "amount_rel20_pre<=1 AND market_breadth>=0":
        raise ValueError("Unexpected fixed-rule diagnostic")
    batch_path = Path(record["candidate_batches"]).resolve()
    if batch_path != path.parent.resolve() / "candidate_batches.json":
        raise ValueError("Shortline stage batches must belong to their selected saved run")
    batches_record = json.loads(batch_path.read_text(encoding="utf-8-sig"))
    _boundary(batches_record)
    if (batches_record.get("schema") != BATCH_SCHEMA
            or batches_record.get("future_label_fields_included") is not False):
        raise ValueError("Saved shortline batches must exclude future labels")
    batches = batches_record["batches"]
    if (not isinstance(batches, list)
            or [(row["day"], row["stage"]) for row in batches] != [(day, stage) for day in days for stage in (1, 2, 3)]):
        raise ValueError("Every source date and stage must remain in the diagnostic")
    summary = []
    shared_membership = {}
    for row in batches:
        membership, fields, known = (row[key] for key in
            ("potential_unknown_stage_members", "unknown_fixed_rule_fields", "known_matching_rows"))
        if (not all(isinstance(value, list) for value in (membership, fields, known))
                or row.get("selection_permitted") is not False
                or row.get("unknown_is_empty_target") is not False
                or row.get("status") != ("incomplete" if membership or fields else "complete")):
            raise ValueError("Unknown stage or rule evidence must not become an empty target")
        if (len(set(membership)) != len(membership)
                or set(membership) != shared_membership.setdefault(row["day"], set(membership))):
            raise ValueError("Three stages must retain the same unallocated membership set")
        summary.append({"day": row["day"], "stage": row["stage"], "status": row["status"],
            "unknown_stage_membership_count": len(membership), "unknown_rule_field_count": len(fields),
            "known_rule_match_rows": len(known)})
    incomplete = _count(record["incomplete_stage_day_batches"])
    if (sum(row["known_rule_match_rows"] for row in summary) != matches
            or sum(row["status"] == "incomplete" for row in summary) != incomplete
            or sum(len(value) for value in shared_membership.values()) != unknown_members):
        raise ValueError("Saved shortline batch counts do not reconcile")
    return {**base, "status": COMPLETED, "source_file": str(path.resolve()),
        "selected_by": "timezone_aware_actual_created_at", "method_id": METHOD,
        "created_at": record["created_at"], "source_days": days, "base_history_cutoff": cutoff,
        "event_rows": events, "complete_labels": sum(row["complete_labels"] for row in stages),
        "unresolved_labels": sum(row["unresolved_labels"] for row in stages), "by_stage": stages,
        "unresolved_membership_rows": unknown_members, "known_rule_match_rows": matches,
        "incomplete_stage_day_batches": incomplete, "stage_day_batch_count": len(summary),
        "stage_day_batches": summary, "candidate_batches_source_file": str(batch_path),
        "fixed_rule": record["fixed_rule"], "history_policy": HISTORY_POLICY, "market_scope": MARKET_SCOPE,
        "historical_publication_verified": False, "pit_verified": False, "corporate_actions_verified": False,
        "fitted_model_loaded": False, "current_signal_admitted": False, "engine_advanced": False,
        "old_panel_overwritten": False, "old_scores_recomputed": False}
