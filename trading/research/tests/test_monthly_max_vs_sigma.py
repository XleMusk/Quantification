"""Synthetic monthly formation and sole-Engine behavior; no market source runs."""
from copy import deepcopy
from datetime import date, timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.research import monthly_max_vs_sigma as method


DAY, NEXT, AFTER = "2024-01-31", "2024-02-01", "2024-02-02"
CONTEXT = RunContext("monthly-max-sigma-fixture", TradingMode.REPLAY)


def tiny_base(symbols=("600001.SSE", "600002.SSE")):
    calendar = pd.bdate_range("2023-12-01", "2024-02-05").strftime("%Y-%m-%d").tolist()
    calendar.remove("2024-01-01")
    row = dict(open=10., high=10., low=10., close=10., preclose=10.,
        volume=1_000_000., amount=30_000_000., isST=0, tradestatus=1)
    return SimpleNamespace(calendar=calendar, day_index={d: i for i, d in enumerate(calendar)},
        symbols=list(symbols), rows={d: {s: dict(row) for s in symbols} for d in calendar},
        instruments={s: dict(ipoDate="2000-01-01", outDate="") for s in symbols},
        unsupported_instruments={}, actions=[], close=Mock(),
        closes=pd.DataFrame(10., index=calendar, columns=symbols),
        volumes=pd.DataFrame(1_000_000., index=calendar, columns=symbols),
        amounts=pd.DataFrame(30_000_000., index=calendar, columns=symbols),
        manifest=dict(selection_date="2023-01-01", limitations=["synthetic fixture"]),
        next_day=lambda day: calendar[calendar.index(day) + 1])


def set_month_returns(base, symbol, returns):
    window = method.month_window(base.calendar, DAY)
    base.closes.loc[window, symbol] = 10 * np.r_[1., np.cumprod(1 + np.asarray(returns))]


def test_same_complete_month_max_and_sigma_are_distinct_and_keep_zero_or_negative_scores():
    base = tiny_base(("600001.SSE", "600002.SSE", "600003.SSE", "600004.SSE"))
    window = method.month_window(base.calendar, DAY)
    n = len(window) - 1
    assert window[0] == "2023-12-29" and window[-1] == DAY and n == 22
    # One rare positive jump has higher MAX but lower total dispersion.
    a = np.r_[.02, np.zeros(n - 1)]
    b = np.asarray([.015 if i % 2 == 0 else -.015 for i in range(n)])
    for symbol, returns in zip(base.symbols, (a, b, np.full(n, -.001), np.zeros(n))):
        set_month_returns(base, symbol, returns)
    active = pd.DataFrame(True, index=base.calendar, columns=base.symbols)
    original = base.closes.copy(deep=True)
    result = method.score_month(base.closes, active, base.calendar, DAY)
    rows = {row["symbol"]: row for row in result["rows"]}
    assert result["unknown"] == {}
    assert rows[base.symbols[0]]["max_daily_return"] == pytest.approx(a.max())
    assert rows[base.symbols[0]]["sigma"] == pytest.approx(a.std(ddof=1))
    assert rows[base.symbols[1]]["max_daily_return"] == pytest.approx(b.max())
    assert rows[base.symbols[1]]["sigma"] == pytest.approx(b.std(ddof=1))
    assert rows[base.symbols[0]]["max_daily_return"] > rows[base.symbols[1]]["max_daily_return"]
    assert rows[base.symbols[0]]["sigma"] < rows[base.symbols[1]]["sigma"]
    assert rows[base.symbols[2]]["max_daily_return"] == pytest.approx(-.001)
    assert rows[base.symbols[3]]["sigma"] == rows[base.symbols[3]]["max_daily_return"] == 0
    assert {row["month_observations"] for row in result["rows"]} == {22}
    pd.testing.assert_frame_equal(base.closes, original)
    base.closes.loc[base.closes.index > DAY] = 1e100
    assert method.score_month(base.closes, active, base.calendar, DAY) == result


