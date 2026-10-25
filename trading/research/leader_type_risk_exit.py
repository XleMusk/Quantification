"""Fixed eight-type structural reductions beside the unchanged five-session expiry.

This explicit research policy anchors each held setup to the earliest actual
surviving FIFO buy fill and its exact previous-session candidate. It never
refreshes a setup from a later candidate list, treats unknown as a sale, writes
an order, or changes the sole Engine/Ledger execution path.
"""
from copy import deepcopy
import math

from ..contracts import SignalUnavailable, TradingMode
from ..engine import ReplayRiskExitIntent
from ..ledger import tracked_holding_lots
from .leader_environment import ARCHETYPES
from .leader_listing_features import METHOD_ID as FEATURE_METHOD
from .shortline_decision_v3 import FiveSessionExpiryPolicy, _known_raw


POLICY_ID = "leader_eight_type_structural_plus_five_session_expiry_v1"
SIGNAL_SOURCE = "research:leader_fixed_entry_structure_and_expiry_v1"
TYPE_IDS = tuple(identity for identity, _ in ARCHETYPES)
RULE_SPEC = {"id": POLICY_ID, "entry_anchor": "earliest_surviving_actual_FIFO_buy_fill_exact_previous_session_candidate",
    "candidate_refresh": False, "unknown": "defer_structural_exit_original_expiry_still_applies",
    "space_height_leader": "qualified_limit_false_or_known_higher_same_board_qualified_stage",
    "relay_continuation_leader": "qualified_limit_false",
    "trend_leader": "complete_close_sma20_sma60_and_close_lt_sma20_or_sma20_le_sma60",
    "liquidity_capacity_proxy": "amount_rank_pct_lt_0.99",
    "rebound_repair_candidate": "close_lt_entry_exact_previous_session_open",
    "cross_regime_candidate": "close_lt_entry_episode_structure_close",
    "sector_catchup_leader": "close_lt_entry_setup_raw_low",
    "turnover_leader": "close_lt_entry_setup_raw_low",
    "expiry": "unchanged_FiveSessionExpiryPolicy", "execution": "same_Engine_exact_next_session_reduce_only",
    "intraday_stop_claimed": False, "parameters_selected_by_PnL": False,
    "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
    "execution_admitted": False, "corporate_actions_verified": False, "pit_verified": False}


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _boolean(value):
    if value is True or str(value) in {"True", "true", "1"}:
        return True
    if value is False or str(value) in {"False", "false", "0"}:
        return False
    return None


def _qualified(row, day):
    """Do not confuse an excluded/unknown IPO feature's False with a break."""
    if not row or row.get("date", day) != day:
        return None, None
    verified = (row.get("listing_feature_method_id") == FEATURE_METHOD
        and row.get("feature_decision_as_of") == day + "T15:05:00+08:00"
        and row.get("listing_qualification_status") == "supported")
    explicit = (row.get("listing_feature_policy_verified") is True and row.get("ipo_feature_support") is True)
    if not (verified or explicit):
        return None, None
    limit = _boolean(row.get("qualified_limit_up") if explicit else row.get("limit_up"))
    stage = _number(row.get("qualified_stage") if explicit else row.get("stage"))
    known = explicit or _boolean(row.get("stage_reference_known")) is True
    return limit, int(stage) if known and stage is not None and stage >= 0 and stage.is_integer() else None


def surviving_buy_anchor(state, symbol, calendar, day):
    """Reconstruct the Ledger's settled FIFO lot consumption without mutation."""
    if state.get("security_conversion_binding") is not None:
        try:
            tracked = tracked_holding_lots(state, symbol)
        except ValueError:
            return None, "actual_conversion_lot_lineage_unknown"
        if not tracked:
            return None, "actual_fill_position_reconciliation_unknown"
        first = tracked[0]
        fill_day = first["origin_day"]
        if fill_day not in calendar or fill_day > day:
            return None, "actual_fill_history_invalid"
        index = calendar.index(fill_day)
        if index == 0:
            return None, "buy_fill_exact_previous_decision_session_unavailable"
        return {"fill_id": first["origin_fill_id"], "fill_day": fill_day,
            "origin_symbol": first["origin_symbol"], "lot_id": first["lot_id"],
            "available_day": first["available"], "surviving_qty": first["qty"],
            "decision_day": calendar[index - 1]}, None
    lots = []
    indexed = sorted(enumerate(state["fills"].items()), key=lambda value: (value[1][1]["day"], value[0]))
    for _, (identifier, fill) in indexed:
        if fill.get("symbol") != symbol:
            continue
        quantity = fill.get("qty")
        if (type(quantity) is not int or quantity <= 0 or fill["day"] not in calendar or fill["day"] > day
                or fill.get("side") not in {"buy", "sell"}):
            return None, "actual_fill_history_invalid"
        if fill["side"] == "buy":
            available = fill.get("available_day")
            if available not in calendar or available <= fill["day"]:
                return None, "actual_buy_settlement_unavailable"
            lots.append({"fill_id": identifier, "fill_day": fill["day"], "available_day": available,
                         "surviving_qty": quantity})
        else:
            remaining = quantity
            for lot in lots:
                if lot["available_day"] <= fill["day"]:
                    taken = min(remaining, lot["surviving_qty"])
                    lot["surviving_qty"] -= taken
                    remaining -= taken
            if remaining:
                return None, "actual_sell_exceeds_settled_buy_history"
    lots = [lot for lot in lots if lot["surviving_qty"]]
    owned = sum(lot["qty"] for lot in state["positions"].get(symbol, []))
    if not lots or sum(lot["surviving_qty"] for lot in lots) != owned:
        return None, "actual_fill_position_reconciliation_unknown"
    first = lots[0]
    index = calendar.index(first["fill_day"])
    if index == 0:
        return None, "buy_fill_exact_previous_decision_session_unavailable"
    return {**first, "decision_day": calendar[index - 1]}, None


