"""One fixed entry-2ATR close exit beside the unchanged Donchian selector.

This is a long-only local adaptation, not a complete Turtle replication. The
provider must declare one frozen signal-price basis. Actual raw fills are
mapped into it only at/after that fill day's close; entry N is the ATR saved
on the actual entry order's previous source session. No future E bar is read
at a D decision and no order is written outside the common Engine/Ledger.
"""
from copy import deepcopy
from dataclasses import replace
import math

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..engine import ReplayRiskExitIntent
from .leader_type_risk_exit import surviving_buy_anchor


POLICY_ID = "donchian_entry_actual_fill_fixed_two_atr_close_stop_v1"
STATE_KEY = "donchian_entry_atr_stop"
SPEC = {"policy_id": POLICY_ID, "stop_multiple": 2,
    "trigger": "same_frozen_basis_D_close_lte_first_actual_fill_price_minus_2_entry_N",
    "N": "unchanged_Wilder_ATR20_at_exact_previous_source_session_of_first_buy_order",
    "fill_mapping": "raw_fill_CNY_times_actual_fill_day_raw_to_signal_factor_at_or_after_E_close",
    "anchor": "first_actual_buy_fill_of_continuous_holding_not_average_cost",
    "partial_or_additional_fills_reset_anchor": False,
    "blocked_exit": "sticky_reduce_only_on_each_later_actual_session",
    "reentry": "veto_stopped_channel_regime_until_explicit_new_regime_after_flat",
    "ranking_position_cost": "unchanged_original_Donchian20_10_ATR20_rank_and_Engine",
    "unknown": "NO_CHANGE_for_unresolved_held_anchor_or_veto_not_empty_target",
    "intraday_stop": False, "pyramiding": False, "risk_based_sizing": False,
    "exact_original_Turtle_replication": False,
    "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False}


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _regime(row):
    value = row.get("regime") if row else None
    if isinstance(value, bool):
        return None
    number = _positive(value)
    return int(number) if number is not None and number.is_integer() else None


def _continuous_anchor(state, symbol, calendar, day):
    # This also checks the real FIFO settlement/remaining-quantity contract.
    # Share distributions not represented by fills remain explicitly unknown;
    # no repair or replacement of the actual Ledger quantity is attempted.
    _, reason = surviving_buy_anchor(state, symbol, calendar, day)
    if reason:
        return None, reason
    quantity, origin = 0, None
    fills = sorted(enumerate(state["fills"].items()), key=lambda item: (item[1][1]["day"], item[0]))
    for _, (identifier, fill) in fills:
        if fill.get("symbol") != symbol:
            continue
        if fill["side"] == "buy":
            if quantity == 0:
                origin = {"fill_id": identifier, "fill_day": fill["day"]}
            quantity += fill["qty"]
        else:
            quantity -= fill["qty"]
    if origin is None or quantity != sum(l["qty"] for l in state["positions"].get(symbol, [])):
        return None, "continuous_actual_fill_anchor_unknown"
    fill = state["fills"][origin["fill_id"]]
    order = state["orders"].get(fill.get("order_id"))
    order_day = order.get("day") if order else None
    if (not order or order.get("side") != "buy" or order.get("symbol") != symbol
            or order_day not in calendar or order_day > origin["fill_day"]
            or calendar.index(order_day) == 0):
        return None, "first_actual_buy_order_source_session_unknown"
    return {**origin, "order_id": fill["order_id"], "order_day": order_day,
        "decision_day": calendar[calendar.index(order_day)-1]}, None


class AtrEntryStopPolicy:
    """Use a pure dated provider; retained state is the recovery checkpoint.

    ``snapshot(day, symbol)`` returns ``date, price_basis_id, price_unit,
    close, atr, raw_to_signal_factor, regime, trend_active, source``. Prices,
    ATR and the factor destination must share ``provider.price_basis_id``;
    ``price_unit`` is exactly ``CNY_per_share``. Missing fields defer rather
    than inventing a price, ATR, flat book or tradable quote.
    """
    def __init__(self, provider):
        basis = getattr(provider, "price_basis_id", None)
        if not isinstance(basis, str) or not basis or not callable(getattr(provider, "snapshot", None)):
            raise ValueError("A dated same-basis price/ATR provider is required")
        self.provider = provider
        self.binding = {"policy_id": POLICY_ID, "price_basis_id": basis,
            "price_unit": "CNY_per_share", "stop_multiple": 2}
        self.policy_id = POLICY_ID + ":" + basis
        self._prepared = None
        self._prepared_state = None

    def _snapshot(self, day, symbol, *, through, feed):
        if day > through:
            raise ValueError("Entry-ATR policy may never request a future session")
        row = self.provider.snapshot(day, symbol)
        raw = feed.rows.get(day, {}).get(symbol)
        raw_close = _positive(raw.get("close")) if isinstance(raw, dict) else None
        if (not isinstance(row, dict) or row.get("date") != day
                or row.get("price_basis_id") != self.binding["price_basis_id"]
                or row.get("price_unit") != "CNY_per_share"
                or not isinstance(row.get("source"), str) or not row["source"]
                or _positive(row.get("close")) is None
                or _positive(row.get("raw_to_signal_factor")) is None
                or raw_close is None or str(raw.get("adjustflag")) != "3"
                or not math.isclose(float(row["close"]), raw_close * float(row["raw_to_signal_factor"]),
                                    rel_tol=1e-10, abs_tol=1e-10)):
            return None
        return row

    def _new_anchor(self, origin, state, symbol, day, feed):
        entry = self._snapshot(origin["decision_day"], symbol, through=day, feed=feed)
        fill_quote = self._snapshot(origin["fill_day"], symbol, through=day, feed=feed)
        fill = state["fills"][origin["fill_id"]]
        price = _positive(fill.get("price"))
        n = _positive(entry.get("atr")) if entry else None
        regime = _regime(entry)
        if (entry is None or fill_quote is None or n is None or regime is None
                or entry.get("trend_active") is not True or price is None
                or fill.get("price_unit", "cent") != "cent"):
            return None, "exact_entry_N_or_actual_fill_same_basis_mapping_unknown"
        mapped = price / 100 * float(fill_quote["raw_to_signal_factor"])
        return {**origin, "price_basis_id": self.binding["price_basis_id"], "price_unit": "CNY_per_share",
            "raw_fill_price_cents": fill["price"], "fill_mapping_factor": float(fill_quote["raw_to_signal_factor"]),
            "entry_price": mapped, "N": n, "stop_price": mapped - 2*n,
            "entry_regime": regime, "entry_source": entry["source"], "fill_mapping_source": fill_quote["source"],
            "stop_triggered": False, "triggered_on": None}, None

    def prepare(self, day, book, feed):
        if book.context.mode != TradingMode.REPLAY or day not in feed.calendar:
            raise ValueError("Entry-ATR stops require one exact replay session")
        binding = {**self.binding, "context": book.context.to_dict()}
        meta = book.state.setdefault(STATE_KEY, {"binding": deepcopy(binding),
            "anchors": {}, "blocked_regimes": {}, "last_assessment_day": None, "assessments": {}})
        if meta["binding"] != binding:
            raise ValueError("Entry-ATR provider basis or fixed stop rule changed during recovery")
        cache_key = (id(book.state), book.context, day)
        if (meta["last_assessment_day"] == day and self._prepared_state is book.state
                and self._prepared == cache_key):
            return meta
        held = {s for s in book.state["positions"] if book.quantity(s) > 0}
        for symbol in set(meta["anchors"]) - held:
            del meta["anchors"][symbol]  # Flat releases anchor, not its stopped-regime veto.
        assessments = {}
        for symbol in sorted(held):
            origin, reason = _continuous_anchor(book.state, symbol, feed.calendar, day)
            anchor = meta["anchors"].get(symbol)
            if reason is None and anchor is not None and anchor["fill_id"] != origin["fill_id"]:
                reason = "continuous_holding_origin_changed_without_observed_flat"
            if reason is None and anchor is None:
                anchor, reason = self._new_anchor(origin, book.state, symbol, day, feed)
                if anchor:
                    meta["anchors"][symbol] = anchor
            quote = self._snapshot(day, symbol, through=day, feed=feed)
            if reason is None and quote is None:
                reason = "current_same_basis_close_or_mapping_unknown"
            if reason is None:
                if float(quote["close"]) <= anchor["stop_price"]:
                    anchor.update(stop_triggered=True, triggered_on=anchor["triggered_on"] or day)
                    veto = meta["blocked_regimes"].setdefault(symbol, [])
                    for regime in (anchor["entry_regime"], _regime(quote)):
                        if regime is not None and regime not in veto:
                            veto.append(regime)
                status = "stop_triggered" if anchor["stop_triggered"] else "not_triggered"
            else:
                status = "unknown"
            assessments[symbol] = {"status": status, "reason": reason,
                "entry": deepcopy(anchor), "close": float(quote["close"]) if quote else None}
            book.emit(day, "donchian_entry_atr_stop_assessed", symbol=symbol,
                policy_id=self.policy_id, **deepcopy(assessments[symbol]))
        meta.update(last_assessment_day=day, assessments=assessments)
        self._prepared = cache_key
        self._prepared_state = book.state
        return meta

    def filter_frame(self, frame, day, book, feed):
        meta = self.prepare(day, book, feed)
        if any(a["status"] == "unknown" and not (a["entry"] or {}).get("stop_triggered")
                for a in meta["assessments"].values()):
            return SignalUnavailable("entry_ATR_held_anchor_or_same_basis_close_unknown")
        retained = []
        for signal in frame.signals:
            symbol = signal.security.symbol
            if (meta["anchors"].get(symbol) or {}).get("stop_triggered"):
                continue
            if book.quantity(symbol) == 0:
                entry = self._snapshot(day, symbol, through=day, feed=feed)
                if (entry is None or _positive(entry.get("atr")) is None or _regime(entry) is None
                        or entry.get("trend_active") is not True):
                    return SignalUnavailable("prospective_entry_N_or_same_basis_mapping_unknown")
            veto = meta["blocked_regimes"].get(symbol, [])
            if veto:
                quote = self._snapshot(day, symbol, through=day, feed=feed)
                regime = _regime(quote)
                if regime is None or quote.get("trend_active") not in {True, False}:
                    return SignalUnavailable("stopped_channel_regime_reentry_evidence_unknown")
                if regime in veto:
                    continue
            retained.append(signal)
        return replace(frame, signals=tuple(retained))

    def __call__(self, day, book, feed):
        meta = self.prepare(day, book, feed)
        stopped = tuple(sorted(s for s,a in meta["anchors"].items()
                               if a["stop_triggered"] and book.quantity(s) > 0))
        if not stopped:
            return None
        index = feed.calendar.index(day)
        if index+1 >= len(feed.calendar):
            return SignalUnavailable("entry_ATR_stop_exact_next_session_unavailable")
        return ReplayRiskExitIntent(book.context, day, feed.calendar[index+1], stopped,
            "research:" + POLICY_ID, "fixed_actual_entry_price_minus_two_entry_ATR_close_stop")


class AtrStopFeed:
    """Filter only an unchanged ordered subsequence of the original frame."""
    def __init__(self, base, policy):
        if not isinstance(policy, AtrEntryStopPolicy):
            raise TypeError("The same independent entry-ATR stop policy must be explicit")
        self.base, self.policy = base, policy
        self.close_candidate_policy = {"id": policy.policy_id,
            "capacity": "original_Donchian_ranking_no_added_or_reordered_candidate",
            "capital_policy": "original_unchanged_target_and_position_limits"}

    def __getattr__(self, name):
        return getattr(self.base, name)

    def signal_frame(self, day, config, context):
        method = getattr(type(self.base), "signal_frame", None)
        if method is not None:
            return method(self.base, day, config, context)
        values = self.base.scores(day, config)
        return values if isinstance(values, (SignalFrame, SignalUnavailable)) else SignalFrame.from_legacy(
            values, day, "legacy_ranked_source", context)

    def close_signal_frame(self, day, config, context, original, book):
        return self.policy.filter_frame(original, day, book, self)
