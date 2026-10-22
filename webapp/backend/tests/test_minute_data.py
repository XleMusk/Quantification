"""Minute clock semantics, observed gaps, capture receipts and causal queries."""

import json

import pandas as pd
import pytest

from trading import settings as trading_settings, stockdb_minutes as source
from webapp.backend.app import minute_data


SYMBOL = "600000.SSE"
DAY = "2026-10-19"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_settings, "DATA_ROOT", tmp_path / "trading")


def bars(times, *, symbol=SYMBOL, close=12., amount=12000., volume=1000.):
    return pd.DataFrame([{"timestamp": f"{DAY}T{time}:00+08:00", "symbol": symbol,
                          "open": close, "high": close + .1, "low": close - .1,
                          "close": close, "volume": volume, "amount": amount} for time in times])


def save(frame, **kwargs):
    return source.save_snapshot(frame, name="minute acceptance (synthetic)", source="test-only synthetic minute rows", kind="synthetic", **kwargs)


def test_clock_grid_is_240_end_labels_without_lunch_and_optional_auction():
    grid = source.clock_grid(DAY)
    assert len(grid) == 240
    assert grid[0].strftime("%H:%M") == "09:31" and grid[119].strftime("%H:%M") == "11:30"
    assert grid[120].strftime("%H:%M") == "13:01" and grid[-1].strftime("%H:%M") == "15:00"
    assert len(source.clock_grid(DAY, True)) == 241
    assert source.clock_grid(DAY, True)[0].strftime("%H:%M") == "09:25"


def test_units_zero_volume_and_gaps_are_preserved_without_conversion():
    frame = pd.concat([bars(["09:31", "09:33", "13:01"]), bars(["14:59"], volume=0, amount=0)])
    normalized = source.normalize_minutes(frame)
    assert normalized.gap_before.tolist() == [0, 1, 0, 117]
    assert normalized.session.tolist() == ["morning", "morning", "afternoon", "afternoon"]
    assert normalized.auction.tolist() == [False, False, False, True]
    assert normalized.iloc[-1].amount == 0 and normalized.iloc[-1].volume == 0
    assert normalized.amount_usable.all()
    wrong_unit = source.normalize_minutes(bars(["09:31"], amount=12))
    assert wrong_unit.amount.iloc[0] == 12 and not wrong_unit.amount_usable.iloc[0]
    missing = source.normalize_minutes(bars(["09:31"], amount=None))
    assert pd.isna(missing.amount.iloc[0]) and not missing.amount_usable.iloc[0]


def test_malformed_dates_duplicates_and_ohlcv_are_rejected():
    with pytest.raises(ValueError, match="重复"):
        source.normalize_minutes(pd.concat([bars(["09:31"]), bars(["09:31"])]))
    with pytest.raises(ValueError, match="结束标签"):
        source.normalize_minutes(bars(["12:00"]))
    with pytest.raises(ValueError, match="OHLC"):
        source.normalize_minutes(bars(["09:31"]).assign(high=1))
    with pytest.raises(ValueError, match="整分钟"):
        source.normalize_minutes(bars(["09:31"]).assign(timestamp=f"{DAY}T09:31:05+08:00"))
    with pytest.raises(ValueError, match="成交额"):
        source.normalize_minutes(bars(["09:31"], amount=-1))


def test_minute_chart_filters_before_aggregation_and_does_not_use_final_daily_bar():
    frame = bars(["09:31", "09:32", "09:33", "09:34", "09:35", "09:36", "13:01", "13:02", "15:00"])
    frame.loc[frame.timestamp.str.contains("15:00"), ["open", "high", "low", "close", "amount"]] = [999., 1000., 998., 999., 999000.]
    item = save(frame)
    full = minute_data.minute_chart(item["id"], period="5m")
    assert len(full["prices"]) == 4
    clipped = minute_data.minute_chart(item["id"], period="5m", as_of=f"{DAY}T09:34:00+08:00")
    assert len(clipped["prices"]) == 1
    candle = clipped["prices"][0]
    assert candle["close"] == 12 and candle["volume"] == 4000
    assert candle["date"] == f"{DAY}T09:34:00+08:00"
    assert candle["bucket_end"] == f"{DAY}T09:35:00+08:00"
    assert candle["is_partial"] and candle["observed_minutes"] == 4 and candle["expected_minutes"] == 5
    assert clipped["coverage"]["requested"] == 4 and clipped["coverage"]["missing"] == 0
    assert clipped["coverage"]["by_symbol_day"][0]["requested"] == 4
    assert clipped["source_kind"] == "minute_snapshot" and clipped["timezone"] == "Asia/Shanghai"
    assert clipped["price_basis"] == "raw" and clipped["execution_admitted"] is False
    assert clipped["timestamp_semantics_verified"] is False
    early = minute_data.aggregate_minutes(source.normalize_minutes(frame.iloc[:4]), "5m")
    assert clipped["prices"] == early


