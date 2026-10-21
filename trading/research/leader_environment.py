"""Fixed, read-only leader archetype and exact-close environment descriptions.

These are evidence labels, not a trained regime selector or trading signals.
Only complete dated provider-close previews may enter the comparison. Missing
adjacent sessions, IPO knowledge, sector membership and intraday observations
remain missing; historical survivor sets are never substituted for denominators.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math

import numpy as np
import pandas as pd

from .ipo_qualification import IpoQualification
from .leader_daily_signals import MIN_AMOUNT, _calendar_view

SCHEMA = "leader_environment_descriptive_v1"
IMPLEMENTATION_REVISION = "20261015_path_and_membership_unknown_preservation_v3"
PREVIEW_SCHEMA = "leader_complete_close_preview_v1"
LOCAL = timezone(timedelta(hours=8))
REQUIRED = {"date", "symbol", "open", "high", "low", "close", "volume", "amount", "pct_chg"}
ARCHETYPES = (
    ("space_height_leader", "空间龙（同板制局部高度代理）"),
    ("relay_continuation_leader", "接力龙（日线连板候选）"),
    ("trend_leader", "趋势龙（观察行趋势代理）"),
    ("liquidity_capacity_proxy", "容量龙（成交额容量代理）"),
    ("cross_regime_candidate", "穿越龙（待建立跨环境证据）"),
    ("rebound_repair_candidate", "反包龙（日线修复代理）"),
    ("sector_catchup_leader", "补涨龙（板块证据缺失）"),
    ("turnover_leader", "换手龙（换手证据缺失）"),
)


def _stamp(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("Environment observation timestamps require an explicit timezone")
    return stamp.astimezone(LOCAL)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bool(value):
    # CSV inference and explicit booleans are supported; arbitrary truthy text
    # and NaNs do not become a positive feature observation.
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def _features(frame, *, allowed_days):
    if not isinstance(frame, pd.DataFrame) or not REQUIRED <= set(frame.columns):
        raise ValueError("Environment features require the explicit dated physical feature columns")
    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise").dt.strftime("%Y-%m-%d")
    if result.empty or not set(result.date) <= set(allowed_days):
        raise ValueError("Every feature day must have its own complete dated preview")
    if result.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate environment feature symbol/day")
    if not result.symbol.astype(str).str.fullmatch(r"\d{6}\.(SSE|SZSE)").all():
        raise ValueError("Environment symbols must be canonical")
    numeric = sorted(REQUIRED - {"date", "symbol"})
    for key in numeric:
        result[key] = pd.to_numeric(result[key], errors="raise")
    if not np.isfinite(result[numeric].to_numpy()).all():
        raise ValueError("Environment active feature values must be finite")
    if not result[["open", "high", "low", "close", "volume"]].gt(0).all().all() or not result.amount.ge(0).all():
        raise ValueError("Environment requires observed active non-ST rows")
    return result


def _validate_tapes(tapes, observed):
    previews = {}
    for item in tapes:
        if (item.get("schema") != PREVIEW_SCHEMA
                or item.get("status") != "complete_source_close_research_preview_only"
                or item.get("source_inventory_complete") is not True
                or item.get("execution_admitted") is not False):
            raise ValueError("Partial, failed or unverified-as-complete preview is not an environment source")
        day, tape = item["day"], item["tape"]
        if day in previews or tape.get("day") != day or not isinstance(item.get("source"), dict):
            raise ValueError("Duplicate or inconsistent exact dated environment source")
        source = item["source"]
        receipt, plan = source.get("receipt", {}), source.get("plan", {})
        if (source.get("read_only_verified") is not True or receipt.get("day") != day or plan.get("day") != day
                or receipt.get("status") != "complete_full_dated_close_inventory_not_admitted"
                or receipt.get("full_dated_roster_inventory_complete") is not True):
            raise ValueError("Environment preview lacks its strict complete-package source proof")
        if datetime.fromisoformat(day).date().isoformat() != day:
            raise ValueError("Environment source day must be canonical")
        created = _stamp(item["created_at"])
        close = _stamp(day + "T15:00:00+08:00")
        if created < close or created > observed or close > observed:
            raise ValueError("Environment source/observation clocks contradict an actual complete close")
        expected = tape.get("active_non_st_rows")
        counts = [tape.get(key) for key in ("advance_count", "decline_count", "flat_count")]
        if not isinstance(expected, int) or expected <= 0 or any(not isinstance(v, int) or v < 0 for v in counts) or sum(counts) != expected:
            raise ValueError("Environment tape active count identities are incomplete")
        if tape.get("scope") != "complete_dated_provider_ordinary_SH_SZ_active_non_ST":
            raise ValueError("Environment tape needs the explicit complete-provider active non-ST scope")
        if _number(tape.get("amount_cny")) is None or tape["amount_cny"] <= 0:
            raise ValueError("Environment tape amount is missing")
        breadth = (counts[0] - counts[1]) / expected
        if _number(tape.get("breadth")) is None or abs(tape["breadth"] - breadth) > 1e-12:
            raise ValueError("Environment tape breadth does not match its observations")
        previews[day] = item
    if not previews:
        raise ValueError("No complete dated preview supplied")
    return previews


def _source_clock(day, preview, *, latest_day):
    source = preview["source"]
    receipt, plan = source["receipt"], source["plan"]
    # Original large provider rosters and recovery byte inventories remain in
    # the preserved source files; copying them once per daily analysis would
    # inflate a multi-day dashboard without adding a different observation.
    return {"source_day": day, "preview_created_at": preview["created_at"],
        "source_folder": source.get("source_folder"), "read_only_verified": True,
        "raw_daily_sha256": receipt.get("raw_daily_sha256"), "roster_raw_sha256": plan.get("roster_raw_sha256"),
        "source_created_at": receipt.get("created_at"), "source_run_started_at": receipt.get("run_started_at"),
        "source_updated_at": receipt.get("updated_at"), "source_completed_at": receipt.get("completed_at"),
        "terminal_clock_basis": "completed_at" if receipt.get("completed_at") else "terminal_complete_receipt_updated_at_not_publication",
        "actual_signal_publication_verified": False, "is_latest": day == latest_day}


def _match_tape(frame, tape):
    if len(frame) != tape["active_non_st_rows"]:
        raise ValueError("Environment feature frame is a subset or mismatched complete close")
    counts = [int(frame.pct_chg.gt(0).sum()), int(frame.pct_chg.lt(0).sum()), int(frame.pct_chg.eq(0).sum())]
    if counts != [tape[key] for key in ("advance_count", "decline_count", "flat_count")]:
        raise ValueError("Environment feature and tape breadth differ")
    if not math.isclose(float(frame.amount.sum()), float(tape["amount_cny"]), rel_tol=1e-10, abs_tol=.01):
        raise ValueError("Environment feature and tape turnover differ")
    if _number(tape.get("median_return_pct")) is not None and not math.isclose(float(frame.pct_chg.median()), float(tape["median_return_pct"]), abs_tol=1e-9):
        raise ValueError("Environment feature and tape return distributions differ")


def _qualified(frame, qualification, *, feature_qualification_verified=False):
    rows = []
    session_index = {day: index for index, day in enumerate(qualification.calendar)} if qualification is not None else {}
    for row in frame.to_dict("records"):
        cutoff = None
        if feature_qualification_verified:
            from .leader_listing_features import METHOD_ID
            cutoff = row["date"] + "T15:05:00+08:00"
            if row.get("listing_feature_method_id") != METHOD_ID or row.get("feature_decision_as_of") != cutoff:
                raise ValueError("Verified listing features must retain their own exact decision cutoff")
        options = {"decision_as_of": cutoff} if cutoff else {}
        proof = qualification.qualify(row["date"], row["symbol"], **options) if qualification is not None else None
        row["listing_feature_policy_verified"] = feature_qualification_verified
        support = bool(proof and proof["ipo_window_feature_support"] and proof["engine_block_reason"] is None)
        row["ipo_qualification"] = proof
        row["ipo_feature_support"] = support
        stage = _number(row.get("stage"))
        row["qualified_stage"] = (int(stage) if support and stage is not None and stage >= 0 and stage.is_integer()
                                  and _bool(row.get("stage_reference_known")) is True else None)
        # The sixth session may have a valid daily limit, while a streak that
        # started inside the first five unrestricted sessions remains invalid.
        current_index = session_index.get(row["date"])
        if row["qualified_stage"] and current_index is not None:
            start_index = current_index - row["qualified_stage"] + 1
            start_day = qualification.calendar[start_index] if start_index >= 0 else None
            start_proof = qualification.qualify(start_day, row["symbol"], **options) if start_day is not None else None
            if not start_proof or not start_proof["ipo_window_feature_support"] or start_proof["engine_block_reason"] is not None:
                row["qualified_stage"] = None
        row["rebound_listing_history_supported"] = False
        if support and current_index is not None and current_index >= 2:
            start_proof = qualification.qualify(qualification.calendar[current_index - 2], row["symbol"], **options)
            row["rebound_listing_history_supported"] = bool(start_proof["ipo_window_feature_support"] and start_proof["engine_block_reason"] is None)
        limit = _bool(row.get("limit_up"))
        row["qualified_limit_up"] = limit if support else None
        rows.append(row)
    return rows


def _candidate(row, evidence, invalidation):
    return {"symbol": row["symbol"], "name": row.get("name"), "source_day": row["date"], "board": row.get("board"),
            "amount_cny": float(row["amount"]), "pct_chg": float(row["pct_chg"]),
            "stage": row.get("qualified_stage"), "amount_rank_pct": _number(row.get("amount_rank_pct")),
            "evidence": evidence, "invalidation": invalidation,
            "ipo_feature_support": row["ipo_feature_support"],
            "identity_status": "daily_price_volume_proxy_not_verified_market_or_sector_leader",
            "trade_ready": False}


def _rebound_paths(current, prior_by_day, previous_map):
    """Independently assess the old conjunction without treating gaps as False.

    A reliable observed necessary-condition failure can rule out a rebound.
    A positive match needs both exact historical feature rows and raw price
    reference links. The producer's False alone cannot establish a non-match.
    """
    lookups = {day: {row["symbol"]: row for row in rows} for day, rows in prior_by_day.items()}
    for row in current:
        day1 = previous_map.get(row["date"])
        day2 = previous_map.get(day1)
        prior = lookups.get(day1, {}).get(row["symbol"])
        two_back = lookups.get(day2, {}).get(row["symbol"])
        row["rebound_evidence_status"] = "unknown"
        row["rebound_evidence_basis"] = "required_path_not_yet_verified"
        row["rebound_missing_evidence"] = []
        if not row["ipo_feature_support"]:
            row["rebound_missing_evidence"] = ["rebound_current_IPO_qualification_unavailable"]
            continue
        if row["pct_chg"] <= 0:
            row["rebound_evidence_status"] = "not_matched"
            row["rebound_evidence_basis"] = "observed_current_return_not_positive"
            continue
        reference = _number(row.get("preclose_for_limits"))
        if prior is not None:
            pair_known = reference is not None and abs(reference / prior["close"] - 1) <= .01
            prev_open, prev_pct = prior["open"], prior["pct_chg"]
        else:
            prev_close = _number(row.get("previous_close"))
            prev_open, prev_pct = _number(row.get("previous_open")), _number(row.get("previous_pct_chg"))
            pair_known = (_bool(row.get("calendar_contiguous")) is True
                and str(row.get("previous_observed_day")) == day1 and reference is not None
                and prev_close is not None and prev_close > 0 and abs(reference / prev_close - 1) <= .01)
        if not pair_known or prev_open is None or prev_pct is None:
            row["rebound_missing_evidence"] = ["rebound_D_minus1_exact_reference_chain_unavailable"]
            continue
        if prev_pct >= 0 or row["close"] <= prev_open:
            row["rebound_evidence_status"] = "not_matched"
            row["rebound_evidence_basis"] = "observed_exact_D_minus1_necessary_condition_failed"
            continue
        # Known price repair conditions cannot prove what happened on D-2.
        # Both dated rows are required even if daily_rebound was exported True.
        if prior is None or two_back is None:
            row["rebound_missing_evidence"] = ["rebound_D_minus2_exact_reference_chain_unavailable"]
            continue
        prior_reference = _number(prior.get("preclose_for_limits"))
        if prior_reference is None or abs(prior_reference / two_back["close"] - 1) > .01:
            row["rebound_missing_evidence"] = ["rebound_D_minus2_exact_reference_chain_unavailable"]
            continue
        if row.get("listing_feature_policy_verified"):
            # The shared producer already evaluated D-2 listing eligibility at
            # this row's decision cutoff. Earlier child outputs keep their own
            # cutoffs and must not be rewritten merely to reuse that fact here.
            qualified_rebound = _bool(row.get("daily_rebound"))
            if qualified_rebound is not None:
                row["rebound_evidence_status"] = "matched" if qualified_rebound else "not_matched"
                row["rebound_evidence_basis"] = "exact_reference_chain_and_verified_shared_producer_own_decision_listing_policy"
                continue
        if not row["rebound_listing_history_supported"] or two_back["qualified_limit_up"] is None:
            row["rebound_missing_evidence"] = ["rebound_D_minus2_limit_proxy_qualification_unavailable"]
            continue
        row["rebound_evidence_status"] = "matched" if two_back["qualified_limit_up"] else "not_matched"
        row["rebound_evidence_basis"] = "exact_D_minus1_D_minus2_rows_and_reference_links_recomputed"


def _archetypes(current):
    liquid = [row for row in current if row["amount"] >= MIN_AMOUNT]
    valid = [row for row in liquid if row["ipo_feature_support"]]
    valid_stage = [row for row in valid if row["qualified_stage"] is not None]
    unknown_stage_limit = [row for row in valid if row["qualified_limit_up"] is True and row["qualified_stage"] is None]
    unknown_rebound = [row for row in valid if row["rebound_evidence_status"] == "unknown"]
    rebound_missing = sorted({reason for row in unknown_rebound for reason in row["rebound_missing_evidence"]})
    rebound_status_counts = {status: sum(row["rebound_evidence_status"] == status for row in valid)
                             for status in ("matched", "not_matched", "unknown")}
    unknown_stage_limit_by_board = {}
    for row in unknown_stage_limit:
        board = row.get("board", "unknown")
        unknown_stage_limit_by_board[board] = unknown_stage_limit_by_board.get(board, 0) + 1
    heights = {}
    board_unknown = {}
    for row in current:
        board = row.get("board", "unknown")
        if row["qualified_stage"] is None:
            board_unknown[board] = board_unknown.get(board, 0) + 1
        else:
            heights[board] = max(heights.get(board, 0), row["qualified_stage"])
    space, relay, trend, capacity, rebound = [], [], [], [], []
    for row in liquid:
        stage, board = row["qualified_stage"], row.get("board", "unknown")
        if stage is not None and stage >= 2:
            relay.append(_candidate(row, {"known_stage": stage, "limit_up_proxy": row["qualified_limit_up"]},
                                    ["准确后继收盘不再连板", "次日不可成交或风险资格不明"]))
            if stage == heights.get(board):
                space.append(_candidate(row, {"board_observed_max_stage": heights[board],
                    "board_unknown_stage_rows": board_unknown.get(board, 0),
                    "peer_scope": "same_board_complete_provider_active_non_ST_not_exchange_PIT",
                    "global_space_leader_verified": False}, ["同板制更高梯队出现", "自身断板或资格不明"]))
        close, s20, s60, r20, r60 = [_number(row.get(key)) for key in ("close", "sma20", "sma60", "ret20", "ret60")]
        if row["ipo_feature_support"] and all(v is not None for v in (close, s20, s60, r20, r60)) and close > s20 > s60 and r20 > 0:
            trend.append(_candidate(row, {"sma20": s20, "sma60": s60, "ret20": r20, "ret60": r60,
                "window_semantics": "valid_observed_rows_not_guaranteed_calendar_sessions",
                "calendar_contiguous_latest_pair": _bool(row.get("calendar_contiguous"))},
                ["收盘跌破观察行SMA20", "历史日历缺口使精确交易日趋势未验证"]))
        rank = _number(row.get("amount_rank_pct"))
        if rank is not None and rank >= .99:
            capacity.append(_candidate(row, {"amount_rank_pct": rank, "rank_cutoff": .99,
                "capacity_basis": "observed_daily_turnover_only", "float_market_cap_available": False},
                ["成交额分位显著回落", "真实换手、流通盘与承接证据缺失"]))
        if row["rebound_evidence_status"] == "matched":
            rebound.append(_candidate(row, {"daily_rebound": True,
                "path_evidence_basis": row["rebound_evidence_basis"],
                "definition": "D-2_limit_proxy_D-1_negative_D_close_above_D-1_open_with_calendar_continuity"},
                ["失守前一日开盘价", "缺逐笔或分钟数据，无法确认炸板回封路径"]))
    matches = {"space_height_leader": space, "relay_continuation_leader": relay, "trend_leader": trend,
               "liquidity_capacity_proxy": capacity, "rebound_repair_candidate": rebound}
    available_counts = {
        "space_height_leader": len(valid_stage), "relay_continuation_leader": len(valid_stage),
        "trend_leader": sum(all(_number(row.get(key)) is not None for key in ("sma20", "sma60", "ret20", "ret60")) for row in valid),
        "liquidity_capacity_proxy": sum(_number(row.get("amount_rank_pct")) is not None for row in liquid),
        "rebound_repair_candidate": rebound_status_counts["matched"] + rebound_status_counts["not_matched"],
    }
    missing = {
        "space_height_leader": ["exchange_PIT_peer_and_corporate_action_verification", "intraday_leadership_confirmation"],
        "relay_continuation_leader": ["next_open_clearance", "intraday_queue_and_seal_quality", "dated_sector_cohesion"],
        "trend_leader": ["calendar_session_20_60_window_completeness", "dated_sector_relative_strength"],
        "liquidity_capacity_proxy": ["dated_free_float_market_cap", "turnover_rate", "intraday_absorption"],
        "rebound_repair_candidate": ["intraday_break_and_reseal_path", "dated_sector_repair_confirmation"],
        "cross_regime_candidate": ["pre_episode_frozen_leader_identity", "at_least_three_adjacent_complete_closes", "defined_environment_episode", "sector_relative_resilience"],
        "sector_catchup_leader": ["dated_sector_membership", "prior_sector_leader_identity", "sector_rotation_and_lag_evidence"],
        "turnover_leader": ["dated_turnover_rate_or_free_float_shares", "intraday_exchange_of_holders_and_queue_quality"],
    }
    results = []
    for identity, name in ARCHETYPES:
        candidates = sorted(matches.get(identity, []), key=lambda row: (-row["amount_cny"], row["symbol"]))
        supported = identity in matches
        feature_fields = {"space_height_leader": ("stage", "stage_reference_known", "board"),
                          "relay_continuation_leader": ("stage", "stage_reference_known"),
                          "trend_leader": ("sma20", "sma60", "ret20", "ret60"),
                          "liquidity_capacity_proxy": ("amount_rank_pct",),
                          "rebound_repair_candidate": ()}.get(identity, ())
        missing_fields = [field for field in feature_fields if not any(field in row for row in current)]
        stage_archetype = identity in {"space_height_leader", "relay_continuation_leader"}
        stage_unknown_without_match = stage_archetype and not candidates and bool(unknown_stage_limit)
        rebound_archetype = identity == "rebound_repair_candidate"
        rebound_unknown_without_match = rebound_archetype and not candidates and bool(unknown_rebound)
        if supported and (missing_fields or not available_counts[identity] or stage_unknown_without_match or rebound_unknown_without_match):
            status = "unknown"
        else:
            status = "matched" if candidates else "not_matched" if supported else "unknown"
        results.append({"id": identity, "name": name, "status": status, "candidate_count": len(candidates),
            "candidates": candidates, "missing_evidence": missing[identity] + ["feature_field:" + field for field in missing_fields]
                + (["qualified_liquid_limit_up_peer_stage_unknown"] if stage_archetype and unknown_stage_limit else [])
                + (rebound_missing if rebound_archetype else []),
            "feature_supported_liquid_rows": available_counts.get(identity, 0),
            "unknown_stage_liquid_limit_up_rows": len(unknown_stage_limit) if stage_archetype else None,
            "unknown_stage_liquid_limit_up_rows_by_board": unknown_stage_limit_by_board if stage_archetype else {},
            "unresolved_stage_symbols": sorted(row["symbol"] for row in unknown_stage_limit) if stage_archetype else [],
            "unknown_rebound_path_liquid_rows": len(unknown_rebound) if rebound_archetype else None,
            "unresolved_rebound_symbols": sorted(row["symbol"] for row in unknown_rebound) if rebound_archetype else [],
            "rebound_path_status_counts": rebound_status_counts if rebound_archetype else {},
            "readiness": "ready_for_descriptive_research" if status != "unknown" else "missing_evidence",
            "selection_enabled": False, "trade_ready": False,
            "display_order_semantics": "turnover_desc_symbol_tiebreak_not_selected_portfolio"})
    return results, {"qualified_stage_rows": sum(row["qualified_stage"] is not None for row in current),
                     "qualified_stage_liquid_rows": len(valid_stage), "ipo_supported_liquid_rows": len(valid),
                     "same_board_max_known_stage": heights, "unknown_stage_rows_by_board": board_unknown}


def _transition(current, previous, current_tape, previous_tape, previous_day):
    base = {"status": "unknown", "previous_day_required": previous_day,
            "reason": "exact_previous_complete_close_and_features_required",
            "breadth_change": None, "amount_ratio_to_previous": None,
            "relay_by_prior_stage": {}, "unknown_stage_cohort": {"status": "unknown", "member_count": None},
            "strong_stock_feedback": {"status": "unknown"}}
    if previous_tape is None:
        return base
    base.update(status="adjacent_tape_change_only", reason=None,
                breadth_change=current_tape["breadth"] - previous_tape["breadth"],
                amount_ratio_to_previous=current_tape["amount_cny"] / previous_tape["amount_cny"])
    if previous is None:
        base["reason"] = "adjacent_complete_tape_present_but_previous_feature_rows_missing"
        return base
    lookup = {row["symbol"]: row for row in current}
    prior_leaders = [row for row in previous if row["qualified_limit_up"] is True and row["qualified_stage"] is not None and row["qualified_stage"] >= 1]
    unassigned = [row for row in previous if row["qualified_limit_up"] is True and row["qualified_stage"] is None]
    unassigned_count = len(unassigned)
    base["unknown_stage_cohort"] = {"status": "observed_prior_unknown_stage_membership",
        "member_count": unassigned_count, "symbols": sorted(row["symbol"] for row in unassigned),
        "scope": "all_prior_IPO_supported_limit_up_with_unknown_stage_no_current_survivor_filter",
        "assignment": "unassigned_shared_cohort_not_duplicated_across_stages"}
    def outcome(row):
        now = lookup.get(row["symbol"])
        if now is None:
            return None, "missing_current_active_raw_not_removed_from_denominator"
        preclose = _number(now.get("preclose_for_limits"))
        if (_bool(now.get("calendar_contiguous")) is not True or str(now.get("previous_observed_day")) != previous_day
                or preclose is None or abs(preclose / row["close"] - 1) > .01):
            return None, "calendar_or_raw_price_reference_unknown"
        if now["qualified_limit_up"] is None:
            return None, "current_IPO_or_limit_qualification_unknown"
        if now["qualified_limit_up"] and now["qualified_stage"] != row["qualified_stage"] + 1:
            return None, "current_streak_not_verified_from_exact_prior_stage"
        return now, None
    for label, predicate in (("1_to_2", lambda s: s == 1), ("2_to_3", lambda s: s == 2), ("3plus_to_next", lambda s: s >= 3)):
        cohort = [row for row in prior_leaders if predicate(row["qualified_stage"])]
        success = known = 0
        unknown = []
        for row in cohort:
            now, reason = outcome(row)
            if now is None:
                unknown.append({"symbol": row["symbol"], "reason": reason})
            else:
                known += 1
                success += int(now["qualified_limit_up"] is True)
        count = len(cohort)
        possible_total = count + unassigned_count
        conditional_rate = success / count if count and not unknown else None
        base["relay_by_prior_stage"][label] = {"status": "complete_prior_cohort_outcomes" if count and not unknown and not unassigned_count else "unknown",
            "prior_cohort_count": count, "known_stage_count": count,
            "unknown_stage_membership_count": unassigned_count, "possible_denominator_range": [count, possible_total],
            "known_outcome_count": known, "unknown_outcome_count": len(unknown),
            "continuation_count": success,
            "continuation_rate": conditional_rate if not unassigned_count else None,
            "known_stage_conditional_continuation_rate": conditional_rate,
            "conditional_rate_basis": "only_prior_members_with_known_stage_and_all_their_current_outcomes_known_not_full_stage_population",
            "continuation_rate_bounds": [success / possible_total, (success + len(unknown) + unassigned_count) / possible_total] if possible_total else None,
            "unknown_outcomes": unknown,
            "denominator_basis": "known_stage_members_keep_missing_outcomes_plus_shared_unknown_stage_possible_members_not_definite_assignments",
            "bounds_basis": "conservative_membership_and_outcome_bounds_s_over_c_plus_u_to_s_plus_k_plus_u_over_c_plus_u"}
    strong = [row for row in prior_leaders if row["qualified_stage"] >= 2]
    outcomes = [outcome(row) for row in strong]
    known = [row for row, _ in outcomes if row is not None]
    unknown_count = len(strong) - len(known)
    conditional_mean = float(np.mean([row["pct_chg"] for row in known])) if known and not unknown_count else None
    base["strong_stock_feedback"] = {"status": "complete_prior_cohort_outcomes" if strong and not unknown_count and not unassigned_count else "unknown",
        "prior_stage2plus_count": len(strong), "known_stage2plus_count": len(strong),
        "unknown_stage_membership_count": unassigned_count,
        "possible_cohort_count_range": [len(strong), len(strong) + unassigned_count],
        "known_outcome_count": len(known), "unknown_outcome_count": unknown_count,
        "broken_limit_count": sum(row["qualified_limit_up"] is False for row in known),
        "negative_return_count": sum(row["pct_chg"] < 0 for row in known),
        "loss_at_least5pct_count": sum(row["pct_chg"] <= -5 for row in known),
        "counts_scope": "known_stage2plus_members_with_observed_outcomes_only_not_full_possible_cohort",
        "mean_return_pct": conditional_mean if not unassigned_count else None,
        "conditional_known_stage_mean_return_pct": conditional_mean,
        "conditional_mean_basis": "only_known_prior_stage2plus_members_with_all_their_current_outcomes_known",
        "return_basis": "source_reported_pctChg_on_calendar_and_reference_matched_pair_not_strategy_PnL"}
    base["status"] = "adjacent_complete_close_feedback"
    return base


def build_environment(current_features, tapes, calendar_payload, observed_at, *, feature_history=None, ipo_qualification=None,
                      feature_qualification_verified=False):
    """Return one JSON-safe exact-close description; never mutate a ledger.

    ``tapes`` contains complete ``leader_close_preview`` result envelopes, not
    bare counts. ``feature_history`` contains prior days only, each backed by
    its own supplied complete envelope. Callers retain strict package-loading
    and preview-file provenance verification before this function.
    """
    observed = _stamp(observed_at)
    previews = _validate_tapes(tapes, observed)
    day = max(previews)
    if not calendar_payload or calendar_payload.get("schema") != "cn_a_share_exchange_calendar_v1":
        raise ValueError("Environment needs a pinned exchange calendar")
    year = int(calendar_payload["year"])
    sessions, previous_map, calendar_status = _calendar_view(calendar_payload, f"{year}-01-01", f"{year}-12-31", [])
    if calendar_status != "pinned_static_exchange_calendar" or day not in sessions or observed.year != year:
        raise ValueError("Environment source/observation lies outside the supported pinned calendar")
    if any(source_day not in sessions for source_day in previews):
        raise ValueError("Environment preview is not an exchange session")
    if ipo_qualification is not None and not isinstance(ipo_qualification, IpoQualification):
        raise ValueError("IPO qualification must be the explicit typed evidence overlay")
    if feature_qualification_verified and ipo_qualification is None:
        raise ValueError("Verified feature policy requires the same typed IPO qualification")
    latest = _features(current_features, allowed_days={day})
    _match_tape(latest, previews[day]["tape"])
    prior_by_day = {}
    if feature_history is not None and not feature_history.empty:
        history = _features(feature_history, allowed_days=set(previews) - {day})
        for source_day, piece in history.groupby("date", sort=False):
            _match_tape(piece, previews[source_day]["tape"])
            prior_by_day[source_day] = _qualified(piece, ipo_qualification, feature_qualification_verified=feature_qualification_verified)
    current = _qualified(latest, ipo_qualification, feature_qualification_verified=feature_qualification_verified)
    tape = previews[day]["tape"]
    previous_day = previous_map.get(day)
    previous_tape = previews.get(previous_day, {}).get("tape")
    change = _transition(current, prior_by_day.get(previous_day), tape, previous_tape, previous_day)
    _rebound_paths(current, prior_by_day, previous_map)
    archetypes, stage_evidence = _archetypes(current)
    breadth, median = tape["breadth"], _number(tape.get("median_return_pct"))
    if median is None:
        phase, label = "unknown", "收盘分布证据缺失"
    elif breadth >= .2 and median > 0:
        phase, label = "broad_strength_snapshot", "收盘普涨快照"
    elif breadth <= -.2 and median < 0:
        phase, label = "broad_weakness_snapshot", "收盘普跌快照"
    else:
        phase, label = "mixed_breadth_snapshot", "收盘涨跌分化快照"
    obs_day, clock = observed.date().isoformat(), observed.time().replace(tzinfo=None)
    if obs_day == day:
        freshness = "latest_same_day_completed_close"
    elif obs_day in sessions and clock >= datetime.strptime("09:30", "%H:%M").time():
        freshness = "prior_close_not_current_session_or_intraday"
    else:
        freshness = "prior_complete_close_only"
    qualified = [row for row in current if row["ipo_feature_support"]]
    limit_up = sum(row["qualified_limit_up"] is True for row in qualified)
    limit_down = 0
    limit_down_unknown = 0
    for row in current:
        previous_close = _number(row.get("preclose_for_limits"))
        if not row["ipo_feature_support"] or previous_close is None or row.get("board") not in {"main", "chinext", "star"}:
            limit_down_unknown += 1
            continue
        rate = .10 if row["board"] == "main" else .20
        limit_down += math.floor(row["close"] * 100 + .5) <= math.floor(previous_close * (1-rate) * 100 + .5)
    available = {"complete_provider_close": True, "current_intraday": False, "dated_sector_membership": False,
        "turnover_rate": False, "free_float_market_cap": False, "order_book_or_queue": False,
        "identified_net_capital_flow": False, "hidden_participant_intent": False,
        "exact_previous_complete_tape": previous_tape is not None,
        "exact_previous_complete_features": previous_day in prior_by_day,
        "IPO_qualification": ipo_qualification is not None, "exchange_PIT_universe": False,
        "corporate_actions_verified": False, "fresh_account_and_quote": False}
    environment = {"phase": phase, "label": label, "state_semantics": "single_close_distribution_not_market_cycle_or_forecast",
        "evidence": {key: tape.get(key) for key in ("active_non_st_rows", "advance_count", "decline_count", "flat_count", "breadth", "median_return_pct", "amount_cny", "amount_weighted_return_pct", "signed_turnover_association", "top20_turnover_share", "median_close_range_location")},
        "limit_and_ladder": {"raw_limit_up_proxy_count": sum(_bool(row.get("unqualified_limit_up") if feature_qualification_verified else row.get("limit_up")) is True for row in current),
            "IPO_supported_limit_up_proxy_count": limit_up, "IPO_supported_limit_down_proxy_count": int(limit_down),
            "IPO_supported_limit_up_stage_unknown_count": sum(row["qualified_limit_up"] is True and row["qualified_stage"] is None for row in qualified),
            "limit_down_qualification_unknown_rows": limit_down_unknown,
            "IPO_unsupported_feature_rows": len(current) - len(qualified), **stage_evidence},
        "change": change, "missing_evidence": ["intraday_updates", "dated_sector_diffusion", "verified_order_flow", "multi_session_environment_episode"],
        "funds_interpretation": "signed_turnover_is_price_association_not_net_inflow_identity_or_intent"}
    scenarios = []
    for state, names, trigger, invalidate in (
        ("broad_strength_snapshot", ["relay_continuation_leader", "space_height_leader"], "先核对准确相邻收盘接力成功率、板块共振及次日可成交证据", "普涨衰减、强势股亏钱或高位断板反馈恶化"),
        ("mixed_breadth_snapshot", ["trend_leader", "liquidity_capacity_proxy"], "先核对分化是否集中于少数成交额标的及板块相对强度", "趋势结构破坏、集中度上升但广度和承接继续走弱"),
        ("broad_weakness_snapshot", ["cross_regime_candidate", "rebound_repair_candidate"], "先建立跨环境身份记录和修复确认；候选不构成逆势买入许可", "修复失败或缺少跨环境、板块、可成交证据"),
    ):
        scenarios.append({"market_state": state, "current_match": state == phase, "preferred_archetypes": names,
            "association_semantics": "untested_research_hypothesis_not_current_selection",
            "trigger": trigger, "invalidations": [invalidate],
            "readiness": "research_only_missing_intraday_sector_and_execution_evidence", "selection_enabled": False})
    return _clean({"schema": SCHEMA, "implementation_revision": IMPLEMENTATION_REVISION,
        "day": day, "observed_at": observed.isoformat(),
        "status": "complete_close_descriptive_research_only", "freshness": freshness,
        "source_clocks": [_source_clock(source_day, item, latest_day=day) for source_day, item in sorted(previews.items())],
        "market_environment": environment, "archetypes": archetypes, "scenario_matrix": scenarios,
        "data_availability": available,
        "frozen_rule_spec": {"id": "leader_environment_descriptive_v1", "scope": "new_fixed_descriptions_not_old_five_family_or_40day_protocol_change",
            "breadth_strength_threshold": .2, "breadth_weakness_threshold": -.2,
            "archetype_min_amount_cny": MIN_AMOUNT, "capacity_amount_rank_threshold": .99,
            "strong_stock_loss_threshold_pct": -5, "source_reference_quality_bound": .01,
            "unknown_stage_policy": "no_known_space_or_relay_match_plus_IPO_supported_liquid_limit_up_unknown_stage_is_unknown_not_no_match",
            "rebound_path_policy": "observed_necessary_condition_failure_may_exclude_positive_match_requires_exact_D_minus1_D_minus2_reference_chain_never_producer_False_alone",
            "unknown_stage_feedback_policy": "preserve_one_shared_unassigned_prior_cohort_possible_denominator_ranges_null_full_rates_and_means_with_explicit_known_stage_conditionals",
            "trend_rule": "existing_close_gt_sma20_gt_sma60_and_ret20_gt0_and_ret60_known_observed_row_features",
            "multi_label": True, "ranking_or_topN_portfolio_selection": False,
            "calendar_payload_sha256": sha256(json.dumps(calendar_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()},
        "history_semantics": "20/60 means valid observed rows; gaps prevent exact calendar-session interpretation",
        "limitations": ["完整供应商收盘名册不等于交易所PIT资格或公司行动通过", "空间高度仅同板制已知梯队的局部代理；未知梯队不被自动当作低位", "穿越需要事前冻结身份与多日环境episode；单日逆势不能确认穿越", "盘后收盘数据不代表下一交易日盘中市场；未提供盘中更新", "真实换手、板块、盘口、净资金流和参与者意图没有被日线价量替代"],
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
        "signal_admitted": False, "execution_admitted": False, "orders_generated": False,
        "ledger_mutated": False, "model_refit": False, "future_labels_used_for_score": False})
