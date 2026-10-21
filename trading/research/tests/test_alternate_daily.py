import json
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from trading.research import alternate_daily as module
from trading.research.alternate_daily import compare, coverage_table, decode_raw, quality


def payload(symbol="sz000001", rows=None, key="day"):
    return "kline_day=" + json.dumps({"code": 0, "data": {symbol: {key: rows or [
        ["2026-09-30", "10", "10", "10.2", "9.8", "100", {}, ".1", "10"]]}}})


def test_ordinary_sz000_volume_is_not_old_amount_or_index_units():
    frame = decode_raw(payload(), "000001.SZSE", "2026-09-01", "2026-09-30")
    assert frame.volume_shares_candidate.tolist() == [10000]
    assert frame.amount_cny_candidate.tolist() == [100000]
    assert quality(frame)["amount_volume_outside_ohlc_rows"] == 0
    assert not {"preclose", "tradestatus", "isST"} & set(frame)


def test_star_shares_dates_and_adjusted_fallback():
    rows = [["2026-08-31", "10", "10", "10.2", "9.8", "10000", {}, ".1", "10"],
            ["2026-09-30", "10", "10", "10.2", "9.8", "10000", {}, ".1", "10"]]
    frame = decode_raw(payload("sh688001", rows), "688001.SSE", "2026-09-01", "2026-09-30")
    assert frame.date.tolist() == ["2026-09-30"]
    assert frame.volume_shares_candidate.tolist() == [10000]
    with pytest.raises(ValueError, match="fallback"):
        decode_raw(payload(key="qfqday"), "000001.SZSE", "2026-09-01", "2026-09-30")


def test_cross_source_missing_dates_and_differences_remain_visible():
    frame = decode_raw(payload(), "000001.SZSE", "2026-09-01", "2026-09-30")
    ref = pd.DataFrame({"date": ["2026-09-29", "2026-09-30"], "symbol": ["000001.SZSE"] * 2,
        "open": [10, 11], "high": [10.2, 11.2], "low": [9.8, 10.8], "close": [10, 11],
        "volume": [10000, 9999], "amount": [100000, 110000], "tradestatus": ["1", "1"], "isST": ["0", "0"]})
    rows, result = compare(frame, ref)
    assert result["paired_rows"] == 1
    assert result["reference_only_rows"] == 1
    assert result["exact_ohlc_rows"] == 0
    assert result["max_ohlc_abs_gap"] == 1
    assert result["max_volume_abs_gap"] == 1
    assert len(rows) == 2


def test_queried_empty_dates_are_not_available_data_or_imputed_status():
    text = "kline_day=" + json.dumps({"code": 0, "data": {"sz000003": {"day": []}}})
    frame = decode_raw(text, "000003.SZSE", "2026-09-01", "2026-09-30")
    assert frame.empty and frame.attrs["raw_rows_received"] == 0
    assert not {"tradestatus", "isST"} & set(frame)
    q = quality(frame)
    comparison = {"paired_rows": 0, "exact_ohlc_rows": 0, "candidate_only_rows": 0, "reference_only_rows": 0}
    plan = {"symbols": ["000003.SZSE", "600000.SSE"]}
    result = {"entries": {"000003.SZSE": {"quality": q, "comparison": comparison}}}
    table, counts = coverage_table(plan, result)
    assert table.status.tolist() == ["no_rows_in_requested_range", "pending"]
    assert counts["queried_stocks"] == 1 and counts["stocks_with_data"] == 0
    assert counts["stocks_without_requested_rows"] == 1 and counts["pending_stocks"] == 1
    assert counts["rows"] == 0 and counts["execution_admitted"] is False


@pytest.mark.parametrize("symbol,market", [("000001.SZSE", 0), ("688981.SSE", 1)])
def test_eastmoney_documented_lots_and_cny_are_not_tencent_star_units(symbol, market):
    text = json.dumps({"rc": 0, "data": {"code": symbol[:6], "market": market,
                         "klines": ["2026-09-30,10,10,10.2,9.8,100,100000,4,0,0,.1"]}})
    frame = module.decode_eastmoney_raw(text, symbol, "2023-01-01", "2026-09-30")
    assert frame.volume_shares_candidate.tolist() == [10000]
    assert frame.amount_cny_candidate.tolist() == [100000]
    assert quality(frame)["amount_volume_outside_ohlc_rows"] == 0
    assert not {"preclose", "tradestatus", "isST"} & set(frame)
    with pytest.raises(ValueError, match="identity/schema"):
        module.decode_eastmoney_raw(text, "600000.SSE", "2023-01-01", "2026-09-30")


def test_eastmoney_explicit_null_and_changed_schema_are_not_synthesized():
    frame = module.decode_eastmoney_raw('{"rc":0,"data":null}', "000001.SZSE", "2023-01-01", "2026-09-30")
    assert frame.empty and frame.attrs["provider_data_state"] == "explicit_null_data"
    with pytest.raises(ValueError, match="source error"):
        module.decode_eastmoney_raw('{"rc":1,"data":null}', "000001.SZSE", "2023-01-01", "2026-09-30")
    changed = {"rc": 0, "data": {"code": "000001", "market": 0, "klines": ["2026-09-30,10,10"]}}
    with pytest.raises(ValueError, match="positional schema"):
        module.decode_eastmoney_raw(json.dumps(changed), "000001.SZSE", "2023-01-01", "2026-09-30")


def test_new_provider_request_and_failure_preserve_cache_and_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module.time, "sleep", lambda value: None)
    universe = tmp_path / "universe.csv"
    pd.DataFrame({"code": ["sz.000001", "sh.600000"], "type": ["1", "1"]}).to_csv(universe, index=False)
    reference = tmp_path / "reference"
    reference.mkdir()
    pd.DataFrame({"date": ["2026-09-30"], "code": ["sz.000001"], "open": [10.], "high": [10.2],
                  "low": [9.8], "close": [10.], "volume": [10000.], "amount": [100000.],
                  "tradestatus": ["1"], "isST": ["0"]}).to_parquet(reference / "raw_daily.parquet", index=False)
    root = module.create(universe, reference, "2023-01-01", "2026-09-30", ["000001.SZSE"], provider="eastmoney")
    calls = []
    text = json.dumps({"rc": 0, "data": {"code": "000001", "market": 0,
                                         "klines": ["2026-09-30,10,10,10.2,9.8,100,100000,4,0,0,.1"]}})
    def request(url, *, params, timeout):
        calls.append((url, params, timeout))
        if len(calls) > 1:
            raise requests.ReadTimeout("source stopped")
        return SimpleNamespace(text=text, content=text.encode(), url=url, raise_for_status=lambda: None)
    _, result = module.collect(root, 3, request_get=request)
    assert len(calls) == 2
    assert all(url == module.EASTMONEY_ENDPOINT for url, _, _ in calls)
    assert calls[0][1]["fqt"] == "0" and calls[0][1]["klt"] == "101"
    assert calls[0][1]["beg"] == "20230101"
    assert result["status"] == "failed_source_request"
    assert result["failed_symbol"] == "600000.SSE"
    assert result["entries"]["000001.SZSE"]["comparison"]["exact_ohlc_rows"] == 1
    assert (root / "raw/000001.SZSE.txt").read_bytes() == text.encode()
    with pytest.raises(ValueError, match="not automatically retried"):
        module.collect(root, request_get=lambda *args, **kwargs: pytest.fail("No source retry"))
