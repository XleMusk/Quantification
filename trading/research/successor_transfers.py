"""Source-backed successor-security transfers for research-only replay."""

from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


# This is deliberately a small, explicit registry.  A successor transfer is
# not inferred from an instrument outDate or a carried quote.
SUCCESSOR_TRANSFERS = (
    {
        "source_symbol": "601989.SSE",
        "target_symbol": "600150.SSE",
        "effective_date": "2025-09-05",
        "record_date": "2025-09-04",
        "listing_date": "2025-09-16",
        "ratio": "0.1339",
        "source_kind": "official_exchange_merger_announcement",
        "source_ref": "https://static.cninfo.com.cn/finalpage/2025-08-30/1224625027.PDF",
        "source_note": "China Shipbuilding exchange merger implementation; 1:0.1339",
    },
)


def _event_for(symbol, day):
    for event in SUCCESSOR_TRANSFERS:
        if event["source_symbol"] == symbol and day >= event["effective_date"]:
            return event
    return None


def _round_quantity(value, policy):
    rounding = ROUND_FLOOR if policy == "floor" else ROUND_CEILING
    return int(value.to_integral_value(rounding=rounding))


def apply_replay_successor_transfers(state, day, symbols, policy):
    """Apply explicit successor transfers before the common Engine/Ledger checks.

    The policy is intentionally required and replay-only.  Public notices prove
    the mapping and ratio, but do not prove this account's fractional remainder.
    """
    if policy is None:
        return []
    if policy not in {"floor", "ceil"}:
        raise ValueError("Successor transfer replay policy must be floor or ceil")
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("Successor transfer day must be an ISO date")

    symbols = set(symbols)
    receipts = state.setdefault("successor_transfer_receipts", {})
    applied = []
    for event in SUCCESSOR_TRANSFERS:
        if event["source_symbol"] not in symbols or event["target_symbol"] not in symbols:
            continue
        if day < event["effective_date"]:
            continue
        identifier = f"successor:{event['source_symbol']}:{event['effective_date']}"
        source_lots = state["positions"].get(event["source_symbol"], [])
        source_quantity = sum(int(lot["qty"]) for lot in source_lots)
        if not source_quantity:
            continue
        if identifier in receipts:
            if receipts[identifier]["policy"] != policy:
                raise ValueError("Successor transfer rounding policy changed within a replay")
            continue
        if any(order.get("symbol") == event["source_symbol"] and
               order.get("status") in {"submitted", "partial", "cancel_pending"}
               for order in state["orders"].values()):
            raise ValueError("Successor transfer has an active source-security order")

        ratio = Decimal(event["ratio"])
        nominal_quantity = Decimal(source_quantity) * ratio
        allocated_quantity = _round_quantity(nominal_quantity, policy)
        if allocated_quantity <= 0:
            raise ValueError("Successor transfer produced no integer successor shares")

        target_lots = state["positions"].setdefault(event["target_symbol"], [])
        target_lots.append({"qty": allocated_quantity, "available": event["listing_date"]})
        state["positions"][event["source_symbol"]] = []
        state.get("prices", {}).pop(event["source_symbol"], None)
        for field in ("pending_targets", "targets"):
            values = state.get(field)
            if isinstance(values, dict) and event["source_symbol"] in values:
                value = values.pop(event["source_symbol"])
                values[event["target_symbol"]] = values.get(event["target_symbol"], 0) + value

        receipt = {
            **deepcopy(event),
            "id": identifier,
            "day_applied": day,
            "source_quantity": source_quantity,
            "nominal_successor_quantity": str(nominal_quantity),
            "allocated_quantity": allocated_quantity,
            "policy": policy,
            "execution_admitted": False,
            "account_specific_receipt": False,
        }
        receipts[identifier] = receipt
        state["sequence"] = int(state.get("sequence", 0)) + 1
        state.setdefault("events", []).append({
            "sequence": state["sequence"],
            "day": day,
            "kind": "successor_security_transfer",
            **receipt,
        })
        applied.append(receipt)
    return applied
