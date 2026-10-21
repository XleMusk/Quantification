"""Quantity-only source attribution; no Engine, cash, cost or price model."""
from copy import deepcopy

from trading.research.leader_fill_attribution import trace_fifo_fill_attribution


CALENDAR = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
SYMBOL = "000001.SZSE"


def fixture():
    rows = [("b1", "ob1", "buy", CALENDAR[0], CALENDAR[1], 60),
            ("b2", "ob1", "buy", CALENDAR[0], CALENDAR[1], 40),
            ("s1", "os1", "sell", CALENDAR[1], CALENDAR[2], 80),
            ("b3", "ob2", "buy", CALENDAR[1], CALENDAR[2], 50),
            ("s2", "os2", "sell", CALENDAR[2], CALENDAR[3], 30)]
    account = {"day": CALENDAR[3], "orders": {}, "fills": {}, "events": [],
        "positions": {SYMBOL: [{"qty": 40, "available": CALENDAR[2]}]}}
    for sequence, (identifier, order_id, side, day, available, quantity) in enumerate(rows, 1):
        order = account["orders"].setdefault(order_id, {"id": order_id, "symbol": SYMBOL,
            "side": side, "day": day, "qty": 0, "filled": 0})
        order["qty"] += quantity; order["filled"] += quantity
        account["fills"][identifier] = {"order_id": order_id, "symbol": SYMBOL, "side": side,
            "day": day, "available_day": available, "qty": quantity}
        account["events"].append({"sequence": sequence, "kind": "filled" if identifier != "b1" else "partial",
            "execution_id": identifier, "order_id": order_id, "symbol": SYMBOL, "day": day, "qty": quantity})
    return account


def trace(account):
    return trace_fifo_fill_attribution(account, SYMBOL, CALENDAR)


def test_partial_buy_fills_same_order_and_later_buy_do_not_merge_into_first_lot():
    account = fixture(); before = deepcopy(account)
    # Deliberately reverse JSON map order: execution-event sequence is primary.
    account["fills"] = dict(reversed(list(account["fills"].items())))
    before = deepcopy(account)
    result = trace(account)
    assert account == before
    assert result["status"] == "attributed_original_fifo_quantities"
    assert result["buy_fills"]["b1"]["original_qty"] == result["buy_fills"]["b1"]["sold_qty"] == 60
    assert result["buy_fills"]["b1"]["remaining_qty"] == 0
    assert result["buy_fills"]["b2"]["sold_qty"] == 40 and result["buy_fills"]["b2"]["remaining_qty"] == 0
    assert result["buy_fills"]["b3"]["sold_qty"] == 10 and result["buy_fills"]["b3"]["remaining_qty"] == 40
    assert result["sell_fills"]["s1"]["allocations"] == [{"buy_fill_id": "b1", "qty": 60}, {"buy_fill_id": "b2", "qty": 20}]
    assert result["sell_fills"]["s2"]["allocations"] == [{"buy_fill_id": "b2", "qty": 20}, {"buy_fill_id": "b3", "qty": 10}]
    assert result["final_original_position_qty"] == result["attributed_remaining_qty"] == 40
    assert result["execution_events_consumed"] == 5
    assert result["prices_calculated"] is result["fees_allocated"] is result["PnL_calculated"] is False


def test_entire_symbol_unknown_on_missing_event_bad_settlement_oversell_or_final_mismatch():
    failures = []
    account = fixture(); account["events"].pop(0); failures.append((account, "missing_or_duplicate_execution_event"))
    account = fixture(); account["events"].append(deepcopy(account["events"][0])); failures.append((account, "missing_or_duplicate_execution_event"))
    account = fixture(); account["events"][1]["sequence"] = 1; failures.append((account, "duplicate_execution_sequence"))
    account = fixture(); account["events"][0]["qty"] = 1; failures.append((account, "execution_event_identity_quantity_or_sequence_disagrees"))
    account = fixture(); account["fills"]["b1"]["available_day"] = CALENDAR[0]; failures.append((account, "original_T1_available_day_unknown_or_invalid"))
    account = fixture(); account["fills"]["b1"]["available_day"] = CALENDAR[3]; failures.append((account, "sell_exceeds_original_settled_FIFO_buy_quantity"))
    account = fixture(); account["positions"][SYMBOL][0]["qty"] = 41; failures.append((account, "final_original_position_quantity_mismatch"))
    account = fixture(); account["orders"]["ob1"]["filled"] = 99; failures.append((account, "order_filled_quantity_does_not_equal_original_fill_chain"))
    for account, reason in failures:
        result = trace(account)
        assert result["status"] == "unknown_symbol_fill_attribution" and result["unknown_reason"] == reason
        assert result["buy_fills"] is result["sell_fills"] is result["attributed_remaining_qty"] is None


def test_unfilled_order_creates_no_lot_and_complete_closed_history_remains_known():
    account = {"day": CALENDAR[2], "orders": {"empty": {"id": "empty", "day": CALENDAR[0],
        "symbol": SYMBOL, "side": "buy", "qty": 100, "filled": 0}}, "fills": {}, "events": [], "positions": {}}
    result = trace(account)
    assert result["status"] == "attributed_original_fifo_quantities" and result["buy_fills"] == {}
    assert result["attributed_remaining_qty"] == 0
    account = fixture()
    account["fills"]["s3"] = {"order_id": "os3", "symbol": SYMBOL, "side": "sell", "day": CALENDAR[3],
        "available_day": CALENDAR[4], "qty": 40}
    account["orders"]["os3"] = {"id": "os3", "symbol": SYMBOL, "side": "sell", "day": CALENDAR[3], "qty": 40, "filled": 40}
    account["events"].append({"sequence": 6, "kind": "filled", "execution_id": "s3", "order_id": "os3",
        "symbol": SYMBOL, "day": CALENDAR[3], "qty": 40})
    account["positions"] = {}
    result = trace(account)
    assert result["status"] == "attributed_original_fifo_quantities"
    assert result["attributed_remaining_qty"] == 0 and len(result["buy_fills"]) == 3
    assert all(buy["remaining_qty"] == 0 for buy in result["buy_fills"].values())


def test_non_fill_quantity_actions_and_orphan_execution_are_not_silently_attributed():
    for extra, reason in [({"sequence": 6, "kind": "corporate_action", "symbol": SYMBOL, "day": CALENDAR[2]},
            "non_fill_position_or_action_history_not_attributable"),
        ({"sequence": 6, "kind": "filled", "symbol": SYMBOL, "execution_id": "missing", "day": CALENDAR[2]},
            "execution_event_missing_from_original_fills"),
        ({"sequence": 6, "kind": "filled", "execution_id": "missing", "day": CALENDAR[2]},
            "execution_event_security_identity_unknown")]:
        account = fixture(); account["events"].append(extra)
        result = trace(account)
        assert result["unknown_reason"] == reason and result["buy_fills"] is None
