import json

import numpy as np
import pandas as pd
import pytest

from trading.research.adjusted_prices import reconstruct
from trading.research.bulk_adjusted_prices import build as price_build
from trading.research.factors import normalized_prices, write_provider
from trading.research.qlib_research_provider import build
from trading.research.tests.test_adjusted_prices import factor_frame, raw_frame
from trading.research.tests.test_bulk_adjusted_prices import derived_views


def numeric_frames():
    raw = raw_frame()
    adjusted = reconstruct(raw, factor_frame(), "2017-05-25")
    for frame in (raw, adjusted):
        frame["symbol"] = "600000.SSE"
        frame["date"] = pd.to_datetime(frame.date)
        for field in ["open", "high", "low", "close", "volume", "amount", "tradestatus"]:
            frame[field] = pd.to_numeric(frame[field], errors="raise")
        frame.set_index(["date", "symbol"], inplace=True)
    return raw, adjusted


def test_shared_normalization_anchors_prices_and_preserves_vwap_volume_amount():
    raw, adjusted = numeric_frames()
    values, checks = normalized_prices(raw, adjusted)
    np.testing.assert_allclose(values.iloc[0][["open", "high", "low", "close"]], raw.iloc[0][["open", "high", "low", "close"]].astype(float))
    np.testing.assert_allclose(values.factor, [1., 1. / .759535])
    np.testing.assert_allclose(values.volume * values.vwap, raw.amount)
    assert checks["ohlc_adjustment_check"] == "passed"


def test_shared_normalization_masks_halts_and_rejects_bad_amount_units():
    raw, adjusted = numeric_frames()
    raw.iloc[0, raw.columns.get_loc("tradestatus")] = 0
    values, _ = normalized_prices(raw, adjusted)
    assert values.iloc[0].isna().all()
    raw.iloc[1, raw.columns.get_loc("amount")] = 1
    with pytest.raises(ValueError, match="Amount/volume units"):
        normalized_prices(raw, adjusted)


@pytest.mark.parametrize("field", ["volume", "amount", "tradestatus"])
def test_unknown_active_activity_or_status_cannot_become_silent_nan_halt(field):
    raw, adjusted = numeric_frames()
    raw.iloc[0, raw.columns.get_loc(field)] = np.nan
    with pytest.raises(ValueError):
        normalized_prices(raw, adjusted)


def test_shared_provider_preserves_missing_calendar_observations_as_nan(tmp_path):
    raw, adjusted = numeric_frames()
    values, _ = normalized_prices(raw, adjusted)
    calendar = ["2017-05-24", "2017-05-25", "2017-05-26"]
    assert write_provider(values.reset_index(), calendar, tmp_path / "provider") == ["600000.SSE"]
    close = np.fromfile(tmp_path / "provider/features/sh600000/close.day.bin", dtype="<f4")
    assert len(close) == 4 and close[0] == 0 and np.isnan(close[-1])
    np.testing.assert_allclose(close[1:3], values.close, rtol=1e-6)


def test_full_canonical_view_enters_shared_provider_without_feed_or_account_admission(tmp_path, monkeypatch):
    inputs = derived_views(tmp_path / "inputs", monkeypatch)
    prices, price_result = price_build(*inputs, tmp_path / "prices", allow_derived=True)
    assert price_result["status"] == "research_reconstruction_completed"
    folder, result = build(prices, tmp_path / "provider")
    assert result["status"] == "research_provider_completed", result.get("error")
    assert (result["rows"], result["symbols"], result["dates"], result["binary_files"]) == (3, 1, 3, 7)
    assert not result["execution_admitted"] and not result["strategy_validated"] and not result["model_trained"]
    assert result["alpha158_definitions"] == 158 and "native_smoke" not in result
    data = np.fromfile(folder / "qlib_provider/features/sz302132/close.day.bin", dtype="<f4")
    np.testing.assert_allclose(data[1:], [15.47, 15.47, 15.47 / .986265], rtol=1e-6)
    assert not (folder / "manifest.json").exists()


def test_provider_rejects_partial_prices_and_unknown_smoke_code(tmp_path, monkeypatch):
    inputs = derived_views(tmp_path / "inputs", monkeypatch)
    prices, _ = price_build(*inputs, tmp_path / "prices", allow_derived=True)
    _, result = build(prices, tmp_path / "unknown", smoke_codes=["sh.999999"])
    assert result["status"] == "failed_provider" and "unknown smoke code" in result["error"]
    result_path = prices / "result.json"
    partial = json.loads(result_path.read_text())
    partial["status"] = "failed_reconstruction"
    result_path.write_text(json.dumps(partial))
    _, result = build(prices, tmp_path / "partial")
    assert result["status"] == "failed_provider" and "Completed explicit canonical" in result["error"]
