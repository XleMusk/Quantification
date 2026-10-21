"""Canonical daily-field semantics across saved data, preview and window reads."""

import numpy as np
import pandas as pd
import pytest

from webapp.backend.app import data, settings, store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for module in (settings, store, data):
        monkeypatch.setattr(module, "STORAGE", tmp_path / "research")
    store.initialize()


def panel():
    return pd.DataFrame({"date": pd.to_datetime(["2026-10-16", "2026-10-19"]),
                         "symbol": "600000.SSE", "open": [11., 12.], "high": [12., 13.],
                         "low": [10., 11.], "close": [11., 12.], "volume": 10000.,
                         "amount": [110000., 120000.], "turnover_rate_pct": [.15, .16],
                         "pre_close": [10.9, None], "pe_ttm": [-2., None], "pb": [1.1, None],
                         "total_mv": [1e9, None], "float_mv": [9e8, None],
                         "is_st": [False, None], "name": [" 浦发银行 ", None]})


def register(frame):
    return data.register(frame, "extended", "historical", "saved StockDB daily rows", "raw")


def test_extended_fields_preserve_cash_rate_nulls_and_declared_units():
    item = register(panel())
    saved = data.read_bars(item["id"])
    assert saved.amount.iloc[-1] == 120000
    assert saved.turnover_rate_pct.iloc[-1] == .16
    assert saved.amount.iloc[-1] / saved.volume.iloc[-1] == 12
    assert "turnover" not in saved
    assert pd.isna(saved.pe_ttm.iloc[-1]) and pd.isna(saved.is_st.iloc[-1])
    assert saved.name.iloc[0] == "浦发银行"
    assert item["field_units"]["amount"] == "CNY"
    assert item["field_units"]["volume"] == "share"
    assert item["field_units"]["turnover_rate_pct"] == "percent"
    assert item["field_coverage"]["pre_close"] == .5
    assert item["pit_status"] == "not_verified"
    assert any("历史可得时间" in warning for warning in item["warnings"])


def test_ambiguous_turnover_cannot_silently_become_cash_amount():
    with pytest.raises(ValueError, match="turnover 含义不明确"):
        register(panel().assign(turnover=.16))


@pytest.mark.parametrize("field,value", [("amount", -1), ("amount", np.inf),
                                        ("turnover_rate_pct", -1), ("total_mv", -1),
                                        ("is_st", "unknown")])
def test_malformed_optional_values_are_not_silently_replaced(field, value):
    frame = panel()
    frame[field] = value
    with pytest.raises(ValueError):
        register(frame)


def test_latest_preserves_optional_nulls_and_uses_observed_previous_close():
    item = register(panel())
    row = data.read_latest(item["id"])[0]
    assert row["amount"] == 120000 and row["turnover_rate_pct"] == .16
    assert row["pre_close"] is None and row["is_st"] is None
    assert row["previous_close"] == 11
    assert row["change_pct"] == pytest.approx(12 / 11 - 1)


def test_window_reads_filter_dates_symbol_and_project_before_loading():
    frame = pd.concat([panel(), panel().assign(symbol="000001.SZSE")], ignore_index=True)
    item = register(frame)
    saved = data.read_bars(item["id"], symbol="600000.SSE", start="2026-10-19", end="2026-10-19",
                           columns=["date", "symbol", "amount"])
    assert list(saved.columns) == ["date", "symbol", "amount"]
    assert saved.symbol.tolist() == ["600000.SSE"] and saved.amount.tolist() == [120000.]
    with pytest.raises(ValueError, match="日频日期"):
        data.read_bars(item["id"], start="2026-10-19T10:15:00")


def test_latest_cache_prevents_large_history_read(monkeypatch):
    item = register(panel())
    monkeypatch.setattr(data, "read_bars", lambda *args, **kwargs: pytest.fail("latest must use compact cache"))
    assert data.read_latest(item["id"])[0]["close"] == 12


def test_chunk_normalization_does_not_relax_csv_symbol_limit():
    frame = pd.concat([panel().assign(symbol=f"S{i:03}") for i in range(101)], ignore_index=True)
    normalized, invalid = data.normalize_daily_fields(frame)
    assert len(normalized) == 202 and not invalid.any()
    with pytest.raises(ValueError, match="最多 100"):
        data.validate_panel(frame)
