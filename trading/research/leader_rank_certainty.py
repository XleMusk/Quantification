"""Sufficient rank/capacity proofs with unresolved candidate membership.

Unknown members remain unknown. A proof only says that their most favorable
score cannot alter the saved top-k order. This module produces no orders and
does not change the original full-panel strategy's admission rule.
"""
from __future__ import annotations

from datetime import date
import math


METHOD_ID = "amount_rank_upper_bound_and_causal_capacity_certainty_v1"


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("An explicit finite nonnegative rank score is required")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("An explicit finite nonnegative rank score is required") from error
    if not math.isfinite(number) or number < 0:
        raise ValueError("An explicit finite nonnegative rank score is required")
    return number


def _symbol(value):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("Distinct explicit source symbols are required")
    return value


def _key(row):
    return -row["score"], row["symbol"]


def certify_topk(known, unknown, k=5):
    """Prove a fixed ranking for every optional unknown-member completion.

    Scores retain the original runner's float conversion and symbol tie-break.
    Unknown scores may be anything up to the provided upper bound. Missing
    upper bounds stay unbounded, never zero. The result is a sufficient proof,
    not an assertion that unproven candidates would actually rank differently.
    """
    if type(k) is not int or k <= 0:
        raise ValueError("A positive integer rank size is required")
    rows = [{"symbol": _symbol(row["symbol"]), "score": _number(row["score"])} for row in known]
    bounds = [{"symbol": _symbol(row["symbol"]), "upper_score":
               None if row.get("upper_score") is None else _number(row["upper_score"])} for row in unknown]
    symbols = [row["symbol"] for row in rows + bounds]
    if len(symbols) != len(set(symbols)):
        raise ValueError("Known and unknown source identities must be distinct and disjoint")
    ranked = sorted(rows, key=_key)[:k]
    reasons, entrants = [], []
    if bounds and len(ranked) < k:
        reasons.append("known_ranking_shorter_than_k_with_unknown_membership")
        entrants = [row["symbol"] for row in bounds]
    elif bounds:
        cutoff = _key(ranked[-1])
        entrants = [row["symbol"] for row in bounds if row["upper_score"] is None
            or (-row["upper_score"], row["symbol"]) < cutoff]
    if any(row["upper_score"] is None for row in bounds):
        reasons.append("unbounded_unknown_score")
    if entrants:
        reasons.append("unknown_may_enter_topk")
    certified = not reasons
    return {"method_id": METHOD_ID, "k": k, "certified": certified,
        "ranking": ranked, "known_count": len(rows), "unknown_count": len(bounds),
        "potential_unknown_entrants": entrants, "reasons": reasons,
        "target_nonempty_certified": certified and bool(ranked),
        "empty_semantics": "certified_empty_rank_is_original_NO_CHANGE_not_liquidation",
        "score_semantics": "original_float_score_desc_symbol_asc",
        "all_supplied_memberships_known": not bounds, "unknown_membership_modified": False}


class CapacityTracker:
    """Prove the exact first-seen pool while retaining its observed saved path.

    Once earlier uncertainty could have changed the not-yet-full pool, later
    known-only observations do not heal that historical ambiguity. A proven
    full pool stays fixed. Rank uncertainty confined wholly to existing pool
    members also cannot change the pool, although targets remain unproven.
    """
    def __init__(self, cap=20, expected_days=None):
        if type(cap) is not int or cap <= 0:
            raise ValueError("A positive integer source capacity is required")
        if expected_days is not None and (not expected_days or expected_days != sorted(set(expected_days))):
            raise ValueError("An exact ordered source decision calendar is required")
        self.cap, self.expected_days = cap, expected_days
        self._observed, self._seen = [], []
        self._certified, self._first_ambiguity = True, None

    def observe(self, day, certificate, saved_before, saved_after):
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("An exact source decision date is required")
        if self._observed and day <= self._observed[-1]:
            raise ValueError("Capacity observations cannot skip backward or repeat")
        if self.expected_days is not None and (len(self._observed) >= len(self.expected_days)
                or day != self.expected_days[len(self._observed)]):
            raise ValueError("Capacity must consume the complete exact source-calendar prefix")
        if (certificate.get("method_id") != METHOD_ID or type(certificate.get("certified")) is not bool
                or certificate.get("unknown_membership_modified") is not False):
            raise ValueError("An explicit rank-certainty result is required")
        ranked = certificate["ranking"]
        symbols = [_symbol(row["symbol"]) for row in ranked]
        if (len(symbols) != len(set(symbols)) or len(ranked) > certificate["k"]
                or ranked != sorted(ranked, key=_key)):
            raise ValueError("The original distinct known top-k order is required")
        for row in ranked:
            _number(row["score"])
        before = list(self._seen)
        after = list(before)
        for symbol in symbols:
            if symbol not in after and len(after) < self.cap:
                after.append(symbol)
        if saved_before != before or saved_after != after:
            raise ValueError("Saved capacity before/after differs from the exact observed known-top-k path")
        possible = set(symbols) | set(certificate["potential_unknown_entrants"])
        uncertain_growth = (not certificate["certified"] and len(before) < self.cap
                            and not possible <= set(before))
        certified = self._certified and not uncertain_growth
        first = self._first_ambiguity or (day if not certified else None)
        # Commit only after all source-path validation succeeds.
        self._seen, self._certified, self._first_ambiguity = after, certified, first
        self._observed.append(day)
        retained = [dict(row) for row in ranked if row["symbol"] in after]
        return {"method_id": METHOD_ID, "day": day, "cap": self.cap,
            "observed_pool_before": before, "observed_pool_after": after,
            "capacity_state_certified": certified,
            "selected_ranking": retained,
            "selection_set_certified": certified and certificate["certified"],
            "entry_targets_certified": certified and certificate["certified"] and bool(retained),
            "first_capacity_ambiguity_day": first,
            "rank_certified": certificate["certified"], "unknown_membership_modified": False,
            "observed_sessions": len(self._observed), "source_calendar_bound": self.expected_days is not None,
            "conservative_projection": "uncertain_global_topk_is_not_admitted_even_if_filtered_targets_might_match"}
