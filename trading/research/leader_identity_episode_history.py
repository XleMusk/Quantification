"""Every dated close-phase transition of an existing frozen proxy cohort.

This is a read-only research context.  It neither replaces the original
first-episode experiment nor produces new candidate memberships or targets.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime
import argparse
import json
import math
from pathlib import Path

from .baostock_daily_slice import digest
from .leader_identity_registry import (
    CN, FLAGS as REGISTRY_FLAGS, RULE as FIRST_EPISODE_RULE,
    _close, _observations, _registry, _time,
)

SCHEMA = "leader_identity_episode_history_v1"
RULE = {**deepcopy(FIRST_EPISODE_RULE),
    "id": "all_adjacent_close_phase_changes_three_closes_two_intervals_v1",
    "original_first_episode_rule_id": FIRST_EPISODE_RULE["id"],
    "episode": "every_exact_adjacent_observed_close_phase_change_in_source_order",
    "all_phase_changes_retained": True, "episode_selection_by_result": False,
    "original_first_episode_experiment_modified": False,
    "overlapping_windows_not_independent": True, "group_labels_overlap": True}
FLAGS = {**REGISTRY_FLAGS, "candidates_generated": False,
    "selection_enabled": False, "execution_admitted": False,
    "strategy_validated": False, "cross_regime_leader_verified": False,
    "pit_verified": False, "historical_publication_verified": False,
    "economic_return_or_PnL": False, "original_registry_modified": False,
    "original_observations_modified": False, "original_candidate_membership_modified": False}
MEMBER_STATUSES = ("pending_three_closes", "price_proxy_conditions_matched",
    "price_proxy_conditions_not_matched", "unknown_missing_or_unqualified_or_reference_incompatible")


def _actual(now):
    actual = datetime.now(CN) if now is None else now
    if isinstance(actual, str):
        actual = _time(actual)
    if not isinstance(actual, datetime) or actual.utcoffset() is None:
        raise ValueError("The actual read clock must have an explicit timezone")
    return actual.astimezone(CN)


def _number(value, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        return None
    return value


def _available(record):
    return max(_time(record["source"]["created_at"]), _time(record["recorded_at"]))


def _member(member, records, window, mature):
    symbol = member["symbol"]
    values = [next(row for row in record["members"] if row["symbol"] == symbol) for record in records]
    reasons = []
    for day, row in zip(window, values):
        if row.get("status") != "active_price_proxy":
            reasons.append({"day": day, "reason": "member_active_price_observation_unknown"})
        if row.get("listing_qualification_status") != "supported":
            reasons.append({"day": day, "reason": "member_listing_qualification_unknown_or_unsupported"})
        if (_number(row.get("close"), positive=True) is None
                or _number(row.get("provider_preclose"), positive=True) is None
                or _number(row.get("provider_pct_chg")) is None):
            reasons.append({"day": day, "reason": "member_price_or_provider_return_unknown"})
    intervals = []
    for index in range(1, len(values)):
        before, current = values[index - 1], values[index]
        prior_close = _number(before.get("close"), positive=True)
        reference = _number(current.get("provider_preclose"), positive=True)
        compatible = (prior_close is not None and reference is not None
            and abs(reference / prior_close - 1) <= RULE["reference_check_bound"])
        if not compatible:
            reasons.append({"day": window[index], "reason": "exact_reference_unknown_or_incompatible"})
        pct = _number(current.get("provider_pct_chg"))
        median = records[index]["market_median_provider_pct"]
        intervals.append({"from_close_day": window[index - 1], "to_close_day": window[index],
            "reference_approximately_consistent": compatible,
            "provider_pct_chg": pct, "market_median_provider_pct": median,
            "excess_provider_percentage_points": pct - median if pct is not None else None})
    result = {"symbol": symbol, "name": member["name"],
        "labels": [item["id"] for item in member["labels"]],
        "status": "pending_three_closes", "observed_close_count": len(values),
        "provider_relative_condition": None, "raw_price_condition": None,
        "unadjusted_endpoint_change_pct": None, "intervals": intervals,
        "unknown_reasons": reasons, "confirmation_day_return_used_as_performance": False}
    if not mature:
        return result
    if reasons:
        result["status"] = "unknown_missing_or_unqualified_or_reference_incompatible"
        return result
    relative = all(interval["excess_provider_percentage_points"] >= 0 for interval in intervals)
    change = (values[-1]["close"] / values[0]["close"] - 1) * 100
    result.update(status="price_proxy_conditions_matched" if relative and change >= 0
        else "price_proxy_conditions_not_matched", provider_relative_condition=relative,
        raw_price_condition=change >= 0, unadjusted_endpoint_change_pct=change)
    return result


def episode_history(registry_file, *, through=None, now=None):
    """Read all transitions through an exact *observed* source close.

    ``through`` is a price/source-date cutoff, not a backdated knowledge clock.
    ``now`` is the actual timezone-aware read clock (injectable for tests).
    The original entire chain is verified; suffix observations never enter the
    returned prefix's episodes, source proofs or completion clocks.
    """
    actual, path = _actual(now), Path(registry_file).resolve()
    registry, registry_sha, sessions, previous = _registry(path, actual)
    records, _ = _observations(path, registry, registry_sha, sessions, actual)
    observed_days = [record["source"]["day"] for record in records]
    if through is not None:
        if (not isinstance(through, str) or date.fromisoformat(through).isoformat() != through
                or through not in observed_days):
            raise ValueError("Through must be an exact actually observed close, never a fallback day")
        records = records[:observed_days.index(through) + 1]
    used_days = [record["source"]["day"] for record in records]
    sources = []
    for record in records:
        file = path.parent / "observations" / record["source"]["day"].replace("-", "") / "observation.json"
        receipt = file.parent / "observation_receipt.json"
        sources.append({"day": record["source"]["day"], "file": str(file), "sha256": digest(file),
            "receipt_file": str(receipt), "receipt_sha256": digest(receipt),
            "source_created_at": record["source"]["created_at"], "recorded_at": record["recorded_at"]})
    baseline_day, prior = registry["source"]["day"], None
    baseline_adjacent = bool(records and registry["baseline_phase_adjacent_to_first_eligible_close"]
        and previous[used_days[0]] == baseline_day)
    if baseline_adjacent:
        prior = {"day": baseline_day, "phase": registry["baseline_environment"]["phase"],
            "available_at": registry["registered_at"], "basis": "frozen_registration_context"}
    episodes = []
    for index, record in enumerate(records):
        day = record["source"]["day"]
        if prior is not None and previous[day] == prior["day"] and record["phase"] != prior["phase"]:
            window = sessions[sessions.index(day):sessions.index(day) + 3]
            captured = records[index:index + 3]
            if [item["source"]["day"] for item in captured] != window[:len(captured)]:
                raise ValueError("An episode must retain its exact consecutive source closes")
            mature = len(window) == len(captured) == 3
            members = [_member(member, captured, window, mature) for member in registry["members"]]
            status_counts = {status: sum(row["status"] == status for row in members) for status in MEMBER_STATUSES}
            known_at = max([_time(prior["available_at"])] + [_available(item) for item in captured])
            episodes.append({"episode_id": day.replace("-", "") + "_" + prior["phase"] + "_to_" + record["phase"],
                "from_phase": prior["phase"], "to_phase": record["phase"],
                "previous_close_day": prior["day"], "previous_context_basis": prior["basis"],
                "transition_close_day": day, "fixed_window_close_days": window,
                "observed_close_days": [item["source"]["day"] for item in captured],
                "complete_close_count": len(captured), "required_close_count": 3,
                "missing_future_calendar_session_count": 3 - len(window),
                "next_required_close_day": window[len(captured)] if len(captured) < len(window) else None,
                "status": "complete" if mature else "pending",
                "transition_known_at": max(_time(prior["available_at"]), _available(record)).isoformat(),
                "available_evidence_at": known_at.isoformat(),
                "earliest_possible_completion_close_at": _close(window[2]).isoformat() if len(window) == 3 else None,
                "complete_known_at": known_at.isoformat() if mature else None,
                "complete_known_at_basis": "original_observation_window_only_not_new_method_publication",
                "identity_registered_at": registry["registered_at"],
                "registered_before_transition_close": _time(registry["registered_at"]) < _close(day),
                "member_count": registry["member_count"], "members": members, "status_counts": status_counts,
                "overlapping_episode_ids": [], "three_closes_mean_two_intervals": True,
                "unmatured_is_failure": False, **FLAGS})
        prior = {"day": day, "phase": record["phase"], "available_at": _available(record).isoformat(),
            "basis": "exact_previous_immutable_close_observation"}
    for episode in episodes:
        episode["overlapping_episode_ids"] = [other["episode_id"] for other in episodes
            if other is not episode and set(episode["fixed_window_close_days"]).intersection(other["fixed_window_close_days"])]
    # Only the used prefix is sealed into this returned read. An independently
    # appended suffix is not grounds to change an old prefix's facts.
    if digest(path) != registry_sha or any(digest(Path(item[key])) != item[sha]
            for item in sources for key, sha in (("file", "sha256"), ("receipt_file", "receipt_sha256"))):
        raise ValueError("Used immutable registry/observation bytes changed during the read")
    counts = Counter(episode["status"] for episode in episodes)
    return {"schema": SCHEMA, "status": "continuous_close_episode_research_context_only",
        "read_at": actual.isoformat(), "computed_at": actual.isoformat(),
        "research_result_available_at": actual.isoformat(),
        "knowledge_as_of": actual.isoformat(), "source_cutoff_day": used_days[-1] if used_days else None,
        "observed_close_days": used_days, "member_count": registry["member_count"],
        "baseline_context_is_exact_adjacent": baseline_adjacent,
        "source": {"registry_file": str(path), "registry_sha256": registry_sha,
            "registered_at": registry["registered_at"], "registration_source_day": baseline_day,
            "calendar": registry["calendar"], "calendar_sha256": registry["binding"]["calendar_sha256"],
            "observations": sources}, "rule": deepcopy(RULE), "episodes": episodes,
        "counts": {"episodes": len(episodes), "complete": counts["complete"], "pending": counts["pending"],
            "complete_member_observations": sum(episode["member_count"] for episode in episodes if episode["status"] == "complete")},
        "overlapping_windows_not_independent": True, "group_labels_overlap": True,
        "close_environment_is_post_close_context_not_open_signal": True, **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--through")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    registry, out = Path(args.registry).resolve(), Path(args.out).resolve()
    if out.exists():
        raise FileExistsError("Episode history requires an explicitly new JSON artifact")
    if out == registry or registry.parent in out.parents:
        raise ValueError("The derived artifact cannot be written into the immutable registry directory")
    result = episode_history(registry, through=args.through)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({key: result[key] for key in
        ("schema", "source_cutoff_day", "member_count", "counts", *FLAGS)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
