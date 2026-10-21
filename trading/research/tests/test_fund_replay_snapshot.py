from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import advance
from trading.feed import HistoricalFeed
from trading.ledger import new_state
from trading.rules import FUND_PROFILES
from trading.research import fund_dayk, fund_replay_snapshot as source


DAYS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]


def events():
    return pd.DataFrame([{"ex_date": DAYS[2], "cash_rmb_per_unit": "0.010"}])


def fund_frame():
    rows = []
    for day in ["2023-12-29", *DAYS]:
        price = "3.927" if day >= DAYS[2] else "3.937"
        rows.append({"code": "510300", "date": day, **{key: price for key in ("open", "high", "low", "close")},
                     "volume": "1000000", "field7_amount_candidate": str(float(price) * 1000000)})
    return pd.DataFrame(rows)


def test_calculated_reference_and_adjustment_are_not_provider_relabels():
    frame = fund_frame()
    original = frame.copy(deep=True)
    raw, adjusted, provenance = source.calculated_bars(frame, events(), DAYS)
    assert raw.preclose.tolist() == [3.937, 3.937, 3.927, 3.927]
    assert raw.close.tolist() == [3.937, 3.937, 3.927, 3.927]
    assert adjusted.close.tolist() == [3.927] * 4
    assert raw.adjustflag.eq("3").all() and adjusted.adjustflag.eq("2").all()
    assert provenance["calculated_not_published_reference"]
    assert provenance["first_preceding_date"] == "2023-12-29"
    assert "not provider flag2" in provenance["adjustment"]
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("problem", ["missing", "preceding", "duplicate_action", "cash", "vwap", "volume"])
def test_bad_or_missing_inputs_do_not_create_bar_candidates(problem):
    frame, actions = fund_frame(), events()
    if problem == "missing":
        frame = frame.loc[frame.date.ne(DAYS[1])]
    elif problem == "preceding":
        frame = frame.loc[frame.date.ge(DAYS[0])]
    elif problem == "duplicate_action":
        actions = pd.concat([actions, actions])
    elif problem == "cash":
        actions["cash_rmb_per_unit"] = "5"
    elif problem == "vwap":
        frame.loc[frame.date.eq(DAYS[1]), "field7_amount_candidate"] = "1"
    else:
        frame.loc[frame.date.eq(DAYS[1]), "volume"] = "0"
    with pytest.raises(ValueError):
        source.calculated_bars(frame, actions, DAYS)


def test_future_cash_outside_declared_window_cannot_change_adjusted_prices():
    future = pd.concat([events(), pd.DataFrame([{"ex_date": "2026-01-19", "cash_rmb_per_unit": "100"}])])
    original = source.calculated_bars(fund_frame(), events(), DAYS)
    changed = source.calculated_bars(fund_frame(), future, DAYS)
    pd.testing.assert_frame_equal(original[0], changed[0])
    pd.testing.assert_frame_equal(original[1], changed[1])


def stock_fixture(folder):
    folder.mkdir()
    rows = pd.DataFrame([{"date": day, "symbol": "600000.SSE", "open": 10., "high": 10., "low": 10.,
                          "close": 10., "preclose": 10., "volume": 1000000, "amount": 10000000,
                          "tradestatus": 1, "isST": 0, "adjustflag": "3"} for day in DAYS])
    rows.to_parquet(folder / "raw.parquet", index=False)
    rows.assign(adjustflag="2").to_parquet(folder / "adjusted.parquet", index=False)
    pd.DataFrame({"date": DAYS}).to_csv(folder / "calendar.csv", index=False)
    pd.DataFrame([{"symbol": "600000.SSE", "ipoDate": "2000-01-01", "outDate": "", "board": "main"}]).to_csv(
        folder / "instruments.csv", index=False)
    pd.DataFrame(columns=["symbol", "dividRegistDate", "dividOperateDate", "dividPayDate", "dividCashPsBeforeTax",
                          "dividStocksPs", "dividReserveToStockPs"]).to_csv(folder / "actions.csv", index=False)
    (folder / "manifest.json").write_text(json.dumps({"status": "ready", "start": DAYS[0], "end": DAYS[-1],
                                                      "scope": "synthetic_fixture_not_real_data", "selection_date": "2023-12-29"}))
    return folder


