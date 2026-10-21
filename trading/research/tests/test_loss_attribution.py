from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import advance, commission_deadband_skip, corporate_actions
from trading.ledger import Ledger, new_state
from trading.research.experiment import load_replay_checkpoint, replay
from trading.research.loss_attribution import asset_pnl, order_costs, raw_open_slippage
from trading.research.tests import test_replay_resume
from trading.rules import FUND_Q3_2026_PROFILES


def book():
    config = StrategyConfig(initial_cash=140000)
    state = new_state(config.initial_cash)
    state["positions"] = {"600000.SSE": [{"qty": 1000, "available": "2024-01-02"}]}
    state["prices"] = {"600000.SSE": 1000}
    state["cash"] -= 1000000
    return Ledger(state, config)


def test_commission_band_only_skips_internal_reweights():
    ledger = book()
    args = (ledger, "600000.SSE", "2024-01-03")
    assert commission_deadband_skip(*args, "buy", 1000, 1100, 100, 10)
    assert commission_deadband_skip(*args, "sell", 1000, 900, 100, 10)
    assert not commission_deadband_skip(*args, "buy", 0, 100, 100, 10)
    assert not commission_deadband_skip(*args, "sell", 1000, 0, 100, 10)
    assert not commission_deadband_skip(*args, "sell", 1000, 100, 1000, 10)
    assert not commission_deadband_skip(*args, "buy", 1000, 3000, 2000, 10)


def test_single_or_gross_risk_reduction_is_not_deferred():
    ledger = book()
    ledger.state["prices"]["600000.SSE"] = 10000
    assert not commission_deadband_skip(ledger, "600000.SSE", "2024-01-03", "sell", 1000, 900, 100, 100)
    ledger = book()
    ledger.config = replace(ledger.config, single_limit=.8)
    ledger.state["positions"]["600001.SSE"] = [{"qty": 12000, "available": "2024-01-02"}]
    ledger.state["prices"]["600001.SSE"] = 1000
    ledger.state["cash"] = 1000000
    assert not commission_deadband_skip(ledger, "600000.SSE", "2024-01-03", "sell", 1000, 900, 100, 10)


def test_band_is_replay_only_and_cannot_switch_in_existing_account():
    with pytest.raises(ValueError, match="replay-only"):
        advance(new_state(140000), SimpleNamespace(), "2024-01-02", StrategyConfig(), 0,
                context=RunContext("paper", TradingMode.PAPER), commission_deadband=True)
    state = new_state(140000)
    state["day"] = "2024-01-02"
    with pytest.raises(ValueError, match="cannot change"):
        advance(state, SimpleNamespace(), "2024-01-03", StrategyConfig(), 1, commission_deadband=True)


def test_order_level_fee_not_per_fill_and_fund_units(tmp_path):
    config = StrategyConfig()
    order = {"id": "x", "day": "2024-01-02", "symbol": "600000.SSE", "side": "sell",
             "filled": 100, "notional": 100000, "fees": 551}
    costs = order_costs([order], config)
    assert costs.iloc[0].commission_cny == 5
    assert costs.iloc[0].minimum_commission_premium_cny == 4.7
    fund = {**order, "symbol": "511010.SSE", "fees": 500}
    assert order_costs([fund], config, FUND_Q3_2026_PROFILES).iloc[0].sell_tax_cny == 0
    pd.DataFrame([{"date": "2024-01-02", "code": "sh.600000", "open": "10.00"},
                  {"date": "2024-01-02", "code": "sh.511010", "open": "100.001"}]).to_parquet(tmp_path / "raw_daily.parquet")
    fills = [{"day": "2024-01-02", "symbol": "600000.SSE", "price": 1001, "side": "buy", "qty": 100},
             {"day": "2024-01-02", "symbol": "511010.SSE", "price": 100002, "side": "buy", "qty": 100}]
    summary, _ = raw_open_slippage(fills, tmp_path, FUND_Q3_2026_PROFILES)
    assert summary["slippage_cny"] == 1.1
    summary, _ = raw_open_slippage(fills + [{**fills[0], "symbol": "600001.SSE"}], tmp_path, FUND_Q3_2026_PROFILES)
    assert summary["slippage_cny"] is None
    assert summary["matched_fills"] == 2


def test_replay_receipt_binds_band_and_old_checkpoint_cannot_adopt_it(tmp_path):
    feed = test_replay_resume.ReplayResumeTests().feed()
    config = StrategyConfig(initial_cash=140000)
    replay(feed, config, feed.calendar, tmp_path / "band", commission_deadband=True)
    receipt = json.loads((tmp_path / "band/replay.json").read_text(encoding="utf-8"))
    assert receipt["execution_policy"] == "minimum_commission_internal_reweights_v1"
    with pytest.raises(ValueError, match="deadband changed"):
        load_replay_checkpoint(tmp_path / "band", config, feed.calendar)
    checkpoint = load_replay_checkpoint(tmp_path / "band", config, feed.calendar, commission_deadband=True)
    replay(feed, config, feed.calendar, tmp_path / "continued", checkpoint=checkpoint, commission_deadband=True)


