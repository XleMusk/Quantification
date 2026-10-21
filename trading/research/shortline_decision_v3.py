"""Future research lifecycle: signal NO_CHANGE and independent expiry reductions.

V2 producers, historical results and engine defaults remain unchanged.  This
opt-in path only adds next-session liquidation of five-session-old holdings
from the sole Ledger's actual fills.  It never creates a replacement signal,
weights, account, broker connection or persistent store.
"""

import math

from ..contracts import SignalUnavailable, TradingMode
from ..engine import ReplayRiskExitIntent, advance
from ..ledger import new_state
from .shortline_decision_v2 import (
    MAX_HOLDING_SESSIONS, ShortlineDecisionFeedV2, fixed_exit_policy, holding_origin,
)
from .shortline_panel_replay import _config


POLICY_ID = "shortline_v3_independent_five_session_expiry_v1"
SIGNAL_SOURCE = "research:shortline_independent_expiry_v3"


def _known_raw(row):
    if not isinstance(row, dict):
        return False
    try:
        values = {field: float(row[field]) for field in
                  ("open", "high", "low", "close", "preclose", "volume", "amount")}
    except (KeyError, TypeError, ValueError):
        return False
    if (not all(math.isfinite(value) for value in values.values())
            or any(values[field] <= 0 for field in ("open", "high", "low", "close", "preclose"))
            or any(values[field] < 0 for field in ("volume", "amount"))
            or values["low"] > min(values["open"], values["close"])
            or values["high"] < max(values["open"], values["close"])
            or values["low"] > values["high"]):
        return False
    # Unknown status is not converted into tradable/non-ST or raw semantics.
    return (type(row.get("tradestatus")) is int and row["tradestatus"] in {0, 1}
            and type(row.get("isST")) is int and row["isST"] in {0, 1}
            and str(row.get("adjustflag")) == "3")


class FiveSessionExpiryPolicy:
    """Reduce expired shares even when new-entry observations are unavailable."""

    policy_id = POLICY_ID

    def validate_session(self, state, feed, day, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Independent shortline expiry is replay-only")
        if day not in feed.calendar:
            raise ValueError("Independent expiry requires a declared exchange session")
        rows = feed.rows.get(day, {})
        # Current owned shares must remain priceable before the Engine marks or
        # executes anything.  No prior close or suspension zero-fill is used.
        for symbol, lots in state["positions"].items():
            if sum(lot["qty"] for lot in lots) and not _known_raw(rows.get(symbol)):
                raise ValueError(f"Unpriceable risk-exit holding: {symbol} {day}; known raw/status required")

    def __call__(self, day, book, feed):
        if book.context.mode != TradingMode.REPLAY:
            raise ValueError("Independent shortline expiry is replay-only")
        indices = {session: index for index, session in enumerate(feed.calendar)}
        held = {symbol for symbol in book.state["positions"] if book.quantity(symbol)}
        expired = []
        for symbol in sorted(held):
            origin = holding_origin(book.state, symbol)
            if origin not in indices or indices[origin] > indices[day]:
                raise ValueError("Expiry needs the current continuous holding's actual Ledger buy-fill session")
            if indices[day] - indices[origin] + 1 >= MAX_HOLDING_SESSIONS:
                expired.append(symbol)
        if not expired:
            return None
        next_index = indices[day] + 1
        if next_index >= len(feed.calendar):
            return SignalUnavailable("shortline_v3_expiry_actual_next_session_unavailable")
        next_day = feed.calendar[next_index]
        # Do not repurpose missing candidate observations into a liquidation
        # signal. Only the held book and already pending real targets matter.
        required = held | set(book.state.get("pending_targets") or {})
        for session in (day, next_day):
            rows = feed.rows.get(session, {})
            if any(not _known_raw(rows.get(symbol)) for symbol in required):
                return SignalUnavailable("shortline_v3_expiry_relevant_raw_or_status_unavailable")
        return ReplayRiskExitIntent(book.context, day, next_day, tuple(expired), SIGNAL_SOURCE,
                                    "shortline_v3_fixed_five_session_holding_exit")


def replay_decisions(base, decision_batches, *, market_defense=False, config=None):
    """Synthetic/independently frozen future research only; never fit or admit."""
    config = config or _config(100000, 5)
    feed = ShortlineDecisionFeedV2(base, decision_batches, market_defense=market_defense)
    state = new_state(config.initial_cash)
    target_policy = fixed_exit_policy(feed, state, config)
    risk_policy = FiveSessionExpiryPolicy()
    daily = [advance(state, feed, day, config, index, target_policy=target_policy,
                     replay_risk_exit_policy=risk_policy) for index, day in enumerate(feed.calendar)]
    return {"schema": "shortline_independent_expiry_replay_v3", "state": state, "daily": daily,
            "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
            "model_refit": False, "future_labels_used_for_score": False, "execution_admitted": False,
            "persistent_ledger_mutated": False, "broker_interacted": False,
            "exit_policy": {"policy_id": POLICY_ID, "max_holding_open_sessions": MAX_HOLDING_SESSIONS,
                            "age_origin": "first_actual_buy_fill_of_current_continuous_holding",
                            "expiry_timing": "decision_close_session_5_execute_actual_session_6_open",
                            "missing_batch": "signal_no_change_plus_independent_reduce_only_expiry",
                            "nonexpired_positions": "retain_exact_shares_when_no_signal_target",
                            "blocked_exit": "retain_residual_and_retry_later_known_session",
                            "verified_full_market_breadth_defense": market_defense}}