def test_capture_queries_all_exact_minutes_and_records_missing_errors_and_invalid_rows():
    keys = []
    def getter(key):
        keys.append(key)
        compact = key.rsplit(":", 1)[1]
        if compact.endswith("093100"):
            return {"code": "600000", "date": int(compact), "open": 12., "high": 12.1, "low": 11.9,
                    "close": 12., "volume": 1000., "amount": 12000.}
        if compact.endswith("093200"):
            raise TimeoutError("source timed out")
        if compact.endswith("093300"):
            return {"code": "600000", "date": int(compact), "open": 0, "high": 12.1, "low": 11.9,
                    "close": 12., "volume": 1000., "amount": 12000.}
        return []
    item = source.capture_snapshot([SYMBOL], [DAY], _get=getter, workers=2)
    assert len(keys) == 240 and len(set(keys)) == 240
    assert all(key.startswith("分钟k:600000:20261019") for key in keys)
    assert item["rows"] == 1 and item["coverage"]["missing"] == 239
    assert item["coverage"]["errors"] == 1 and item["coverage"]["invalid"] == 1
    folder = source.snapshot_root() / item["id"]
    receipt = pd.read_parquet(folder / "query_receipts.parquet")
    assert len(receipt) == 240 and set(receipt.status) == {"observed", "missing", "error", "invalid"}
    assert len((folder / "decoded_source_records.jsonl").read_text().splitlines()) == 240
    assert minute_data.list_minute_datasets()[0]["id"] == item["id"]
    chart = minute_data.minute_chart(item["id"])
    assert len(chart["prices"]) == 1 and chart["prices"][0]["turnover"] == 12000


def test_transport_failure_stops_bounded_attempts_without_claiming_complete_coverage():
    count = 0
    def down(key):
        nonlocal count
        count += 1
        raise ConnectionRefusedError("service offline")
    item = source.capture_snapshot([SYMBOL], [DAY], _get=down, workers=2)
    assert count == 4
    assert item["rows"] == 0 and item["coverage"]["status"] == "partial"
    receipt = pd.read_parquet(source.snapshot_root() / item["id"] / "query_receipts.parquet")
    assert receipt.status.eq("not_queried").sum() == 236
    assert minute_data.minute_chart(item["id"])["prices"] == []


def test_frozen_snapshot_hash_checked_and_query_does_not_acquire(monkeypatch):
    item = save(bars(["09:31", "09:32"]))
    monkeypatch.setattr(source, "local_get", lambda *args: pytest.fail("query must not acquire"))
    assert len(minute_data.minute_chart(item["id"])["prices"]) == 2
    folder = source.snapshot_root() / item["id"]
    (folder / "minutes.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="已变化"):
        minute_data.minute_chart(item["id"])


def test_capture_jobs_persist_reuse_finished_result_and_do_not_mutate_accounts(monkeypatch):
    monkeypatch.setattr(source, "local_get", lambda key: [])
    job = minute_data.create_capture([SYMBOL], DAY, DAY, "job capture")
    assert job["status"] == "queued" and job["requested"] == 240
    result = minute_data.run_capture(job["id"])
    assert result["status"] == "partial" and result["snapshot_id"]
    assert result["coverage"]["status"] == "partial" and result["processed"] == 240
    monkeypatch.setattr(source, "capture_snapshot", lambda *args, **kwargs: pytest.fail("finished job must be reused"))
    assert minute_data.run_capture(job["id"])["snapshot_id"] == result["snapshot_id"]
    assert not (trading_settings.DATA_ROOT / "trading.sqlite3").exists()
    with pytest.raises(ValueError, match="1–5"):
        minute_data.create_capture([SYMBOL], "2026-10-01", "2026-10-10")
    with pytest.raises(ValueError, match="ISO"):
        minute_data.minute_chart(result["snapshot_id"], as_of="20261019101500")


@pytest.mark.parametrize("recorded_status", ["queued", "running"])
def test_new_api_process_projects_interrupted_capture_without_rewriting_history(monkeypatch, recorded_status):
    job = minute_data.create_capture([SYMBOL], DAY, DAY)
    folder = minute_data._capture_folder(job["id"])
    job["status"] = recorded_status
    minute_data._save_job(folder, job)
    before = (folder / "job.json").read_bytes()
    monkeypatch.setattr(minute_data, "PROCESS_INSTANCE", "new-api-instance")
    projected = minute_data.capture_status(job["id"])
    assert projected["status"] == "interrupted" and projected["recorded_status"] == recorded_status
    monkeypatch.setattr(source, "capture_snapshot", lambda *a, **kw: pytest.fail("old task must not be resumed"))
    assert minute_data.run_capture(job["id"])["status"] == "interrupted"
    assert (folder / "job.json").read_bytes() == before
