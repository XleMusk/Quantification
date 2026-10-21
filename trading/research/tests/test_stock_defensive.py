from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from trading.contracts import PortfolioIntent, RunContext, SignalFrame, SignalUnavailable, TradingMode, decision_time
from trading.engine import advance, allocate_replay_target, choose_targets
from trading.ledger import new_state
from trading.research.bulk_feed import BULK_SCOPE
from trading.research.fund_replay_snapshot import SCOPE
from trading.research.low_risk import VARIANTS, portfolio
from trading.research.experiment import load_replay_checkpoint, replay, write_json
from trading.research.stock_defensive import (DEFENSIVE, START, MixedResearchFeed, StockDefensiveFeed,
                                              mixed_config, validate_fund_prefix)
from trading.rules import FUND_PROFILES

DAY, NEXT = "2024-01-31", "2024-02-01"
CONTEXT = RunContext("mixed-research-test", TradingMode.REPLAY)
STOCKS = [f"600{i:03d}.SSE" for i in range(40)]


def sources():
    days = [START, DAY, NEXT]
    row = {"open": 10., "high": 10., "low": 5.001, "close": 10., "preclose": 10.,
           "volume": 1000000., "amount": 10000000., "isST": 0, "tradestatus": 1, "adjustflag": "3"}
    stocks = SimpleNamespace(calendar=days, symbols=STOCKS,
        manifest={"scope": BULK_SCOPE, "replay_only": True, "end": NEXT, "limitations": []},
        rows={day: {s: dict(row) for s in STOCKS} for day in days}, actions=[],
        instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in STOCKS},
        volumes=pd.DataFrame(1000000., index=days, columns=STOCKS),
        next_day=lambda day: NEXT if day == DAY else "2024-02-02", validate_session=Mock())
    funds = SimpleNamespace(calendar=days, symbols=[STOCKS[0], *DEFENSIVE],
        manifest={"scope": SCOPE, "replay_only": True, "end": NEXT}, folder="reviewed-funds",
        instrument_profiles={s: FUND_PROFILES[s] for s in DEFENSIVE},
        instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in DEFENSIVE}, actions=[],
        rows={day: {STOCKS[0]: {**row, "close": 999.},
                   **{s: {**row, "open": 5.001, "close": 5.001, "preclose": 5.001} for s in DEFENSIVE}}
              for day in days},
        volumes=pd.DataFrame(1000000., index=days, columns=DEFENSIVE))
    funds.raw = pd.DataFrame([{**r, "date": day, "symbol": s} for day in days
                              for s, r in funds.rows[day].items() if s in DEFENSIVE])
    return stocks, funds


def wrapper():
    stocks, funds = sources()
    base = MixedResearchFeed(stocks, funds, NEXT)
    feed = StockDefensiveFeed(base, {}, VARIANTS[0], 140000)
    ranked = SignalFrame.from_legacy([{"symbol": s, "score": 100. - i} for i, s in enumerate(STOCKS)],
                                     DAY, "research:fixed-stock", CONTEXT)
    feed.stock_signal.signal_frame = Mock(side_effect=lambda day, config, context:
        ranked.with_context(context) if day == DAY else SignalUnavailable("not_month_end"))
    return feed


def test_defensive_slots_preserve_original_stock_selection_and_allocation_bounds():
    feed = wrapper()
    config = mixed_config()
    ranked = feed.signal_frame(DAY, config, CONTEXT).ranked_candidates()
    held = [STOCKS[29], STOCKS[30], *STOCKS[:18]]
    original = choose_targets(ranked[2:], held, portfolio())
    chosen = choose_targets(ranked, [*held, *DEFENSIVE], config)
    assert set(chosen) - set(DEFENSIVE) == set(original)
    assert STOCKS[29] in chosen and STOCKS[30] not in chosen
    target = PortfolioIntent.full_target(chosen, context=CONTEXT, decided_at=decision_time(DAY),
                                         execution_not_before=NEXT)
    weights = dict(allocate_replay_target(target, feed.allocate, config).weights)
    assert all(weights[s] == .2 for s in DEFENSIVE)
    assert all(weights[s] == .02 for s in original)
    assert sum(weights.values()) == pytest.approx(.8)
    with pytest.raises(ValueError, match="admitted candidates"):
        allocate_replay_target(target, lambda intent: replace(intent, weights=((STOCKS[35], .1),)), config)
    with pytest.raises(ValueError, match="holdings/rank"):
        replace(config, holdings=23).validate()


def test_stocks_and_mill_funds_fill_next_session_in_one_account_and_missing_signal_preserves_it():
    feed, config = wrapper(), mixed_config()
    state = new_state(config.initial_cash)
    advance(state, feed, DAY, config, 0, context=CONTEXT, portfolio_policy=feed.allocate)
    assert not state["fills"]
    assert state["pending_targets"][DEFENSIVE[0]] == .2
    advance(state, feed, NEXT, config, 1, context=CONTEXT, portfolio_policy=feed.allocate)
    assert state["pending_targets"] is None
    assert set(state["positions"]) == set(STOCKS[:20]) | set(DEFENSIVE)
    assert all(fill["day"] == NEXT for fill in state["fills"].values())
    assert state["price_units"][DEFENSIVE[0]] == "mill"
    assert state["prices"][STOCKS[0]] == 1000
    assert state["cash"] >= 0
    assert feed.base.stocks.rows[NEXT][STOCKS[0]]["close"] == 10.
    assert isinstance(feed.signal_frame(NEXT, config, CONTEXT), SignalUnavailable)
    before = deepcopy(state)
    with pytest.raises(ValueError, match="historical-replay-only"):
        advance(state, feed, "2024-02-02", config, 2,
                context=RunContext("paper", TradingMode.PAPER), portfolio_policy=feed.allocate)
    assert state == before


