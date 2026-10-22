"""Research windows retain exchange-calendar warmup without reading future rows."""

import json

import numpy as np
import pandas as pd
import pytest

from quant_learning_starter.factor_lab import (
    FACTORS, LABEL, compute_factors_and_labels, evaluate_cross_sections,
    summarize, synthetic_panel,
)
from webapp.backend.app import data, settings, store
from webapp.workers import factor_worker as worker


def test_window_counts_calendar_sessions_including_missing_stock_rows():
    calendar = pd.bdate_range("2025-01-06", periods=90)
    window = worker.research_calendar_window(calendar, calendar[40], calendar[70])
    assert window.equals(pd.DatetimeIndex(calendar[20:71], name="date"))
    assert worker.research_calendar_window(calendar, calendar[5], calendar[12]).equals(
        pd.DatetimeIndex(calendar[:13], name="date"))
    with pytest.raises(ValueError, match="没有交易日"):
        worker.research_calendar_window(calendar, "2030-01-01", "2030-01-02")


def test_windowed_values_match_full_history_with_gaps_and_end_boundary():
    panel, calendar = synthetic_panel(stocks=8, sessions=110)
    # A missing exchange session stays missing. Zero-volume rows follow the
    # worker's existing policy and are not silently usable prices.
    panel = panel.loc[~(panel.symbol.eq("SYN000") & panel.date.eq(calendar[50]))].copy()
    panel.loc[panel.symbol.eq("SYN001") & panel.date.eq(calendar[51]), "volume"] = 0
    panel.loc[panel.volume.le(0), ["open", "close", "volume"]] = np.nan
    start, end = calendar[65], calendar[94]
    full = compute_factors_and_labels(panel, calendar)
    full = full.loc[full.date.between(start, end)].copy()
    full.loc[full.exit_date.gt(end), LABEL] = np.nan
    window = worker.research_calendar_window(calendar, start, end)
    subset = panel.loc[panel.date.between(window[0], window[-1])]
    actual = compute_factors_and_labels(subset, window)
    actual = actual.loc[actual.date.between(start, end)].copy()
    columns = ["date", "symbol", *FACTORS, LABEL]
    pd.testing.assert_frame_equal(full[columns].reset_index(drop=True),
                                  actual[columns].reset_index(drop=True))
    expected_daily, expected_groups = evaluate_cross_sections(full)
    actual_daily, actual_groups = evaluate_cross_sections(actual)
    pd.testing.assert_frame_equal(expected_daily, actual_daily)
    pd.testing.assert_frame_equal(expected_groups, actual_groups)
    pd.testing.assert_frame_equal(summarize(expected_daily, expected_groups),
                                  summarize(actual_daily, actual_groups))
    assert actual.loc[actual.date.gt(calendar[88]), LABEL].isna().all()


def test_worker_reads_only_requested_window_and_saves_input_receipt(tmp_path, monkeypatch):
    for module in (settings, store, data, worker):
        monkeypatch.setattr(module, "STORAGE", tmp_path, raising=False)
    store.initialize()
    panel, calendar = synthetic_panel(stocks=8, sessions=110)
    dataset = data.register(panel, "window test", "synthetic", "unit test", "synthetic", calendar)
    start, end = str(calendar[60].date()), str(calendar[95].date())
    config = {"start": start, "end": end, "min_pairs": 5,
              "factors": list(FACTORS), "holding_sessions": 5}
    store.create_job(dataset["id"], config, "window-worker-request")
    calls = []
    read_bars = data.read_bars

    def capture(identifier, **kwargs):
        calls.append({"identifier": identifier, **kwargs})
        return read_bars(identifier, **kwargs)

    monkeypatch.setattr(data, "read_bars", capture)
    item = store.claim_job()
    worker.execute(item)
    finished = store.job(item["id"])
    assert finished["status"] == "completed", finished["error"]
    assert calls == [{"identifier": dataset["id"], "start": str(calendar[40].date()),
                      "end": end, "columns": data.COLUMNS}]
    result = json.loads((tmp_path / "results" / item["id"] / "result.json").read_text())
    assert result["input_window"] == {
        "start": str(calendar[40].date()), "end": end, "sessions": 56,
        "rows": len(panel.loc[panel.date.between(calendar[40], calendar[95])]),
        "warmup_sessions_requested": 20, "calendar_basis": "saved_dataset_calendar",
        "after_end_prices_read": False,
    }
    saved = pd.read_parquet(tmp_path / "results" / item["id"] / "factors.parquet")
    assert saved.date.min() >= pd.Timestamp(start) and saved.date.max() == pd.Timestamp(end)
    assert saved.loc[saved.date.gt(calendar[89]), LABEL].isna().all()
    assert result["dataset"]["id"] == dataset["id"]
