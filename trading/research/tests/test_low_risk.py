from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.research import low_risk as research

DAY = "2024-01-31"
END = "2024-02-02"
CONTEXT = RunContext("low-risk-fixture", TradingMode.REPLAY)
SYMBOLS = ["600000.SSE", "600001.SSE", "600002.SSE", "689009.SSE"]


def base():
    days = pd.bdate_range("2023-01-02", "2024-02-05").strftime("%Y-%m-%d").tolist()
    row = {"open": 10., "high": 10., "low": 10., "close": 10., "preclose": 10.,
           "volume": 1000000., "amount": 30000000., "isST": 0, "tradestatus": 1}
    return SimpleNamespace(calendar=days, day_index={d: i for i, d in enumerate(days)}, symbols=SYMBOLS,
        rows={d: {s: dict(row) for s in SYMBOLS} for d in days},
        instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in SYMBOLS},
        unsupported_instruments={SYMBOLS[-1]: "unsupported CDR"}, actions=[], close=Mock(),
        closes=pd.DataFrame(10., index=days, columns=SYMBOLS),
        volumes=pd.DataFrame(1000000., index=days, columns=SYMBOLS),
        amounts=pd.DataFrame(30000000., index=days, columns=SYMBOLS),
        manifest={"scope": research.BULK_SCOPE, "start": DAY, "end": END,
                  "selection_date": "2023-01-01", "limitations": ["synthetic fixture"]},
        next_day=lambda day: days[days.index(day) + 1])


def values():
    return {"momentum": pd.DataFrame([[-.1, .5, .3, 9.]], index=[DAY], columns=SYMBOLS),
            "volatility": pd.DataFrame([[.1, .4, .2, .001]], index=[DAY], columns=SYMBOLS),
            "median_amount": pd.DataFrame(30000000., index=[DAY], columns=SYMBOLS)}


def test_exact_features_cutoff_no_future_and_no_zero_return_suspension():
    feed = base()
    returns = np.array([.002 if i % 2 else -.001 for i in range(len(feed.calendar))])
    closes = 10 * np.cumprod(1 + returns)
    feed.closes[:] = np.tile(closes[:, None], (1, len(SYMBOLS)))
    actual = research.features(feed, END)
    i = feed.day_index[DAY]
    expected_returns = feed.closes[SYMBOLS[0]].pct_change(fill_method=None).iloc[i-251:i+1]
    assert actual["volatility"].at[DAY, SYMBOLS[0]] == pytest.approx(expected_returns.std(ddof=1) * np.sqrt(252))
    assert actual["momentum"].at[DAY, SYMBOLS[0]] == pytest.approx(closes[i-21] / closes[i-252] - 1)
    feed.closes.loc[feed.closes.index > END] = 1e9
    for key, frame in actual.items():
        pd.testing.assert_frame_equal(frame, research.features(feed, END)[key])
    feed.volumes.loc[feed.calendar[i-40], SYMBOLS[0]] = 0
    assert pd.isna(research.features(feed, END)["volatility"].at[DAY, SYMBOLS[0]])


def test_half_filter_and_ranking_are_deterministic_and_keep_negative_momentum():
    pool = [{"symbol": s, "momentum": m, "volatility": v} for s, m, v in (
        ("600003.SSE", .9, .3), ("600002.SSE", .4, .1), ("600001.SSE", -.2, .1))]
    assert [r["symbol"] for r in research.rank(pool, research.VARIANTS[1])] == ["600001.SSE", "600002.SSE", "600003.SSE"]
    assert [r["symbol"] for r in research.rank(pool, research.VARIANTS[2])] == ["600002.SSE", "600001.SSE"]
    assert research.rank(pool, research.VARIANTS[2])[1]["score"] == -.2
    assert research.rank([], research.VARIANTS[2]) == []


def test_replay_scope_missing_batches_and_known_empty_are_distinct():
    feed = base()
    wrapper = research.LowRiskFeed(feed, values(), research.VARIANTS[0], END)
    config = research.portfolio()
    with pytest.raises(ValueError, match="replay-only"):
        wrapper.signal_frame(DAY, config, RunContext("paper", TradingMode.PAPER))
    with pytest.raises(ValueError, match="fixed 20-name"):
        wrapper.signal_frame(DAY, replace(config, holdings=5), CONTEXT)
    assert wrapper.signal_frame("2024-02-01", config, CONTEXT).reason == "not_month_end"
    assert len(wrapper.signal_frame(DAY, config, CONTEXT).signals) == 3
    feed.rows[DAY] = {}
    assert wrapper.signal_frame(DAY, config, CONTEXT).reason == "low_risk_raw_batch_missing"
    feed = base()
    wrapper.base = feed
    feed.rows[DAY] = {s: {**row, "isST": 1} for s, row in feed.rows[DAY].items()}
    assert wrapper.signal_frame(DAY, config, CONTEXT).signals == ()
    wrapper.values["volatility"].loc[DAY] = np.nan
    assert isinstance(wrapper.signal_frame(DAY, config, CONTEXT), SignalUnavailable)


