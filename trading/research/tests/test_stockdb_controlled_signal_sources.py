"""Only new context/source and fixed-mode contracts, on synthetic small rows."""
from pathlib import Path

import pandas as pd
import pytest

from trading.research import stockdb_controlled_signal_comparison as m
from trading.research.tests.test_stockdb_controlled_signal_comparison import inputs


A, B = "600000.SSE", "000001.SZSE"
JAN, OCT = "2026-01-05", "2026-10-09"


def native_row(day, symbol, *, halted=False):
    return {"date": day, "symbol": symbol, "open": "18.6900", "high": "18.6900",
        "low": "18.6900", "close": "18.6900", "preclose": "18.6900",
        "volume": "" if halted else "100", "amount": "" if halted else "1869",
        "tradestatus": "0" if halted else "1", "isST": "0", "adjustflag": "3"}


def test_january_context_uses_previous_21_provider_sessions_without_verifying_2025():
    provider = [d for d in pd.bdate_range("2025-11-01", "2026-01-06").strftime("%Y-%m-%d")
                if d not in {"2026-01-01", "2026-01-02"}]
    start, context = m.raw_history_context(provider, JAN)
    index = provider.index(JAN)
    assert start == provider[index-21] and start.startswith("2025-")
    assert context["preceding_provider_sessions"] == provider[index-21:index]
    assert context["preceding_provider_session_count"] == 21
    assert context["execution_calendar_year"] == 2026
    assert context["prior_year_context_years"] == [2025]
    assert context["prior_year_exchange_calendar_independently_verified"] is False
    with pytest.raises(ValueError, match="preceding 21"):
        m.raw_history_context([JAN, "2026-01-06"], JAN)


def fake_sources(monkeypatch, tmp_path):
    calls, opened = [], []
    paths = [tmp_path / "base908", tmp_path / "october4"]
    data = {paths[0].resolve(): (["2025-12-31", JAN, "2026-10-08"], [native_row(JAN, B, halted=True)]),
            paths[1].resolve(): (["2026-10-08", OCT], [native_row(OCT, A)])}
    class Source:
        def __init__(self, path):
            self.path = path; self.calendar, self.rows = data[path]
            self.provenance = {"source_view": str(path), "synthetic_only": True}
            opened.append(path)
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def frame(self, day, symbols):
            calls.append((self.path, day, list(symbols)))
            return pd.DataFrame([row for row in self.rows if row["date"] == day and row["symbol"] in symbols])
    monkeypatch.setattr(m, "NativeHistorySource", Source)
    raw = pd.DataFrame([{**native_row(JAN, A), "open": None, "volume": 123.0},
                        {**native_row(OCT, B), "open": 7.0, "volume": 456.0}])
    return paths, raw, calls, opened


def test_two_explicit_sources_only_fill_requested_missing_keys_and_preserve_nulls(tmp_path, monkeypatch):
    paths, raw, calls, _ = fake_sources(monkeypatch, tmp_path)
    result, proofs, rows = m.append_native_sources(raw, paths, [A, B], [JAN, OCT])
    assert len(rows) == 2 and len(result) == 4
    original = result.loc[result.date.eq(JAN) & result.symbol.eq(A)].iloc[0]
    assert pd.isna(original.open) and original.volume == 123
    assert result.loc[result.date.eq(OCT) & result.symbol.eq(B), "open"].item() == 7
    halt = result.loc[result.date.eq(JAN) & result.symbol.eq(B)].iloc[0]
    assert halt.tradestatus == 0 and pd.isna(halt.volume) and pd.isna(halt.amount)
    assert calls == [(paths[0].resolve(), JAN, [B]), (paths[1].resolve(), OCT, [A])]
    assert [p["read_days"] for p in proofs] == [[JAN], [OCT]]
    assert [p["source_view"] for p in proofs] == [str(p.resolve()) for p in paths]
    # One path remains a supported direct-Python call, rather than being
    # iterated as its characters or requiring a new list-only contract.
    _, single, _ = m.append_native_sources(raw, paths[0], [A, B], [JAN])
    assert len(single) == 1 and single[0]["read_days"] == [JAN]


def test_duplicate_source_or_requested_day_overlap_rejects_before_any_price_read(tmp_path, monkeypatch):
    paths, raw, calls, opened = fake_sources(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="Duplicate"):
        m.append_native_sources(raw, [paths[0], paths[0]], [A, B], [JAN])
    assert opened == calls == []
    with pytest.raises(ValueError, match="Overlapping.*2026-10-08"):
        m.append_native_sources(raw, paths, [A, B], [JAN, "2026-10-08", OCT])
    assert calls == []


def test_corrected_only_runs_fixed_two_arms_without_new_global_comparison(tmp_path, monkeypatch):
    fixture = inputs()
    monkeypatch.setattr(m, "_sources", lambda *args: fixture)
    seen = []
    def advance_without_engine(state, feed, day, config, index, *, context):
        seen.append(feed.arm)
        return {"date": day, "equity": config.initial_cash, "cash": config.initial_cash}
    monkeypatch.setattr(m, "advance", advance_without_engine)
    result = m.run("synthetic", "synthetic", "synthetic", tmp_path / "two-arms", corrected_only=True)
    assert [row["arm"] for row in result["results"]] == list(m.ARMS[:2])
    assert set(seen) == set(m.ARMS[:2]) and m.ARMS[2] not in seen
    assert result["method"]["comparison_mode"] == "fixed_rule_and_day_local_only"
    assert result["method"]["legacy_global_diagnostic_enabled"] is False
    assert result["rank_impact"] == [] and result["model_refit"] is False
    assert not (tmp_path / "two-arms" / m.ARMS[2]).exists()
