"""Explicit complete/missing shortline decisions for shared research replay.

V1 candidate-panel behaviour is unchanged.  A complete dated batch must carry
an explicit candidate list; only its empty list means a cash target.  Missing
observations and unobserved following raw bars remain NO_CHANGE.  This module
does not fit models, persist an account, or interact with a broker/client.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import math

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import Ledger, new_state, tracked_holding_origin
from .shortline_panel_replay import FUTURE_LABEL_COLUMNS, ShortlinePanelFeed, _config


BATCH_SCHEMA = "shortline_decision_batch_v2"
BREADTH_SCOPE = "full_market_ordinary_sh_sz_equities"
SIGNAL_SOURCE = "research:shortline_complete_decision_v2"
MAX_HOLDING_SESSIONS = 5


def verified_breadth(batch):
    """Return a complete, sourced full-market breadth; unknown is not zero."""
    view = batch.get("market_breadth")
    if (not isinstance(view, dict) or view.get("scope") != BREADTH_SCOPE
            or view.get("complete") is not True or view.get("verified") is not True):
        return None
    if view.get("day") != batch["day"] or not isinstance(view.get("source"), str) or not view["source"]:
        raise ValueError("Full-market breadth needs the exact decision day and source")
    counts = [view.get(name) for name in ("advance_count", "decline_count", "flat_count", "expected_active_count")]
    if (any(type(value) is not int or value < 0 for value in counts)
            or not counts[3] or sum(counts[:3]) != counts[3]):
        raise ValueError("Full-market breadth requires complete aligned active-universe counts")
    return (counts[0] - counts[1]) / counts[3]


class ShortlineDecisionFeedV2:
    """Wrap the existing raw panel, with separately declared decision coverage."""

    def __init__(self, base: ShortlinePanelFeed, decision_batches: dict, *, market_defense=False):
        if not isinstance(base, ShortlinePanelFeed) or type(market_defense) is not bool:
            raise ValueError("V2 needs an existing shortline raw panel and explicit defense variant")
        self.base = base
        self.batches = deepcopy(decision_batches)
        self.market_defense = market_defense
        self.manifest = {**base.manifest, "scope": "shortline_candidate_panel_v2", "replay_only": True,
                         "candidate_panel_only": True, "execution_admitted": False}
        for day, batch in self.batches.items():
            if (not isinstance(batch, dict) or batch.get("schema") != BATCH_SCHEMA
                    or batch.get("day") != day or date.fromisoformat(day).isoformat() != day):
                raise ValueError("V2 decision batches require explicit schema and canonical matching dates")
            if batch.get("status") == "complete":
                if (not isinstance(batch.get("source"), str) or not batch["source"]
                        or not isinstance(batch.get("candidates"), list)):
                    raise ValueError("Complete batch requires a source and explicit candidate list")
                if any(not isinstance(row, dict) or FUTURE_LABEL_COLUMNS.intersection(row)
                       for row in batch["candidates"]):
                    raise ValueError("Decision candidates cannot contain future continuation labels")
                if any(row.get("symbol") not in base.symbols for row in batch["candidates"]):
                    raise ValueError("Decision candidate is outside the observed raw panel")

    def __getattr__(self, key):
        return getattr(self.base, key)

    def _raw_panel_complete(self, day):
        rows = self.base.rows.get(day, {})
        for symbol in self.base.symbols:
            row = rows.get(symbol)
            if row is None:
                return False
            for field in ("open", "high", "low", "close", "preclose", "volume", "amount"):
                value = float(row[field])
                if not math.isfinite(value) or (value <= 0 if field not in {"volume", "amount"} else value < 0):
                    return False
        return bool(self.base.symbols)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Shortline V2 decisions are replay-only")
        batch = self.batches.get(day)
        if batch is None:
            return SignalUnavailable("shortline_v2_decision_batch_missing")
        if batch.get("status") != "complete":
            return SignalUnavailable("shortline_v2_decision_batch_incomplete")
        if day not in self.calendar or not self._raw_panel_complete(day):
            return SignalUnavailable("shortline_v2_current_raw_panel_incomplete")
        position = self.calendar.index(day)
        if position + 1 >= len(self.calendar) or not self._raw_panel_complete(self.calendar[position + 1]):
            return SignalUnavailable("shortline_v2_following_raw_panel_unavailable")
        if self.market_defense and verified_breadth(batch) is None:
            return SignalUnavailable("shortline_v2_full_market_breadth_unavailable")
        # Explicit [] is a complete observation, so the shared selector returns
        # FULL_TARGET({}).  It must never be replaced with SignalUnavailable.
        return SignalFrame.from_legacy(batch["candidates"], day, SIGNAL_SOURCE, context)


def holding_origin(state, symbol):
    """Current continuous holding origin from the sole Ledger's actual fills."""
    if state.get("security_conversion_binding") is not None:
        return tracked_holding_origin(state, symbol)
    quantity, origin = 0, None
    fills = sorted(enumerate(state["fills"].values()), key=lambda item: (item[1]["day"], item[0]))
    for _, fill in fills:
        if fill["symbol"] != symbol:
            continue
        if fill["side"] == "buy":
            if quantity == 0:
                origin = fill["day"]
            quantity += fill["qty"]
        else:
            quantity -= fill["qty"]
            if quantity == 0:
                origin = None
            if quantity < 0:
                raise ValueError("Holding age cannot be derived from inconsistent Ledger fills")
    return origin