def test_economic_screen_requires_every_formation_and_preserves_fixed_positive_net_and_yearly_rules():
    def case(net, yearly):
        return dict(full_period_metrics_available=True, net_return=net,
            same_account_year_metrics={year: dict(net_return=value)
                for year, value in zip(("2024", "2025", "2026"), yearly)})
    cases = [case(.08, (.05, .02, .01)), case(.03, (.01, .01, .01))]
    unknown = method._economic_assessment(cases, formation_complete=False)
    assert unknown["status"] == "unknown_incomplete_formation_source"
    assert unknown["future_observation_candidate"] is None
    accepted = method._economic_assessment(cases, formation_complete=True)
    assert accepted["future_observation_candidate"] is True and accepted["positive_year_count"] == 2
    negative = method._economic_assessment([case(-.01, (.05, .02, .01)), cases[1]], formation_complete=True)
    assert negative["future_observation_candidate"] is False
    assert negative["status"] == "reject_fixed_expression_without_retuning"


def test_confirmed_month_and_exact_missing_rows_are_never_replaced_by_rolling_or_filled_returns():
    base = tiny_base()
    active = pd.DataFrame(True, index=base.calendar, columns=base.symbols)
    for calendar, day in ((base.calendar, "2024-01-30"),
            ([d for d in base.calendar if d <= DAY], DAY),
            ([d for d in base.calendar if d >= "2024-01-02"], DAY)):
        with pytest.raises(ValueError, match="month"):
            method.month_window(calendar, day)
    with pytest.raises(ValueError, match="never compress"):
        method.score_month(base.closes.drop("2024-01-12"), active, base.calendar, DAY)
    base.closes.at["2024-01-12", base.symbols[0]] = np.nan
    active.at["2024-01-15", base.symbols[1]] = False
    result = method.score_month(base.closes, active, base.calendar, DAY)
    assert result["rows"] == []
    assert result["unknown"] == {base.symbols[0]: "incomplete_positive_adjusted_month_or_anchor",
        base.symbols[1]: "month_or_anchor_not_observed_active_nonST"}


def test_only_supported_ten_percent_mainboard_and_known_listing_period_are_qualified():
    own_config = method.config()
    info = dict(ipoDate="2000-01-01", outDate="")
    for symbol in ("600001.SSE", "601001.SSE", "603001.SSE", "605001.SSE",
            "000001.SZSE", "002001.SZSE", "003001.SZSE"):
        assert method.qualification_reason(symbol, info, DAY, own_config) is None
    for symbol in ("300001.SZSE", "301001.SZSE", "688001.SSE", "689009.SSE",
            "830001.BSE", "510300.SSE"):
        assert method.qualification_reason(symbol, info, DAY, own_config) == "outside_10percent_mainboard"
    assert method.qualification_reason("600001.SSE", {}, DAY, own_config) == "unknown_listing_dates"
    cutoff = date.fromisoformat(DAY)
    assert method.qualification_reason("600001.SSE", {**info,
        "ipoDate": (cutoff - timedelta(days=364)).isoformat()}, DAY, own_config) == "listing_age_below365"
    assert method.qualification_reason("600001.SSE", {**info,
        "ipoDate": (cutoff - timedelta(days=365)).isoformat()}, DAY, own_config) is None
    assert method.qualification_reason("600001.SSE", {**info, "outDate": DAY},
        DAY, own_config) == "reported_outside_listing_period"


