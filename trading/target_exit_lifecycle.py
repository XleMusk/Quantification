"""Explicit fixed-strategy full-exit continuation; the Engine owns all trading.

A new full target replaces the previous target. NO_CHANGE retains any unfinished
full exit, without replaying an old buy or turning a partial reduction into an
instruction to liquidate. Existing accounts retain their original behavior.
"""
from copy import deepcopy
from datetime import datetime

from .contracts import IntentKind, TradingMode, PreopenPortfolioIntent, MARKET_TZ


FULL_EXIT_LIFECYCLE = "full_exit_residual_latest_full_target_v1"
PREOPEN_FULL_EXIT_LIFECYCLE = "preopen_full_exit_residual_latest_full_target_v1"
RISK_COMPAT_FULL_EXIT_LIFECYCLE = "replay_risk_compatible_full_exit_residual_v1"


def validate_binding(state, method, context):
    """Return a session-local binding without changing the account on failure."""
    saved = state.get("full_exit_lifecycle")
    if method is None:
        if saved is not None:
            raise ValueError("Full-exit lifecycle cannot be disabled on an existing account")
        return None
    if method not in {FULL_EXIT_LIFECYCLE, PREOPEN_FULL_EXIT_LIFECYCLE,
                      RISK_COMPAT_FULL_EXIT_LIFECYCLE} or context.mode != TradingMode.REPLAY:
        raise ValueError("Full-exit lifecycle requires its explicit replay-only method")
    if saved is None:
        if state["day"] is not None:
            raise ValueError("Full-exit lifecycle cannot be attached to an existing account")
        return {"schema_version": 1, "method_id": method, "target_version": 0,
                "latest_target": None, "remaining_full_exits": {}}
    if (not isinstance(saved, dict) or saved.get("schema_version") != 1
            or saved.get("method_id") != method
            or type(saved.get("target_version")) is not int or saved["target_version"] < 0
            or not isinstance(saved.get("remaining_full_exits"), dict)):
        raise ValueError("Invalid full-exit lifecycle checkpoint")
    latest = saved.get("latest_target")
    if (saved["target_version"] == 0 and latest is not None
            or saved["target_version"] > 0 and (not isinstance(latest, dict)
                or latest.get("version") != saved["target_version"]
                or not isinstance(latest.get("weights"), dict)
                or not isinstance(latest.get("source"), str)
                or not isinstance(latest.get("decided_on"), str)
                or latest["decided_on"] > state["day"])):
        raise ValueError("Invalid full-exit latest target version")
    for symbol, exit_request in saved["remaining_full_exits"].items():
        if (not isinstance(symbol, str) or not isinstance(exit_request, dict)
                or type(exit_request.get("remaining_quantity")) is not int
                or exit_request["remaining_quantity"] <= 0
                or not isinstance(exit_request.get("requested_on"), str)
                or exit_request["requested_on"] > state["day"]):
            raise ValueError("Invalid remaining full-exit request")
    if method == PREOPEN_FULL_EXIT_LIFECYCLE and latest is not None:
        envelope = PreopenPortfolioIntent.from_dict(latest.get("preopen_envelope"))
        published_day = datetime.fromisoformat(envelope.published_at.replace("Z", "+00:00")).astimezone(MARKET_TZ).date().isoformat()
        if (envelope.intent.kind != IntentKind.FULL_TARGET or envelope.intent.context != context
                or latest.get("weights") != envelope.intent.to_legacy_pending()
                or latest.get("source") != envelope.intent.source
                or latest.get("source_day") != envelope.source_day
                or latest.get("published_at") != envelope.published_at
                or latest.get("decided_at") != envelope.published_at
                or latest.get("decided_on") != published_day
                or latest.get("execution_not_before") != envelope.execution_day
                or envelope.execution_day > state["day"]):
            raise ValueError("Invalid restored preopen full-exit target provenance")
    if method == PREOPEN_FULL_EXIT_LIFECYCLE:
        last = state.get("last_preopen_intent")
        if (isinstance(last, dict) and last.get("envelope", {}).get("intent", {}).get("kind") == IntentKind.FULL_TARGET.value
                and (latest is None or last["envelope"] != latest["preopen_envelope"])):
            raise ValueError("Restored full-exit target differs from the last preopen full target")
    return deepcopy(saved)


