"""One synthetic lifecycle regression; no source, account or performance run."""

import pandas as pd

from trading.ledger import Ledger
from trading.research.shortline_decision_v2 import BATCH_SCHEMA
from trading.research.shortline_decision_v3 import POLICY_ID, replay_decisions
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config


def test_unheld_candidate_raw_gap_cannot_disable_fifth_day_exit_or_blocked_retry():
    # These are explicit synthetic sessions, not an inferred real-data calendar.
    days = [
        "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
        "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10",
        "2026-09-11",
    ]
    held, unheld = "000017.SZSE", "000016.SZSE"
    records = []
    for index, day in enumerate(days):
        # Day 1 can form a valid signal with exact Day 2 raw. Thereafter only
        # the nonheld cohort member disappears; the owned raw stays known.
        for symbol in (held, unheld):
            if symbol == unheld and index >= 2:
                continue
            row = {
                "date": day, "symbol": symbol,
                "open": 10.0, "high": 10.05, "low": 9.95,
                "close": 10.0, "preclose_for_limits": 10.0,
                "volume": 10_000_000.0, "amount": 100_000_000.0,
                "is_st": False,
            }
            if symbol == held and index == 6:
                row.update(open=9.0, high=9.0, low=9.0, close=9.0)
            elif symbol == held and index == 7:
                row.update(open=8.1, high=8.1, low=8.1, close=8.1,
                           preclose_for_limits=9.0)
            elif symbol == held and index == 8:
                row.update(open=8.2, high=8.3, low=8.1, close=8.2,
                           preclose_for_limits=8.1)
            records.append(row)

    base = ShortlinePanelFeed(pd.DataFrame(records), {}, [held, unheld])
    base.instruments = {
        symbol: {"ipoDate": "2000-01-01", "outDate": ""}
        for symbol in (held, unheld)
    }
    batches = {
        day: {"schema": BATCH_SCHEMA, "day": day, "status": "complete",
              "source": "synthetic_candidate_gap_only",
              "candidates": [{"symbol": held, "score": 1.0}] if index == 0 else []}
        for index, day in enumerate(days)
    }
    config = _config(100000, 5)  # Existing costs, capacity and holding settings.
    result = replay_decisions(base, batches, config=config)
    state, daily = result["state"], result["daily"]
    events = state["events"]
    buys = [fill for fill in state["fills"].values() if fill["side"] == "buy"]
    sells = [fill for fill in state["fills"].values() if fill["side"] == "sell"]
    bought_qty = sum(fill["qty"] for fill in buys)

    assert bought_qty > 0 and {fill["day"] for fill in buys} == {days[1]}
    assert {fill["available_day"] for fill in buys} == {days[2]}
    assert all(unheld not in base.rows[day] and held in base.rows[day] for day in days[2:])
    # The raw gap itself makes the candidate feed unavailable, rather than
    # the fixture merely omitting decision batches or substituting cash.
    assert any(event["kind"] == "no_decision"
               and event["reason"] == "shortline_v2_current_raw_panel_incomplete"
               for event in daily[5]["events"])
    assert not any(event["kind"] == "targets_generated"
                   for day in daily[1:] for event in day["events"])

    schedules = [event for event in events if event["kind"] == "risk_exit_scheduled"]
    assert [(event["day"], event["execution_not_before"]) for event in schedules] == [
        (days[5], days[6]), (days[6], days[7]), (days[7], days[8]),
    ]
    assert all(event["symbols"] == [held] for event in schedules)
    blocks = [event for event in events if event["kind"] == "order_blocked"
              and event.get("symbol") == held and event.get("side") == "sell"]
    assert [(event["day"], event["reason"], event["qty"]) for event in blocks] == [
        (days[6], "limit_down_queue_unknown", bought_qty),
        (days[7], "limit_down_queue_unknown", bought_qty),
    ]
    reductions = [event for event in events if event["kind"] == "risk_exit_execution"]
    assert [(event["day"], event["remaining_quantities"]) for event in reductions] == [
        (days[6], {held: bought_qty}),
        (days[7], {held: bought_qty}),
        (days[8], {held: 0}),
    ]
    assert sells and {fill["day"] for fill in sells} == {days[8]}
    assert sum(fill["qty"] for fill in sells) == bought_qty
    sell_orders = [order for order in state["orders"].values() if order["side"] == "sell"]
    assert len(sell_orders) == 1 and sell_orders[0]["filled"] == bought_qty
    assert all(order["symbol"] == held for order in state["orders"].values())
    assert Ledger(state, config).quantity(held) == 0
    assert state.get("pending_risk_exits") is None and state["pending_targets"] is None
    assert state["targets"] == {held: 0.16}  # No fake full-cash candidate intent.
    assert state["replay_risk_exit_policy"]["policy_id"] == POLICY_ID
    assert result["execution_admitted"] is False and result["strategy_validated"] is False
