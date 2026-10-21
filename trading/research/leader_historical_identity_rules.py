"""Causal price-label identities in an explicitly posthoc historical method.

Actual evidence clocks are retained. No prospective registration, real leader
identity, theme, order, account, or source-publication claim is manufactured.
"""
from collections import Counter
from copy import deepcopy
from datetime import date

from .leader_daily_signals import MIN_AMOUNT
from .leader_environment import _bool, _number
from .leader_environment_policy import classify_snapshot
from .leader_missing_type_candidates import _eligibility, _reference, _time


METHOD_ID = "historical_observed_known_price_labels_cross_and_broad_industry_catchup_v1"
OLD_TYPES = ("space_height_leader", "relay_continuation_leader", "trend_leader",
             "liquidity_capacity_proxy", "rebound_repair_candidate")
ROLES = frozenset(OLD_TYPES[:2])
SPEC = {"id": METHOD_ID, "identity": "all_observed_known_matched_five_type_labels_no_topN",
    "unknown_labels": "excluded_identity_unknown_not_known_nonleader",
    "episode": "each_exact_adjacent_known_phase_change_E_freeze_Eminus1_members",
    "episode_selection": "latest_only_new_entry_context_not_held_anchor_replacement",
    "superseded_episode": "emit_full_prior_members_results_and_interruption_status_on_replacement",
    "confirmation": "E_E1_E2_two_exact_intervals_market_and_frozen_industry_relative_endpoint_nonnegative",
    "after_confirmation": "original_conditions_fixed_current_close_ge_E_close_until_next_episode",
    "catchup": "prior_saved_space_or_relay_qualified_stage_ge2_positive_sector_excess_current_firstboard_lag_then_positive_excess",
    "reference_tolerance": .01, "minimum_amount_cny": MIN_AMOUNT,
    "industry": "latest_explicit_posthoc_dated_broad_membership_at_or_before_anchor_frozen_full_members",
    "rank": "amount_desc_symbol_asc", "historical_availability_verified": False,
    "old_registry_or_rules_modified": False}
FLAGS = {"candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
    "execution_admitted": False, "pit_verified": False, "returns_reportable": False,
    "historical_signal_publication_verified": False, "confirmed_leader": False,
    "old_publications_modified": False, "model_refit": False, "future_labels_used_for_score": False}


def _stage(row):
    return _number(row.get("qualified_stage") if "qualified_stage" in row else row.get("stage"))


def _limit(row):
    return _bool(row.get("qualified_limit_up") if "qualified_limit_up" in row else row.get("limit_up"))


def _reference_row(row):
    return {**row, "preclose_for_limits": row.get("preclose_for_limits", row.get("preclose"))}


