import json
from pathlib import Path

from trading.research import stockdb_live_coverage


def test_verify_marks_stale_when_latest_requested_day_is_missing(tmp_path, monkeypatch):
    rows = {
        ("000001.SZSE", "2026-10-13"): {"date": 20261013, "code": "000001", "close": 11.57,
                                        "pct_chg": -1.36, "volume": 1, "amount": 1},
        ("000001.SZSE", "2026-10-14"): None,
    }
    monkeypatch.setattr(stockdb_live_coverage, "source_bar",
                        lambda symbol, day: rows[(symbol, day)])
    path, record = stockdb_live_coverage.verify(
        ["000001"], ["2026-10-13", "2026-10-14"], out=tmp_path / "receipt"
    )
    assert record["status"] == "stale_current_day"
    assert record["execution_admitted"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["snapshot_modified"] is False


def test_verify_records_service_error_without_filling(tmp_path, monkeypatch):
    def fail(symbol, day):
        raise TimeoutError("test timeout")

    monkeypatch.setattr(stockdb_live_coverage, "source_bar", fail)
    path, record = stockdb_live_coverage.verify(
        ["000001.SZSE"], ["2026-10-14"], out=tmp_path / "receipt"
    )
    assert record["status"] == "service_error"
    assert len(record["errors"]) == 1
    assert Path(path).is_file()


def test_offline_endpoint_does_not_fan_out_into_false_missing_quotes(tmp_path, monkeypatch):
    calls = []
    def refused(symbol, day):
        calls.append((symbol, day))
        raise ConnectionRefusedError("connection refused")
    monkeypatch.setattr(stockdb_live_coverage, "source_bar", refused)
    _, result = stockdb_live_coverage.verify(
        ["000001.SZSE", "600519.SSE"], ["2026-10-13", "2026-10-14"], out=tmp_path / "receipt",
    )
    assert len(calls) == result["attempted_queries"] == len(result["errors"]) == 1
    assert result["not_attempted_queries"] == 3
    assert result["latest_requested_coverage"] == "unknown_service_error"
    assert all(row.get("not_attempted_reason") for row in result["observations"] if not row["query_attempted"])
