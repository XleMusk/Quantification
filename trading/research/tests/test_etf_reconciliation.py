from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research.etf_reconciliation import (
    archived, distribution_coverage, inspect_baostock, reconcile, run, tencent_days,
)
from trading.research.full_market_etf import SOURCES


CALENDAR = ["2024-01-17", "2024-01-18", "2024-01-19"]


def test_annual_evidence_never_applies_to_another_fund(tmp_path):
    raw = b"%PDF-only a test fixture"
    (tmp_path / "report.pdf").write_bytes(raw)
    item = {"start": "2024-01-01", "end": "2024-12-31",
            "source_url": "https://www.sse.com.cn/fixture.pdf", "source_file": "report.pdf",
            "source_sha256": sha256(raw).hexdigest(), "events": []}
    (tmp_path / "distribution_periods.json").write_text(json.dumps([item]))
    empty = pd.DataFrame(columns=["ex_date", "cash_rmb_per_unit"])
    assert distribution_coverage(tmp_path, empty, "2024-01-01", "2024-12-31")["complete_for_requested_period"]
    assert not distribution_coverage(tmp_path, empty, "2024-01-01", "2024-12-31", "588000")["complete_for_requested_period"]


def payload():
    return {"code": 0, "data": {"sh510300": {"day": [
        [day, "3.501", "3.502", "3.600", "3.400", "1235.00", {}, "1.00", "43.2300", "0", "0"]
        for day in CALENDAR]}}}


def sina():
    return pd.DataFrame({"date": [day.replace("-", "") for day in CALENDAR],
                         "symbol": ["510300"] * 3, "open": [3.501] * 3,
                         "close": [3.502] * 3, "high": [3.6] * 3,
                         "low": [3.4] * 3, "volume": [123451] * 3})


def events():
    return pd.DataFrame({"ex_date": ["2024-01-18"], "cash_rmb_per_unit": ["0.069"]})


def test_actual_pair_candidates_keep_mill_quotes_cash_and_unknown_first_reference():
    paired, result = reconcile(sina(), tencent_days(payload()), CALENDAR, events())
    assert result["price_volume_candidate_reconciled"]
    assert result["volume_residual_min_units"] == -49
    assert paired.open_sina.iloc[0] == 3.501
    assert pd.isna(paired.reference_close_candidate_rmb.iloc[0])
    assert paired.reference_close_candidate_rmb.iloc[1] == pytest.approx(3.433)
    assert paired.amount_rmb_candidate.iloc[0] == 432300
    assert not {"tradestatus", "isST", "adjustflag"} & set(paired)


def test_missing_session_is_not_filled_or_bridged():
    paired, result = reconcile(sina().drop(index=1), tencent_days(payload()).drop(index=1), CALENDAR, events())
    assert result["missing_calendar_sessions"] == ["2024-01-18"]
    assert not result["price_volume_candidate_reconciled"]
    assert pd.isna(paired.reference_close_candidate_rmb.iloc[-1])


@pytest.mark.parametrize("field,value,key", [
    ("open", 3.503, "ohlc_conflict_rows"),
    ("volume", 123, "volume_candidate_conflict_rows"),
])
def test_unit_and_price_conflicts_never_pass(field, value, key):
    frame = sina()
    frame.loc[1, field] = value
    _, result = reconcile(frame, tencent_days(payload()), CALENDAR, events())
    assert result[key] == 1 and not result["price_volume_candidate_reconciled"]


def test_amount_candidate_must_imply_a_price_inside_ohlc():
    data = payload()
    data["data"]["sh510300"]["day"][1][8] = "4323.00"
    _, result = reconcile(sina(), tencent_days(data), CALENDAR, events())
    assert result["vwap_outside_ohlc_rows"] == 1


@pytest.mark.parametrize("mutation", ["qfq", "duplicate", "wrong_tick", "wrong_layout"])
def test_tencent_schema_has_no_adjustment_fallback_or_silent_rounding(mutation):
    data = deepcopy(payload())
    source = data["data"]["sh510300"]
    if mutation == "qfq":
        source["qfqday"] = source.pop("day")
    elif mutation == "duplicate":
        source["day"][1][0] = CALENDAR[0]
    elif mutation == "wrong_tick":
        source["day"][1][1] = "3.5011"
    else:
        source["day"][1] = source["day"][1][:6]
    with pytest.raises(ValueError):
        tencent_days(data)


