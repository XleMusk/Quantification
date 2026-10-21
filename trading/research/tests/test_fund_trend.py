from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.research import fund_trend as source
from trading.research.monthly_timing import month_ends
from trading.rules import EQUITY_ETF_PROFILES, FUND_PROFILES, FUND_H1_2026_PROFILES
from evidence.research_comparison_20260930 import check_fund_trend as checker
from trading.research.experiment import baseline_ledger_result


SYMBOLS = ("510300.SSE", "588000.SSE")
CONTEXT = RunContext("fund_trend_fixture", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, signal="equal_weight", holdings=2, retain_rank=2,
                        single_limit=.4, gross_limit=.8, rebalance_sessions=1, slippage_bps=0).validate()


def feed():
    days = pd.bdate_range("2023-01-02", "2024-03-04").strftime("%Y-%m-%d").tolist()
    row = {"open": 1., "high": 1., "low": 1., "close": 1., "preclose": 1.,
           "tradestatus": 1, "isST": 0, "adjustflag": "3"}
    return SimpleNamespace(calendar=days, raw=pd.DataFrame([
        {"date": d, "symbol": s, "close": 1.} for d in days for s in SYMBOLS]), actions=[],
        rows={d: {s: dict(row) for s in SYMBOLS} for d in days},
        volumes=pd.DataFrame(1000000., index=days, columns=SYMBOLS),
        manifest={"start": "2024-01-02", "end": "2024-03-04", "replay_only": True},
        instrument_profiles={s: EQUITY_ETF_PROFILES[s] for s in SYMBOLS},
        instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in SYMBOLS},
        next_day=lambda d: days[days.index(d) + 1])


def test_total_return_dividend_is_causal_not_rounded_reference_or_qfq():
    base = feed()
    ex = "2024-01-15"
    base.raw.loc[base.raw.date.ge(ex), "close"] = .95
    base.actions = [{"symbol": s, "dividOperateDate": ex, "dividPayDate": "2024-01-18",
                     "dividCashPsBeforeTax": ".05"} for s in SYMBOLS]
    actual = source.total_return_table(base, SYMBOLS, "2024-02-29")
    assert actual.index_close.eq(1).all()
    assert actual.loc[actual.date.eq("2024-01-31"), "gate"].eq("equal_no_change").all()
    changed = deepcopy(base)
    changed.raw.loc[changed.raw.date.ge("2024-03-01"), "close"] = 999.
    changed.actions.append({"symbol": SYMBOLS[0], "dividOperateDate": "2026-01-19", "dividCashPsBeforeTax": "999"})
    pd.testing.assert_frame_equal(actual, source.total_return_table(changed, SYMBOLS, "2024-02-29"))
    # Missing real bars fail rather than bridge an ex-date or fill prices.
    base.raw = base.raw.loc[~(base.raw.date.eq(ex) & base.raw.symbol.eq(SYMBOLS[0]))]
    with pytest.raises(ValueError, match="Every declared"):
        source.total_return_table(base, SYMBOLS, "2024-02-29")


def test_monthly_exit_equal_retains_prior_state_and_no_future_feedback():
    base = feed()
    ends = month_ends(base.calendar, "2024-02-29")
    for index, day in enumerate(ends):
        base.raw.loc[base.raw.date.eq(day) & base.raw.symbol.eq(SYMBOLS[0]), "close"] = 1 + index / 10
    table = source.total_return_table(base, SYMBOLS, "2024-02-29")
    assert table.loc[table.date.eq("2023-08-31"), "gate"].eq("warmup").all()
    assert table.loc[table.date.eq("2024-01-31") & table.symbol.eq(SYMBOLS[0]), "desired_on"].item()
    assert not table.loc[table.date.eq("2024-01-31") & table.symbol.eq(SYMBOLS[1]), "desired_on"].item()
    wrapper = source.FundTrendFeed(base, table, source.VARIANTS[1], SYMBOLS, "2024-02-29")
    assert wrapper.signal_frame("2024-01-30", CONFIG, CONTEXT) == SignalUnavailable("not_month_end")
    candidates = wrapper.signal_frame("2024-01-31", CONFIG, CONTEXT).ranked_candidates()
    assert candidates == [{"symbol": SYMBOLS[0], "score": 1.}]
    with pytest.raises(ValueError, match="paper/live"):
        wrapper.signal_frame("2024-01-31", CONFIG, RunContext("paper", TradingMode.PAPER))
    with pytest.raises(ValueError, match="fixed sleeves"):
        wrapper.signal_frame("2024-01-31", replace(CONFIG, holdings=1), CONTEXT)
    with pytest.raises(ValueError, match="cutoff"):
        wrapper.signal_frame("2024-03-01", CONFIG, CONTEXT)


