"""Full decoded StockDB evidence survives the legacy projection and failures."""
from collections import Counter
import json

import pandas as pd
import pytest

from trading import stockdb_source as stockdb


DAY = "2026-10-19"


def _daily(code, *, day=20261019):
    return dict(code=code, date=day, open=10.0, high=10.5, low=9.5, close=10.25,
                pre_close=190.12, volume=301, amount=123456.789,
                is_st=False, turnover=1.23450017, float_share=1234567890, mv=0,
                nullable_field=None, extra_nested={"values": [0, None, False], "caption": "source"})


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _records(out):
    return [json.loads(line) for line in (out / "decoded_daily_records.jsonl").read_text(encoding="utf-8").splitlines()]


def test_catalog_and_all_decoded_fields_survive_without_changing_the_v1_projection(tmp_path, monkeypatch):
    catalog = {"0": ["000001"], "3": ["300001"], "6": ["600001"], "9": ["900001"]}
    originals = {code: _daily(code) for code in ("000001", "300001", "600001")}
    calls = []

    def get(key):
        calls.append(key)
        return catalog if key == "股票代码" else originals[key.split(":")[1]]

    monkeypatch.setattr(stockdb, "local_get", get)
    out = tmp_path / "snapshot"
    result = stockdb.freeze_snapshot(tmp_path / "source", out, [], [DAY], "",
        catalog_prefixes=["0", "3", "6"], session_dates=[DAY], calendar_basis="fixed-calendar.json")
    assert calls == ["股票代码", "日k:000001:20261019", "日k:300001:20261019", "日k:600001:20261019"]
    assert result["schema"] == "stockdb_research_snapshot_v1" and result["capture_complete"] is True
    assert result["rows"] == dict(requested_daily=3, available_daily=3, missing_daily=0, available_minute=0)
    assert result["minute_time"] is None and result["original_msgpack_bytes_preserved"] is False
    assert result["catalog_capture"]["dated_PIT_roster_verified"] is False
    saved_catalog = _read(out / "catalog_capture.json")
    assert saved_catalog["payload"] == catalog  # Including unselected original prefix 9.
    assert saved_catalog["request_started_at"] <= saved_catalog["response_decoded_at"]
    rows = _records(out)
    assert len(rows) == 3
    for row in rows:
        assert row["payload"] == originals[row["payload"]["code"]]
        assert row["request_started_at"] <= row["response_decoded_at"] <= result["captured_at"]
        assert row["payload_representation"] == "decoded_messagepack_payload_json_not_original_transport_bytes"
    projected = pd.read_csv(out / "daily.csv", dtype={"code": str})
    assert list(projected) == ["symbol", "date", "code", "open", "high", "low", "close", "pre_close", "volume", "amount"]
    assert projected.code.tolist() == ["000001", "300001", "600001"]
    assert projected.pre_close.tolist() == [190.12] * 3
    assert projected.volume.tolist() == [301] * 3 and projected.amount.tolist() == [123456.789] * 3
    assert "is_st" not in projected and rows[0]["payload"]["float_share"] == 1234567890
    assert _read(out / "capture_receipt.json")["status"] == "completed_snapshot_capture"


def test_a_transport_failure_retains_partial_payloads_missingness_and_has_no_success_manifest(tmp_path, monkeypatch):
    calls = Counter()

    def get(key):
        calls[key] += 1
        if key == "股票代码":
            return {"0": ["000001", "000002", "000003", "000004"]}
        if key == "日k:000001:20261019":
            return _daily("000001")
        if key == "日k:000002:20261019":
            return []
        raise OSError("single synthetic local-service failure")

    monkeypatch.setattr(stockdb, "local_get", get)
    out = tmp_path / "failed"
    with pytest.raises(OSError, match="single synthetic"):
        stockdb.freeze_snapshot(tmp_path / "source", out, [], [DAY], "", catalog_prefixes=["0"])
    assert not (out / "snapshot.json").exists()
    receipt = _read(out / "capture_receipt.json")
    assert receipt["status"] == "partial_snapshot_capture_stopped_no_retry"
    assert receipt["snapshot_published"] is False and receipt["automatic_retry"] is False
    assert receipt["transport_failures"] == 1
    records = _records(out)
    assert [row["status"] for row in records] == ["decoded", "decoded", "request_failed_no_retry"]
    assert records[0]["payload"] == _daily("000001") and records[1]["payload"] == []
    availability = pd.read_csv(out / "availability.csv")
    assert availability.status.tolist() == ["available", "missing"]
    assert len(pd.read_csv(out / "daily.csv")) == 1
    assert all(count == 1 for count in calls.values()) and "日k:000004:20261019" not in calls
    before = dict(calls)
    with pytest.raises(FileExistsError):
        stockdb.freeze_snapshot(tmp_path / "source", out, [], [DAY], "", catalog_prefixes=["0"])
    assert dict(calls) == before


def test_wrong_day_payload_is_preserved_but_cannot_publish_a_snapshot(tmp_path, monkeypatch):
    wrong = _daily("600001", day=20261018)
    monkeypatch.setattr(stockdb, "local_get", lambda key: wrong)
    out = tmp_path / "wrong-day"
    with pytest.raises(ValueError, match="Unexpected StockDB daily bar"):
        stockdb.freeze_snapshot(tmp_path / "source", out, ["600001.SSE"], [DAY], "")
    assert _records(out)[0]["payload"] == wrong
    assert not (out / "snapshot.json").exists()
    receipt = _read(out / "capture_receipt.json")
    assert receipt["status"] == "partial_snapshot_capture_stopped_no_retry"
    assert receipt["error_type"] == "ValueError" and receipt["transport_failures"] == 0
    assert pd.read_csv(out / "daily.csv").empty