def test_same_pool_excludes_st_zero_volume_and_unsupported_not_future_events():
    feed, config = base(), research.portfolio()
    wrapper = research.LowRiskFeed(feed, values(), research.VARIANTS[0], END)
    feed.rows[DAY][SYMBOLS[1]]["isST"] = 1
    feed.rows[DAY][SYMBOLS[2]]["volume"] = 0
    assert [row["symbol"] for row in wrapper.pool(DAY, config)] == [SYMBOLS[0]]
    feed.actions = [{"symbol": SYMBOLS[0], "dividReserveToStockPs": "", "dividOperateDate": "2024-06-01"}]
    assert [row["symbol"] for row in wrapper.pool(DAY, config)] == [SYMBOLS[0]]


def test_source_plan_uses_full_fixed_ranks_not_unknown_actions_or_account_results():
    feed = base()
    before = research.source_plan(feed, values(), research.VARIANTS[1:], DAY, END, research.portfolio())
    feed.actions = [{"symbol": SYMBOLS[0], "dividReserveToStockPs": "", "dividOperateDate": END}]
    after = research.source_plan(feed, values(), research.VARIANTS[1:], DAY, END, research.portfolio())
    assert before == after
    assert before["source_symbols"] == SYMBOLS[:3]
    assert {row["date"] for row in before["rank_rows"]} == {DAY}
    assert not before["account_created"] and not before["execution_admitted"]


def test_source_plan_run_does_not_create_or_advance_an_account(tmp_path):
    feed = base()
    feed.manifest.update(**{key: str(tmp_path / key) for key in (
        "price_view", "event_view", "reference_view", "raw_view", "listing_master")},
        source_codes=4, canonical_codes=4, unsupported_instruments=feed.unsupported_instruments)
    source = tmp_path / "review.json"
    source.write_text("{}", encoding="utf-8")
    with patch.object(research, "ROOT", tmp_path), patch.object(research, "DATA_ROOT", tmp_path / "data"), \
            patch.object(research, "BulkResearchFeed", return_value=feed), \
            patch.object(research, "features", return_value=values()), \
            patch.object(research, "baseline_ledger_result") as consume:
        out = research.run("p", "e", "r", "l", source, DAY, END,
                           variants=research.VARIANTS[1:], plan_sources=True)
    consume.assert_not_called()
    record = json.loads((out / "run.json").read_text())
    plan = json.loads((out / "source_plan.json").read_text())
    assert record["status"] == "prepared_low_risk_source_targets" and record["results"] == []
    assert plan["source_symbols"] == SYMBOLS[:3] and not list((tmp_path / "data").rglob("account.json"))
    feed.close.assert_called_once()


def test_actual_sole_ledger_delays_small_equal_weight_target_and_keeps_no_change():
    feed, config = base(), research.portfolio()
    for rows in feed.rows.values():
        rows.pop(SYMBOLS[-1])
    feed.rows[DAY][SYMBOLS[1]]["isST"] = 1
    feed.rows[DAY][SYMBOLS[2]]["isST"] = 1
    wrapper = research.LowRiskFeed(feed, values(), research.VARIANTS[1], END)
    state = new_state(config.initial_cash)
    advance(state, wrapper, DAY, config, 0)
    assert not state["fills"] and state["pending_targets"] == {SYMBOLS[0]: .04}
    advance(state, wrapper, "2024-02-01", config, 1)
    assert sum(lot["qty"] for lot in state["positions"][SYMBOLS[0]]) == 500
    assert all(fill["day"] == "2024-02-01" for fill in state["fills"].values())
    targets = deepcopy(state["targets"])
    advance(state, wrapper, END, config, 2)
    assert state["targets"] == targets and not state["pending_targets"]


def test_2026_continuation_keeps_rules_costs_and_terminal_cases():
    protocol = {key: key for key in research.FIXED_PROTOCOL_FIELDS}
    protocol.update(portfolio=research.portfolio().to_dict(), signals=list(research.VARIANTS))
    previous = {"status": "completed_low_risk_diagnostic", "purpose": "fixed_low_risk_2024_2025_diagnostic",
                "research_family": "low_risk_stock_selection", "scope": research.BULK_SCOPE,
                "symbols": 5369, "start": "2024-01-02", "end": research.PERIOD_END,
                "sessions": 485, "protocol": protocol,
                "results": [{"signal": s, "status": "completed", "completed_sessions": 485}
                            for s in research.VARIANTS]}
    current = deepcopy(previous)
    current.update(end="2026-09-30", sessions=668, purpose="fixed_low_risk_2026_continuation_diagnostic")
    research.validate_continuation(previous, current)
    for key, value in (("selection", "new optimized weights"), ("portfolio", {"initial_cash": 140001})):
        changed = deepcopy(current)
        changed["protocol"][key] = value
        with pytest.raises(ValueError, match="unchanged rules/costs"):
            research.validate_continuation(previous, changed)
    for change in ({"status": "running"}, {"end": "2025-12-30"}):
        with pytest.raises(ValueError, match="terminal fixed"):
            research.validate_continuation({**previous, **change}, current)
    previous["results"][1]["status"] = "failed"
    with pytest.raises(ValueError, match="terminal account"):
        research.validate_continuation(previous, current)


