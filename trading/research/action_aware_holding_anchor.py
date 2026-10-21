"""Read a continuous actual-buy origin through committed ordinary share credits.

No Ledger mutation, allocation callback, announcement request or quantity-delta
inference occurs here. Typed terms, actual record-close entitlements, committed
events and fills must explain every surviving lot and its availability date.
This reconciliation does not establish issuer coverage or historical PIT.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from ..contracts import RunContext, TradingMode
from ..engine import ordinary_share_terms


METHOD_ID = "ordinary_action_aware_actual_continuous_buy_anchor_v1"


class _Unknown(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise _Unknown(reason)


def _day(value):
    try:
        parsed = date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise _Unknown("action_anchor_exact_date_unknown")
    _require(parsed == value, "action_anchor_exact_date_unknown")
    return parsed


def _count(value):
    _require(type(value) is int and value >= 0, "action_anchor_quantity_unknown")
    return value


def _group(lots):
    result = {}
    for lot in lots:
        quantity, available = _count(lot["qty"]), _day(lot["available"])
        if quantity:
            result[available] = result.get(available, 0) + quantity
    return result


def _allocation(state, action, event, quantity, context):
    try:
        terms = ordinary_share_terms(action)  # Pure normalization, never allocation(book,...).
        ratio = Decimal(terms["dividStocksPs"]) + Decimal(terms["dividReserveToStockPs"])
        nominal = quantity * ratio
    except (ValueError, KeyError, InvalidOperation):
        raise _Unknown("ordinary_action_explicit_terms_unknown")
    _require(action.get("action_kind") != "restricted_restructuring",
             "restricted_action_origin_not_supported")
    record, ex = (_day(terms[k]) for k in ("registration_date", "date"))
    listing = _day(terms["listing_date"]) if ratio else None
    _require(record < ex, "ordinary_action_record_ex_dates_unknown")
    if ratio:
        _require(listing >= ex, "ordinary_action_listing_date_unknown")
    if nominal == nominal.to_integral_value():
        allocation = int(nominal)
        _require(event.get("allocated_shares", allocation) == allocation,
                 "ordinary_action_event_integer_allocation_conflict")
        kind = "exact_integer_typed_terms_and_committed_event"
    else:
        identifier = action["symbol"] + ":" + ex
        receipt = state.get("corporate_action_reconciliations", {}).get(identifier)
        _require(isinstance(receipt, dict), "ordinary_fractional_allocation_receipt_unknown")
        allocation = receipt.get("allocated_shares")
        lower, upper = (int(nominal.to_integral_value(rounding=mode)) for mode in (ROUND_FLOOR, ROUND_CEILING))
        _require(receipt.get("kind") == "ordinary_share_allocation"
            and receipt.get("account_id") == context.account_id and receipt.get("mode") == context.mode.value
            and receipt.get("registered_quantity") == quantity and type(receipt.get("registered_quantity")) is int
            and receipt.get("action_terms") == terms and type(allocation) is int and allocation in {lower, upper}
            and receipt.get("source_kind") in {"broker_account_record", "explicit_replay_assumption"}
            and isinstance(receipt.get("source_ref"), str) and bool(receipt["source_ref"].strip()),
            "ordinary_fractional_allocation_receipt_conflict")
        if receipt["source_kind"] == "explicit_replay_assumption":
            policy = receipt.get("allocation_policy")
            _require(policy in {"ordinary_fraction_floor_v1", "ordinary_fraction_ceil_v1"}
                and allocation == (lower if policy == "ordinary_fraction_floor_v1" else upper),
                "ordinary_fractional_allocation_policy_conflict")
        _require(event.get("allocated_shares") == allocation
            and event.get("allocation_source_kind") == receipt["source_kind"]
            and event.get("allocation_source_ref") == receipt["source_ref"]
            and Decimal(str(event.get("nominal_bonus_shares"))) == nominal,
            "ordinary_fractional_allocation_event_conflict")
        kind = receipt["source_kind"]
    return allocation, {"action_id": action["symbol"]+":"+ex, "record_day": record, "ex_day": ex,
        "available_day": listing if allocation else None, "registered_quantity": quantity,
        "allocated_shares": allocation, "stock_ratio": terms["dividStocksPs"],
        "reserve_ratio": terms["dividReserveToStockPs"], "allocation_source_kind": kind,
        "event_sequence": event["sequence"], "pit_verified": False}


def action_aware_continuous_anchor(state, symbol, calendar, day, actions, *, context):
    """Return ``(origin, None)`` or ``(None, explicit_unknown_reason)``.

    Origin includes the v1 ``fill_id/fill_day/order_id/order_day/decision_day``
    keys. ``flat`` means the fully reconciled account has no remaining shares.
    A gift arriving after a real flat interval has no implicit fresh-buy rule.
    Unknown source terms or a lot/settlement residual never changes the state.
    """
    try:
        _require(isinstance(context, RunContext) and context.mode == TradingMode.REPLAY
            and not state.get("broker_binding"), "ordinary_action_anchor_requires_unbound_REPLAY")
        _require(day in calendar and calendar == sorted(set(calendar)), "action_anchor_exact_calendar_unknown")
        actual = state.get("positions", {}).get(symbol, [])
        actual_groups = _group(actual)
        fills = {key: fill for key,fill in state["fills"].items() if fill.get("symbol") == symbol}
        _require(bool(fills) or not actual_groups, "actual_buy_origin_missing")
        first_fill = min((f["day"] for f in fills.values()), default=day)
        by_key, record_days = {}, {}
        for original in actions:
            if original.get("symbol") != symbol:
                continue
            action = deepcopy(original)
            ex, record = _day(action.get("dividOperateDate")), _day(action.get("dividRegistDate"))
            if record > day:
                continue
            if ex < first_fill and symbol+":"+ex not in state.get("entitlements", {}):
                continue  # Pre-context action, not a quantity change in this holding.
            _require(ex in calendar and record in calendar and record < ex,
                     "ordinary_action_exact_calendar_dates_unknown")
            identifier = symbol + ":" + ex
            _require(identifier not in by_key, "ordinary_action_duplicate_identity_unknown")
            by_key[identifier] = action
            record_days.setdefault(record, []).append(identifier)
        events = [e for e in state.get("events", []) if e.get("symbol") == symbol
            and (e.get("execution_id") in fills or e.get("kind") == "corporate_action")]
        sequences = [e.get("sequence") for e in events]
        _require(all(type(s) is int and s > 0 for s in sequences)
            and sequences == sorted(set(sequences)), "actual_event_sequence_unknown")
        by_day, seen_fills, seen_actions = {}, set(), set()
        for event in events:
            event_day = _day(event.get("day"))
            _require(event_day in calendar and event_day <= day, "actual_event_date_outside_anchor_frontier")
            by_day.setdefault(event_day, []).append(event)
            if event.get("execution_id") in fills:
                identifier, fill = event["execution_id"], fills[event["execution_id"]]
                _require(identifier not in seen_fills and fill.get("day") == event_day
                    and all(event.get(k) == fill.get(k) for k in ("order_id", "qty", "price")),
                    "actual_fill_event_conflict")
                seen_fills.add(identifier)
            else:
                identifier = symbol+":"+event_day
                _require(identifier in by_key and identifier not in seen_actions,
                         "committed_action_missing_or_duplicate_typed_terms")
                seen_actions.add(identifier)
        _require(seen_fills == set(fills), "actual_fill_event_missing")
        lots, current_origin, registered, action_lots = [], None, {}, []
        for session in calendar:
            if session > day:
                break
            for event in by_day.get(session, []):
                fill = fills.get(event.get("execution_id"))
                if fill is not None:
                    quantity = _count(fill.get("qty"))
                    _require(quantity > 0 and fill.get("side") in {"buy", "sell"}, "actual_fill_quantity_or_side_unknown")
                    if fill["side"] == "buy":
                        available = _day(fill.get("available_day"))
                        _require(available in calendar and available > session, "actual_buy_settlement_unknown")
                        order = state["orders"].get(fill.get("order_id"))
                        order_day = order.get("day") if order else None
                        _require(order and order.get("symbol") == symbol and order.get("side") == "buy"
                            and order_day in calendar and order_day <= session and calendar.index(order_day) > 0,
                            "actual_buy_order_source_session_unknown")
                        if not sum(l["qty"] for l in lots):
                            current_origin = {"fill_id": event["execution_id"], "fill_day": session,
                                "order_id": fill["order_id"], "order_day": order_day,
                                "decision_day": calendar[calendar.index(order_day)-1]}
                        lots.append({"qty": quantity, "available": available, "origin": deepcopy(current_origin)})
                    else:
                        remaining = quantity
                        for lot in lots:
                            if lot["available"] <= session:
                                take = min(remaining, lot["qty"]); lot["qty"] -= take; remaining -= take
                        _require(remaining == 0, "actual_sell_exceeds_settled_action_aware_FIFO")
                        if not sum(l["qty"] for l in lots):
                            current_origin = None
                else:
                    identifier = symbol+":"+session
                    action = by_key[identifier]
                    quantity = state.get("entitlements", {}).get(identifier)
                    _require(type(quantity) is int and quantity >= 0 and identifier in registered
                        and quantity == registered[identifier]["quantity"] and event.get("entitlement") == quantity,
                        "actual_recorded_entitlement_unknown_or_conflict")
                    allocation, proof = _allocation(state, action, event, quantity, context)
                    if allocation:
                        origin = registered[identifier]["origin"]
                        _require(origin is not None and current_origin == origin,
                            "bonus_after_flat_or_mixed_holding_origin_unknown")
                        lots.append({"qty": allocation, "available": proof["available_day"], "origin": deepcopy(origin)})
                    action_lots.append(proof)
            for identifier in record_days.get(session, []):
                quantity = sum(l["qty"] for l in lots)
                saved = state.get("entitlements", {}).get(identifier)
                if session < first_fill and saved is None:
                    continue  # No actual buy existed at that pre-account record close.
                _require(type(saved) is int and saved >= 0 and saved == quantity,
                         "actual_record_close_quantity_unknown_or_conflict")
                registered[identifier] = {"quantity": quantity, "origin": deepcopy(current_origin)}
        for identifier, action in by_key.items():
            saved = state.get("entitlements", {}).get(identifier)
            if action["dividOperateDate"] <= day and type(saved) is int and saved > 0:
                _require(identifier in seen_actions, "positive_entitlement_without_committed_action_event")
        _require(_group(lots) == actual_groups, "actual_action_aware_lot_or_settlement_residual")
        if not actual_groups:
            return None, "flat"
        _require(current_origin is not None, "continuous_actual_buy_origin_unknown")
        return {**current_origin, "method_id": METHOD_ID, "remaining_qty": sum(actual_groups.values()),
            "action_lots": action_lots, "settlement_reconciled": True,
            "action_scope_complete": False, "pit_verified": False}, None
    except (_Unknown, KeyError, TypeError, InvalidOperation) as error:
        return None, str(error) if isinstance(error, _Unknown) else "action_anchor_saved_evidence_incomplete"