def exit_symbols(lifecycle, book):
    return {symbol for symbol in lifecycle["remaining_full_exits"] if book.quantity(symbol) > 0}


def record_opening_attempts(lifecycle, book, day, diagnostics, *, risk_intent=None):
    """Capture actual zero-share targets after the Engine's opening attempt."""
    exits = lifecycle["remaining_full_exits"]
    for symbol in list(exits):
        if book.quantity(symbol) == 0:
            request = exits.pop(symbol)
            book.emit(day, "full_exit_completed", symbol=symbol, original_request=request)
    for symbol, diagnostic in diagnostics.items():
        if (diagnostic["engine_target_quantity"] == 0
                and diagnostic["held_before"] > 0 and book.quantity(symbol) > 0):
            if symbol not in exits:
                target = lifecycle["latest_target"]
                forced = diagnostic["mechanical_reason"] == "forced_risk_exit"
                independent = (lifecycle["method_id"] == RISK_COMPAT_FULL_EXIT_LIFECYCLE
                    and risk_intent is not None and symbol in risk_intent.symbols)
                exits[symbol] = {"target_version": target["version"] if target else None,
                    "requested_on": target["decided_on"] if target and not forced else day,
                    "source": target["source"] if target and not forced else "engine_forced_exit",
                    "reason": diagnostic["mechanical_reason"],
                    "remaining_quantity": book.quantity(symbol)}
                if forced:
                    exits[symbol]["target_version"] = None
                elif independent:
                    exits[symbol].update(target_version=None, requested_on=risk_intent.decided_on,
                        source=risk_intent.source, risk_reason=risk_intent.reason)
            exits[symbol]["remaining_quantity"] = book.quantity(symbol)
            book.emit(day, "full_exit_remaining", symbol=symbol,
                request=deepcopy(exits[symbol]), constraint_flags=diagnostic["constraint_flags"],
                old_buy_retried=False)


def _replace_target(lifecycle, book, day, intent, metadata=None):
    if intent.kind != IntentKind.FULL_TARGET:
        raise ValueError("Full-exit target replacement requires a full target")
    lifecycle["target_version"] += 1
    weights = intent.to_legacy_pending()
    latest = {"version": lifecycle["target_version"], "decided_on": day,
        "decided_at": intent.decided_at, "execution_not_before": intent.execution_not_before,
        "source": intent.source, "weights": weights}
    if metadata is not None:
        latest.update(metadata)
    lifecycle["latest_target"] = latest
    for symbol in list(lifecycle["remaining_full_exits"]):
        if weights.get(symbol, 0) > 0:
            original = lifecycle["remaining_full_exits"].pop(symbol)
            book.emit(day, "full_exit_replaced_by_new_target", symbol=symbol,
                original_request=original, replacement_version=latest["version"],
                replacement_source=intent.source, target_weight=weights[symbol])
    book.emit(day, "full_target_version", target=deepcopy(latest))


def replace_target(lifecycle, book, day, intent):
    """Original close-target behavior is unchanged."""
    _replace_target(lifecycle, book, day, intent)


def replace_preopen_target(lifecycle, book, day, envelope):
    """Activate the frozen target before this opening's residual reductions."""
    if (lifecycle["method_id"] != PREOPEN_FULL_EXIT_LIFECYCLE
            or not isinstance(envelope, PreopenPortfolioIntent) or envelope.execution_day != day
            or envelope.intent.kind != IntentKind.FULL_TARGET or envelope.intent.context != book.context
            or book.context.mode != TradingMode.REPLAY):
        raise ValueError("Preopen full-exit replacement requires its exact explicit opening method")
    published_day = datetime.fromisoformat(envelope.published_at.replace("Z", "+00:00")).astimezone(MARKET_TZ).date().isoformat()
    _replace_target(lifecycle, book, day, envelope.intent, {
        "decided_on": published_day, "decided_at": envelope.published_at,
        "execution_not_before": day, "source_day": envelope.source_day,
        "published_at": envelope.published_at, "preopen_envelope": envelope.to_dict()})


def refresh_remaining(lifecycle, book):
    # Closing corporate actions can change shares after opening executions.
    for symbol in list(lifecycle["remaining_full_exits"]):
        quantity = book.quantity(symbol)
        if quantity:
            lifecycle["remaining_full_exits"][symbol]["remaining_quantity"] = quantity
        else:
            lifecycle["remaining_full_exits"].pop(symbol)