def test_coverage_matches_period_table_but_cannot_extend_it(tmp_path):
    raw = b"%PDF-reviewed annual"
    (tmp_path / "annual.pdf").write_bytes(raw)
    periods = [{"start": "2024-01-01", "end": "2024-12-31", "source_url": "https://www.sse.com.cn/annual.pdf",
                "source_file": "annual.pdf", "source_sha256": sha256(raw).hexdigest(),
                "events": events().to_dict("records")}]
    (tmp_path / "distribution_periods.json").write_text(json.dumps(periods))
    result = distribution_coverage(tmp_path, events(), "2024-01-01", "2025-01-03")
    assert result["uncovered_periods"] == [{"start": "2025-01-01", "end": "2025-01-03"}]
    assert not result["complete_for_requested_period"]
    with pytest.raises(ValueError, match="conflict"):
        distribution_coverage(tmp_path, events().assign(cash_rmb_per_unit="0.07"), "2024-01-01", "2024-12-31")


def test_run_reads_receipts_writes_only_diagnostics_and_detects_tampering(tmp_path, monkeypatch):
    source, tx = tmp_path / "sina", tmp_path / "tencent"
    source.mkdir()
    tx.mkdir()
    (source / "supplements").mkdir()
    raw = b"%PDF-reviewed cash event"
    (source / "cash.pdf").write_bytes(raw)
    rows = events().assign(instrument_code="510300", exchange="SSE", announced_at="2024-01-11",
                           record_date="2024-01-17", pay_date="2024-01-23",
                           source_url="https://www.sse.com.cn/cash.pdf", source_file="cash.pdf",
                           source_sha256=sha256(raw).hexdigest())
    rows.to_csv(source / "supplements/cash_events.csv", index=False)
    target = source / "510300.parquet"
    sina().to_parquet(target, index=False)
    state = {"provider": "sina-ohlcv", "source": SOURCES["sina-ohlcv"], "receipts": {"510300": {
        "status": "acquired_unvalidated", "file": target.name, "sha256": sha256(target.read_bytes()).hexdigest(), "rows": 3}}}
    original = json.dumps(state).encode()
    (source / "status.json").write_bytes(original)
    body = json.dumps(payload()).encode()
    (tx / "tencent_response.json").write_bytes(body)
    receipt = {"url": "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param=sh510300,day,2024-01-01,2024-12-31,1000,",
               "file": "tencent_response.json", "sha256": sha256(body).hexdigest()}
    (tx / "receipt.json").write_text(json.dumps(receipt))
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": CALENDAR}).to_csv(calendar, index=False)
    monkeypatch.setattr("trading.research.etf_reconciliation.DATA_ROOT", tmp_path / "output")
    folder, result = run(source, tx, calendar)
    assert result["paired_rows"] == 3 and not result["execution_admitted"]
    assert not (folder / "manifest.json").exists()
    assert (source / "status.json").read_bytes() == original
    (tx / "tencent_response.json").write_text("{}")
    with pytest.raises(ValueError, match="hash changed"):
        run(source, tx, calendar)
    with pytest.raises(ValueError):
        archived(tx, {"file": "../sina/cash.pdf", "sha256": sha256(raw).hexdigest()})


def test_baostock_returned_raw_flag_and_fund_risk_flags_are_not_relabelled(tmp_path):
    paired, _ = reconcile(sina(), tencent_days(payload()), CALENDAR, events())
    raw = pd.DataFrame({"date": CALENDAR, "code": ["sh.510300"] * 3,
                        "open": ["3.501"] * 3, "close": ["3.502"] * 3,
                        "high": ["3.600"] * 3, "low": ["3.400"] * 3,
                        "preclose": ["3.500", "3.433", "3.502"], "volume": ["123451"] * 3,
                        "amount": ["432300"] * 3, "tradestatus": ["1"] * 3,
                        "isST": ["1"] * 3, "adjustflag": ["3"] * 3})
    entries = []
    for kind in ("raw", "adjusted"):
        path = tmp_path / (kind + ".parquet")
        raw.to_parquet(path, index=False)
        entries.append({"kind": kind, "file": path.name, "rows": 3, "sha256": sha256(path.read_bytes()).hexdigest()})
    receipt = {"status": "complete_source_acquisition", "symbol": "510300.SSE", "start": "2023-01-01",
               "end": CALENDAR[-1], "entries": entries}
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    report = inspect_baostock(tmp_path, paired, events(), ["2024-01-16", *CALENDAR])
    assert report["volume_conflict_rows"] == 0 and report["reference_candidate_conflict_rows"] == 0
    assert report["returned_adjusted_flags"] == ["3"]
    assert report["isST_raw_counts"] == {"1": 3} and not report["execution_admitted"]
    assert report["known_primary_cash_events_missing_from_provider"] == ["2024-01-18"]
    assert report["missing_requested_calendar_sessions"] == ["2024-01-16"]
    assert pd.read_parquet(tmp_path / "raw.parquet").isST.eq("1").all()