def test_separate_pinned_fund_raw_opens_complete_mixed_bridge_without_subset_extrapolation(tmp_path, monkeypatch):
    from trading.research import loss_attribution
    pd.DataFrame([{"date": "2024-01-02", "code": "sh.600000", "open": "10.00"},
                  {"date": "2024-01-02", "code": "sh.511010", "open": "999"}]).to_parquet(tmp_path / "raw_daily.parquet")
    fund = SimpleNamespace(folder=tmp_path / "funds", instrument_profiles=FUND_Q3_2026_PROFILES,
                           raw=pd.DataFrame([{"date": "2024-01-02", "symbol": "511010.SSE", "open": 100.001}]))
    monkeypatch.setattr(loss_attribution, "HistoricalFeed", lambda _: fund)
    fills = [{"day": "2024-01-02", "symbol": "600000.SSE", "price": 1001, "side": "buy", "qty": 100},
             {"day": "2024-01-02", "symbol": "511010.SSE", "price": 100002, "side": "sell", "qty": 100}]
    summary, details = raw_open_slippage(fills, tmp_path, FUND_Q3_2026_PROFILES, fund_dataset=fund.folder)
    assert summary["status"] == "all_matched" and summary["slippage_cny"] == .9
    assert details.loc[details.symbol.eq("511010.SSE"), "raw_open_source"].iloc[0].endswith("raw.parquet")
    incomplete, _ = raw_open_slippage(fills, None, FUND_Q3_2026_PROFILES, fund_dataset=fund.folder)
    assert incomplete["matched_fills"] == 1 and incomplete["slippage_cny"] is None
    fund.instrument_profiles = {}
    with pytest.raises(ValueError, match="profile differs"):
        raw_open_slippage(fills, tmp_path, FUND_Q3_2026_PROFILES, fund_dataset=fund.folder)


def test_asset_pnl_reconciles_bonus_receivable_paid_dividend_and_fragmented_mill_fills():
    config = StrategyConfig(initial_cash=100000)
    ledger = Ledger(new_state(config.initial_cash), config, instrument_profiles=FUND_Q3_2026_PROFILES)
    day = "2026-07-01"
    ledger.submit("stock", day, "600000.SSE", "buy", 100, 1000)
    ledger.fill("s1", "stock", 100, 1000, day, "2026-07-02")
    ledger.submit("fund", day, "511010.SSE", "buy", 100, 100001, price_unit="mill")
    ledger.fill("f1", "fund", 43, 100001, day, day, price_unit="mill")
    ledger.fill("f2", "fund", 57, 100001, day, day, price_unit="mill")
    action = {"symbol": "600000.SSE", "dividRegistDate": day, "dividOperateDate": "2026-07-02",
              "dividPayDate": "2026-07-03", "dividStockMarketDate": "2026-07-02",
              "dividCashPsBeforeTax": ".5", "dividStocksPs": "0", "dividReserveToStockPs": ".5"}
    feed = SimpleNamespace(actions=[action])
    corporate_actions(ledger, feed, day, at_close=True)
    corporate_actions(ledger, feed, "2026-07-02")
    ledger.state["day"] = "2026-07-02"
    ledger.mark_price("600000.SSE", 800, ledger.state["day"])
    ledger.mark_price("511010.SSE", 100100, ledger.state["day"], price_unit="mill")
    summary, details = asset_pnl(ledger.state, config, FUND_Q3_2026_PROFILES)
    stock = details.set_index("symbol").loc["600000.SSE"]
    fund = details.set_index("symbol").loc["511010.SSE"]
    assert stock.terminal_market_value_cny == 1200 and stock.unpaid_net_distribution_cny == 40
    assert stock.observed_pnl_cny == 234.99 and fund.observed_pnl_cny == 4.9
    assert summary["observed_account_pnl_cny"] == 239.89
    assert sum(item["account_return_contribution"] for item in summary["classes"]) == pytest.approx(.0023989)
    corporate_actions(ledger, feed, "2026-07-03")
    ledger.state["day"] = "2026-07-03"
    paid, details = asset_pnl(ledger.state, config, FUND_Q3_2026_PROFILES)
    assert paid["observed_account_pnl_cny"] == summary["observed_account_pnl_cny"]
    stock = details.set_index("symbol").loc["600000.SSE"]
    assert stock.unpaid_net_distribution_cny == 0 and stock.paid_net_distribution_cny == 40


def test_asset_pnl_rejects_unexplained_cash_and_fund_mark_units():
    config = StrategyConfig(initial_cash=140000)
    account = new_state(config.initial_cash)
    account["day"] = "2026-07-01"
    account["cash"] += 1
    with pytest.raises(ValueError, match="unexplained"):
        asset_pnl(account, config)
    account["cash"] -= 1
    account["positions"] = {"511010.SSE": [{"qty": 100, "available": account["day"]}]}
    account["prices"] = {"511010.SSE": 100001}
    with pytest.raises(ValueError, match="quote unit"):
        asset_pnl(account, config, FUND_Q3_2026_PROFILES)
