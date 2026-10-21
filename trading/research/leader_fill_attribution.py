"""Read-only provenance of original settled FIFO share consumption.

The quantity rule mirrors Ledger.fill and surviving_buy_anchor. This is not a
new account or PnL book: prices, cash, costs and fees are never calculated.
"""
from datetime import date
import re


SCHEMA = "leader_original_fill_fifo_quantity_attribution_v1"
FLAGS = {"execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "engine_advanced": False, "ledger_mutated": False, "account_created": False,
    "prices_calculated": False, "costs_allocated": False, "fees_allocated": False,
    "PnL_calculated": False, "orders_generated": False, "provider_contacted": False}
NON_FILL_POSITION_EVENTS = {"corporate_action", "broker_share_credit", "broker_restriction_release"}


def _day(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def trace_fifo_fill_attribution(account, symbol, calendar):
    """Attribute all original fills through account.day for one whole symbol.

calendar must include the complete original session span, including settlement
dates and the saved final account day, rather than just the decision window.
Any missing chain invalidates the whole symbol; no partly computed closed lot
is returned. Unfilled orders create no lots and cannot become filled examples.
"""
    result = {"schema": SCHEMA, "symbol": symbol, "as_of": account.get("day"),
        "status": "unknown_symbol_fill_attribution", "unknown_reason": None,
        "unknown_details": [], "buy_fills": None, "sell_fills": None,
        "final_original_position_qty": None, "attributed_remaining_qty": None,
        "execution_events_consumed": None,
        "processing_order": "original_execution_event_sequence_settled_FIFO",
        "settlement_basis": "original_fill_available_day_not_reassigned", **FLAGS}

    def unknown(reason, **details):
        return {**result, "unknown_reason": reason,
            "unknown_details": [{"reason": reason, **details}]}

    if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}\.(SSE|SZSE)", symbol):
        return unknown("canonical_security_identity_required")
    if (not isinstance(calendar, (tuple, list)) or not calendar
            or not all(_day(d) for d in calendar) or list(calendar) != sorted(set(calendar))
            or account.get("day") not in calendar):
        return unknown("full_original_calendar_or_account_asof_unknown")
    if account.get("broker_binding"):
        return unknown("broker_bound_account_not_supported")
    if (not isinstance(account.get("orders"), dict) or not isinstance(account.get("fills"), dict)
            or not isinstance(account.get("events"), list) or not isinstance(account.get("positions"), dict)):
        return unknown("original_ledger_collections_missing")
    orders, all_fills = account["orders"], account["fills"]
    if (any(not isinstance(row, dict) for row in orders.values())
            or any(not isinstance(row, dict) for row in all_fills.values())
            or any(not isinstance(row, dict) for row in account["events"])):
        return unknown("original_ledger_record_shape_invalid")
    for identifier, fill in all_fills.items():
        if not fill.get("symbol") and fill.get("order_id") not in orders:
            return unknown("original_fill_security_identity_unknown", fill_id=identifier)
    positions = account["positions"].get(symbol, [])
    if (not isinstance(positions, list) or any(not isinstance(lot, dict)
            or type(lot.get("qty")) is not int or lot["qty"] < 0 for lot in positions)):
        return unknown("original_position_quantity_invalid")
    final_quantity = sum(lot["qty"] for lot in positions)
    result["final_original_position_qty"] = final_quantity
    own_orders = {key: order for key, order in orders.items() if order.get("symbol") == symbol}
    own_fills = {key: fill for key, fill in all_fills.items()
        if fill.get("symbol") == symbol or orders.get(fill.get("order_id"), {}).get("symbol") == symbol}
    for order_id, order in own_orders.items():
        if (order.get("id") != order_id or order.get("side") not in {"buy", "sell"}
                or order.get("day") not in calendar or order["day"] > account["day"]
                or type(order.get("qty")) is not int or order["qty"] <= 0
                or type(order.get("filled")) is not int or not 0 <= order["filled"] <= order["qty"]):
            return unknown("original_order_identity_or_quantity_invalid", order_id=order_id)
    matches = {key: [] for key in own_fills}
    for event in account["events"]:
        event_symbol = event.get("symbol")
        if (event.get("kind") in {"partial", "filled"} and not event_symbol
                and event.get("order_id") not in orders and event.get("execution_id") not in all_fills):
            return unknown("execution_event_security_identity_unknown", sequence=event.get("sequence"))
        related = event_symbol == symbol or event.get("order_id") in own_orders or event.get("execution_id") in own_fills
        if not related:
            continue
        if event.get("kind") in NON_FILL_POSITION_EVENTS or str(event.get("kind", "")).startswith("rights_"):
            return unknown("non_fill_position_or_action_history_not_attributable", sequence=event.get("sequence"), kind=event.get("kind"))
        if event.get("kind") not in {"partial", "filled"}:
            continue
        identifier = event.get("execution_id")
        if identifier not in own_fills:
            return unknown("execution_event_missing_from_original_fills", execution_id=identifier, sequence=event.get("sequence"))
        matches[identifier].append(event)
    ordered = []
    for identifier, fill in own_fills.items():
        events = matches[identifier]
        if len(events) != 1:
            return unknown("missing_or_duplicate_execution_event", fill_id=identifier, matching_events=len(events))
        event, order = events[0], orders.get(fill.get("order_id"))
        if (order is None or order.get("symbol") != symbol or fill.get("symbol") != symbol
                or fill.get("side") != order.get("side") or fill.get("day") != order.get("day")
                or fill.get("day") not in calendar or fill["day"] > account["day"]
                or type(fill.get("qty")) is not int or fill["qty"] <= 0):
            return unknown("original_fill_order_or_quantity_invalid", fill_id=identifier)
        if (fill.get("available_day") not in calendar or fill["available_day"] <= fill["day"]):
            return unknown("original_T1_available_day_unknown_or_invalid", fill_id=identifier)
        if (type(event.get("sequence")) is not int or event["sequence"] <= 0
                or any(event.get(key) != fill.get(key) for key in ("order_id", "symbol", "day", "qty"))):
            return unknown("execution_event_identity_quantity_or_sequence_disagrees", fill_id=identifier)
        ordered.append((event["sequence"], identifier, fill))
    if len({seq for seq, _, _ in ordered}) != len(ordered):
        return unknown("duplicate_execution_sequence")
    for order_id, order in own_orders.items():
        recorded_qty = sum(fill["qty"] for fill in own_fills.values() if fill["order_id"] == order_id)
        if recorded_qty != order["filled"]:
            return unknown("order_filled_quantity_does_not_equal_original_fill_chain", order_id=order_id,
                order_filled=order["filled"], original_fill_qty=recorded_qty)
    ordered.sort(key=lambda row: row[0])
    if [row[2]["day"] for row in ordered] != sorted(row[2]["day"] for row in ordered):
        return unknown("execution_sequence_moves_backwards_across_session_dates")
    buys, sells, lots = {}, {}, []
    for sequence, identifier, fill in ordered:
        if fill["side"] == "buy":
            row = {"order_id": fill["order_id"], "day": fill["day"], "available_day": fill["available_day"],
                "sequence": sequence, "original_qty": fill["qty"], "sold_qty": 0,
                "remaining_qty": fill["qty"], "sell_allocations": []}
            buys[identifier] = row
            lots.append((identifier, row))
            continue
        remaining, allocations = fill["qty"], []
        for buy_id, buy in lots:
            if buy["available_day"] <= fill["day"]:
                take = min(remaining, buy["remaining_qty"])
                if take:
                    buy["remaining_qty"] -= take
                    buy["sold_qty"] += take
                    buy["sell_allocations"].append({"sell_fill_id": identifier,
                        "sell_order_id": fill["order_id"], "sequence": sequence, "qty": take, "day": fill["day"]})
                    allocations.append({"buy_fill_id": buy_id, "qty": take})
                    remaining -= take
        if remaining:
            return unknown("sell_exceeds_original_settled_FIFO_buy_quantity", sell_fill_id=identifier,
                unmatched_qty=remaining)
        sells[identifier] = {"order_id": fill["order_id"], "day": fill["day"], "sequence": sequence,
            "original_qty": fill["qty"], "allocations": allocations}
    surviving = sum(row["remaining_qty"] for row in buys.values())
    if surviving != final_quantity:
        return unknown("final_original_position_quantity_mismatch", attributed_remaining_qty=surviving,
            original_position_qty=final_quantity)
    return {**result, "status": "attributed_original_fifo_quantities", "unknown_reason": None,
        "unknown_details": [], "buy_fills": buys, "sell_fills": sells,
        "attributed_remaining_qty": surviving, "execution_events_consumed": len(ordered)}