def test_mixed_sources_reject_calendar_holes_or_beyond_fund_coverage():
    stocks, funds = sources()
    funds.calendar = [START, DAY]
    with pytest.raises(ValueError, match="exchange sessions"):
        MixedResearchFeed(stocks, funds, NEXT)
    stocks, funds = sources()
    stocks.calendar.append("2024-02-02")
    stocks.manifest["end"] = "2024-02-02"
    with pytest.raises(ValueError, match="coverage windows"):
        MixedResearchFeed(stocks, funds, "2024-02-02")


def test_fixed_policy_continuation_preserves_pending_fund_stock_targets_and_cash(tmp_path):
    feed, config = wrapper(), mixed_config()
    contract = {"id": "fixed_test_allocation_v1", "config": config.to_dict(), "allocation": "stock2_funds20"}
    protocol = tmp_path / "protocol.json"
    write_json(protocol, contract)
    prefix = tmp_path / "prefix"
    replay(feed, config, [DAY], prefix, portfolio_policy=feed.allocate)
    original = (prefix / "account.json").read_bytes()
    with pytest.raises(ValueError, match="unpinned"):
        load_replay_checkpoint(prefix, config, [DAY, NEXT])
    with pytest.raises(ValueError, match="original predeclared"):
        load_replay_checkpoint(prefix, config, [DAY, NEXT], portfolio_policy_contract=contract)
    with pytest.raises(ValueError, match="does not match"):
        load_replay_checkpoint(prefix, config, [DAY, NEXT], portfolio_policy_contract={**contract, "allocation": "changed"},
                               legacy_policy_protocol=protocol)
    checkpoint = load_replay_checkpoint(prefix, config, [DAY, NEXT], portfolio_policy_contract=contract,
                                        legacy_policy_protocol=protocol)
    assert checkpoint["state"]["pending_targets"][DEFENSIVE[0]] == .2
    continued = tmp_path / "continued"
    result = replay(feed, config, [DAY, NEXT], continued, portfolio_policy=feed.allocate,
                    portfolio_policy_contract=contract, checkpoint=checkpoint)
    assert result["reused_sessions"] == result["newly_completed_sessions"] == 1
    assert (prefix / "account.json").read_bytes() == original
    full = tmp_path / "full"
    replay(feed, config, [DAY, NEXT], full, portfolio_policy=feed.allocate, portfolio_policy_contract=contract)
    import json
    assert json.loads((continued / "account.json").read_text()) == json.loads((full / "account.json").read_text())
    with pytest.raises(ValueError, match="contract changed"):
        load_replay_checkpoint(continued, config, [DAY, NEXT],
                               portfolio_policy_contract={**contract, "allocation": "changed"})
    with pytest.raises(ValueError, match="configuration/policy mismatch"):
        replay(feed, config, [DAY, NEXT], tmp_path / "bad", portfolio_policy=feed.allocate, checkpoint=checkpoint)


def test_fund_prefix_rejects_changed_reference_or_registration_but_allows_future_action():
    _, before = sources()
    after = deepcopy(before)
    after.actions = [{"symbol": DEFENSIVE[0], "dividOperateDate": "2024-03-01", "dividRegistDate": "2024-02-29",
                      "dividPayDate": "2024-03-01", "dividCashPsBeforeTax": .1,
                      "dividStocksPs": 0, "dividReserveToStockPs": 0}]
    assert validate_fund_prefix(before, after, NEXT)["unchanged_observed_prefix"]
    after.raw.loc[after.raw.symbol.eq(DEFENSIVE[0]) & after.raw.date.eq(NEXT), "preclose"] += .001
    with pytest.raises(ValueError, match="raw quote"):
        validate_fund_prefix(before, after, NEXT)
    after = deepcopy(before)
    after.actions = [{"symbol": DEFENSIVE[0], "dividOperateDate": "2024-03-01", "dividRegistDate": DAY,
                      "dividPayDate": "2024-03-01", "dividCashPsBeforeTax": .1,
                      "dividStocksPs": 0, "dividReserveToStockPs": 0}]
    with pytest.raises(ValueError, match="economic action/entitlement"):
        validate_fund_prefix(before, after, NEXT)


def test_fund_prefix_compares_exact_amounts_across_csv_types_without_defaulting_unknowns():
    _, before = sources()
    before.actions = [{"symbol": DEFENSIVE[0], "dividRegistDate": DAY,
                      "dividOperateDate": NEXT, "dividPayDate": NEXT,
                      "dividCashPsBeforeTax": ".6542", "dividStocksPs": 0.,
                      "dividReserveToStockPs": "0"}]
    after = deepcopy(before)
    after.actions[0].update(dividCashPsBeforeTax=.6542, dividStocksPs=0, dividReserveToStockPs=0)
    assert validate_fund_prefix(before, after, NEXT)["unchanged_observed_prefix"]
    after.actions[0]["dividCashPsBeforeTax"] = .6543
    with pytest.raises(ValueError, match="economic action/entitlement"):
        validate_fund_prefix(before, after, NEXT)
    for unknown in ("", None, float("nan")):
        after.actions[0]["dividCashPsBeforeTax"] = unknown
        with pytest.raises(ValueError, match="economic amount is unknown"):
            validate_fund_prefix(before, after, NEXT)
