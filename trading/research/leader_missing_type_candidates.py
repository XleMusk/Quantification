"""Three separate fixed research proxies; the old eight labels are unchanged.

Inputs are local views already validated by read_preview and the strict source
loader. This pure layer keeps every fixed-scope unknown, never repairs raw data,
backdates a registered identity, selects a strategy, or creates an order.
"""
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import re
from statistics import median

import pandas as pd

from .leader_daily_signals import MIN_AMOUNT, _calendar_view
from .leader_environment import _bool, _number
from .leader_listing_features import METHOD_ID as FEATURE_METHOD

SCHEMA = "leader_missing_type_candidates_v1"
METHOD_ID = "fixed_cross_broad_industry_catchup_daily_turnover_proxies_v1"
CN = timezone(timedelta(hours=8))
TYPE_IDS = ("cross_regime_candidate", "sector_catchup_leader", "turnover_leader")
CATCHUP_LEADER_TYPES = frozenset({"space_height_leader", "relay_continuation_leader"})
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
         "selection_enabled": False, "execution_admitted": False, "orders_generated": False,
         "ledger_mutated": False, "provider_contacted": False, "model_refit": False,
         "future_labels_used_for_score": False, "confirmed_leader": False, "pit_verified": False,
         "corporate_actions_verified": False}
RULE_SPEC = {"id": METHOD_ID, "minimum_amount_cny": MIN_AMOUNT, "reference_tolerance": .01,
    "cross_regime_candidate": "original_first_episode_three_closes_two_intervals_price_proxy_plus_same_two_sector_relative_legs; no_backdated_identity",
    "sector_catchup_leader": "preregistered_space_or_relay_peer; prior_qualified_stage_ge2_and_peer_excess_gt0; candidate_prior_not_limit_and_pct_lt_sector_median; current_qualified_stage1_and_pct_gt_positive_sector_median",
    "turnover_leader": "current_qualified_limit_and_high_gt_low; own_previous_exact_session_reported_turn_percent; current_turn_gt_previous_turn",
    "industry": "frozen_lagged_broad_industry_not_theme; full_anchor_active_non_ST_members; missing_member_makes_median_unknown",
    "turnover": "reported_daily_percent_proxy_not_free_float_PIT_or_intraday_queue_evidence",
    "candidate_order": "amount_desc_symbol_asc_no_new_threshold", "labels_overlap": True,
    "unknown": "retained_in_full_fixed_scope_not_empty_target", "old_labels_or_protocol_changed": False}


def _time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Actual evidence availability needs an offset-aware timestamp")
    return result.astimezone(CN)


def _symbol(value):
    value = str(value)
    if re.fullmatch(r"\d{6}\.(SSE|SZSE)", value):
        return value
    if re.fullmatch(r"(?:sh|sz)\.\d{6}", value):
        return value[-6:] + (".SSE" if value.startswith("sh.") else ".SZSE")
    raise ValueError("Only canonical or original provider SH/SZ symbols are supported")


def _records(value):
    return value.to_dict("records") if isinstance(value, pd.DataFrame) else list(value)