def fixed_exit_policy(feed: ShortlineDecisionFeedV2, state, config, *, max_holding=True):
    """Fixed five-session/verified-negative-breadth reduction; no extra book."""
    if type(max_holding) is not bool:
        raise ValueError("Fixed holding variant must be explicit")
    book = Ledger(state, config)
    day_index = {day: index for index, day in enumerate(feed.calendar)}
    def reduce(intent):
        day = intent.decided_at[:10]
        if feed.market_defense and verified_breadth(feed.batches[day]) < 0:
            return replace(intent, weights=(), reason="shortline_v2_negative_verified_full_market_breadth")
        expired = set()
        if max_holding:
            for symbol, _ in intent.weights:
                if not book.quantity(symbol):
                    continue
                origin = holding_origin(state, symbol)
                if origin not in day_index:
                    raise ValueError("Held position lacks an observed Ledger buy-fill session")
                if day_index[day] - day_index[origin] + 1 >= MAX_HOLDING_SESSIONS:
                    expired.add(symbol)
        if not expired:
            return intent
        return replace(intent, weights=tuple((symbol, weight) for symbol, weight in intent.weights if symbol not in expired),
                       reason="shortline_v2_fixed_five_session_holding_exit")
    return reduce


def replay_decisions(base, decision_batches, *, market_defense=False, max_holding=True, config=None):
    """In-memory research consumer; the caller must freeze any evaluation window."""
    config = config or _config(100000, 5)
    feed = ShortlineDecisionFeedV2(base, decision_batches, market_defense=market_defense)
    state = new_state(config.initial_cash)
    policy = fixed_exit_policy(feed, state, config, max_holding=max_holding)
    daily = [advance(state, feed, day, config, index, target_policy=policy)
             for index, day in enumerate(feed.calendar)]
    return {"schema": "shortline_complete_decision_replay_v2", "state": state, "daily": daily,
            "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
            "model_refit": False, "future_labels_used_for_score": False, "execution_admitted": False,
            "persistent_ledger_mutated": False, "broker_interacted": False,
            "exit_policy": {"complete_empty_batch": "full_target_cash", "missing_batch": "no_change",
                            "max_holding_open_sessions": MAX_HOLDING_SESSIONS if max_holding else None,
                            "verified_full_market_breadth_defense": market_defense}}
