import json
from types import SimpleNamespace

import pandas as pd

from trading.research import native_lowvol_replay as replay
from trading.research import saved_lowvol_signals
from trading.research.stockdb_action_accounting import StockDBActionAccounting
from trading.research.stockdb_rolling_low_volatility import portfolio
from trading.contracts import RunContext, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.target_exit_lifecycle import FULL_EXIT_LIFECYCLE


def setup_inputs(tmp_path, monkeypatch, fractional=False):
    days = ["2026-01-30", "2026-02-02", "2026-02-03"]
    raw_days = ["2026-01-29"] + days
    symbols = ["000001.SZSE", "600000.SSE"]
    config = portfolio()
    saved = SimpleNamespace(config=config, symbols=symbols,
        signals={days[0]: [{"symbol": s, "score": -.1 - i * .01} for i, s in enumerate(symbols)]},
        decisions={days[0]: "monthly_ranking", days[1]: "not_month_end", days[2]: "not_month_end"},
        preceding_session=raw_days[0], provenance={"fixture_saved_scores_only": True})
    actions = [{"symbol": s, "dividRegistDate": days[1], "dividOperateDate": days[2],
                "dividPayDate": days[2], "dividStockMarketDate": days[2], "dividCashPsBeforeTax": "0.1",
                "dividStocksPs": "0", "dividReserveToStockPs": "0.013" if fractional and i == 1 else "0.2"}
               for i, s in enumerate(symbols)]
    raw = pd.DataFrame([{"date": day, "symbol": s,
        **{field: (9.77 if fractional and s == symbols[1] else 8.25) if day == days[2] else 10.
           for field in ["open", "high", "low", "close", "preclose"]},
        "volume": 100000000., "amount": 1000000000., "tradestatus": 1, "isST": 0, "adjustflag": "3"}
        for day in raw_days for s in symbols])
    universe = pd.DataFrame({"code": ["sz.000001", "sh.600000"], "symbol": symbols})
    accounting = StockDBActionAccounting(actions, [], [],
        {(code, 2026): True for code in universe.code}, universe, {"rename": None}, symbols,
        raw_days, days, {"price_source": "independent_baostock_raw"})
    calendar, master = tmp_path / "calendar.json", tmp_path / "master.csv"
    calendar.write_text("{}", encoding="utf-8")
    pd.DataFrame([{"code": code, "type": "1", "ipoDate": "2000-01-01", "outDate": ""}
                  for code in universe.code]).to_csv(master, index=False)
    monkeypatch.setattr(replay, "load_calendar", lambda path: {"year": 2026, "closed_ranges": [], "open_sessions": []})
    monkeypatch.setattr(saved_lowvol_signals, "load_saved_lowvol_signals", lambda *a: saved)
    monkeypatch.setattr(replay, "load_native_execution_quotes", lambda *a: (raw, {"fixture_whole_native_rows": True}))
    monkeypatch.setattr(replay, "load_action_accounting", lambda *a, **k: accounting)
    return days, calendar, master, actions


def test_saved_monthly_signals_share_engine_cash_stock_actions_and_no_change(tmp_path, monkeypatch):
    days, calendar, master, _ = setup_inputs(tmp_path, monkeypatch)
    out = tmp_path / "new"
    r = replay.run("saved", ["native"], "events", "references", calendar, master, days[0], days[-1], out)
    state = json.loads((out / "account.json").read_text(encoding="utf-8"))
    assert r["status"] == "completed" and r["completed_sessions"] == 3
    assert r["orders"] > 0 and r["fills"] > 0 and r["metrics"] is not None
    assert state["day"] == days[-1] and state["pending_targets"] is None
    for symbol in ("000001.SZSE", "600000.SSE"):
        entitlement = state["entitlements"][symbol + ":" + days[-1]]
        assert entitlement > 0
        assert sum(lot["qty"] for lot in state["positions"][symbol]) == int(entitlement * 1.2)
    assert len([e for e in state["events"] if e["kind"] == "dividend_paid"]) == 2
    assert r["old_account_mutated"] is False and r["execution_admitted"] is False


def test_failed_fractional_allocation_does_not_commit_an_earlier_action_in_same_trial(tmp_path, monkeypatch):
    days, calendar, master, _ = setup_inputs(tmp_path, monkeypatch, fractional=True)
    out = tmp_path / "failed"
    r = replay.run("saved", ["native"], "events", "references", calendar, master, days[0], days[-1], out)
    state = json.loads((out / "account.json").read_text(encoding="utf-8"))
    trial = json.loads((out / "failed_trial.json").read_text(encoding="utf-8"))
    assert r["status"] == "incomplete" and r["completed_sessions"] == 2 and r["metrics"] is None
    assert r["error"]["day"] == days[-1] and r["failed_trial_committed"] is False
    assert state["day"] == days[1]
    assert not [e for e in state["events"] if e["kind"] == "corporate_action"]
    assert [e for e in trial["events"] if e["kind"] == "corporate_action"]


def test_unknown_prior_volume_blocks_opening_trade_but_not_no_change_valuation(tmp_path, monkeypatch):
    days, calendar, master, _ = setup_inputs(tmp_path, monkeypatch)
    original_raw, proof = replay.load_native_execution_quotes(None)
    missing = original_raw.copy(deep=True)
    missing.loc[missing.date.eq(days[0]), "volume"] = float("nan")
    monkeypatch.setattr(replay, "load_native_execution_quotes", lambda *a: (missing, proof))
    out = tmp_path / "unknown_capacity"
    result = replay.run("saved", ["native"], "events", "references", calendar, master, days[0], days[-1], out)
    assert result["status"] == "incomplete" and result["completed_sessions"] == 1
    assert "Unknown exact prior execution capacity" in result["error"]["message"]
    assert result["orders"] == result["fills"] == 0 and result["metrics"] is None

    saved = saved_lowvol_signals.load_saved_lowvol_signals(None)
    accounting = replay.load_action_accounting(None)
    instruments = {s: {"ipoDate": "2000-01-01", "outDate": ""} for s in saved.symbols}
    feed = replay.NativeLowVolFeed(saved, original_raw, ["2026-01-29"] + days + ["2026-02-04"],
                                 instruments, accounting, days[0], days[-1])
    state, context = new_state(saved.config.initial_cash), RunContext(replay.METHOD, TradingMode.REPLAY)
    for index, day in enumerate(days[:2]):
        advance(state, feed, day, saved.config, index, context=context, full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
    fills_before = len(state["fills"])
    feed.volumes.loc[days[1], :] = float("nan")
    advance(state, feed, days[2], saved.config, 2, context=context, full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
    assert state["day"] == days[2] and len(state["fills"]) == fills_before
    assert [e for e in state["events"] if e["kind"] == "dividend_paid"]
