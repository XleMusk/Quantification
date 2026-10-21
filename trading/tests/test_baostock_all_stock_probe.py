from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import baostock_all_stock_probe as probe


def _completed(folder: Path, day: str):
    folder.mkdir(parents=True)
    path = folder / f"query_all_stock_{day}.csv"
    pd.DataFrame([{"code": "sz.000001", "tradeStatus": "0", "code_name": "dated provider name"}]).to_csv(path, index=False, encoding="utf-8-sig")
    receipt = {"schema": "baostock_historical_all_stock_status_probe_v1", "status": "completed_provider_status_list",
               "day": day, "response_day": day, "method": "query_all_stock", "kwargs": {"day": day},
               "quality_issues": [], "provider_error_code": "0", "fields_returned": probe.EXPECTED_FIELDS,
               "sdk": {"sha256": probe.WHEEL_SHA256}, "execution_admitted": False, "price_bars_generated": False,
               "raw_csv": str(path.resolve()), "raw_sha256": sha256(path.read_bytes()).hexdigest(), "rows_preserved": 1,
               "logical_query_attempts": 1, "elapsed_seconds": .01, "historical_bounded_collection_feasible": True}
    (folder / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


def test_exact_cache_resume_does_not_select_sdk_or_query(tmp_path, monkeypatch):
    _completed(tmp_path / "2026-01-06", "2026-01-06")
    def forbidden(*args, **kwargs):
        raise AssertionError("Validated resume must not query or reload SDK")
    monkeypatch.setattr(probe, "select_sdk", forbidden)
    monkeypatch.setattr(probe, "probe", forbidden)
    result = probe.collect(["2026-01-06"], tmp_path / "unused.whl", tmp_path)
    assert result["status"] == "completed_bounded_explicit_status_days"
    assert result["logical_queries_this_run"] == 0
    assert result["entries"][0]["verification"]["original_acquisition_hash_verified"] is True


def test_corrupted_cached_csv_stops_without_reacquisition_or_later_queries(tmp_path, monkeypatch):
    receipt = _completed(tmp_path / "2026-01-06", "2026-01-06")
    Path(receipt["raw_csv"]).write_text("corrupted", encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("Corrupt evidence must never trigger a replacement query")
    monkeypatch.setattr(probe, "select_sdk", forbidden)
    monkeypatch.setattr(probe, "probe", forbidden)
    result = probe.collect(["2026-01-06", "2026-02-02"], tmp_path / "unused.whl", tmp_path)
    assert result["status"] == "stopped_on_first_evidence_failure_no_retry"
    assert "hash mismatch" in result["entries"][0]["error"]
    assert result["not_attempted_days"] == ["2026-02-02"]


def test_first_provider_failure_stops_sequential_batch_no_retry(tmp_path, monkeypatch):
    calls = []
    sdk_calls = []
    def selected(wheel):
        sdk_calls.append(wheel)
        return {"sha256": probe.WHEEL_SHA256}
    def simulated(day, wheel, folder, **kwargs):
        calls.append(day)
        if len(calls) == 1:
            return _completed(folder, day)
        return {"status": "provider_probe_failed_no_retry", "logical_query_attempts": 1,
                "rows_preserved": 0, "elapsed_seconds": 30., "historical_bounded_collection_feasible": False}
    monkeypatch.setattr(probe, "select_sdk", selected)
    monkeypatch.setattr(probe, "probe", simulated)
    result = probe.collect(["2026-01-06", "2026-02-02", "2026-05-06"], tmp_path / "unused.whl", tmp_path)
    assert calls == ["2026-01-06", "2026-02-02"]
    assert len(sdk_calls) == 1
    assert result["status"] == "stopped_on_first_provider_failure_or_unusable_day_no_retry"
    assert result["not_attempted_days"] == ["2026-05-06"] and result["retries"] == 0


def test_exact_missing_reconciliation_keeps_unknown_and_truncated_preview_boundary():
    symbols = [f"{index:06d}.SZSE" for index in range(21)]
    exact = {"missing_symbols": symbols}
    preview = {"missing_count": "21", "missing_symbols_first20": "|".join(symbols[:20])}
    raw = pd.DataFrame([
        {"code": "sz.000000", "tradeStatus": "0", "code_name": "name on requested day"},
        {"code": "sz.000001", "tradeStatus": "1", "code_name": "another dated name"},
    ])
    rows, facts = probe.reconcile_missing("2026-01-06", raw, exact, preview)
    assert len(rows) == 21 and facts["preview_truncated"] is True
    assert (facts["provider_status0"], facts["provider_status1_missing_prices"], facts["no_provider_row_unknown"]) == (1, 1, 19)
    assert all(row["price_bar_created"] is False for row in rows)
    wrong = {**preview, "missing_count": "20"}
    with pytest.raises(ValueError, match="disagree"):
        probe.reconcile_missing("2026-01-06", raw, exact, wrong)


def test_batch_rejects_more_than_ten_or_duplicate_explicit_days(tmp_path):
    with pytest.raises(ValueError, match="1..10"):
        probe.collect(["2026-01-06"] * 2, tmp_path / "unused.whl", tmp_path)
    with pytest.raises(ValueError, match="1..10"):
        probe.collect(pd.date_range("2026-01-01", periods=11).strftime("%Y-%m-%d").tolist(), tmp_path / "unused.whl", tmp_path)


def test_cli_reports_nonzero_when_dated_provider_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "probe", lambda *args, **kwargs: {
        "status": "provider_probe_failed_no_retry", "day": "2026-01-06",
        "logical_query_attempts": 1, "rows_preserved": 0, "elapsed_seconds": 30.,
        "historical_bounded_collection_feasible": False,
    })
    assert probe.main(["--day", "2026-01-06", "--sdk-wheel", "unused.whl", "--out", str(tmp_path)]) == 2


def test_cli_reports_nonzero_when_batch_stops_on_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "collect", lambda *args, **kwargs: {
        "status": "stopped_on_first_evidence_failure_no_retry", "completed_requested_days": 0,
        "logical_queries_this_run": 0,
    })
    assert probe.main(["--days", "2026-01-06", "--sdk-wheel", "unused.whl", "--out", str(tmp_path)]) == 2
