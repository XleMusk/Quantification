"""Pure own-D opportunity selection: skip proved empty types, never unknowns.

This function supplies research selection evidence, not a target or an Engine
callback. Its caller retains the original calendar rotation, capacity paths,
flat/owner lifecycle and all later raw/reference/IPO checks.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import date

from .leader_environment import ARCHETYPES


METHOD_ID = "fixed_cyclic_known_empty_type_skip_opportunity_selection_v1"
TYPE_IDS = tuple(identity for identity, _ in ARCHETYPES)
CAPACITIES = ("causal_first_seen_cap20", "uncapped")
FLAGS = {"candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False,
    "engine_advanced": False, "persistent_ledger_mutated": False,
    "future_labels_used_for_score": False, "network_accessed": False}
SPEC = {"id": METHOD_ID,
    "applicability": "only_flat_without_old_positive_pending_and_not_exiting_or_tail",
    "order": "original_calendar_cycle_first_original_environment_ALLOW_type",
    "skip": "known_matched_or_not_matched_without_unresolved_fixed_members_and_explicit_empty_original_capacity_path",
    "unknown": "stop_at_original_type_NO_CHANGE_never_cross_unknown",
    "nonempty": "first_complete_nonempty_original_path_unchanged_order",
    "all_empty": "known_all_empty_NO_CHANGE_never_empty_FULL",
    "later_checks": "original_raw_reference_IPO_failure_NO_CHANGE_without_fallback",
    "type_score_amount_or_return_comparison": False,
    "new_numeric_thresholds": False, **FLAGS}


def select_opportunity(day, *, cycle, modes, panel, paths, capacity, members):
    """Read only ``panel[D]`` and original ``paths[type][capacity][D]``.

    ``modes`` is the caller's exact own-D original environment-mode mapping.
    A selected nonempty path remains only a candidate input; this helper does
    not validate tradability, construct weights or change any saved evidence.
    Unknown/all-empty results keep candidates null so NO_CHANGE cannot become
    an empty FULL target.
    """
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("One exact source-D date is required")
    cycle = list(cycle)
    if (len(cycle) != len(TYPE_IDS) or set(cycle) != set(TYPE_IDS)
            or cycle != [TYPE_IDS[(TYPE_IDS.index(cycle[0])+i) % len(TYPE_IDS)] for i in range(len(TYPE_IDS))]):
        raise ValueError("Preserve the original complete cyclic TYPE_IDS rotation")
    if capacity not in CAPACITIES or not isinstance(modes, Mapping):
        raise ValueError("Use the original capacity and own-D environment modes")
    member_set = set(members)
    considered = []

    def result(status, selected, reason, candidates=None):
        return {"schema": METHOD_ID + "_result", "method_id": METHOD_ID,
            "source_day": day, "capacity": capacity, "cycle": cycle,
            "status": status, "selected_type": selected, "reason": reason,
            "decision_kind": "CANDIDATE_INPUT" if status == "selected_known_nonempty" else "NO_CHANGE",
            "selected_candidates": deepcopy(candidates), "considered_types": deepcopy(considered), **FLAGS}

    allowed_count = 0
    for identity in cycle:
        mode = modes.get(identity)
        if mode != "ALLOW_FIXED_CANDIDATES":
            considered.append({"type_id": identity, "environment_mode": mode,
                "reason": "original_environment_not_allowed", "skipped_known_empty": False})
            continue
        allowed_count += 1
        evidence = {"type_id": identity, "environment_mode": mode,
            "item_status": None, "unresolved_fixed_members": None,
            "candidate_count": None, "skipped_known_empty": False}
        reason = None
        if not isinstance(panel, Mapping) or panel.get("day") != day or not isinstance(panel.get("types"), list):
            reason = "exact_source_D_panel_unknown"
            rows = []
        else:
            rows = [row for row in panel["types"] if isinstance(row, Mapping) and row.get("id") == identity]
        item = rows[0] if len(rows) == 1 else None
        if reason is None and item is None:
            reason = "type_item_missing_or_duplicated"
        if item is not None:
            evidence["item_status"] = item.get("status")
            if item.get("status") not in {"matched", "not_matched"}:
                reason = "type_observation_unknown"
            unresolved = item.get("unresolved_symbols", [])
            if not isinstance(unresolved, list):
                reason = reason or "type_membership_evidence_malformed"
            else:
                evidence["unresolved_fixed_members"] = sorted(set(unresolved) & member_set)
                if evidence["unresolved_fixed_members"]:
                    reason = reason or "type_membership_unknown_in_fixed_panel"
        type_paths = paths.get(identity) if isinstance(paths, Mapping) else None
        capacity_paths = type_paths.get(capacity) if isinstance(type_paths, Mapping) else None
        path = capacity_paths.get(day) if isinstance(capacity_paths, Mapping) else None
        if not isinstance(path, Mapping):
            reason = reason or "exact_type_capacity_path_unknown"
            candidates = None
        else:
            candidates = path.get("candidates")
            if not isinstance(candidates, list):
                reason = reason or "original_path_candidates_not_explicit_list"
                candidates = None
        if candidates is not None:
            evidence["candidate_count"] = len(candidates)
        if reason is not None:
            evidence["reason"] = reason
            considered.append(evidence)
            return result("blocked_unknown", identity, reason)
        if candidates:
            evidence["reason"] = "first_complete_nonempty_original_path"
            considered.append(evidence)
            return result("selected_known_nonempty", identity, evidence["reason"], candidates)
        evidence.update(reason="known_empty_original_capacity_path", skipped_known_empty=True)
        considered.append(evidence)
    return result("known_all_empty" if allowed_count else "no_allowed_type", None,
        "all_environment_allowed_types_known_empty" if allowed_count else "no_environment_allowed_type")
