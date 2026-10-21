from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research import fund_dayk as source


def payload(code="510300"):
    return {"code": code, "total": 3, "begin": 1, "end": 3,
            "kline": [[20260923, 4.503, 4.510, 4.491, 4.505, 1001, 4503],
                      [20260924, 4.578, 4.579, 4.512, 4.515, 710251931, 3220888135]]}


def recipe(folder):
    path = folder / "recipe.js"
    path.write_bytes(b'fixture only: "dayk/"; res.kline[i][5]')
    return path


def collection(folder, monkeypatch, code="510300"):
    monkeypatch.setattr(source, "fetch", lambda session, code: json.dumps(payload(code)).encode())
    return source.collect([code], recipe(folder), folder / "dayk", session=object())


def test_exact_mills_and_original_candidate_field_are_preserved():
    original = payload()
    frame = source.normalize(original, "510300")
    assert frame.iloc[1].to_dict() == {
        "code": "510300", "date": "2026-09-24", "open": "4.578", "high": "4.579",
        "low": "4.512", "close": "4.515", "volume": "710251931",
        "field7_amount_candidate": "3220888135"}
    assert original == payload() and not {"preclose", "isST", "adjustflag", "tradestatus"} & set(frame.columns)


@pytest.mark.parametrize("problem", ["code", "count", "position", "bool", "tick", "ohlc", "volume",
                                     "candidate", "date", "duplicate", "unordered", "layout"])
def test_bad_source_identity_count_layout_and_observations_fail_closed(problem):
    data = deepcopy(payload())
    if problem == "code":
        data["code"] = "510050"
    elif problem == "count":
        data["end"] = 2
    elif problem == "position":
        data["begin"] = -1
    elif problem == "bool":
        data["kline"][0][1] = True
    elif problem == "tick":
        data["kline"][0][1] = 4.5031
    elif problem == "ohlc":
        data["kline"][0][2] = 4.5
    elif problem == "volume":
        data["kline"][0][5] = 1.5
    elif problem == "candidate":
        data["kline"][0][6] = float("nan")
    elif problem == "date":
        data["kline"][0][0] = 20261323
    elif problem == "duplicate":
        data["kline"][1][0] = data["kline"][0][0]
    elif problem == "unordered":
        data["kline"].reverse()
    else:
        data["kline"][0].append(0)
    with pytest.raises(ValueError):
        source.normalize(data, "510300")


