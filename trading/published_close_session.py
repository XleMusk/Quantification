"""Explicit research method: settle known orders independently of a new close plan.

Daily settlement still uses engine.advance and its ordinary opening/close path.
Only attaching a publication is separate: it can queue the next opening but
cannot execute, mark, accrue an action or repeat any session's fills.
"""
from copy import deepcopy

from .contracts import IntentKind, TradingMode
from .ledger import Ledger

METHOD_ID = "published_close_settlement_then_attach_v1"
BINDING_SCHEMA = "published_close_session_binding_v1"


def marker(day, receipt):
    return {"day": day, "status": "publication_attached" if receipt is not None else "awaiting_publication",
            "publication": deepcopy(receipt)}


def _binding(context, config):
    return {"schema": BINDING_SCHEMA, "method_id": METHOD_ID,
            "context": context.to_dict(), "config": config.to_dict()}


def validate_binding(state, method, day, feed, config, context, intent):
    saved = state.get("published_close_session_binding")
    if method is None:
        if saved is not None or "last_published_close_session" in state:
            raise ValueError("Split published-close accounts cannot return to the legacy daily method")
        return None
    if method != METHOD_ID or context.mode != TradingMode.REPLAY:
        raise ValueError("An exact explicit replay split published-close method is required")
    binding = _binding(context, config)
    calendar = list(feed.calendar)
    if day not in calendar or calendar != sorted(set(calendar)):
        raise ValueError("Split published-close settlement needs an exact ordered calendar")
    if saved is None:
        if (state["day"] is not None or intent is None or state["orders"] or state["fills"]
                or state.get("pending_targets") is not None or state.get("pending_risk_exits") is not None
                or state.get("pending_published_close_intent") is not None
                or state.get("published_close_target_policy") is not None
                or state.get("replay_risk_exit_policy") is not None):
            raise ValueError("Split settlement must be explicitly seeded with a publication in a new account")
    else:
        if saved != binding or state["day"] not in calendar:
            raise ValueError("Split published-close context/configuration/method changed")
        prior = state.get("last_published_close_session")
        if not isinstance(prior, dict) or prior.get("day") != state["day"]:
            raise ValueError("Split published-close session marker changed")
        receipt = prior.get("publication")
        if prior != marker(state["day"], receipt):
            raise ValueError("Split published-close publication status changed")
        if receipt is not None:
            from .engine import _published_close_receipt
            from .contracts import PortfolioIntent
            if receipt != _published_close_receipt(PortfolioIntent.from_dict(receipt.get("intent")),
                    state["day"], feed, config, context):
                raise ValueError("Split published-close original receipt changed")
        old_index = calendar.index(state["day"])
        if old_index + 1 >= len(calendar) or calendar[old_index + 1] != day:
            raise ValueError("Split settlement cannot skip an exact execution session")
    return binding


def attach_close(state, feed, day, intent, config, session_number, *, context,
                 published_close_target_policy, replay_risk_exit_policy):
    """Attach only this already-settled day's actual close intent, atomically.

The caller seals this phase separately from settlement. Repeating the identical
receipt is a read-only no-op. Historical attachment after advancing is rejected.
Already scheduled expiry cannot be cancelled by later structural information.
"""
    from . import engine
    if (state.get("published_close_session_binding") != _binding(context, config)
            or context.mode != TradingMode.REPLAY or state["day"] != day
            or config.rebalance_sessions != 1 or state.get("broker_binding")
            or state.get("intraday_session_method") is not None):
        raise ValueError("Attach requires the same settled day of an explicit split research account")
    original = state.get("last_published_close_session")
    if not isinstance(original, dict) or original != marker(day, original.get("publication")):
        raise ValueError("The original settled session marker is required")
    receipt = engine._published_close_receipt(intent, day, feed, config, context)
    target_binding = engine._published_close_target_binding(state, published_close_target_policy, context, intent)
    engine._replay_risk_exit_binding(state, replay_risk_exit_policy, context)
    if original["publication"] is not None:
        if original["publication"] != receipt:
            raise ValueError("An attached actual publication cannot be replaced")
        return {"date": day, "phase": "attach_close_publication", "already_attached": True,
                "new_orders": 0, "new_fills": 0, "events": [], "published_close_intent_frontier": receipt}
    if state.get("pending_targets") is not None or state.get("pending_published_close_intent") is not None:
        raise ValueError("A missing publication cannot already own a queued weight target")
    trial = deepcopy(state)
    book = Ledger(trial, config, context, instrument_profiles=getattr(feed, "instrument_profiles", None))
    before_events = len(trial["events"])
    reduction = None
    if intent.kind == IntentKind.FULL_TARGET:
        reduced = engine.reduce_replay_target(intent, published_close_target_policy)
        reduction = engine._published_close_reduction_receipt(intent, reduced, day, feed, config, context,
            target_binding["policy_id"])
        trial["targets"] = reduced.to_legacy_pending()
        trial["pending_targets"] = reduced.to_legacy_pending()
        trial["pending_published_close_intent"] = reduction
        trial["candidates"] = []
        book.emit(day, "targets_generated", signal_date=day, execution_not_before=feed.next_day(day),
            targets=trial["targets"], signal_source=reduced.source, decision_reason=reduced.reason or None,
            published_close_intent_frontier=receipt, published_close_reduction=reduction,
            attached_after_settlement=True)
    else:
        book.emit(day, "no_decision", signal_date=day, reason=intent.reason,
                  published_close_intent_frontier=receipt, attached_after_settlement=True)
    prior_risk = deepcopy(trial.get("pending_risk_exits"))
    # The callback can capture the caller's state (the existing fixed-expiry
    # policy does). Its callable sees unchanged shares/cash in this phase.
    engine._schedule_replay_risk_exit(book, feed, day, replay_risk_exit_policy)
    if prior_risk is not None:
        before = engine.ReplayRiskExitIntent.from_dict(prior_risk)
        if before.decided_on != day or before.execution_on != feed.next_day(day) or before.context != context:
            raise ValueError("Existing split risk decision must retain its original exact next session")
        current = trial.get("pending_risk_exits")
        if current is None or current == prior_risk:
            trial["pending_risk_exits"] = prior_risk
        else:
            from dataclasses import replace
            after = engine.ReplayRiskExitIntent.from_dict(current)
            trial["pending_risk_exits"] = replace(after,
                symbols=tuple(sorted(set(before.symbols) | set(after.symbols)))).to_dict()
    trial["last_published_close_session"] = marker(day, receipt)
    # No opening/valuation/action path is called. These quantities are exact,
    # not reconstructed from target weights or public quotes.
    for name in ("orders", "fills", "cash", "positions", "prices", "receivables"):
        if trial[name] != state[name]:
            raise ValueError("Attaching a publication cannot change fills, cash, positions or valuation")
    result = {"date": day, "phase": "attach_close_publication", "already_attached": False,
        "new_orders": 0, "new_fills": 0, "events": trial["events"][before_events:],
        "published_close_intent_frontier": receipt}
    if reduction is not None:
        result["published_close_reduction"] = reduction
    state.clear()
    state.update(trial)
    return result