class HistoricalIdentityRules:
    def __init__(self, scope, sessions, industry_provider, actual_created_at, spec=SPEC):
        self.scope, self.sessions = list(scope), list(sessions)
        if (not self.scope or len(set(self.scope)) != len(self.scope) or not self.sessions
                or self.sessions != sorted(set(self.sessions)) or spec != SPEC
                or not callable(industry_provider)):
            raise ValueError("Fixed identity scope, exact calendar, provider and original research spec required")
        for day in self.sessions:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("Canonical identity session required")
        self.created_at = _time(actual_created_at).isoformat()
        self.provider, self.frames, self.episode = industry_provider, {}, None
        self.last_day = None

    def _identity(self, day, panel, view):
        members, unknown = {}, {}
        items = {item["id"]: item for item in panel["types"]}
        if not set(OLD_TYPES) <= set(items):
            raise ValueError("All five saved original type observations are required")
        for identity in OLD_TYPES:
            item = items[identity]
            unknown[identity] = sorted(set(item.get("unresolved_symbols", [])) & set(self.scope))
            for candidate in item.get("candidates", []):
                symbol = candidate["symbol"]
                if symbol not in self.scope:
                    continue
                if candidate.get("source_day", candidate.get("date", day)) != day:
                    raise ValueError("Identity candidate must retain its exact source day")
                state, _, _ = _eligibility(view, symbol)
                if state is not None:
                    unknown[identity] = sorted(set(unknown[identity]) | {symbol})
                    continue
                member = members.setdefault(symbol, {"symbol": symbol, "labels": [], "identity_source_day": day})
                member["labels"].append(identity)
        return {"source_day": day, "members": [members[s] for s in sorted(members)],
            "excluded_identity_unknown": unknown,
            "definition": "observed_known_positive_labels_only_not_complete_real_leader_membership"}

    def _industry(self, anchor):
        value = self.provider(anchor)
        if value is None:
            return None
        value = deepcopy(value)
        if (not isinstance(value.get("membership"), dict) or not value["membership"]
                or not value.get("source_file") or len(value.get("source_sha256", "")) != 64
                or value.get("snapshot_date", "9999") > anchor or value.get("posthoc_only") is not True
                or value.get("revision_safe_PIT_verified") is not False or value.get("theme_membership") is not False
                or _time(value["actual_available_at"]) > _time(self.created_at)):
            raise ValueError("Explicit posthoc dated industry evidence and actual source clock required")
        return value

    def _feature(self, day, symbol):
        view = self.frames.get(day, {}).get("view")
        raw = (view or {}).get("ordinary", {}).get(symbol)
        row = (view or {}).get("features", {}).get(symbol)
        if (not raw or str(raw.get("tradestatus")) != "1" or str(raw.get("isST")) != "0"
                or not row or row.get("listing_qualification_status") != "supported"):
            return None
        return row

    def _sector(self, industry, symbol, anchor_day, measured_day):
        if industry is None or not industry["membership"].get(symbol):
            return None, "dated_broad_industry_membership_unknown", 0
        sector = industry["membership"][symbol]
        anchor = self.frames.get(anchor_day, {}).get("view")
        measured = self.frames.get(measured_day, {}).get("view")
        if anchor is None or measured is None:
            return None, "exact_sector_session_missing", 0
        if (self.frames[anchor_day]["source_complete"] is not True
                or self.frames[measured_day]["source_complete"] is not True):
            return None, "sector_source_incomplete", 0
        members = [s for s, raw in anchor["ordinary"].items()
            if industry["membership"].get(s) == sector and str(raw.get("isST")) == "0"
            and str(raw.get("tradestatus")) == "1"]
        values = [_number(measured["features"].get(s, {}).get("pct_chg"))
                  if s in anchor["features"] else None for s in members]
        if not members or any(value is None for value in values):
            return None, "fixed_industry_anchor_member_outcome_unknown", len(members)
        from statistics import median
        return median(values), None, len(members)

    def _confirm(self):
        episode = self.episode
        days = episode["fixed_window_close_days"]
        results = {}
        for member in episode["identity"]["members"]:
            symbol, legs, reason = member["symbol"], [], None
            if len(days) != 3 or any(day not in self.frames for day in days):
                results[symbol] = {"status": "unknown", "reason": "three_exact_episode_closes_missing", "legs": []}
                continue
            anchor = self._feature(days[0], symbol)
            for prior, current in zip(days[:-1], days[1:]):
                before, own = self._feature(prior, symbol), self._feature(current, symbol)
                if own is None or before is None or _reference(_reference_row(own), before) is not True:
                    reason = "episode_qualification_or_exact_reference_unknown"
                    break
                sector, sector_reason, denominator = self._sector(episode["industry"], symbol, days[0], current)
                pct = _number(own.get("pct_chg"))
                market = self.frames[current]["market_median"]
                if sector is None or pct is None or market is None:
                    reason = sector_reason or "episode_return_or_market_median_unknown"
                    break
                legs.append({"from_day": prior, "to_day": current, "provider_pct": pct,
                    "prior_raw_close": before["close"], "native_preclose": _reference_row(own)["preclose_for_limits"],
                    "market_median_pct": market, "sector_median_pct": sector, "sector_denominator": denominator,
                    "reference_status": "original_one_percent_proxy_not_absence_of_actions"})
            start = _number((anchor or {}).get("close"))
            end = _number((self._feature(days[-1], symbol) or {}).get("close"))
            if reason is not None or start is None or end is None or start <= 0:
                results[symbol] = {"status": "unknown", "reason": reason or "episode_structure_price_unknown", "legs": legs}
            else:
                matched = all(leg["provider_pct"] >= leg["market_median_pct"]
                    and leg["provider_pct"] >= leg["sector_median_pct"] for leg in legs) and end >= start
                results[symbol] = {"status": "matched" if matched else "not_matched",
                    "reason": None if matched else "fixed_episode_relative_or_endpoint_conditions_failed",
                    "structure_close": start, "confirmation_day": days[-1], "legs": legs}
        episode["member_results"] = results
        episode["confirmation_evaluated"] = True

    def _advance_episode(self, day):
        if self.episode is None:
            return
        self.episode["complete_close_count"] = max(self.episode.get("complete_close_count", 0),
            sum(d in self.frames and self.frames[d]["source_complete"] is True
                for d in self.episode["fixed_window_close_days"]))
        if (not self.episode.get("confirmation_evaluated") and self.episode.get("E2") is not None
                and day >= self.episode["E2"]):
            self._confirm()

    def _cross(self, symbol, row, day):
        episode = self.episode
        if episode is None:
            return "unknown", "no_exact_phase_change_episode_observed", {}
        members = {member["symbol"]: member for member in episode["identity"]["members"]}
        if symbol not in members:
            return "not_matched", "outside_defined_observed_identity_universe", {}
        result = episode.get("member_results", {}).get(symbol)
        if result is None:
            return "unknown", "episode_waiting_three_exact_closes", {}
        if result["status"] != "matched":
            return result["status"], result["reason"], deepcopy(result)
        close = _number(row.get("close"))
        if close is None:
            return "unknown", "current_structure_close_unknown", {}
        if close < result["structure_close"]:
            return "not_matched", "fixed_episode_structure_close_broken", deepcopy(result)
        return "matched", None, {**deepcopy(result), "episode_id": episode["episode_id"],
            "episode_close_days": episode["fixed_window_close_days"], "original_proxy_identity": members[symbol]}

    def _catchup(self, symbol, row, day, prior_day, industry):
        limit, stage = _limit(row), _stage(row)
        if limit is False or stage is not None and stage != 1:
            return "not_matched", "current_not_qualified_first_board", {}
        if limit is not True or stage is None:
            return "unknown", "current_first_board_qualification_unknown", {}
        before = self._feature(prior_day, symbol)
        if before is None or _reference(_reference_row(row), before) is not True:
            return "unknown", "catchup_exact_prior_qualification_or_reference_unknown", {}
        if _limit(before) is True:
            return "not_matched", "candidate_already_prior_limit_up", {}
        if _limit(before) is None:
            return "unknown", "prior_candidate_limit_unknown", {}
        identity = self.frames.get(prior_day, {}).get("identity")
        if identity is None:
            return "unknown", "exact_prior_saved_identity_missing", {}
        first, reason, denominator = self._sector(industry, symbol, prior_day, prior_day)
        current, reason2, _ = self._sector(industry, symbol, prior_day, day)
        prior_pct, pct = _number(before.get("pct_chg")), _number(row.get("pct_chg"))
        if first is None or current is None or prior_pct is None or pct is None:
            return "unknown", reason or reason2 or "catchup_relative_return_unknown", {}
        if prior_pct >= first or not pct > current > 0:
            return "not_matched", "prior_lag_or_current_positive_sector_excess_not_matched", {}
        sector = industry["membership"][symbol]
        role_members = [member for member in identity["members"] if ROLES.intersection(member["labels"])]
        if any(not industry["membership"].get(member["symbol"]) for member in role_members):
            return "unknown", "prior_role_member_industry_unknown", {}
        leaders = [member["symbol"] for member in role_members if industry["membership"][member["symbol"]] == sector]
        if symbol in leaders:
            return "not_matched", "candidate_already_prior_observed_leader_proxy", {}
        supported, unknown = [], []
        for leader in leaders:
            prior = self._feature(prior_day, leader)
            leader_pct = _number((prior or {}).get("pct_chg"))
            if prior is None or _stage(prior) is None or _limit(prior) is None or leader_pct is None:
                unknown.append(leader)
            elif _stage(prior) >= 2 and _limit(prior) is True and leader_pct > first:
                supported.append(leader)
        if not supported:
            return ("unknown" if unknown else "not_matched"), ("prior_leader_qualification_unknown" if unknown else
                "no_supported_prior_observed_leader_proxy"), {"unknown_prior_leaders": unknown}
        return "matched", None, {"identity_source_day": prior_day, "frozen_prior_leaders": supported,
            "broad_industry": sector, "prior_sector_median_pct": first, "current_sector_median_pct": current,
            "sector_denominator": denominator, "candidate_prior_pct": prior_pct, "candidate_current_pct": pct}

    def observe(self, day, panel, view):
        from .leader_historical_listing_classification import DESCRIPTOR, VerifiedHistoricalListingDay, eligibility
        hook = view.get(DESCRIPTOR)
        if hook is not None and (not isinstance(hook, VerifiedHistoricalListingDay)
                or hook.day != day or hook.scope_members != tuple(self.scope)):
            raise ValueError("Historical identity classification must retain its verified own-day frozen scope")
        if (day not in self.sessions or self.last_day is not None and day <= self.last_day
                or panel.get("day", day) != day or _time(view["available_at"]) > _time(self.created_at)
                or _time(view["available_at"]) < _time(day + "T15:00:00+08:00")
                or any(row.get("date", day) != day for row in view["features"].values())
                or any(row.get("date", day) != day for row in view["ordinary"].values())):
            raise ValueError("One chronological own-date observation with preserved actual clock required")
        index = self.sessions.index(day)
        prior_day = self.sessions[index - 1] if index else None
        environment = panel["environment"]
        source_complete = environment.get("source_complete") is True
        phase = (classify_snapshot(environment.get("breadth"), environment.get("median_return_pct"))
                 if source_complete else "unknown")
        identity = self._identity(day, panel, view)
        self.frames[day] = {"view": deepcopy(view), "identity": identity, "phase": phase,
                            "source_complete": source_complete,
                            "market_median": _number(environment.get("median_return_pct")) if source_complete else None}
        prior = self.frames.get(prior_day)
        superseded = None
        if prior is not None and phase != "unknown" and prior["phase"] != "unknown" and phase != prior["phase"]:
            self._advance_episode(day)
            if self.episode is not None:
                superseded = {**deepcopy(self.episode), "superseded_on": day,
                    "status_at_supersession": ("superseded_before_three_closes"
                        if self.episode.get("complete_close_count", 0) < 3 else "entry_context_superseded")}
            days = self.sessions[index:index + 3]
            self.episode = {"episode_id": METHOD_ID + ":" + day, "from_phase": prior["phase"], "to_phase": phase,
                "E": day, "E1": days[1] if len(days) > 1 else None, "E2": days[2] if len(days) > 2 else None,
                "fixed_window_close_days": days, "identity": deepcopy(prior["identity"]),
                "industry": self._industry(day), "actual_created_at": self.created_at,
                "historical_registration_claimed": False, "member_results": {}}
        self._advance_episode(day)
        catchup_industry = self._industry(prior_day) if prior is not None else None
        types = []
        for type_id in ("cross_regime_candidate", "sector_catchup_leader"):
            candidates, unknown, exclusions, reasons = [], [], {}, Counter()
            for symbol in self.scope:
                state, reason, row = eligibility(view, symbol, day)
                evidence = {}
                if state is None:
                    state, reason, evidence = (self._cross(symbol, row, day) if type_id == "cross_regime_candidate" else
                        self._catchup(symbol, row, day, prior_day, catchup_industry))
                if state == "unknown":
                    unknown.append(symbol); reasons[reason] += 1
                elif state == "not_matched":
                    exclusions[symbol] = reason
                else:
                    candidates.append({"date": day, "source_day": day, "symbol": symbol,
                        "score": float(row["amount"]), "amount_cny": float(row["amount"]),
                        "available_at": self.created_at, "setup_low": _number(row.get("low")),
                        "structure_high": _number(row.get("high")), "evidence": evidence,
                        "historical_identity_method": METHOD_ID, **FLAGS})
            candidates.sort(key=lambda row: (-row["amount_cny"], row["symbol"]))
            types.append({"id": type_id, "status": "matched" if candidates else "unknown" if unknown else "not_matched",
                "candidates": candidates, "candidate_count": len(candidates), "unresolved_symbols": unknown,
                "scope_complete": not unknown, "known_exclusions": exclusions, "missing_evidence": dict(reasons),
                "scope_member_count": len(self.scope), "source_method": METHOD_ID, **FLAGS})
        result = {"day": day, "method_id": METHOD_ID, "types": types,
            "identity_evidence": {"episode": deepcopy(self.episode), "current_identity": identity,
                "superseded_episode": superseded, "current_environment_phase": phase,
                "current_environment_source_complete": source_complete,
                "prior_identity_scope": deepcopy(prior["identity"]) if prior is not None else None,
                "actual_created_at": self.created_at, "actual_view_available_at": view["available_at"],
                "historical_registration_claimed": False, "reference_tolerance_is_proxy_not_no_actions_proof": True}, **FLAGS}
        self.last_day = day
        self.frames = {d: value for d, value in self.frames.items() if d in self.sessions[max(0, index - 2):index + 1]}
        return result
