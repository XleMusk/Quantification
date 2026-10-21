"""Independent entry-2ATR stops with explicit ordinary share-action lineage.

The original frozen policy and Ledger stay unchanged. This version accepts a
share-distribution lot only when saved actions, holder entitlements, committed
Engine action events and the exact settled FIFO inventory reconcile. It does
not reset the continuous-holding first-fill price or entry N.
"""
from copy import deepcopy

from ..contracts import SignalUnavailable, TradingMode
from ..engine import ReplayRiskExitIntent
from . import trend_atr_stop_policy as original
from .action_aware_holding_anchor import action_aware_continuous_anchor


POLICY_ID = "donchian_entry_actual_fill_fixed_two_atr_action_aware_stop_v2"
STATE_KEY = "donchian_entry_atr_action_aware_stop_v2"
SPEC = {**deepcopy(original.SPEC), "policy_id": POLICY_ID,
    "share_actions": "saved_ordinary_action_entitlement_committed_event_and_exact_settled_FIFO_reconciliation",
    "action_unknown": "defer_without_invented_fill_or_inventory_repair",
    "v1_state_migration": False, "old_results_modified": False}


class AtrEntryStopActionsPolicy(original.AtrEntryStopPolicy):
    def __init__(self, provider):
        super().__init__(provider)
        self.binding = {**self.binding, "policy_id": POLICY_ID,
            "share_action_lineage": "explicit_saved_ordinary_distribution_only"}
        self.policy_id = POLICY_ID + ":" + provider.price_basis_id

    def prepare(self, day, book, feed):
        if book.context.mode != TradingMode.REPLAY or day not in feed.calendar:
            raise ValueError("Action-aware entry-ATR stops require one exact replay session")
        binding = {**self.binding, "context": book.context.to_dict()}
        if original.STATE_KEY in book.state:
            raise ValueError("An existing v1 entry-ATR account cannot silently adopt the action-aware policy")
        meta = book.state.setdefault(STATE_KEY, {"binding": deepcopy(binding),
            "anchors": {}, "blocked_regimes": {}, "last_assessment_day": None, "assessments": {}})
        if meta["binding"] != binding:
            raise ValueError("Action-aware entry-ATR basis or fixed rule changed during recovery")
        cache_key = (id(book.state), book.context, day)
        if (meta["last_assessment_day"] == day and self._prepared_state is book.state
                and self._prepared == cache_key):
            return meta
        held = {s for s in book.state["positions"] if book.quantity(s) > 0}
        for symbol in set(meta["anchors"]) - held:
            del meta["anchors"][symbol]
        assessments = {}
        for symbol in sorted(held):
            actions = getattr(feed, "actions", None)
            if actions is None:
                origin, reason = None, "saved_action_inventory_unavailable"
            else:
                origin, reason = action_aware_continuous_anchor(
                    book.state, symbol, feed.calendar, day, actions, context=book.context)
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
                    for regime in (anchor["entry_regime"], original._regime(quote)):
                        if regime is not None and regime not in veto:
                            veto.append(regime)
                status = "stop_triggered" if anchor["stop_triggered"] else "not_triggered"
            else:
                status = "unknown"
            assessments[symbol] = {"status": status, "reason": reason,
                "entry": deepcopy(anchor), "holding_reconciliation": deepcopy(origin),
                "close": float(quote["close"]) if quote else None}
            book.emit(day, "donchian_entry_atr_stop_assessed", symbol=symbol,
                policy_id=self.policy_id, **deepcopy(assessments[symbol]))
        meta.update(last_assessment_day=day, assessments=assessments)
        self._prepared = cache_key
        self._prepared_state = book.state
        return meta

    def __call__(self, day, book, feed):
        meta = self.prepare(day, book, feed)
        stopped = tuple(sorted(s for s, anchor in meta["anchors"].items()
                               if anchor["stop_triggered"] and book.quantity(s) > 0))
        if not stopped:
            return None
        index = feed.calendar.index(day)
        if index + 1 >= len(feed.calendar):
            return SignalUnavailable("entry_ATR_stop_exact_next_session_unavailable")
        return ReplayRiskExitIntent(book.context, day, feed.calendar[index+1], stopped,
            "research:" + POLICY_ID, "fixed_entry_two_ATR_stop_with_reconciled_share_action_lineage")


class AtrActionsStopFeed(original.AtrStopFeed):
    def __init__(self, base, policy):
        if not isinstance(policy, AtrEntryStopActionsPolicy):
            raise TypeError("An explicit independent action-aware ATR policy is required")
        super().__init__(base, policy)