def test_actual_2026_checkpoint_carries_pending_target_without_replaying_old_day(tmp_path):
    feed = base()
    days = pd.bdate_range("2023-01-02", "2026-01-06").strftime("%Y-%m-%d").tolist()
    days = [d for d in days if d not in {"2026-01-01", "2026-01-02"}]
    feed.calendar, feed.day_index = days, {d: i for i, d in enumerate(days)}
    feed.next_day = lambda day: days[days.index(day) + 1]
    feed.volumes = pd.DataFrame(1000000., index=days, columns=SYMBOLS)
    original_row = feed.rows[DAY][SYMBOLS[0]]
    feed.rows = {d: {SYMBOLS[0]: dict(original_row)} for d in days}
    frame = {key: pd.DataFrame([[v] * len(SYMBOLS)], index=[research.PERIOD_END], columns=SYMBOLS)
             for key, v in (("momentum", .1), ("volatility", .2), ("median_amount", 30000000.))}
    config = research.portfolio()
    variant = research.VARIANTS[1]
    prior_dates = [research.PERIOD_END]
    all_dates = prior_dates + ["2026-01-05", "2026-01-06"]
    prior = tmp_path / "prior"
    research.baseline_ledger_result(research.LowRiskFeed(feed, frame, variant, research.PERIOD_END),
                                   config, prior_dates, prior)
    checkpoint = research.load_replay_checkpoint(prior, config, all_dates)
    assert checkpoint["state"]["pending_targets"] == {SYMBOLS[0]: .04}
    wrapper = research.LowRiskFeed(feed, frame, variant, "2026-01-06", diagnostic_extension=True)
    destination = tmp_path / "continued"
    result = research.baseline_ledger_result(wrapper, config, all_dates, destination, checkpoint=checkpoint)
    assert result["status"] == "completed" and result["reused_sessions"] == 1
    assert result["newly_completed_sessions"] == 2 and [r["date"] for r in wrapper.audit] == all_dates[1:]
    state = json.loads((destination / "account.json").read_text())
    assert sum(lot["qty"] for lot in state["positions"][SYMBOLS[0]]) == 500
    stage = research.extension_metrics(destination)
    assert not stage["account_reset"] and stage["opening_equity"] == config.initial_cash
    assert stage["sessions"] == 2 and stage["fees"] > 0 and stage["net_return"] < 0
    with pytest.raises(ValueError, match="cutoff beyond 2025"):
        research.LowRiskFeed(feed, frame, variant, "2026-01-02")


@pytest.mark.parametrize("variants", [research.VARIANTS, research.VARIANTS[1:]])
def test_fixed_protocol_before_same_config_consumers_retains_failure(tmp_path, variants):
    feed = base()
    inputs = {key: str(tmp_path / key) for key in (
        "price_view", "event_view", "reference_view", "raw_view", "listing_master")}
    feed.manifest.update(**inputs, source_codes=4, canonical_codes=4,
                         unsupported_instruments=feed.unsupported_instruments)
    source = tmp_path / "review.json"
    source.write_text("{}", encoding="utf-8")
    calls = []
    def consume(wrapper, config, dates, destination):
        saved = json.loads(next((tmp_path / "outputs/market_replay").glob("*/protocol.json")).read_text())
        assert saved["signals"] == list(variants) and not saved["tuning"]
        assert saved["predefined_variants"] == list(research.VARIANTS)
        assert saved["portfolio"] == config.to_dict() and wrapper.base is feed
        calls.append((config.to_dict(), dates))
        if len(calls) == 2:
            return {"status": "failed", "completed_sessions": 1, "attempted_session": "2024-02-01",
                    "account_basis": "last_successfully_completed_session", "error": "unknown action"}
        return {"status": "completed", "completed_sessions": len(dates), "net_return": -.1,
                "max_drawdown": .2, "sharpe_zero_rf_252": -1, "fees": 5., "worst_day": -.02}
    with patch.object(research, "ROOT", tmp_path), patch.object(research, "DATA_ROOT", tmp_path / "data"), \
            patch.object(research, "BulkResearchFeed", return_value=feed), \
            patch.object(research, "features", return_value=values()), \
            patch.object(research, "baseline_ledger_result", side_effect=consume):
        out = research.run("prices", "events", "references", "listing", source, DAY, END, variants=variants)
    record = json.loads((out / "run.json").read_text())
    assert len(calls) == len(variants) and all(call == calls[0] for call in calls)
    assert record["status"] == "failed_low_risk_diagnostic"
    assert [r.get("net_return") for r in record["results"]] == [-.1, None, -.1][:len(variants)]
    feed.close.assert_called_once()
    with patch.object(research, "BulkResearchFeed") as load:
        with pytest.raises(ValueError, match="2024-2025"):
            research.run("p", "e", "r", "l", source, end="2026-09-30")
        with pytest.raises(ValueError, match="unique predefined"):
            research.run("p", "e", "r", "l", source, variants=[variants[0]] * 2)
        load.assert_not_called()
