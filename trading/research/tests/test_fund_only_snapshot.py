from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import advance
from trading.feed import HistoricalFeed
from trading.ledger import new_state
from trading.research import fund_replay_snapshot as source
from .test_fund_replay_snapshot import DAYS, inputs


def calendar_fixture(folder, problem=None):
    folder.mkdir()
    natural = pd.date_range("2024-01-01", DAYS[-1]).strftime("%Y-%m-%d").tolist()
    provider = pd.DataFrame({"calendar_date": natural, "is_trading_day": [int(day in DAYS) for day in natural]})
    calendar = DAYS.copy()
    if problem == "missing_provider_day":
        provider = provider.iloc[1:]
    elif problem == "invalid_flag":
        provider.loc[0, "is_trading_day"] = 2
    elif problem == "closed_day":
        calendar = ["2024-01-01", *calendar]
    provider.to_csv(folder / "provider_calendar.csv", index=False)
    pd.DataFrame({"date": calendar}).to_csv(folder / "calendar.csv", index=False)
    receipt = {"query": "query_trade_dates", "execution_admitted": False,
               "start": "2024-01-01", "end": DAYS[-1], "first": calendar[0], "last": calendar[-1],
               "sessions": len(calendar), "provider_calendar_sha256": sha256((folder / "provider_calendar.csv").read_bytes()).hexdigest(),
               "calendar_sha256": sha256((folder / "calendar.csv").read_bytes()).hexdigest()}
    (folder / "receipt.json").write_text(json.dumps(receipt))
    return folder


def test_fund_only_uses_observed_calendar_same_feed_ledger_and_paper_rejection(tmp_path, monkeypatch):
    _, dayk, sina, reports = inputs(tmp_path, monkeypatch)
    calendar = calendar_fixture(tmp_path / "calendar")
    folder, manifest = source.build(None, dayk, sina, reports, out=tmp_path / "funds", calendar_source=calendar)
    assert manifest["scope"] == source.FUND_SCOPE and manifest["stock_symbols"] == 0
    assert manifest["fund_symbols"] == 1 and "stock_source" not in manifest
    assert manifest["calendar_source"]["receipt"]["query"] == "query_trade_dates"
    assert (folder / "calendar_evidence/provider_calendar.csv").read_bytes() == (calendar / "provider_calendar.csv").read_bytes()
    feed = HistoricalFeed(folder)
    assert feed.symbols == ["510300.SSE"] and feed.calendar == DAYS
    config = StrategyConfig(initial_cash=10000, holdings=1, retain_rank=1,
                            single_limit=.8, gross_limit=.8, slippage_bps=0).validate()
    state = new_state(config.initial_cash)
    state["pending_targets"] = {"510300.SSE": .4}
    advance(state, feed, DAYS[1], config, 1)
    assert state["entitlements"]["510300.SSE:" + DAYS[2]] == 1000
    advance(state, feed, DAYS[2], config, 2)
    cash = state["cash"]
    advance(state, feed, DAYS[3], config, 3)
    assert state["cash"] == cash + 1000 and state["receivables"] == {}
    with pytest.raises(ValueError, match="historical-replay-only"):
        advance(new_state(config.initial_cash), feed, DAYS[1], config, 1,
                context=RunContext("fund_only_fixture", TradingMode.PAPER))


@pytest.mark.parametrize("problem", ["missing_provider_day", "invalid_flag", "closed_day"])
def test_fund_calendar_cannot_invent_days_or_ignore_provider_gaps(tmp_path, monkeypatch, problem):
    inputs(tmp_path, monkeypatch)
    calendar = calendar_fixture(tmp_path / "calendar", problem)
    with pytest.raises(ValueError, match="complete observed"):
        source.pinned_fund_calendar(calendar, DAYS[-1])


def test_calendar_mode_does_not_bypass_distribution_coverage(tmp_path, monkeypatch):
    _, dayk, sina, reports = inputs(tmp_path, monkeypatch)
    calendar = calendar_fixture(tmp_path / "calendar")
    (reports / "distribution_periods.json").write_text("[]")
    with pytest.raises(ValueError, match="entire diagnostic"):
        source.build(None, dayk, sina, reports, out=tmp_path / "funds", calendar_source=calendar)
    assert not (tmp_path / "funds").exists()


def test_stock_and_calendar_modes_are_explicitly_exclusive(tmp_path, monkeypatch):
    paths = inputs(tmp_path, monkeypatch)
    calendar = calendar_fixture(tmp_path / "calendar")
    with pytest.raises(ValueError, match="Choose either"):
        source.build(*paths, calendar_source=calendar)


def test_fixed_strategy_accepts_fund_only_scope_but_retains_its_full_input_set(tmp_path, monkeypatch):
    from trading.research import fund_trend
    from trading.rules import FUND_PROFILES
    feed = SimpleNamespace(manifest={"scope": source.FUND_SCOPE, "replay_only": True, "file_hashes": {}},
                           instrument_profiles=FUND_PROFILES.copy(), folder=tmp_path)
    monkeypatch.setattr(fund_trend, "HistoricalFeed", lambda _: feed)
    paper = tmp_path / "fixture_not_a_source_paper"
    paper.write_bytes(b"fixture only")
    with pytest.raises(ValueError, match="author's source paper"):
        fund_trend.run(tmp_path, paper, cross_asset=True)
    feed.instrument_profiles = {"511010.SSE": FUND_PROFILES["511010.SSE"]}
    with pytest.raises(ValueError, match="predetermined funds"):
        fund_trend.run(tmp_path, paper, cross_asset=True)
