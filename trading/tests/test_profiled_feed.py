from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import advance
from trading.feed import HistoricalFeed
from trading.ledger import Ledger, new_state
from trading.rules import ETF_PROFILE


FUND, STOCK = "510300.SSE", "600000.SSE"
DAYS = ["2024-01-02", "2024-01-03", "2024-01-04"]


def fixture(folder, *, action=False):
    folder.mkdir()
    rows = []
    for number, day in enumerate(DAYS):
        for symbol, price in ((FUND, 3.937), (STOCK, 10.0)):
            reference = price - (.001 if action and symbol == FUND and number == 1 else 0)
            rows.append({"date": day, "symbol": symbol, "open": price, "high": price,
                         "low": price, "close": price, "preclose": reference, "volume": 1000000,
                         "amount": price * 1000000, "tradestatus": 1, "isST": 0, "adjustflag": "3"})
    raw = pd.DataFrame(rows)
    raw.to_parquet(folder / "raw.parquet", index=False)
    raw.assign(adjustflag="2").to_parquet(folder / "adjusted.parquet", index=False)
    pd.DataFrame({"date": DAYS}).to_csv(folder / "calendar.csv", index=False)
    pd.DataFrame([{"symbol": FUND, "board": "", "instrument_type": "equity_etf", "ipoDate": "2012-05-28", "outDate": ""},
                  {"symbol": STOCK, "board": "main", "instrument_type": "ordinary_equity", "ipoDate": "2000-01-01", "outDate": ""}]).to_csv(
        folder / "instruments.csv", index=False)
    actions = []
    if action:
        primary = b"%PDF-FIXTURE ONLY; not an issuer document or historical strategy result"
        (folder / "cash.pdf").write_bytes(primary)
        actions.append({"symbol": FUND, "dividRegistDate": DAYS[0], "dividOperateDate": DAYS[1],
                        "dividPayDate": DAYS[2], "dividCashPsBeforeTax": ".001", "dividStocksPs": "0",
                        "dividReserveToStockPs": "0", "source_file": "cash.pdf", "source_url": "https://www.sse.com.cn/fixture.pdf",
                        "source_sha256": sha256(primary).hexdigest()})
    pd.DataFrame(actions, columns=["symbol", "dividRegistDate", "dividOperateDate", "dividPayDate",
                                  "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs",
                                  "source_file", "source_url", "source_sha256"]).to_csv(folder / "actions.csv", index=False)
    manifest = {"status": "ready", "start": DAYS[0], "end": DAYS[-1], "selection_date": "2023-12-29",
                "scope": "synthetic_engine_fixture_not_real_data", "execution_admitted": False,
                "instrument_profiles": {FUND: ETF_PROFILE}}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return folder


def test_actual_file_reader_passes_mixed_units_to_existing_engine_and_ledger(tmp_path):
    feed = HistoricalFeed(fixture(tmp_path / "mixed"))
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2, single_limit=.4,
                            gross_limit=.8, slippage_bps=0).validate()
    state = new_state(config.initial_cash)
    state["pending_targets"] = {FUND: .4, STOCK: .4}
    context = RunContext("file_reader_fixture", TradingMode.REPLAY)
    advance(state, feed, DAYS[1], config, 1, context=context)
    ledger = Ledger(state, config, context, instrument_profiles=feed.instrument_profiles)
    assert ledger.quantity(FUND) == 1000 and ledger.quantity(STOCK) == 400
    assert state["price_units"] == {FUND: "mill"}
    assert state["prices"][FUND] == 3937 and state["prices"][STOCK] == 1000
    assert ledger.available(FUND, DAYS[1]) == 0 and ledger.available(FUND, DAYS[2]) == 1000
    assert ledger.equity() == 998996
    assert all(order["status"] == "filled" for order in state["orders"].values())
    assert not feed.manifest["execution_admitted"]


@pytest.mark.parametrize("problem", ["missing", "unknown", "wrong_type", "wrong_board", "past_span", "not_map"])
def test_profile_is_explicit_and_never_inferred_from_code_or_quote(tmp_path, problem):
    folder = fixture(tmp_path / problem)
    manifest = json.loads((folder / "manifest.json").read_text())
    if problem == "missing":
        manifest.pop("instrument_profiles")
    elif problem == "unknown":
        manifest["instrument_profiles"][FUND] = "infer_any_fund"
    elif problem == "past_span":
        manifest["end"] = "2026-10-01"
    elif problem == "not_map":
        manifest["instrument_profiles"] = [FUND]
    else:
        items = pd.read_csv(folder / "instruments.csv", keep_default_na=False)
        items.loc[items.symbol.eq(FUND), "instrument_type" if problem == "wrong_type" else "board"] = "main"
        items.to_csv(folder / "instruments.csv", index=False)
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        HistoricalFeed(folder)


@pytest.mark.parametrize("field,value", [("open", 3.9371), ("isST", 1)])
def test_source_fund_flags_and_submill_quotes_cannot_be_silently_normalized(tmp_path, field, value):
    folder = fixture(tmp_path / "invalid")
    raw = pd.read_parquet(folder / "raw.parquet")
    raw.loc[raw.symbol.eq(FUND), field] = value
    raw.to_parquet(folder / "raw.parquet", index=False)
    before = (folder / "raw.parquet").read_bytes()
    with pytest.raises(ValueError):
        HistoricalFeed(folder)
    assert (folder / "raw.parquet").read_bytes() == before


def test_small_cash_reference_requires_pinned_action_not_stock_cent_tolerance(tmp_path):
    folder = fixture(tmp_path / "cash", action=True)
    feed = HistoricalFeed(folder)
    state, config = new_state(10000), StrategyConfig()
    state["positions"][FUND] = [{"qty": 100, "available": DAYS[0]}]
    context = RunContext("cash_fixture", TradingMode.REPLAY)
    advance(state, feed, DAYS[0], config, 1, context=context)
    advance(state, feed, DAYS[1], config, 1, context=context)
    assert sum(state["receivables"].values()) == 10
    advance(state, feed, DAYS[2], config, 1, context=context)
    assert not state["receivables"] and state["cash"] == 1000010
    feed.actions = []
    with pytest.raises(ValueError, match="Unexplained price adjustment"):
        feed.validate()


@pytest.mark.parametrize("problem", ["wrong_amount", "changed_pdf", "stock_bonus"])
def test_fund_cash_source_or_economic_conflict_is_rejected(tmp_path, problem):
    feed = HistoricalFeed(fixture(tmp_path / problem, action=True))
    before = deepcopy(feed.actions)
    if problem == "changed_pdf":
        (feed.folder / "cash.pdf").write_bytes(b"%PDF-changed")
    elif problem == "stock_bonus":
        feed.actions[0]["dividStocksPs"] = .1
    else:
        feed.actions[0]["dividCashPsBeforeTax"] = .002
    with pytest.raises(ValueError):
        feed.validate()
    assert before[0]["dividCashPsBeforeTax"] == .001