def test_both_rankings_share_full_month_eligibility_and_adjusted_close_not_raw_pct_change():
    symbols = [f"600{i:03d}.SSE" for i in range(1, 10)] + ["300001.SZSE"]
    base = tiny_base(symbols)
    set_month_returns(base, symbols[1], np.full(22, -.001))
    base.rows["2024-01-12"][symbols[2]]["volume"] = 0
    base.rows["2024-01-12"][symbols[3]]["isST"] = 1
    base.closes.at["2024-01-12", symbols[4]] = np.nan
    base.amounts.loc[:, symbols[5]] = 19_999_999.
    base.instruments[symbols[6]]["ipoDate"] = "2023-12-01"
    base.rows["2024-01-12"][symbols[7]].pop("isST")
    # Raw execution fields may jump without changing the adjusted feature path.
    base.rows["2024-01-12"][symbols[0]].update(close=5., preclose=10., pctChg=-50.)
    batches, scored = method.formation(base, [DAY], method.config())
    batch = batches[DAY]
    expected = {symbols[0], symbols[1], symbols[8]}
    assert batch["status"] == "complete" and batch["common_pool"] == 3
    assert batch["execution_day"] == NEXT
    assert len(base.calendar) < 252  # No hidden 252-day or momentum qualification.
    assert {row["symbol"] for row in scored} == expected
    assert all({row["symbol"] for row in batch["targets"][arm]} == expected for arm in method.ARMS)
    by_symbol = {row["symbol"]: row for row in scored}
    assert by_symbol[symbols[0]]["max_daily_return"] == by_symbol[symbols[0]]["sigma"] == 0
    assert batch["targets"]["low_max"][0]["symbol"] == symbols[1]
    assert [row["symbol"] for row in batch["targets"]["low_sigma"] if row["sigma"] == 0] == [symbols[0], symbols[8]]


def test_whole_missing_activity_day_is_no_change_while_known_empty_pool_is_explicit_empty():
    base, own_config = tiny_base(), method.config()
    del base.rows["2024-01-12"]
    batches, scored = method.formation(base, [DAY], own_config)
    assert scored == [] and batches[DAY]["status"] == "incomplete"
    feed = method.MonthlyMaxSigmaFeed(base, batches, "low_max", own_config)
    assert isinstance(feed.signal_frame(DAY, own_config, CONTEXT), SignalUnavailable)
    assert isinstance(feed.signal_frame(NEXT, own_config, CONTEXT), SignalUnavailable)
    unknown = tiny_base()
    unknown.closes.loc["2024-01-12"] = np.nan
    unknown_batches, _ = method.formation(unknown, [DAY], own_config)
    assert unknown_batches[DAY]["status"] == "incomplete"
    assert isinstance(method.MonthlyMaxSigmaFeed(unknown, unknown_batches, "low_max", own_config)
        .signal_frame(DAY, own_config, CONTEXT), SignalUnavailable)
    base = tiny_base()
    for rows in base.rows.values():
        for row in rows.values():
            row["isST"] = 1
    batches, scored = method.formation(base, [DAY], own_config)
    assert scored == [] and batches[DAY]["status"] == "complete"
    for arm in method.ARMS:
        feed = method.MonthlyMaxSigmaFeed(base, batches, arm, own_config)
        assert feed.signal_frame(DAY, own_config, CONTEXT).signals == ()
        assert feed.audit[-1]["reason"] == "known_empty_common_pool"
    with pytest.raises(ValueError, match="REPLAY"):
        feed.signal_frame(DAY, own_config, RunContext("paper", TradingMode.PAPER))


def test_tiny_real_engine_uses_next_raw_open_lots_fees_t1_and_leaves_non_month_end_unchanged():
    base, own_config = tiny_base(("600001.SSE",)), method.config()
    batches, _ = method.formation(base, [DAY], own_config)
    for day in (NEXT, AFTER):
        base.rows[day][base.symbols[0]].update(open=10.2, high=10.3, low=10., close=10.2,
            preclose=10. if day == NEXT else 10.2)
    feed = method.MonthlyMaxSigmaFeed(base, batches, "low_max", own_config)
    state = new_state(own_config.initial_cash)
    advance(state, feed, DAY, own_config, 0)
    assert state["fills"] == {} and state["pending_targets"] == {base.symbols[0]: .04}
    advance(state, feed, NEXT, own_config, 1)
    assert state["fills"] and {fill["day"] for fill in state["fills"].values()} == {NEXT}
    # The unchanged ten-basis-point buy slippage rounds the 10.20 raw open to 10.21.
    assert all(fill["price"] == 1021 and fill["qty"] % 100 == 0 and fill["fees"] > 0
        for fill in state["fills"].values())
    lots = state["positions"][base.symbols[0]]
    assert sum(lot["qty"] for lot in lots) == 500 and {lot["available"] for lot in lots} == {AFTER}
    assert state["cash"] > 0 and sum(state["targets"].values()) == pytest.approx(.04)
    orders, fills, targets = deepcopy(state["orders"]), deepcopy(state["fills"]), deepcopy(state["targets"])
    advance(state, feed, AFTER, own_config, 2)
    assert state["orders"] == orders and state["fills"] == fills and state["targets"] == targets