def inputs(tmp_path, monkeypatch):
    stock = stock_fixture(tmp_path / "stock")
    dayk = tmp_path / "dayk"
    recipe = tmp_path / "recipe.js"
    recipe.write_bytes(b'fixture only: "dayk/"; res.kline[i][5]')
    frame = fund_frame()
    payload = {"code": "510300", "begin": 0, "end": 5, "total": 5, "kline": [
        [int(row["date"].replace("-", "")), *[float(row[key]) for key in ("open", "high", "low", "close")],
         int(row["volume"]), float(row["field7_amount_candidate"])] for row in frame.to_dict("records")]}
    monkeypatch.setattr(fund_dayk, "fetch", lambda *_: json.dumps(payload).encode())
    fund_dayk.collect(["510300"], recipe, dayk, session=object())
    sina, reports = tmp_path / "sina", tmp_path / "reports"
    (sina / "supplements").mkdir(parents=True)
    reports.mkdir()
    primary = b"%PDF-FIXTURE ONLY, not an issuer announcement"
    (sina / "cash.pdf").write_bytes(primary)
    (reports / "annual.pdf").write_bytes(primary)
    cash = events().assign(instrument_code="510300", exchange="SSE", announced_at=DAYS[0], record_date=DAYS[1],
                           pay_date=DAYS[3], source_file="cash.pdf", source_url="https://www.sse.com.cn/fixture.pdf",
                           source_sha256=sha256(primary).hexdigest())
    cash.to_csv(sina / "supplements/cash_events.csv", index=False)
    (reports / "distribution_periods.json").write_text(json.dumps([{
        "start": DAYS[0], "end": DAYS[-1], "source_url": "https://www.sse.com.cn/annual_fixture.pdf",
        "source_file": "annual.pdf", "source_sha256": sha256(primary).hexdigest(), "events": events().to_dict("records")}]))
    monkeypatch.setattr(source, "FIRST", DAYS[0])
    monkeypatch.setattr(source, "START", DAYS[1])
    monkeypatch.setattr(source, "LAST", DAYS[-1])
    return stock, dayk, sina, reports


def test_actual_mixed_reader_ledger_cash_entitlement_and_replay_only_boundary(tmp_path, monkeypatch):
    paths = inputs(tmp_path, monkeypatch)
    stock_manifest = (paths[0] / "manifest.json").read_bytes()
    folder, manifest = source.build(*paths, out=tmp_path / "mixed")
    assert manifest["scope"] == source.SCOPE and manifest["replay_only"]
    assert not manifest["execution_admitted"] and not manifest["strategy_validated"]
    assert manifest["stock_symbols"] == manifest["fund_symbols"] == 1
    assert manifest["fund_rows"] == 4 and manifest["fund_action_rows"] == 1
    feed = HistoricalFeed(folder)
    config = StrategyConfig(initial_cash=10000, holdings=2, retain_rank=2,
                            single_limit=.4, gross_limit=.8, slippage_bps=0).validate()
    state = new_state(config.initial_cash)
    state["pending_targets"] = {"510300.SSE": .4, "600000.SSE": .4}
    advance(state, feed, DAYS[1], config, 1)
    assert state["entitlements"]["510300.SSE:" + DAYS[2]] == 1000
    advance(state, feed, DAYS[2], config, 2)
    assert state["receivables"]["510300.SSE:" + DAYS[2]] == 1000
    cash_before_pay = state["cash"]
    advance(state, feed, DAYS[3], config, 3)
    assert state["cash"] == cash_before_pay + 1000 and state["receivables"] == {}
    assert state["price_units"] == {"510300.SSE": "mill"}
    assert (paths[0] / "manifest.json").read_bytes() == stock_manifest
    paper = new_state(config.initial_cash)
    before = deepcopy(paper)
    with pytest.raises(ValueError, match="historical-replay-only"):
        advance(paper, feed, DAYS[1], config, 1, context=RunContext("paper_fixture", TradingMode.PAPER))
    assert paper == before


def test_uncovered_distributions_fail_before_creating_ready_snapshot(tmp_path, monkeypatch):
    paths = inputs(tmp_path, monkeypatch)
    (paths[-1] / "distribution_periods.json").write_text("[]")
    with pytest.raises(ValueError, match="entire diagnostic"):
        source.build(*paths, out=tmp_path / "uncovered")
    assert not (tmp_path / "uncovered").exists()


def test_explicit_cutoff_does_not_bypass_distribution_coverage(tmp_path, monkeypatch):
    paths = inputs(tmp_path, monkeypatch)
    folder = paths[0]
    for name in ("raw.parquet", "adjusted.parquet"):
        frame = pd.read_parquet(folder / name)
        frame = pd.concat([frame, frame.iloc[-1:].assign(date="2024-01-08")], ignore_index=True)
        frame.to_parquet(folder / name, index=False)
    pd.DataFrame({"date": [*DAYS, "2024-01-08"]}).to_csv(folder / "calendar.csv", index=False)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["end"] = "2024-01-08"
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="entire diagnostic"):
        source.build(*paths, out=tmp_path / "uncovered_extension", end="2024-01-08")
    assert not (tmp_path / "uncovered_extension").exists()


