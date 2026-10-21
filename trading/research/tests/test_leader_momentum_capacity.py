"""Annual consumer wiring for per-order unknown capacity; synthetic sources only."""
import json
import math
from types import SimpleNamespace

import pandas as pd

from trading.contracts import RunContext, TradingMode
from trading.rules import cents, notional_cents, quote_value
from trading.research.experiment import replay
from trading.research.leader_historical_execution_policy import HistoricalExecutionPolicy
from trading.research.leader_momentum_year import AnnualFeed
from trading.research.order_capacity_replay import PriorVolumeReplayPolicy
from trading.research.recent_alpha158_panel_replay import ExactSourceRawFeed
from trading.research.shortline_panel_replay import _config
from trading.research.stockdb_action_accounting import StockDBActionAccounting
from trading.target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE


A, B, C = "600001.SSE", "600002.SSE", "600003.SSE"
D1, E1, D2, E2, F, TAIL = (
    "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09")
DAYS = [D1, E1, D2, E2, F, TAIL]
CONTEXT = RunContext("standalone_replay", TradingMode.REPLAY)


def test_annual_monthly_targets_unknown_ST_exit_capacity_keeps_other_orders_and_marks(tmp_path):
    records = []
    a_prices = {E2: (9.8, 9.6, 10.), F: (9.5, 9.5, 9.6), TAIL: (9.4, 9.4, 9.5)}
    source_days = ['2024-12-31'] + DAYS
    for day in source_days:
        for symbol in (A, B, C):
            opening, close, previous = a_prices.get(day, (10., 10., 10.)) if symbol == A else (10., 10., 10.)
            suspended = symbol == A and day == D2
            records.append({"date": day, "code": "sh." + symbol[:6],
                "open": str(opening), "high": str(max(opening, close)), "low": str(min(opening, close)),
                "close": str(close), "preclose": str(previous),
                "volume": "" if suspended else "1000000",
                "amount": "" if suspended else str(close * 1_000_000),
                "tradestatus": "0" if suspended else "1",
                "isST": "1" if symbol == A and day >= E2 else "0", "adjustflag": "3"})
    calendar = source_days + ["2025-01-10"]  # Prior raw context plus exact next settlement session.
    base = ExactSourceRawFeed(pd.DataFrame(records), calendar, [A, B, C])
    base.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""} for symbol in (A, B, C)}
    assert math.isnan(base.volumes.at[D2, A])
    universe = pd.DataFrame({"symbol": [A, B, C], "code": ["sh." + s[:6] for s in (A, B, C)]})
    accounting = StockDBActionAccounting([], [], [], {(code, 2025): True for code in universe.code},
        universe, {"rename": None}, [A, B, C], source_days, DAYS,
        {"price_source": "independent_baostock_raw", "returns_reportable": False})
    execution = HistoricalExecutionPolicy(base, accounting, DAYS,
        prior_volume_policy=PriorVolumeReplayPolicy(CONTEXT))
    qualification = SimpleNamespace(assess=lambda *args, **kwargs: {"engine_block_reason": None})
    batches = {
        D1: {"status": "complete", "reason": "monthly_ranking",
             "candidates": [{"symbol": A, "score": 2.}, {"symbol": B, "score": 1.}]},
        D2: {"status": "complete", "reason": "monthly_ranking",
             "candidates": [{"symbol": B, "score": 2.}, {"symbol": C, "score": 1.}]},
    }
    config = _config(100000, 5)
    feed = AnnualFeed(execution, batches, config, F, TAIL, qualification,
                      "uncapped", "known_close_minimum_entry")
    destination = tmp_path / "annual_per_order_capacity"
    replay(feed, config, DAYS, destination, full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    state = json.loads((destination / "account.json").read_text(encoding="utf-8"))
    receipt = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
    daily = pd.read_csv(destination / "daily.csv").set_index("date")
    orders, fills, events = list(state["orders"].values()), list(state["fills"].values()), state["events"]
    assert receipt["status"] == "completed" and receipt["completed_sessions"] == 6
    assert receipt["full_exit_lifecycle"] == RISK_COMPAT_FULL_EXIT_LIFECYCLE
    assert state["prior_volume_execution_policy"]["context"] == CONTEXT.to_dict()
    assert not feed.blocks and execution.accounting.checks and execution.checks
    assert all(execution.reference_ok(row) for row in execution.checks)

    generated = {event["signal_date"]: event for event in events if event["kind"] == "targets_generated"}
    assert generated[D1]["targets"] == {A: .16, B: .16}
    assert generated[D2]["targets"] == {B: .16, C: .16}
    assert {o["symbol"] for o in orders if o["side"] == "buy" and o["day"] == E1} == {A, B}
    assert {o["symbol"] for o in orders if o["side"] == "buy" and o["day"] == E2} == {C}
    blocked = [event for event in events if event["day"] == E2
               and event["kind"] == "order_blocked" and event.get("symbol") == A]
    assert len(blocked) == 1 and blocked[0]["reason"] == "prior_session_volume_unknown"
    assert blocked[0]["previous_day_required"] == D2 and blocked[0]["prior_volume"] is None
    assert blocked[0]["order_created"] is False and blocked[0]["capacity_known"] is False
    assert not any(o["symbol"] == A and o["day"] == E2 for o in orders)
    c_buys = [f for f in fills if f["symbol"] == C and f["side"] == "buy"]
    assert c_buys and {f["day"] for f in c_buys} == {E2}
    assert {f["available_day"] for f in c_buys} == {F}

    # The unchanged shared closing mark must value the still-held A at 9.6,
    # despite its blocked sale, rather than using E2's 9.8 opening or zero.
    attempts = {event["symbol"]: event for event in events
                if event["day"] == E2 and event["kind"] == "target_execution_diagnostic"}
    assert attempts[A]["held_after"] > 0 and attempts[A]["order_ids"] == []
    closing_value = sum(notional_cents(quote_value(base.rows[E2][symbol]["close"]),
        trace["held_after"]) for symbol, trace in attempts.items())
    assert cents(daily.loc[E2, "equity"]) == attempts[C]["cash_available_after_cents"] + closing_value
    assert base.rows[E2][A]["close"] == 9.6 and base.rows[E2][A]["open"] == 9.8
    remaining = [event for event in events if event["kind"] == "full_exit_remaining"
                 and event["day"] == E2 and event.get("symbol") == A]
    assert remaining and remaining[0]["request"]["reason"] == "forced_risk_exit"
    assert remaining[0]["request"]["target_version"] is None

    a_sells = [o for o in orders if o["symbol"] == A and o["side"] == "sell"]
    assert a_sells and {o["day"] for o in a_sells} == {F}
    assert all(o["status"] == "filled" for o in a_sells)
    assert any(event["kind"] == "full_exit_completed" and event["day"] == F
               and event.get("symbol") == A for event in events)
    assert not any(o["side"] == "buy" and o["day"] > E2 for o in orders)
    assert generated[F]["targets"] == {} and any(f["side"] == "sell" and f["day"] == TAIL for f in fills)
    assert not any(sum(lot["qty"] for lot in lots) for lots in state["positions"].values())
    assert not state["full_exit_lifecycle"]["remaining_full_exits"]
    assert state["pending_targets"] is None and state["cash"] > 0
    assert math.isnan(base.volumes.at[D2, A])  # Unknown source bytes were never coerced to zero.