def test_h1_extension_keeps_old_signals_and_does_not_read_future_cash_or_prices():
    base = feed()
    days = pd.bdate_range("2023-01-02", "2026-07-03").strftime("%Y-%m-%d").tolist()
    base.calendar = days
    base.manifest["end"] = "2026-06-30"
    base.raw = pd.DataFrame([{"date": day, "symbol": symbol, "close": 2. if day >= "2026-01-01" else 1.}
                             for day in days for symbol in SYMBOLS])
    base.instrument_profiles = {s: FUND_H1_2026_PROFILES[s] for s in SYMBOLS}
    old = source.total_return_table(base, SYMBOLS)
    table = source.total_return_table(base, SYMBOLS, "2026-06-30")
    pd.testing.assert_frame_equal(old, table.loc[table.date.le(source.END)].reset_index(drop=True))
    changed = deepcopy(base)
    changed.raw.loc[changed.raw.date.gt("2026-06-30"), "close"] = 999.
    changed.actions.append({"symbol": SYMBOLS[0], "dividOperateDate": "2026-07-01",
                            "dividCashPsBeforeTax": "999"})
    pd.testing.assert_frame_equal(table, source.total_return_table(changed, SYMBOLS, "2026-06-30"))
    wrapper = source.FundTrendFeed(base, table, source.VARIANTS[1], SYMBOLS, "2026-06-30")
    assert len(wrapper.signal_frame("2026-06-30", CONFIG, CONTEXT).ranked_candidates()) == 2
    with pytest.raises(ValueError, match="cutoff"):
        wrapper.signal_frame("2026-07-01", CONFIG, CONTEXT)
    with pytest.raises(ValueError, match="period boundary"):
        source.FundTrendFeed(base, table, source.VARIANTS[1], SYMBOLS, "2026-07-01")


def test_extension_metrics_use_prior_equity_and_parse_saved_stale_lists(tmp_path):
    pd.DataFrame([{"date": day, "equity": equity, "fees": 1., "turnover_value": 100., "stale_symbols": stale}
                  for day, equity, stale in [("2025-12-30", 190., ["510300.SSE"]),
                                             ("2025-12-31", 200., []), ("2026-01-05", 220., [])]]).to_csv(
        tmp_path / "daily.csv", index=False)
    values = source.extended_period_metrics(tmp_path, 100.)
    assert values["2024_2025_same_account_prefix"]["net_return"] == 1.
    assert values["2024_2025_same_account_prefix"]["stale_valuation_days"] == 1
    assert values["2026_same_account_extension"]["net_return"] == pytest.approx(.1)
    assert values["2026_same_account_extension"]["stale_valuation_days"] == 0
    assert values["2026_same_account_extension"]["fees"] == 1.


def test_actual_shared_engine_keeps_idle_sleeve_cash_and_trades_next_session():
    base = feed()
    table = pd.DataFrame([{"date": d, "symbol": s, "gate": "risk_on" if on else "risk_off",
                          "index_close": 1., "sma_10_months": .9, "desired_on": on}
                         for d, on in [("2024-01-31", True), ("2024-02-29", False)] for s in SYMBOLS])
    table.loc[table.symbol.eq(SYMBOLS[1]), "desired_on"] = False
    wrapper = source.FundTrendFeed(base, table, source.VARIANTS[1], SYMBOLS, "2024-03-01")
    state = new_state(CONFIG.initial_cash)
    for count, day in enumerate(d for d in base.calendar if "2024-01-31" <= d <= "2024-03-01"):
        advance(state, wrapper, day, CONFIG, count)
        if day == "2024-01-31":
            assert state["fills"] == {} and state["pending_targets"] == {SYMBOLS[0]: .4}
        if day == "2024-02-01":
            assert sum(lot["qty"] for lot in state["positions"][SYMBOLS[0]]) == 4000
            assert state["cash"] > 590000
    fills = list(state["fills"].values())
    assert {f["symbol"] for f in fills} == {SYMBOLS[0]}
    assert {f["day"] for f in fills} == {"2024-02-01", "2024-03-01"}
    assert {f["price_unit"] for f in fills} == {"mill"}
    assert not any(state["positions"].values())