class LeaderTypeRiskExitPolicy:
    """Inputs are copied dated candidate/feature evidence from the fixed panel.

    ``candidate_by_day`` is {day: [complete original candidate dictionaries]}.
    ``feature_by_day`` is {day: {symbol: qualified feature dictionary}}. Declare
    full own-date feature days separately; a bounded candidate subset cannot
    establish same-board space height. Raw setup/reference prices are read only
    from the pinned feed's exact decision/reference session, never today's low.
    """

    def __init__(self, type_id, *, candidate_by_day, feature_by_day, complete_feature_days=(), copy_inputs=True):
        if type_id not in TYPE_IDS:
            raise ValueError("A fixed original eight-type identity is required")
        self.type_id = type_id
        self.policy_id = POLICY_ID + ":" + type_id
        self.candidates = {}
        for day, values in candidate_by_day.items():
            rows = deepcopy(list(values)) if copy_inputs else list(values)
            mapping = {row["symbol"]: row for row in rows}
            if len(mapping) != len(rows) or any(row.get("source_day", day) != day or row.get("date", day) != day for row in rows):
                raise ValueError("Entry candidates must be distinct and exactly dated")
            self.candidates[day] = mapping
        self.features = deepcopy(feature_by_day) if copy_inputs else feature_by_day
        self.complete_feature_days = frozenset(complete_feature_days)
        self.expiry = FiveSessionExpiryPolicy()

    def validate_session(self, state, feed, day, context):
        self.expiry.validate_session(state, feed, day, context)

    def _entry(self, state, feed, day, symbol):
        anchor, reason = surviving_buy_anchor(state, symbol, feed.calendar, day)
        if reason:
            return None, reason
        if anchor.get("origin_symbol", symbol) != symbol:
            return anchor, "conversion_structure_not_rebased"
        decision = anchor["decision_day"]
        candidate = self.candidates.get(decision, {}).get(symbol)
        if candidate is None:
            return anchor, "actual_buy_entry_candidate_evidence_unavailable"
        entry = {**anchor, "candidate_source_day": decision,
            "candidate_available_at": candidate.get("available_at"), "evidence": deepcopy(candidate.get("evidence", {})),
            "board": candidate.get("board"), "setup_low": None, "setup_high": None}
        raw = feed.rows.get(decision, {}).get(symbol)
        if _known_raw(raw):
            entry.update(setup_low=float(raw["low"]), setup_high=float(raw["high"]),
                setup_price_source="exact_entry_decision_pinned_raw")
        # An explicit saved setup must agree with the exact raw it describes.
        for key, raw_key in (("setup_low", "low"), ("structure_high", "high")):
            saved = candidate.get(key, entry["evidence"].get(key))
            if saved is not None and (_number(saved) is None or not _known_raw(raw)
                                      or float(saved) != float(raw[raw_key])):
                return entry, "saved_entry_setup_disagrees_with_exact_raw"
        return entry, None

    def evaluate(self, day, book, feed, symbol):
        entry, reason = self._entry(book.state, feed, day, symbol)
        result = {"symbol": symbol, "type_id": self.type_id, "entry": entry,
            "status": "unknown", "reasons": [], "facts": {}, "execution_admitted": False}
        if reason:
            result["reasons"].append(reason)
            return result
        row = self.features.get(day, {}).get(symbol)
        raw = feed.rows.get(day, {}).get(symbol)
        close = _number(raw.get("close")) if _known_raw(raw) else None
        if row and row.get("date", day) != day:
            result["reasons"].append("current_feature_source_day_mismatch")
            return result
        facts, unknown, invalid = result["facts"], [], []
        if self.type_id in {"space_height_leader", "relay_continuation_leader"}:
            limit, stage = _qualified(row, day)
            facts.update(qualified_limit_up=limit, qualified_stage=stage)
            if limit is False:
                invalid.append("current_qualified_limit_explicitly_broken")
            elif limit is None:
                unknown.append("current_qualified_limit_unknown")
            if self.type_id == "space_height_leader" and not invalid:
                board = row.get("board") if row else None
                facts["board"] = board
                if day not in self.complete_feature_days or not board or stage is None:
                    unknown.append("same_board_complete_current_height_unavailable")
                else:
                    peers = [(peer_symbol, _qualified(peer, day)) for peer_symbol, peer in self.features[day].items()
                             if peer.get("board") == board]
                    higher = sorted(peer_symbol for peer_symbol, (peer_limit, peer_stage) in peers
                        if peer_limit is True and peer_stage is not None and peer_stage > stage)
                    facts["known_higher_same_board_symbols"] = higher
                    facts["unknown_same_board_height_count"] = sum(peer_stage is None for _, (_, peer_stage) in peers)
                    if higher:
                        invalid.append("known_higher_same_board_qualified_ladder")
                    elif facts["unknown_same_board_height_count"]:
                        unknown.append("same_board_peer_height_unknown")
        elif self.type_id == "trend_leader":
            s20, s60 = (_number(row.get(key)) if row else None for key in ("sma20", "sma60"))
            facts.update(close=close, sma20=s20, sma60=s60, window_semantics="original_valid_observed_rows_not_exact_session_PIT")
            if close is None or s20 is None or s60 is None or min(close, s20, s60) <= 0:
                unknown.append("complete_current_trend_fields_unavailable")
            elif close < s20 or s20 <= s60:
                invalid.append("current_close_below_sma20_or_sma20_not_above_sma60")
        elif self.type_id == "liquidity_capacity_proxy":
            rank = _number(row.get("amount_rank_pct")) if row else None
            facts.update(amount_rank_pct=rank, fixed_cutoff=.99)
            if rank is None or not 0 <= rank <= 1:
                unknown.append("current_amount_rank_unknown")
            elif rank < .99:
                invalid.append("current_amount_rank_below_fixed_0_99")
        else:
            threshold, basis = None, None
            if self.type_id == "rebound_repair_candidate":
                decision_index = feed.calendar.index(entry["decision_day"])
                before = feed.calendar[decision_index - 1] if decision_index else None
                previous = feed.rows.get(before, {}).get(symbol)
                saved = _number(entry["evidence"].get("previous_open"))
                if saved is not None and saved > 0:
                    threshold, basis = saved, "fixed_entry_candidate_previous_open"
                    if _known_raw(previous) and saved != float(previous["open"]):
                        threshold = None
                        unknown.append("entry_previous_open_conflicts_with_exact_reference")
                elif _known_raw(previous):
                    threshold, basis = float(previous["open"]), "fixed_entry_exact_previous_session_raw_open"
                facts["entry_reference_day"] = before
            elif self.type_id == "cross_regime_candidate":
                threshold = _number(entry["evidence"].get("structure_close"))
                basis = "fixed_entry_episode_structure_close"
            else:
                threshold, basis = entry["setup_low"], "fixed_entry_setup_raw_low"
            facts.update(close=close, structure_price=threshold, structure_basis=basis)
            if close is None or threshold is None or threshold <= 0:
                unknown.append("required_fixed_entry_structure_unavailable")
            elif close < threshold:
                invalid.append("current_close_below_fixed_entry_structure")
        result.update(status="invalid" if invalid else "unknown" if unknown else "not_invalid",
                      reasons=invalid or unknown)
        return result

    def __call__(self, day, book, feed):
        if book.context.mode != TradingMode.REPLAY:
            raise ValueError("Eight-type structural reductions are replay-only")
        expiry = self.expiry(day, book, feed)
        held = sorted(symbol for symbol in book.state["positions"] if book.quantity(symbol))
        invalid = []
        for symbol in held:
            result = self.evaluate(day, book, feed, symbol)
            book.emit(day, "leader_type_risk_assessed", policy_id=self.policy_id, **result)
            if result["status"] == "invalid":
                invalid.append(symbol)
            elif result["status"] == "unknown":
                book.emit(day, "leader_type_risk_deferred", symbol=symbol, type_id=self.type_id,
                          reasons=result["reasons"], independent_expiry_not_suppressed=True)
        if isinstance(expiry, SignalUnavailable):
            return expiry
        symbols = sorted(set(invalid) | set(expiry.symbols if expiry else ()))
        if not symbols:
            return None
        next_index = feed.calendar.index(day) + 1
        if next_index >= len(feed.calendar):
            return SignalUnavailable("leader_type_risk_actual_next_session_unavailable")
        following = feed.calendar[next_index]
        required = set(held) | set(book.state.get("pending_targets") or {})
        if any(not _known_raw(feed.rows.get(date, {}).get(symbol)) for date in (day, following) for symbol in required):
            return SignalUnavailable("leader_type_risk_relevant_raw_or_status_unavailable")
        return ReplayRiskExitIntent(book.context, day, following, tuple(symbols), SIGNAL_SOURCE,
                                    "fixed_entry_structure_invalid_or_original_five_session_expiry")
