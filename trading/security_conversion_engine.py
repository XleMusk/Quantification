"""Dated nontrade conversion preparation for the existing daily Engine.

This projects an already checked target across an explicit security exchange;
it does not create a new signal, a trade fill, a split ratio or a price series.
"""
from copy import deepcopy
from dataclasses import replace
import math

from .contracts import TradingMode
from .ledger import Ledger
from .security_conversion import APPLIED_KEY, CATALOG_KEY, SecurityConversionReceipt

PENDING_SCHEMA = "security_conversion_adjusted_published_target_v1"


def project_weights(weights, receipts, config):
    if weights is None:
        return None
    result = dict(weights)
    for receipt in receipts:
        if receipt.source_symbol in result:
            weight = result.pop(receipt.source_symbol)
            result[receipt.destination_symbol] = result.get(receipt.destination_symbol, 0) + weight
    if (any(isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w)
            or w < 0 or w > config.single_limit + 1e-12 for w in result.values())
            or sum(result.values()) > config.gross_limit + 1e-12):
        raise ValueError("Converted pending weights collide or exceed the unchanged portfolio caps")
    return result


def checked_pending(state, feed, day, config, context, check_original):
    adjusted = state["pending_published_close_intent"]
    if adjusted.get("schema") != PENDING_SCHEMA:
        raise ValueError("Unsupported adjusted security-conversion publication")
    catalog = {row["event_id"]: row for row in state.get(CATALOG_KEY, [])}
    ids = adjusted.get("conversion_event_ids")
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise ValueError("Converted pending target requires its exact nontrade receipts")
    receipts = []
    for event_id in ids:
        payload = state.get(APPLIED_KEY, {}).get(event_id)
        if payload is None or payload != catalog.get(event_id):
            raise ValueError("Converted pending target lost its original applied source receipt")
        receipt = SecurityConversionReceipt.from_dict(payload)
        if receipt.context != context or receipt.effective_day != day:
            raise ValueError("Converted target cannot roll the security exchange to another session or account")
        receipt.validate_source()
        receipts.append(receipt)
    shadow = deepcopy(state)
    shadow["pending_published_close_intent"] = deepcopy(adjusted.get("original_receipt"))
    shadow["pending_targets"] = deepcopy(adjusted.get("original_targets"))
    original = check_original(shadow, feed, day, config, context)
    expected = {"schema": PENDING_SCHEMA, "decision_day": state["day"],
        "execution_not_before": day, "original_receipt": original,
        "original_targets": shadow["pending_targets"],
        "adjusted_targets": project_weights(shadow["pending_targets"], receipts, config),
        "conversion_event_ids": ids, "new_signal_created": False}
    if adjusted != expected or state["pending_targets"] != expected["adjusted_targets"]:
        raise ValueError("Converted pending target or its original publication projection changed")
    return adjusted


def _validate_destination(book, feed, day, receipt):
    source, destination = receipt.source_symbol, receipt.destination_symbol
    instruments = feed.instruments
    out = instruments.get(source, {}).get("outDate")
    if not out or out > day or destination not in instruments:
        raise ValueError("Security conversion requires the known source outDate and destination instrument")
    if book.rule(source, day).is_fund or book.rule(destination, day).is_fund:
        raise ValueError("The first security-conversion method supports ordinary stock holdings only")
    row = feed.rows.get(day, {}).get(destination)
    fields = ("open", "high", "low", "close", "preclose", "volume", "amount")
    if (not isinstance(row, dict)
            or any(isinstance(row.get(k), bool) or not isinstance(row.get(k), (int, float))
                or not math.isfinite(row[k]) for k in fields)
            or any(row[k] <= 0 for k in fields[:5]) or row["volume"] < 0 or row["amount"] < 0
            or row["low"] > min(row["open"], row["close"])
            or row["high"] < max(row["open"], row["close"]) or row["low"] > row["high"]
            or type(row.get("tradestatus")) is not int or row["tradestatus"] not in {0, 1}
            or type(row.get("isST")) is not int or row["isST"] not in {0, 1}
            or str(row.get("adjustflag")) != "3"):
        raise ValueError("Converted holding requires exact known destination raw prices and status")
    # No resolution of simultaneous allocations is inferred from a conversion.
    symbols = {source, destination}
    for action in getattr(feed, "actions", []):
        if action.get("symbol") in symbols and day in {action.get(k) for k in
                ("dividRegistDate", "dividOperateDate", "dividPayDate", "bonus_listing_date")}:
            raise ValueError("Concurrent distribution and security conversion need an explicit joint settlement")
    for reference in getattr(feed, "manifest", {}).get("historical_price_adjustments", []):
        if reference.get("symbol") in symbols and reference.get("date") == day:
            raise ValueError("Concurrent price-adjustment action and security conversion are unresolved")


def apply_due(state, feed, day, config, context, due, *, original_pending=None):
    """Apply credits and project pending execution, retaining every old receipt."""
    if not due:
        return
    from .engine import ReplayRiskExitIntent
    book = Ledger(state, config, context, instrument_profiles=getattr(feed, "instrument_profiles", None))
    before = {key: deepcopy(state.get(key)) for key in
        ("targets", "pending_targets", "pending_risk_exits", "pending_published_close_intent", "full_exit_lifecycle")}
    for receipt in due:
        _validate_destination(book, feed, day, receipt)
        book.apply_security_conversion(receipt)
    state["targets"] = project_weights(before["targets"], due, config)
    state["pending_targets"] = project_weights(before["pending_targets"], due, config)
    mapping = {r.source_symbol: r.destination_symbol for r in due}
    if before["pending_risk_exits"] is not None:
        risk = ReplayRiskExitIntent.from_dict(before["pending_risk_exits"])
        if risk.context != context or risk.execution_on != day:
            raise ValueError("Converted risk exit must keep its original account and exact execution day")
        state["pending_risk_exits"] = replace(risk,
            symbols=tuple(sorted({mapping.get(symbol, symbol) for symbol in risk.symbols}))).to_dict()
    lifecycle = state.get("full_exit_lifecycle")
    if lifecycle is not None:
        # The preopen form carries an immutable envelope whose projection is a
        # separate method. Ordinary close/residual requests retain their dates.
        if "preopen_envelope" in (lifecycle.get("latest_target") or {}):
            raise ValueError("Preopen full-exit conversion projection is not supported by this method")
        if lifecycle.get("latest_target") is not None:
            lifecycle["latest_target"]["weights"] = project_weights(lifecycle["latest_target"]["weights"], due, config)
        remaining = lifecycle["remaining_full_exits"]
        for receipt in due:
            if receipt.source_symbol in remaining:
                if receipt.destination_symbol in remaining:
                    raise ValueError("Converted residual exit collides with a different existing request")
                request = remaining.pop(receipt.source_symbol)
                remaining[receipt.destination_symbol] = {**request,
                    "remaining_quantity": book.quantity(receipt.destination_symbol),
                    "security_conversion_event_id": receipt.event_id, "original_symbol": receipt.source_symbol}
    if original_pending is not None:
        state["pending_published_close_intent"] = {"schema": PENDING_SCHEMA,
            "decision_day": state["day"], "execution_not_before": day,
            "original_receipt": deepcopy(original_pending), "original_targets": before["pending_targets"],
            "adjusted_targets": deepcopy(state["pending_targets"]),
            "conversion_event_ids": [r.event_id for r in due], "new_signal_created": False}
    book.emit(day, "security_conversion_execution_projected", conversion_event_ids=[r.event_id for r in due],
        original_pending=before, projected_pending={key: deepcopy(state.get(key)) for key in before},
        new_signal_created=False, trades_generated=False)