def test_three_class_recipe_uses_existing_engine_and_dated_resale_profiles():
    base = feed()
    symbols = source.CROSS_ASSET_SYMBOLS
    config = replace(CONFIG, holdings=3, retain_rank=3, single_limit=.8 / 3).validate()
    base.rows = {d: {s: dict(next(iter(rows.values()))) for s in symbols} for d, rows in base.rows.items()}
    base.volumes = pd.DataFrame(1000000., index=base.calendar, columns=symbols)
    base.instruments = {s: {"ipoDate": "2000-01-01", "outDate": ""} for s in symbols}
    base.instrument_profiles = {s: FUND_PROFILES[s] for s in symbols}
    table = pd.DataFrame([{"date": d, "symbol": s, "gate": "risk_on" if on else "risk_off",
                          "index_close": 1., "sma_10_months": .9, "desired_on": on}
                         for d, on in [("2024-01-31", True), ("2024-02-29", False)] for s in symbols])
    # Gold's inactive sleeve stays cash rather than increasing equity/bond weights.
    table.loc[table.symbol.eq("518880.SSE"), "desired_on"] = False
    wrapper = source.FundTrendFeed(base, table, source.VARIANTS[1], symbols, "2024-03-01")
    state = new_state(config.initial_cash)
    advance(state, wrapper, "2024-01-31", config, 0)
    assert state["fills"] == {}
    assert state["pending_targets"] == {s: .8 / 3 for s in symbols[:2]}
    advance(state, wrapper, "2024-02-01", config, 1)
    bought = {f["symbol"]: f for f in state["fills"].values()}
    assert bought["510300.SSE"]["available_day"] == "2024-02-02"
    assert bought["511010.SSE"]["available_day"] == "2024-02-01"
    assert "518880.SSE" not in bought and state["cash"] > 460000
    advance(state, wrapper, "2024-02-29", config, 2)
    advance(state, wrapper, "2024-03-01", config, 3)
    assert not any(state["positions"].values())
    assert {f["day"] for f in state["fills"].values()} == {"2024-02-01", "2024-03-01"}
    fixed = source.FundTrendFeed(base, table, source.VARIANTS[0], symbols, "2024-03-01")
    assert {r["symbol"] for r in fixed.signal_frame("2024-01-31", config, CONTEXT).ranked_candidates()} == set(symbols)
    assert source.CROSS_ASSET_INPUTS == set(source.SYMBOLS) | {"511010.SSE", "518880.SSE"}
    assert len(source.SYMBOLS) == 6


@pytest.mark.parametrize("tamper", [None, "availability", "stale_symbols"])
def test_saved_account_checker_recomputes_metrics_and_declared_resale(tmp_path, monkeypatch, tamper):
    base = feed()
    symbols = ("510300.SSE", "511010.SSE")
    base.rows = {d: {s: dict(next(iter(rows.values()))) for s in symbols} for d, rows in base.rows.items()}
    base.volumes = pd.DataFrame(1000000., index=base.calendar, columns=symbols)
    base.instruments = {s: {"ipoDate": "2000-01-01", "outDate": ""} for s in symbols}
    base.instrument_profiles = {s: FUND_PROFILES[s] for s in symbols}
    base.folder = tmp_path / "snapshot"
    base.folder.mkdir()
    base.manifest["file_hashes"] = {}
    manifest = base.folder / "manifest.json"
    manifest.write_text(json.dumps(base.manifest), encoding="utf-8")
    table = pd.DataFrame([{"date": "2024-01-31", "symbol": s, "gate": "risk_on",
                          "index_close": 1., "sma_10_months": .9, "desired_on": True} for s in symbols])
    wrapper = source.FundTrendFeed(base, table, source.VARIANTS[1], symbols, "2024-02-02")
    artifact = tmp_path / "accounts"
    artifact.mkdir()
    folder = artifact / source.VARIANTS[1]
    result = baseline_ledger_result(wrapper, CONFIG, ["2024-01-31", "2024-02-01", "2024-02-02"], folder)
    assert result["status"] == "completed"
    result["signal"] = source.VARIANTS[1]
    protocol = {"dataset": str(base.folder), "dataset_manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
                "start": "2024-01-31", "end": "2024-02-02", "symbols": symbols, "config": CONFIG.to_dict()}
    out = tmp_path / "out"
    out.mkdir()
    (out / "run.json").write_text(json.dumps({"status": "completed_fund_trend_diagnostic",
        "artifacts": str(artifact), "protocol": protocol, "results": [result]}), encoding="utf-8")
    monkeypatch.setattr(checker, "HistoricalFeed", lambda _: base)
    if tamper == "availability":
        account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
        for fill in account["fills"].values():
            if fill["symbol"] == "511010.SSE":
                fill["available_day"] = "2024-02-02"
        (folder / "account.json").write_text(json.dumps(account), encoding="utf-8")
    elif tamper == "stale_symbols":
        daily = pd.read_csv(folder / "daily.csv")
        daily.loc[1, "stale_symbols"] = "['511010.SSE']"
        daily.to_csv(folder / "daily.csv", index=False)
    if tamper is None:
        checked = checker.check(out)
        assert checked["variants"][0]["cash_residual_cents"] == 0
        assert checked["variants"][0]["equity_residual_cents"] == 0
    else:
        with pytest.raises(ValueError, match="availability|metrics differ"):
            checker.check(out)