def _indexed(value):
    rows = _records(value)
    result = {_symbol(row.get("symbol", row.get("code"))): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("Evidence contains duplicate symbols")
    return result


def _latest(*values):
    stamps = [_time(value) for value in values if value is not None]
    return max(stamps).isoformat() if stamps else None


def _views(values, day, observed):
    result = {}
    for value in values:
        date = value["day"]
        if date > day or _time(value["available_at"]) > observed:
            continue
        proof = value["source_proof"]
        terminal_clock = proof.get("source_completed_at") or proof.get("source_updated_at")
        if (date in result or proof.get("day") != date or proof.get("read_only_verified") is not True
                or proof.get("source_inventory_complete") is not True
                or proof.get("full_dated_roster_inventory_complete") is not True
                or proof.get("receipt_status") != "complete_full_dated_close_inventory_not_admitted"
                or not proof.get("raw_daily_sha256") or not proof.get("roster_raw_sha256")
                or value.get("feature_method_id") != FEATURE_METHOD
                or value.get("feature_qualification_verified") is not True
                or not terminal_clock or _time(terminal_clock) > _time(value["available_at"])
                or _time(value["available_at"]) < _time(date + "T15:00:00+08:00")):
            raise ValueError("Only strict complete own-date qualified previews may enter the new proxies")
        features = _indexed(value["features"])
        ordinary = _indexed(value["ordinary_rows"])
        if any(str(row.get("date")) != date for row in features.values()):
            raise ValueError("Feature rows do not match their own source date")
        if not set(features) <= set(ordinary):
            raise ValueError("Active features are outside their own complete ordinary roster")
        result[date] = {**value, "features": features, "ordinary": ordinary}
    return result


def _eligibility(view, symbol):
    if view is None:
        return "unknown", "current_complete_source_unavailable", None
    raw = view["ordinary"].get(symbol)
    if raw is None:
        return "unknown", "fixed_member_missing_from_current_own_roster", None
    if str(raw.get("isST")) == "1":
        return "not_matched", "known_current_ST_excluded", None
    if str(raw.get("tradestatus")) == "0":
        return "not_matched", "known_current_suspension_excluded", None
    if str(raw.get("isST")) != "0" or str(raw.get("tradestatus")) != "1":
        return "unknown", "current_status_or_ST_unknown", None
    row = view["features"].get(symbol)
    if row is None:
        return "unknown", "current_active_qualified_feature_row_missing", None
    amount = _number(row.get("amount"))
    if amount is None:
        return "unknown", "current_amount_unknown", row
    if amount < MIN_AMOUNT:
        return "not_matched", "known_amount_below_fixed_20m", row
    if row.get("listing_qualification_status") != "supported":
        if str(row.get("listing_qualification_status", "")).startswith("known_"):
            return "not_matched", "known_listing_window_excluded", row
        return "unknown", "current_listing_qualification_unknown", row
    return None, None, row


def _reference(current, prior):
    close, previous = _number(prior.get("close")), _number(current.get("preclose_for_limits"))
    return None if close is None or previous is None or close <= 0 else abs(previous / close - 1) <= .01 + 1e-12


def _industry(value, earliest_day, observed):
    if value is None:
        return None, "dated_broad_industry_membership_unavailable"
    if (value.get("theme_membership") is not False or not value.get("source_file") or not value.get("source_sha256")
            or value.get("snapshot_date", "9999") > earliest_day
            or _time(value["available_at"]) > min(observed, _time(earliest_day + "T15:00:00+08:00"))):
        return None, "industry_membership_not_available_before_reference_close"
    rows = _indexed(value["membership"])
    return {symbol: (row["industry"].strip() if isinstance(row.get("industry"), str)
                    and row["industry"].strip().lower() not in {"", "nan", "none"} else None)
            for symbol, row in rows.items()}, None


def _sector_median(views, membership, sector, anchor_day, measured_day):
    anchor, measured = views.get(anchor_day), views.get(measured_day)
    if anchor is None or measured is None:
        return None, "sector_exact_close_source_missing", 0
    members = [symbol for symbol, row in anchor["ordinary"].items()
               if membership.get(symbol) == sector and str(row.get("isST")) == "0"
               and str(row.get("tradestatus")) == "1" and symbol in anchor["features"]]
    values = [_number(measured["features"].get(symbol, {}).get("pct_chg")) for symbol in members]
    if not members or any(value is None for value in values):
        return None, "fixed_sector_member_outcome_unknown", len(members)
    return median(values), None, len(members)


def _tracking(value, day, observed):
    if value is None:
        return None, "pre_episode_frozen_proxy_identity_unavailable"
    if (value.get("schema") != "leader_prospective_proxy_tracking_report_v1"
            or not value.get("registry_sha256") or _time(value["read_at"]) > observed
            or _time(value["registered_at"]) >= _time(day + "T15:00:00+08:00")
            or value.get("rule", {}).get("id") != "first_close_phase_change_three_closes_two_intervals_v1"
            or any(date > day for date in value.get("observed_close_days", []))):
        return None, "identity_report_not_available_asof_requested_day"
    members = value.get("members", [])
    if len(members) != value.get("member_count") or len({member["symbol"] for member in members}) != len(members):
        raise ValueError("Identity evidence must preserve its entire frozen distinct membership")
    return value, None


def _cross(symbol, row, day, observed, views, previous, tracking, industry):
    tracking, reason = _tracking(tracking, day, observed)
    if tracking is None:
        return "unknown", reason, {}, []
    members = {member["symbol"]: member for member in tracking["members"]}
    if symbol not in members:
        return "not_matched", "outside_fixed_preregistered_proxy_identity_scope", {}, []
    episode = tracking.get("episode")
    if not episode or episode.get("complete_close_count", 0) < 3:
        return "unknown", "fixed_first_episode_three_closes_not_available", {}, []
    dates = episode["fixed_window_close_days"]
    if (len(dates) != 3 or previous.get(dates[1]) != dates[0] or previous.get(dates[2]) != dates[1]
            or dates[2] > day or _time(tracking["registered_at"]) >= _time(dates[0] + "T15:00:00+08:00")):
        return "unknown", "fixed_episode_or_preregistration_chain_invalid", {}, []
    member = members[symbol]
    if member.get("status") == "price_proxy_conditions_not_matched":
        return "not_matched", "original_fixed_episode_price_conditions_not_matched", {}, []
    if member.get("status") != "price_proxy_conditions_matched":
        return "unknown", "original_fixed_episode_price_or_qualification_unknown", {}, []
    membership, reason = _industry(industry, dates[0], observed)
    if membership is None or not membership.get(symbol):
        return "unknown", reason or "candidate_industry_unknown", {}, []
    sector, legs = membership[symbol], []
    for prior, current in zip(dates[:-1], dates[1:]):
        source, last = views.get(current), views.get(prior)
        own, before = (source or {}).get("features", {}).get(symbol), (last or {}).get("features", {}).get(symbol)
        if own is None or before is None or _reference(own, before) is not True:
            return "unknown", "cross_exact_reference_chain_unknown_or_incompatible", {}, []
        sector_value, reason, denominator = _sector_median(views, membership, sector, dates[0], current)
        pct = _number(own.get("pct_chg"))
        if sector_value is None or pct is None:
            return "unknown", reason or "cross_return_unknown", {}, []
        legs.append({"from_day": prior, "to_day": current, "provider_pct": pct,
                     "sector_median_pct": sector_value, "sector_denominator": denominator})
        if pct < sector_value:
            return "not_matched", "fixed_episode_sector_relative_leg_failed", {"legs": legs}, []
    anchor_row = views.get(dates[0], {}).get("features", {}).get(symbol)
    if anchor_row is None or _number(row.get("close")) is None:
        return "unknown", "cross_anchor_structure_price_unknown", {}, []
    if float(row["close"]) < float(anchor_row["close"]):
        return "not_matched", "cross_anchor_close_structure_broken", {"structure_close": float(anchor_row["close"])}, []
    evidence = {"episode": episode, "confirmation_day": dates[2], "original_proxy_identity": member,
        "industry": sector, "industry_legs": legs, "structure_close": float(anchor_row["close"]),
        "original_episode_basis_not_new_environment_policy": True}
    clocks = [tracking["read_at"], industry["available_at"]] + [views[date]["available_at"] for date in dates]
    return "matched", None, evidence, clocks


def _catchup(symbol, row, day, observed, views, previous, tracking, industry):
    limit, stage = _bool(row.get("limit_up")), _number(row.get("stage"))
    if limit is False or stage is not None and stage != 1:
        return "not_matched", "current_not_qualified_first_board", {}, []
    if limit is not True or stage is None:
        return "unknown", "current_first_board_qualification_unknown", {}, []
    prior_day = previous.get(day)
    prior = views.get(prior_day)
    before = (prior or {}).get("features", {}).get(symbol)
    if before is None or _reference(row, before) is not True:
        return "unknown", "catchup_exact_previous_reference_unknown_or_incompatible", {}, []
    if before.get("listing_qualification_status") != "supported":
        return "unknown", "catchup_previous_listing_qualification_unknown", {}, []
    before_limit = _bool(before.get("limit_up"))
    if before_limit is True:
        return "not_matched", "candidate_already_limit_up_on_previous_close", {}, []
    if before_limit is None:
        return "unknown", "previous_candidate_limit_state_unknown", {}, []
    membership, reason = _industry(industry, prior_day, observed)
    if membership is None or not membership.get(symbol):
        return "unknown", reason or "candidate_industry_unknown", {}, []
    tracking, reason = _tracking(tracking, day, observed)
    if (tracking is None or tracking.get("source", {}).get("day", "9999") > prior_day
            or _time(tracking["registered_at"]) >= _time(day + "T15:00:00+08:00")):
        return "unknown", reason or "prior_sector_leader_identity_not_preregistered", {}, []
    sector = membership[symbol]
    leaders = [member["symbol"] for member in tracking["members"]
               if CATCHUP_LEADER_TYPES & set(member["labels"])
               and membership.get(member["symbol"]) == sector]
    if symbol in leaders:
        return "not_matched", "candidate_is_already_a_frozen_sector_leader_proxy", {}, []
    if not leaders:
        return "not_matched", "no_frozen_prior_leader_proxy_in_this_broad_industry", {}, []
    first, reason, n = _sector_median(views, membership, sector, prior_day, prior_day)
    current, reason2, _ = _sector_median(views, membership, sector, prior_day, day)
    pct, prior_pct = _number(row.get("pct_chg")), _number(before.get("pct_chg"))
    if first is None or current is None or pct is None or prior_pct is None:
        return "unknown", reason or reason2 or "catchup_relative_return_unknown", {}, []
    if prior_pct >= first or not pct > current > 0:
        return "not_matched", "prior_lag_or_current_positive_sector_excess_not_matched", {}, []
    supported, unknown = [], []
    for leader in leaders:
        leader_row = prior["features"].get(leader, {})
        leader_stage, leader_pct = _number(leader_row.get("stage")), _number(leader_row.get("pct_chg"))
        leader_limit = _bool(leader_row.get("limit_up"))
        if (leader_row.get("listing_qualification_status") == "supported" and leader_stage is not None
                and leader_stage >= 2 and leader_limit is True and leader_pct is not None and leader_pct > first):
            supported.append(leader)
        elif leader_row.get("listing_qualification_status") != "supported" or leader_stage is None or leader_limit is None or leader_pct is None:
            unknown.append(leader)
    if not supported:
        return ("unknown" if unknown else "not_matched"), "prior_leader_qualified_high_board_unknown" if unknown else "prior_leader_high_board_or_sector_excess_not_matched", {}, []
    return "matched", None, {"broad_industry": sector, "frozen_prior_leaders": supported,
        "prior_sector_median_pct": first, "current_sector_median_pct": current, "sector_denominator": n,
        "candidate_prior_pct": prior_pct, "candidate_current_pct": pct,
        "not_verified_theme_catchup_leader": True}, [prior["available_at"], tracking["read_at"], industry["available_at"]]


def _turn(symbol, row, day, observed, views, previous, batches):
    limit = _bool(row.get("limit_up"))
    if limit is False:
        return "not_matched", "current_not_qualified_limit_up", {}, []
    if limit is not True:
        return "unknown", "current_limit_up_qualification_unknown", {}, []
    high, low = _number(row.get("high")), _number(row.get("low"))
    if high is None or low is None:
        return "unknown", "current_daily_price_range_unknown", {}, []
    if high <= low:
        return "not_matched", "known_one_price_daily_range", {}, []
    prior_day = previous.get(day)
    prior = views.get(prior_day)
    before = (prior or {}).get("features", {}).get(symbol)
    if before is None or _reference(row, before) is not True:
        return "unknown", "turnover_exact_previous_reference_unknown_or_incompatible", {}, []
    if before.get("listing_qualification_status") != "supported":
        return "unknown", "turnover_previous_listing_qualification_unknown", {}, []
    values, clocks = [], []
    for date in [prior_day, day]:
        batch = batches.get(date)
        if batch is None:
            return "unknown", "own_dated_turnover_percent_batch_missing", {}, []
        raw = batch["indexed"].get(symbol, {})
        try:
            value = Decimal(str(raw.get("turn_percent", raw.get("turn_pct", raw.get("turn")))))
        except (ValueError, InvalidOperation):
            return "unknown", "member_reported_turnover_percent_unknown", {}, []
        if not value.is_finite() or value < 0:
            return "unknown", "member_reported_turnover_percent_invalid", {}, []
        values.append(value); clocks.append(batch["available_at"])
    if values[1] <= values[0]:
        return "not_matched", "daily_reported_turnover_not_expanding", {}, []
    return "matched", None, {"previous_day": prior_day, "previous_turn_percent": str(values[0]),
        "current_turn_percent": str(values[1]), "daily_high": high, "daily_low": low,
        "denominator_PIT_verified": False, "intraday_holder_exchange_or_queue_verified": False}, clocks + [prior["available_at"]]


def build_missing_type_candidates(*, day, observed_at, calendar_payload, scope_members, complete_views,
                                  identity_tracking=None, industry_membership=None, turnover_batches=None):
    observed = _time(observed_at)
    if not scope_members or len(scope_members) != len(set(scope_members)) or any(_symbol(symbol) != symbol for symbol in scope_members):
        raise ValueError("An explicit distinct fixed canonical cohort is required")
    year = int(calendar_payload["year"])
    sessions, previous, status = _calendar_view(calendar_payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar" or day not in sessions:
        raise ValueError("An exact pinned exchange session is required")
    views = _views(complete_views, day, observed)
    current = views.get(day)
    batches = {}
    for batch in turnover_batches or []:
        date = batch["day"]
        if date > day or _time(batch["available_at"]) > observed:
            continue
        if (date in batches or batch.get("unit") != "percent" or batch.get("units_verified") is not True
                or date not in views or batch.get("price_source_raw_sha256") != views[date]["source_proof"]["raw_daily_sha256"]):
            raise ValueError("Turnover evidence needs its own exact price source, verified percent unit and unique date")
        batches[date] = {**batch, "indexed": _indexed(batch["rows"])}
    types = []
    for identity in TYPE_IDS:
        candidates, unknown, exclusions, reasons = [], [], {}, Counter()
        for symbol in scope_members:
            result, reason, row = _eligibility(current, symbol)
            evidence, clocks = {}, []
            if result is None:
                if identity == TYPE_IDS[0]:
                    result, reason, evidence, clocks = _cross(symbol, row, day, observed, views, previous, identity_tracking, industry_membership)
                elif identity == TYPE_IDS[1]:
                    result, reason, evidence, clocks = _catchup(symbol, row, day, observed, views, previous, identity_tracking, industry_membership)
                else:
                    result, reason, evidence, clocks = _turn(symbol, row, day, observed, views, previous, batches)
            if result == "unknown":
                unknown.append(symbol); reasons[reason] += 1
            elif result == "not_matched":
                exclusions[symbol] = reason
            else:
                available = _latest(current["available_at"], *clocks)
                if _time(available) > observed:
                    unknown.append(symbol); reasons["candidate_evidence_not_yet_available"] += 1
                    continue
                candidates.append({"date": day, "source_day": day, "symbol": symbol,
                    "amount_cny": float(row["amount"]), "score": float(row["amount"]), "available_at": available,
                    "setup_low": _number(row.get("low")), "structure_high": _number(row.get("high")),
                    "evidence": evidence, "invalidation": ["reference_chain_unknown_or_incompatible", "original_structure_broken",
                        "required_sector_or_turnover_evidence_lost"], "confirmed_leader": False})
        candidates.sort(key=lambda row: (-row["amount_cny"], row["symbol"]))
        types.append({"id": identity, "status": "matched" if candidates else "unknown" if unknown else "not_matched",
            "scope_members": list(scope_members), "scope_complete": not unknown, "scope_member_count": len(scope_members),
            "candidates": candidates, "candidate_count": len(candidates), "unresolved_symbols": unknown,
            "unknown_symbols": list(unknown), "known_exclusions": exclusions, "missing_evidence": dict(reasons),
            "evidence": {"known_matched": len(candidates), "known_not_matched": len(exclusions), "unknown": len(unknown)},
            "invalidation": ["missing_exact_successor_source", "reference_or_identity_chain_break", "required_data_unknown"],
            "availability": {"observed_at": observed.isoformat(), "source_available_at": current["available_at"] if current else None},
            "identity_proof": {"registry_sha256": (identity_tracking or {}).get("registry_sha256"),
                "registered_at": (identity_tracking or {}).get("registered_at"), "prospective_identity_not_backfilled": True}, **FLAGS})
    return {"schema": SCHEMA, "method_id": METHOD_ID, "day": day, "observed_at": observed.isoformat(),
        "rule_spec": RULE_SPEC, "scope_members": list(scope_members), "types": types,
        "source_proofs": [views[date]["source_proof"] for date in sorted(views)],
        "source_inventory_complete": bool(current) and set(scope_members) <= set(current["ordinary"]),
        "unknown_symbols": sorted({symbol for value in types for symbol in value["unresolved_symbols"]}), **FLAGS}
