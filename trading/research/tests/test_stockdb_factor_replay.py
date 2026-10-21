from types import SimpleNamespace
import json

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.research.low_risk import portfolio
from trading.research.stockdb_factor_diagnostics import BLEND, DIRECTIONS
from trading.research.stockdb_factor_replay import CASES, HistoricalFactorFeed, monthly_ranks


CALENDAR = ["2023-01-31", "2023-02-01", "2023-02-28", "2023-03-01",
            "2023-03-31", "2023-12-29", "2024-01-02"]


def scores():
    symbols = [f"600{i:03d}.SSE" for i in range(40)]
    index = pd.MultiIndex.from_product([pd.to_datetime(["2023-02-28", "2023-03-31"]), symbols],
                                      names=["date", "symbol"])
    return pd.DataFrame({name: np.tile(np.arange(40, dtype=float), 2)
                         for name in (*DIRECTIONS, BLEND)}, index=index)


def test_monthly_common_pool_and_no_out_of_period_execution():
    frame = scores()
    frame.loc[(pd.Timestamp("2023-02-28"), "600039.SSE"), "earnings_to_price_candidate"] = np.nan
    ranks, counts, unavailable = monthly_ranks(frame, CALENDAR, portfolio())
    assert counts == [{"date": "2023-02-28", "common_rows": 39}, {"date": "2023-03-31", "common_rows": 40}]
    assert unavailable == {"2023-01-31": "saved_liquidity_warmup_unknown",
                           "2023-12-29": "next_execution_session_outside_fixed_period"}
    assert ranks.loc[ranks.date.eq("2023-02-28"), "symbol"].eq("600039.SSE").sum() == 0
    assert ranks.groupby(["date", "case"]).size().eq(30).all()
    assert set(ranks.case) == set(CASES)


def test_missing_after_warmup_is_not_silently_skipped():
    with pytest.raises(ValueError, match="Missing fixed monthly"):
        monthly_ranks(scores().loc[[pd.Timestamp("2023-02-28")]], CALENDAR, portfolio())


def test_rank_ties_are_deterministic_and_mapping_keeps_source_identity():
    frame = scores().rename(index={"600039.SSE": "300114.SZSE"}, level="symbol")
    ranks, _, _ = monthly_ranks(frame, CALENDAR, portfolio(), {"old": "sz.300114", "new": "sz.302132"})
    alias = ranks.loc[ranks.security_identity.eq("300114.SZSE")]
    assert len(alias) == 4 and alias.symbol.eq("302132.SZSE").all()
    frame[:] = 1.
    first, _, _ = monthly_ranks(frame, CALENDAR, portfolio())
    second, _, _ = monthly_ranks(frame.iloc[::-1], CALENDAR, portfolio())
    pd.testing.assert_frame_equal(first, second)


def test_saved_feed_replay_contract_and_inherited_guards():
    config = portfolio()
    ranks, _, unavailable = monthly_ranks(scores(), CALENDAR, config)
    marker = object()
    base = SimpleNamespace(calendar=CALENDAR, symbols=sorted(set(ranks.symbol)),
                           manifest={"start": "2024-01-01", "execution_admitted": False}, validate_session=marker)
    feed = HistoricalFactorFeed(base, ranks, BLEND, config, unavailable)
    context = RunContext("test2023", TradingMode.REPLAY)
    assert feed.validate_session is marker
    assert feed.manifest["start"] == "2023-01-31" and base.manifest["start"] == "2024-01-01"
    assert isinstance(feed.signal_frame("2023-02-01", config, context), SignalUnavailable)
    assert len(feed.signal_frame("2023-02-28", config, context).signals) == 30
    with pytest.raises(ValueError, match="replay-only"):
        feed.signal_frame("2023-02-28", config, RunContext("paper", TradingMode.PAPER))


def test_action_preparation_reuses_both_buffers_and_defers_failed_sources(tmp_path, monkeypatch):
    from trading.research import action_events, action_terms, value_portfolios

    root, data = tmp_path / "repo", tmp_path / "data"
    inputs = {name: str(tmp_path / name) for name in ("event_view", "reference_view")}
    primary = tmp_path / "primary"
    for path in (primary, tmp_path / "event_view"):
        path.mkdir()
        (path / "result.json").write_text(json.dumps({"raw_view": str(tmp_path / "raw")}))
    pending = pd.DataFrame([
        {"symbol": symbol, "dividOperateDate": date, "dividReserveToStockPs": ""}
        for symbol, date in (("600039.SSE", "2023-05-11"), ("600038.SSE", "2023-05-12"),
                             ("600037.SSE", "2023-05-16"), ("600037.SSE", "2026-05-16"),
                             ("600050.SSE", "2023-05-17"))])
    pending.to_parquet(tmp_path / "event_view/canonical_actions.parquet", index=False)
    artifacts = tmp_path / "ranks"
    artifacts.mkdir()
    ranks, _, _ = monthly_ranks(scores(), CALENDAR, portfolio())
    ranks.to_csv(artifacts / "rankings.csv", index=False)
    source = {"status": "blocked_factor_ledger_diagnostics", "artifacts": str(artifacts),
              "start": "2023-01-01", "end": "2023-12-31", "execution_admitted": False,
              "plan": {"start": "2023-01-01", "end": "2023-12-31", "cases": [CASES[1]],
                       "portfolio": portfolio().to_dict(), "stock_inputs": inputs, "execution_admitted": False},
              "results": [{"error": "Unresolved held corporate action: 600037.SSE 2023-05-16"}]}
    source_path = root / "outputs/stockdb_factor_replay/fixture/run.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(json.dumps(source))
    archive = data / "research/secondary_action_terms/archive"
    archive.mkdir(parents=True)
    (archive / "result.json").write_text(json.dumps({"status": "completed_secondary_action_terms", "records": [
        {"symbol": "600039.SSE", "status": "source_histories_compared"},
        {"symbol": "600038.SSE", "status": "source_error"}]}))
    monkeypatch.setattr(value_portfolios, "ROOT", root)
    monkeypatch.setattr(value_portfolios, "DATA_ROOT", data)
    calls, amendments = [], []

    def collect(primary_view, symbols, *, reuse_root, retain_archived):
        calls.append((list(symbols), reuse_root))
        assert primary_view == primary and retain_archived is False
        return tmp_path / "terms", {"records": [{"candidates": [
            {"symbol": s, "date": d} for s, d in zip(pending.symbol, pending.dividOperateDate) if s in symbols]}],
            "reused_symbols": len(symbols) if reuse_root else 0, "source_errors": 0}

    def amend(event, reference, terms, matched):
        amendments.extend(matched)
        return tmp_path / "amended", tmp_path / "amended_references", {}, {}

    monkeypatch.setattr(action_terms, "collect", collect)
    monkeypatch.setattr(action_events, "amend_secondary", amend)
    result = value_portfolios.prepare_actions("fixture", primary, 1, stockdb=True)
    assert calls == [(["600039.SSE"], archive), (["600037.SSE"], None)]
    assert set(amendments) == {("600039.SSE", "2023-05-11"), ("600037.SSE", "2023-05-16")}
    assert result["pending_events"] == 1
    assert result["deferred_source_error_symbols"] == ["600038.SSE"]
    assert result["execution_admitted"] is False
    assert json.loads(source_path.read_text()) == source
