"""Opt-in listing-window qualifications for the fixed daily leader rules.

Each decision uses only metadata available by that decision's own cutoff, even
when it evaluates earlier bars. Unrestricted first-five listing sessions are
known exclusions, rather than ordinary limit events. No price is changed and no
unknown candidate population is converted into a known empty cash decision.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math

import numpy as np
import pandas as pd

from .ipo_qualification import IpoQualification, _time
from .leader_daily_signals import FAMILIES, MIN_AMOUNT, _calendar_view

METHOD_ID = "leader_listing_window_features_asof_decision_v1"
UNQUALIFIED_FIELDS = ("limit_up", "stage", "stage_reference_known", "daily_rebound")
LOOKUP_FIELDS = ("close", "open", "pct_chg", "preclose_for_limits", "limit_up")
KNOWN_EXCLUDED_STATES = {"known_no_limit_listing_window", "known_excluded_listing_window_rule_unverified"}


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _state(proof):
    if proof["ipo_window_feature_support"] and proof["engine_block_reason"] is None:
        return "supported"
    if proof["knowledge_status"] != "unknown" and proof["engine_block_reason"] == "unsupported_first_five_listing_sessions":
        return ("known_no_limit_listing_window" if proof.get("no_price_limit_first_five") is True
                else "known_excluded_listing_window_rule_unverified")
    return "unknown"


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def qualify_leader_window(history, *, qualification, calendar_payload, decision_days):
    """Return qualified requested-day features plus per-family uncertainty.

    Historical raw-feature rows stay unchanged. Four original feature columns
    are copied to ``unqualified_*`` in the returned decision-window copy. The
    original research methods remain the default in ``leader_daily_signals``.
    """
    if not isinstance(qualification, IpoQualification):
        raise ValueError("Listing features require explicit typed IPO qualification")
    if not calendar_payload or calendar_payload.get("schema") != "cn_a_share_exchange_calendar_v1":
        raise ValueError("Listing features require a full pinned exchange calendar")
    year = int(calendar_payload["year"])
    sessions, previous, status = _calendar_view(calendar_payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar" or tuple(sessions) != tuple(qualification.calendar):
        raise ValueError("Listing-feature and qualification calendars must match exactly")
    days = list(decision_days)
    if days != sorted(set(days)) or any(day not in previous for day in days):
        raise ValueError("Listing-feature decision days must be unique pinned sessions")
    required = {"date", "symbol", "amount", "sma20", "sma60", "ret20", "ret60", *LOOKUP_FIELDS, *UNQUALIFIED_FIELDS}
    if not required <= set(history.columns) or history.duplicated(["symbol", "date"]).any():
        raise ValueError("Listing features require unique original physical feature rows")
    window = history.loc[history.date.isin(days)].copy().reset_index(drop=True)
    for field in UNQUALIFIED_FIELDS:
        window["unqualified_" + field] = window[field]
    narrow = history.loc[history.date.ge(sessions[0]) & history.date.le(sessions[-1]), ["symbol", "date", *LOOKUP_FIELDS]]
    index = pd.MultiIndex.from_frame(narrow[["symbol", "date"]])
    arrays = {field: narrow[field].to_numpy(copy=False) for field in LOOKUP_FIELDS}
    def position(symbol, day):
        if day is None:
            return None
        try:
            return int(index.get_loc((symbol, day)))
        except KeyError:
            return None
    def number(field, pos):
        return _finite(arrays[field][pos])
    def linked(pos, prior_pos):
        ref, close = number("preclose_for_limits", pos), number("close", prior_pos)
        return ref is not None and close is not None and close > 0 and abs(ref / close - 1) <= .01
    outputs = {field: [None] * len(window) for field in ("limit_up", "stage", "stage_reference_known", "daily_rebound",
        "listing_qualification_status", "listing_feature_reason", "rebound_feature_reason", "feature_decision_as_of")}
    decisions = {}
    unknown_by_family = {family: {} for family in FAMILIES}
    excluded_by_day = {}
    for day in days:
        cutoff = day + "T15:05:00+08:00"
        subset = window.loc[window.date.eq(day)]
        proof_cache, stage_cache = {}, {}
        def proof(symbol, historical_day):
            key = symbol, historical_day
            if key not in proof_cache:
                proof_cache[key] = qualification.qualify(historical_day, symbol, decision_as_of=cutoff)
            return proof_cache[key]
        def stage(symbol, historical_day):
            key = symbol, historical_day
            if key in stage_cache:
                return stage_cache[key]
            pos = position(symbol, historical_day)
            if pos is None:
                value = None, "missing_exact_historical_bar"
            else:
                view = proof(symbol, historical_day)
                state = _state(view)
                if state != "supported":
                    value = None, state
                elif not bool(arrays["limit_up"][pos]):
                    value = 0, "known_supported_non_limit_bar"
                else:
                    prev_day = previous.get(historical_day)
                    prior_pos = position(symbol, prev_day)
                    if prior_pos is None or not linked(pos, prior_pos):
                        value = None, "missing_exact_previous_bar_or_reference"
                    elif view.get("listing_session_ordinal") == 6:
                        if _state(proof(symbol, prev_day)) == "known_no_limit_listing_window":
                            value = 1, "first_supported_listing_session_starts_new_stage1"
                        else:
                            value = None, "previous_listing_window_no_limit_rule_unverified"
                    else:
                        prior_stage, reason = stage(symbol, prev_day)
                        if prior_stage is None:
                            value = None, "unknown_prior_qualified_stage:" + reason
                        else:
                            value = prior_stage + 1 if prior_stage else 1, "exact_qualified_limit_chain"
            stage_cache[key] = value
            return value
        def rebound(symbol, pos, current_state):
            if number("pct_chg", pos) <= 0:
                return False, "observed_current_return_not_positive"
            if current_state in KNOWN_EXCLUDED_STATES:
                return False, "current_known_excluded_listing_window"
            prev_day = previous.get(day)
            prior_pos = position(symbol, prev_day)
            if prior_pos is None or not linked(pos, prior_pos):
                return None, "missing_exact_D_minus1_bar_or_reference"
            if number("pct_chg", prior_pos) >= 0 or number("close", pos) <= number("open", prior_pos):
                return False, "observed_D_minus1_necessary_condition_failed"
            two_day = previous.get(prev_day)
            two_pos = position(symbol, two_day)
            if two_pos is None or not linked(prior_pos, two_pos):
                return None, "missing_exact_D_minus2_bar_or_reference"
            old_state = _state(proof(symbol, two_day))
            if old_state == "known_no_limit_listing_window":
                return False, "D_minus2_known_no_limit_listing_window"
            if not bool(arrays["limit_up"][two_pos]):
                return False, "observed_D_minus2_below_regular_board_limit"
            if current_state != "supported" or old_state != "supported":
                return None, "current_or_D_minus2_listing_qualification_unknown"
            return True, "exact_qualified_D_minus2_limit_and_repair_chain"
        unknown = {family: [] for family in FAMILIES}
        excluded = []
        visible_records = {}
        reasons = {family: {} for family in FAMILIES}
        for row in subset.itertuples():
            out = row.Index
            pos = position(row.symbol, day)
            current_proof = proof(row.symbol, day)
            current_state = _state(current_proof)
            value, reason = stage(row.symbol, day)
            reb, reb_reason = rebound(row.symbol, pos, current_state)
            outputs["limit_up"][out] = bool(row.limit_up) if current_state == "supported" else None
            outputs["stage"][out] = float(value) if value is not None else np.nan
            outputs["stage_reference_known"][out] = value is not None
            outputs["daily_rebound"][out] = reb
            outputs["listing_qualification_status"][out] = current_state
            outputs["listing_feature_reason"][out] = reason
            outputs["rebound_feature_reason"][out] = reb_reason
            outputs["feature_decision_as_of"][out] = cutoff
            record = qualification.records.get(row.symbol)
            if record and _time(record["available_at"]) <= _time(cutoff):
                visible_records[row.symbol] = record
            if row.amount < MIN_AMOUNT:
                continue
            if current_state in KNOWN_EXCLUDED_STATES:
                excluded.append(row.symbol)
                continue
            if bool(row.limit_up) and value is None:
                for family in FAMILIES[:3]:
                    unknown[family].append(row.symbol)
                    reasons[family][row.symbol] = reason
            trend = (row.close > row.sma20 > row.sma60 and row.ret20 > 0 and pd.notna(row.ret60))
            if trend and current_state != "supported":
                unknown["trend_leader_20_60"].append(row.symbol)
                reasons["trend_leader_20_60"][row.symbol] = "current_listing_qualification_unknown"
            if reb is None:
                unknown["daily_rebound_proxy"].append(row.symbol)
                reasons["daily_rebound_proxy"][row.symbol] = reb_reason
        visible_digest = _digest(visible_records)
        excluded = sorted(excluded)
        excluded_by_day[day] = excluded
        decisions[day] = {}
        for family in FAMILIES:
            values = sorted(set(unknown[family]))
            unknown_by_family[family][day] = values
            decisions[day][family] = {"method_id": METHOD_ID, "day": day, "decision_as_of": cutoff,
                "decision_visible_records_sha256": visible_digest, "unknown_symbols": values,
                "unknown_reasons_by_symbol": reasons[family], "known_excluded_symbols": excluded,
                "qualification_complete": not values,
                "unknown_semantics": "potential_rule_membership_unknown_requires_NO_CHANGE_not_empty_cash",
                "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
                "signal_admitted": False, "execution_admitted": False}
    for field, values in outputs.items():
        window[field] = pd.Series(values, dtype="boolean") if field in {"limit_up", "daily_rebound", "stage_reference_known"} else values
    # Listing eligibility applies to the unchanged trend mask too. This is an
    # explicit policy overlay, never an unexplained change to its 20/60 rule.
    window["listing_feature_method_id"] = METHOD_ID
    return window, {"method_id": METHOD_ID, "verification": deepcopy(qualification.verification),
        "decision_cutoff_policy": "each_decision_own_15:05_local_time_for_current_and_historical_qualification",
        "history_metadata_semantics": "historical_price_features_reassessed_with_metadata_known_by_each_decision_not_historical_PIT_claim",
        "first_supported_listing_session_policy": "session6_limit_event_starts_stage1_only_after_verified_no_limit_first5_boundary_other_board_rule_unknown_stays_unknown",
        "raw_prices_modified": False, "unqualified_feature_columns": ["unqualified_" + field for field in UNQUALIFIED_FIELDS],
        "unknown_symbols_by_family_by_day": unknown_by_family,
        "known_excluded_symbols_by_day": excluded_by_day, "decisions": decisions,
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
        "signal_admitted": False, "execution_admitted": False, "pit_verified": False,
        "model_refit": False, "future_labels_used_for_score": False}