def test_saved_consumer_verifies_receipts_and_does_not_admit_a_feed(tmp_path, monkeypatch):
    folder, result = collection(tmp_path, monkeypatch)
    frame, loaded = source.load(folder)
    assert len(frame) == 2 and loaded == result
    assert not result["execution_admitted"] and not result["strategy_validated"]
    assert not (folder / "manifest.json").exists()
    (folder / "510300.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        source.load(folder)


def test_reuse_does_not_requery_or_invent_retrieval_timestamp(tmp_path, monkeypatch):
    cached = tmp_path / "old_probe.json"
    cached.write_text(json.dumps(payload()))
    monkeypatch.setattr(source, "fetch", lambda *_: pytest.fail("Reused source must not be requested again"))
    folder, result = source.collect(["510300"], recipe(tmp_path), tmp_path / "reuse",
                                    session=object(), reuse=[cached])
    assert result["receipts"][0]["origin"] == "reused_local_probe"
    assert result["receipts"][0]["retrieved_at"] is None
    assert (folder / "510300.json").read_bytes() == cached.read_bytes()
    assert len(source.load(folder)[0]) == 2


def test_failure_raw_is_retained_without_completing_collection(tmp_path, monkeypatch):
    bad = payload()
    bad["code"] = "510050"
    raw = json.dumps(bad).encode()
    monkeypatch.setattr(source, "fetch", lambda *_: raw)
    with pytest.raises(ValueError, match="security mismatch"):
        source.collect(["510300"], recipe(tmp_path), tmp_path / "bad", session=object())
    result = json.loads((tmp_path / "bad/result.json").read_text())
    assert result["status"] == "stopped_source_error" and result["completed_codes"] == []
    assert (tmp_path / "bad/510300.json").read_bytes() == raw


def test_exact_comparison_preserves_conflicts_missing_and_out_of_range_rows():
    official = source.normalize(payload(), "510300").drop(columns="code")
    other = official.iloc[1:].rename(columns={"field7_amount_candidate": "amount"}).copy()
    other["high"] = "4.580"
    other["amount"] = "3220888136"
    days = ["2026-09-23", "2026-09-24", "2026-09-25"]
    result = source.compare_frames(official, other, days, {"high": "high", "field7_amount_candidate": "amount"})
    assert result["paired_rows"] == 1
    assert len(result["conflicts"]) == 2
    assert result["conflicts"][0]["other_minus_sse"] == "0.001"
    assert result["conflicts"][1]["other_minus_sse"] == "1"
    assert result["official_missing_calendar_sessions"] == ["2026-09-25"]
    assert result["other_missing_calendar_sessions"] == ["2026-09-23", "2026-09-25"]
    assert result["source_only_dates"] == [{"date": "2026-09-23", "_merge": "left_only"}]
    trimmed = source.compare_frames(official, other, days[1:], {"close": "close"})
    assert trimmed["official_out_of_comparison_range_rows"] == 1


def test_actual_saved_sina_bulk_calendar_consumers_do_not_change_input(tmp_path, monkeypatch):
    folder, _ = collection(tmp_path, monkeypatch)
    official, _ = source.load(folder)
    sina = tmp_path / "sina"
    sina.mkdir()
    frame = official.drop(columns=["code", "field7_amount_candidate"]).assign(symbol="510300")
    frame["date"] = frame.date.str.replace("-", "", regex=False)
    frame.to_parquet(sina / "510300.parquet", index=False)
    raw_sina = (sina / "510300.parquet").read_bytes()
    status = {"provider": "sina-ohlcv", "source": source.SOURCES["sina-ohlcv"], "receipts": {
        "510300": {"status": "acquired_unvalidated", "file": "510300.parquet",
                   "sha256": sha256(raw_sina).hexdigest(), "rows": 2}}}
    (sina / "status.json").write_text(json.dumps(status))
    panel = tmp_path / "panel"
    panel.mkdir()
    official.rename(columns={"field7_amount_candidate": "amount"}).assign(code="sh.510300").to_parquet(
        panel / "raw_daily.parquet", index=False)
    raw_panel = (panel / "raw_daily.parquet").read_bytes()
    (panel / "result.json").write_text(json.dumps({"status": "source_cache_complete",
                                                   "raw_daily_sha256": sha256(raw_panel).hexdigest()}))
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": official.date}).to_csv(calendar, index=False)
    out, result = source.compare_saved(folder, sina, panel, calendar, tmp_path / "comparison")
    assert result["comparisons"][0]["sina"]["conflicts"] == []
    assert result["comparisons"][0]["baostock"]["conflicts"] == []
    assert not result["execution_admitted"] and not result["strategy_validated"]
    assert (out / "result.json").exists() and not (out / "raw.parquet").exists()
    assert (panel / "raw_daily.parquet").read_bytes() == raw_panel
    assert (sina / "510300.parquet").read_bytes() == raw_sina


def test_batch_and_cached_identity_are_bounded_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(source, "fetch", lambda *_: pytest.fail("Invalid input reached the network"))
    path = recipe(tmp_path)
    with pytest.raises(ValueError, match="1-16"):
        source.collect([f"510{number:03}" for number in range(17)], path, tmp_path / "large")
    cached = tmp_path / "wrong.json"
    cached.write_text(json.dumps(payload("510050")))
    with pytest.raises(ValueError, match="outside the batch"):
        source.collect(["510300"], path, tmp_path / "wrong", reuse=[cached])
    assert not (tmp_path / "large").exists() and not (tmp_path / "wrong").exists()


def named_snapshot():
    return {"code": "510300", "date": 20260924,
            "snap": [20260924, 160000, "fixture", 4.505, 4.578, 4.579, 4.512, 4.515,
                     710251931, 3220888135, 402600, 1817739, "FIXTURE_ONLY"]}


def statistics():
    return {"sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_CJGK_MRGK_C", "result": [
        {"SEC_CODE": "510300", "TX_DATE": "20260924", "DATE_TYPE": "D", "OPEN_PRICE": "4.578",
         "CLOSE_PRICE": "4.515", "HIGH_PRICE": "4.58", "LOW_PRICE": "4.51",
         "TRADE_VOL": "71065.45", "TRADE_AMT": "322270.59"}]}


def test_named_post_close_sample_support_never_extends_to_history():
    report = source.compare_basis(payload(), named_snapshot(), statistics(), "510300")
    assert report["sample_post_close_basis_supported"]
    assert report["activity"]["volume"]["statistics_minus_regular_minus_post_close"] == "-31.00"
    assert report["activity"]["amount"]["statistics_minus_regular_minus_post_close"] == "26.00"
    assert not report["execution_admitted"] and not report["strategy_validated"]
    snap = named_snapshot()
    snap["snap"][10] += 100
    assert not source.compare_basis(payload(), snap, statistics(), "510300")["sample_post_close_basis_supported"]


@pytest.mark.parametrize("problem", ["code", "date", "layout", "missing_day", "statistics_date"])
def test_basis_requires_same_security_date_and_named_layout(problem):
    snap, stat = named_snapshot(), statistics()
    if problem == "code":
        snap["code"] = "510050"
    elif problem == "date":
        snap["snap"][0] = 20260923
    elif problem == "layout":
        snap["snap"].pop()
    elif problem == "missing_day":
        snap["date"] = snap["snap"][0] = 20260925
        stat["result"][0]["TX_DATE"] = "20260925"
    else:
        stat["result"][0]["TX_DATE"] = "20260923"
    with pytest.raises(ValueError):
        source.compare_basis(payload(), snap, stat, "510300")


def test_saved_basis_consumer_pins_real_input_bytes(tmp_path, monkeypatch):
    folder, _ = collection(tmp_path, monkeypatch)
    snap, stat = tmp_path / "snap.json", tmp_path / "statistics.json"
    snap.write_text(json.dumps(named_snapshot()))
    stat.write_text(json.dumps(statistics()))
    out, report = source.basis_saved(folder, "510300", snap, stat, tmp_path / "basis")
    assert report["inputs"]["snapshot"]["sha256"] == sha256(snap.read_bytes()).hexdigest()
    assert report["sample_post_close_basis_supported"]
    assert (out / "result.json").exists() and not (out / "manifest.json").exists()
