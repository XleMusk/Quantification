"""Small native-source fixtures, with no real history, Engine or networking."""
from copy import deepcopy
import shutil

import pandas as pd
import pytest

from trading.research import native_execution_quotes as quotes
from trading.research.baostock_daily_slice import digest
from trading.research.tests.test_native_history_source import (
    A, B, U, H, SYMBOLS, DAYS, base, read, save, write_panel)


def _subset(base, target, indices):
    shutil.copytree(base["root"], target)
    plan, receipt, result = [read(target / name) for name in ("plan.json", "receipts.json", "result.json")]
    selected_days = [DAYS[index] for index in indices]
    groups = [base["groups"][index] for index in indices]
    plan.update(days=selected_days, requested_start=selected_days[0], requested_end=selected_days[-1])
    receipt["tasks"] = {day: receipt["tasks"][day] for day in selected_days}
    receipt["expected_days"] = len(selected_days)
    write_panel(target / "raw_daily.parquet", groups)
    save(target / "plan.json", plan)
    save(target / "receipts.json", receipt)
    result.update(expected_days=len(selected_days), acquired_days=len(selected_days), rows=sum(map(len, groups)),
        raw_daily_sha256=digest(target / "raw_daily.parquet"), panel_sha256=digest(target / "raw_daily.parquet"),
        plan_sha256=digest(target / "plan.json"), receipts_sha256=digest(target / "receipts.json"))
    save(target / "result.json", result)
    return target


def test_missing_stock_rows_raw_blanks_nullable_status_and_original_aliases_are_preserved(base, monkeypatch):
    original = quotes.NativeHistorySource.frame
    calls = []
    def observed(source, day, symbols):
        assert day in DAYS[:3]
        calls.append(day)
        return original(source, day, symbols)
    monkeypatch.setattr(quotes.NativeHistorySource, "frame", observed)
    raw, proof = quotes.load_native_execution_quotes([base["root"]], SYMBOLS, DAYS[:3])
    assert calls == DAYS[:3] and len(raw) == 11
    assert not ((raw.date == DAYS[1]) & raw.symbol.eq(B)).any()
    assert {A, B} <= set(raw.symbol)
    unknown = raw.loc[(raw.date == DAYS[0]) & raw.symbol.eq(U)].iloc[0]
    assert pd.isna(unknown.tradestatus) and pd.isna(unknown.isST)
    assert unknown.tradestatus_raw == unknown.isST_raw == ""
    assert pd.isna(unknown.volume) and pd.isna(unknown.amount) and unknown.pcfNcfTTM == ""
    halted = raw.loc[(raw.date == DAYS[0]) & raw.symbol.eq(H)].iloc[0]
    assert halted.tradestatus == 0 and halted.tradestatus_raw == "0"
    assert pd.isna(halted.open) and pd.isna(halted.volume)
    assert raw.loc[(raw.date == DAYS[2]) & raw.symbol.eq(A), "preclose"].item() == 6.66
    assert proof["missing_stock_day_rows"] == 1 and proof["no_stockdb_fields_replaced"] is True
    assert proof["stockdb_rows_mixed"] is False and proof["raw_prices_imputed"] is False
    assert proof["day_sources"][2]["source_task"]["date"] == DAYS[2]
    assert proof["day_sources"][2]["row_group"] == 2


def test_disjoint_sources_supply_exact_requested_dates_without_loading_other_days(base, tmp_path, monkeypatch):
    first = _subset(base, tmp_path / "first", [0, 1])
    second = _subset(base, tmp_path / "second", [2, 3])
    original = quotes.NativeHistorySource.frame
    calls = []
    def observed(source, day, symbols):
        calls.append((source.folder, day))
        return original(source, day, symbols)
    monkeypatch.setattr(quotes.NativeHistorySource, "frame", observed)
    raw, proof = quotes.load_native_execution_quotes([first, second], [A], DAYS[1:3])
    assert calls == [(first, DAYS[1]), (second, DAYS[2])]
    assert raw.date.tolist() == DAYS[1:3] and raw.symbol.tolist() == [A, A]
    assert [source["source_index"] for source in proof["day_sources"]] == [0, 1]
    assert [source["row_group"] for source in proof["day_sources"]] == [1, 0]


def test_duplicate_views_overlapping_owners_and_missing_source_days_reject_before_frame_read(base, tmp_path, monkeypatch):
    second = _subset(base, tmp_path / "overlap", [1, 2])
    def forbidden(*args, **kwargs):
        raise AssertionError("Ownership must be complete and unique before any daily frame read")
    monkeypatch.setattr(quotes.NativeHistorySource, "frame", forbidden)
    with pytest.raises(ValueError, match="unique"):
        quotes.load_native_execution_quotes([base["root"], base["root"]], [A], [DAYS[0]])
    with pytest.raises(ValueError, match="Overlapping"):
        quotes.load_native_execution_quotes([base["root"], second], [A], [DAYS[1]])
    with pytest.raises(ValueError, match="sessions missing"):
        quotes.load_native_execution_quotes([base["root"]], [A], ["2026-01-07"])