@pytest.mark.parametrize("code,kind", [("588000", "equity_etf"), ("511010", "bond_etf"),
                                       ("518880", "gold_etf")])
def test_explicit_multiple_funds_keep_every_stock_and_own_cash_identity(tmp_path, monkeypatch, code, kind):
    paths = inputs(tmp_path, monkeypatch)
    old_load = source.load_dayk
    def load(folder):
        frame, receipt = old_load(folder)
        return pd.concat([frame, frame.assign(code=code)]), receipt
    monkeypatch.setattr(source, "load_dayk", load)
    reports = paths[-1]
    periods = json.loads((reports / "distribution_periods.json").read_text())
    periods.append({**periods[0], "instrument_code": code, "events": []})
    (reports / "distribution_periods.json").write_text(json.dumps(periods))
    review = {"instrument_type": kind, "ipoDate": "2020-11-16",
              "profile": FUND_PROFILES[code + ".SSE"],
              **{k: periods[0][k] for k in ("source_file", "source_sha256", "source_url")}}
    if kind != "equity_etf":
        review.update(rule_source_file=periods[0]["source_file"], rule_source_sha256=periods[0]["source_sha256"])
    (reports / "instrument_reviews.json").write_text(json.dumps({code: review}))
    folder, manifest = source.build(*paths, out=tmp_path / "multiple", codes=["510300", code])
    feed = HistoricalFeed(folder)
    assert set(feed.symbols) == {"600000.SSE", "510300.SSE", code + ".SSE"}
    assert feed.instruments[code + ".SSE"]["instrument_type"] == kind
    assert manifest["fund_symbols"] == 2 and manifest["stock_symbols"] == 1
    assert manifest["fund_rows"] == 8 and manifest["fund_action_rows"] == 1
    assert {a["symbol"] for a in feed.actions} == {"510300.SSE"}
    assert feed.raw.loc[feed.raw.symbol.eq(code + ".SSE"), "preclose"].tolist() == [3.937, 3.937, 3.937, 3.927]
    _, checked = source.probe(folder, out=tmp_path / "all_fund_probe", all_reviewed_funds=True)
    assert checked["fixed_components"] == {"510300.SSE": .4, code + ".SSE": .4}
    assert checked["saved_account_checks"]["stock_fills"] == 0
    rule_check = next(c for c in checked["saved_account_checks"]["checks"]
                      if c["check"] == "sales_and_final_quantities_from_profile_resale_rules")
    assert bool(rule_check["same_day_resale_fills"]) == (kind != "equity_etf")
    with pytest.raises(ValueError, match="reviewed fund"):
        source.build(*paths, out=tmp_path / "unsupported", codes=["513500"])
    if kind != "equity_etf":
        del review["rule_source_file"]
        (reports / "instrument_reviews.json").write_text(json.dumps({code: review}))
        with pytest.raises(ValueError, match="pinned historical exchange rule"):
            source.build(*paths, out=tmp_path / "missing_rule", codes=[code])
    assert not (tmp_path / "bond").exists()


def test_fixed_probe_uses_shared_replay_and_saved_cent_and_mill_cash_records(tmp_path, monkeypatch):
    paths = inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(source, "START", DAYS[0])
    dataset, _ = source.build(*paths, out=tmp_path / "mixed_probe")
    out, result = source.probe(dataset, tmp_path / "probe")
    assert result["status"] == "completed_real_price_mixed_ledger_diagnostic"
    checked = result["saved_account_checks"]
    assert checked["fund_fills"] > 0 and checked["stock_fills"] > 0
    assert len(checked["checks"][-1]["events"]) == 1
    assert all(check.get("residual_cents", 0) == 0 for check in checked["checks"])
    assert result["fixed_components"] == {"600000.SSE": .4, "510300.SSE": .4}
    assert result["sessions"] == 4 and not result["strategy_validated"]
    assert checked["checks"][0]["metrics"]["stale_valuation_days"] == 0
    daily = pd.read_csv(out / "ledger/daily.csv")
    daily.at[0, "stale_symbols"] = "['600000.SSE']"
    daily.to_csv(out / "ledger/daily.csv", index=False)
    with pytest.raises(ValueError, match="reported metrics"):
        source.saved_probe_checks(out / "ledger", HistoricalFeed(dataset), StrategyConfig(**result["config"]), result["metrics"])
    daily.at[0, "stale_symbols"] = "[]"
    daily.to_csv(out / "ledger/daily.csv", index=False)
    (out / "ledger/account.json").write_text((out / "ledger/account.json").read_text().replace('"cash": ', '"cash": 1'))
    with pytest.raises(ValueError):
        source.saved_probe_checks(out / "ledger", HistoricalFeed(dataset), StrategyConfig(**result["config"]), result["metrics"])