def test_run_preserves_failed_first_arm_runs_second_and_refuses_existing_output(tmp_path, monkeypatch):
    base = tiny_base(("600001.SSE",))
    base.calendar = [DAY, NEXT]
    registration = tmp_path / "registration" / "registration.json"
    registration.parent.mkdir()
    registration.write_text('{"synthetic_metadata_only": true}', encoding="utf8")
    sources = {key: str(tmp_path / "sources" / key) for key in
        ("price_view", "event_view", "reference_view", "raw_view", "listing_master")}
    saved = dict(inputs=sources, source_calendar=base.calendar, decision_days=[DAY],
        source_run=str(tmp_path / "source_run.json"))
    monkeypatch.setattr(method, "SESSIONS", 2)
    monkeypatch.setattr(method, "_registration", lambda path: saved)
    monkeypatch.setattr(method, "BulkResearchFeed", lambda **options: base)
    monkeypatch.setattr(method, "formation", lambda *args: ({}, []))
    calls = []

    def consume(feed, own_config, dates, directory):
        calls.append(feed.arm)
        directory.mkdir()
        state = new_state(own_config.initial_cash)
        state["day"] = method.END if feed.arm == "low_sigma" else DAY
        if feed.arm == "low_sigma":
            state["fills"] = {"synthetic-fill": {"day": NEXT}}
        (directory / "account.json").write_text(json.dumps(state), encoding="utf8")
        return dict(status="failed" if feed.arm == "low_max" else "completed",
            error="synthetic_reference_unknown" if feed.arm == "low_max" else None,
            completed_sessions=1 if feed.arm == "low_max" else 2, net_return=.123,
            max_drawdown=-.01, sharpe_zero_rf_252=1., annualized_volatility=.1, worst_day=-.01)

    monkeypatch.setattr(method, "baseline_ledger_result", consume)
    # Coordination only: the real Engine test above owns execution behavior;
    # this two-session stub cannot stand in for three years of daily evidence.
    def summarize(case, folder, own_config, dates):
        complete = case["status"] == "completed"
        case.update(full_period_metrics_available=complete, account=str(folder / "account.json"))
        if not complete:
            for key in ("net_return", "max_drawdown", "sharpe_zero_rf_252", "annualized_volatility", "worst_day"):
                case[key] = None
        return case

    monkeypatch.setattr(method, "_case_summary", summarize)
    out = tmp_path / "new_two_arm_output"
    result = method.run(registration, out)
    assert calls == list(method.ARMS) and result["full_paired_account_comparison"] is False
    failed, completed = result["cases"]
    assert failed["status"] == "failed" and failed["error"] == "synthetic_reference_unknown"
    assert failed["net_return"] is None and failed["full_period_metrics_available"] is False
    assert completed["status"] == "completed" and completed["full_period_metrics_available"] is True
    assert result["automatic_retry"] is False
    original = (out / "low_max_result.json").read_bytes()
    with pytest.raises(FileExistsError, match="never retried"):
        method.run(registration, out)
    assert calls == list(method.ARMS) and (out / "low_max_result.json").read_bytes() == original
    base.close.assert_called_once()
