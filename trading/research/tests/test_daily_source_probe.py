from contextlib import contextmanager
import json
from types import SimpleNamespace

import pandas as pd
import pyarrow.parquet as pq
import pytest

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from trading.research import daily_source_probe as probe


def frame():
    row = dict.fromkeys(BAOSTOCK_FIELDS.split(","), "")
    row.update(date="2025-12-31", code="sh.510300", open="4", high="4.2", low="3.9", close="4.1",
               preclose="4", volume="1000", amount="4100", adjustflag="3", tradestatus="1", isST="1")
    return pd.DataFrame([row])


def test_valid_payload_does_not_interpret_etf_st_field():
    assert probe.quality(frame(), "2025-12-31")["issues"] == []


@pytest.mark.parametrize("field,value,issue", [
    ("date", "2025-12-30", "response_date_mismatch"),
    ("adjustflag", "2", "not_raw_prices"),
    ("volume", "10", "amount_volume_price_inconsistent"),
    ("amount", "-1", "invalid_price_or_activity"),
    ("close", "", "missing_or_nonfinite_market_fields"),
    ("high", "3", "inconsistent_ohlc"),
    ("tradestatus", "0", "suspension_payload_contradiction"),
])
def test_quality_retains_invalid_fields(field, value, issue):
    payload = frame()
    payload.at[0, field] = value
    assert issue in probe.quality(payload, "2025-12-31")["issues"]
    assert payload.at[0, field] == value


def test_empty_success_is_not_an_observation():
    result = probe.quality(frame().iloc[:0], "2025-12-31")
    assert result == {"rows": 0, "issues": [], "empty": True}


def test_failed_second_query_preserves_first_receipt(tmp_path, monkeypatch):
    @contextmanager
    def session():
        def query(date):
            if date == "2025-12-30":
                raise TimeoutError("provider timed out")
            return SimpleNamespace(error_code="0", error_msg="success")
        yield SimpleNamespace(query_daily_history_k_ETF=query)

    monkeypatch.setattr(probe, "provider_frame", lambda result: frame())
    monkeypatch.setattr(probe.time, "sleep", lambda duration: None)
    folder, receipt = probe.collect(["2025-12-31", "2025-12-30"], ["etf"], {}, tmp_path / "probe", session)
    assert receipt["status"] == "failed_probe"
    assert len(receipt["entries"]) == 1
    assert receipt["failed_query"]["date"] == "2025-12-30"
    assert receipt["execution_admitted"] is False
    assert pd.read_csv(folder / "2025-12-31_etf.csv").shape[0] == 1
    assert json.loads((folder / "receipt.json").read_text())["status"] == "failed_probe"


def test_bound_before_connect_or_artifact(tmp_path):
    with pytest.raises(ValueError, match="six"):
        probe.collect(["2025-12-31"] * 7, ["etf"], {}, tmp_path / "probe")
    assert not (tmp_path / "probe").exists()


def test_wheel_hash_must_match_before_import(tmp_path):
    wheel = tmp_path / "unreviewed.whl"
    wheel.write_bytes(b"not the reviewed package")
    with pytest.raises(ValueError, match="reviewed official"):
        probe.select_sdk(wheel)


def test_suspended_missing_activity_is_missing_not_nonzero():
    payload = frame()
    payload.loc[0, ["open", "high", "low", "close", "preclose"]] = "4"
    payload.loc[0, ["volume", "amount"]] = ""
    payload.at[0, "tradestatus"] = "0"
    issues = probe.quality(payload, "2025-12-31")["issues"]
    assert "missing_or_nonfinite_market_fields" in issues
    assert "suspension_payload_contradiction" not in issues


def test_backfill_resume_empty_day_and_detect_changed_cache(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": ["2025-12-30", "2025-12-31"]}).to_csv(calendar, index=False)
    calls = []
    @contextmanager
    def session():
        def query(date):
            calls.append(date)
            return SimpleNamespace(error_code="0", error_msg="success", day=date)
        yield SimpleNamespace(query_daily_history_k_ETF=query)

    def payload(result):
        value = frame()
        return value.iloc[:0] if result.day == "2025-12-30" else value

    monkeypatch.setattr(probe, "provider_frame", payload)
    monkeypatch.setattr(probe.time, "sleep", lambda duration: None)
    args = (calendar, "2025-12-30", "2025-12-31", {"sha256": "fixed"}, tmp_path / "cache")
    folder, first_view, first = probe.backfill(*args, max_days=1, session_factory=session)
    assert first["status"] == "partial_backfill"
    assert first["empty_days"] == ["2025-12-30"]
    _, second_view, second = probe.backfill(*args, max_days=1, session_factory=session)
    assert second["status"] == "source_cache_complete"
    assert second["rows"] == second["symbols"] == 1
    assert second["execution_admitted"] is False
    assert first_view != second_view
    assert json.loads((first_view / "receipts.json").read_text())["status"] == "partial_backfill"
    assert json.loads((second_view / "receipts.json").read_text())["status"] == "source_cache_complete"
    assert calls == ["2025-12-30", "2025-12-31"]
    (folder / "2025-12-31_etf.csv").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        probe.backfill(*args, session_factory=session)


def test_stock_bulk_uses_stock_api_keeps_suspension_and_resumes(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": ["2025-12-30", "2025-12-31"]}).to_csv(calendar, index=False)
    calls = []

    @contextmanager
    def session():
        def query(date):
            calls.append(date)
            return SimpleNamespace(error_code="0", error_msg="success", day=date)
        yield SimpleNamespace(query_daily_history_k_AStock=query)

    def payload(result):
        value = frame()
        value.loc[0, ["date", "code", "tradestatus"]] = [result.day, "sh.600000", "0"]
        value.loc[0, ["open", "high", "low", "close", "preclose"]] = "4"
        value.loc[0, ["volume", "amount"]] = ""
        return value

    monkeypatch.setattr(probe, "provider_frame", payload)
    monkeypatch.setattr(probe.time, "sleep", lambda duration: None)
    args = (calendar, "2025-12-30", "2025-12-31", {"sha256": "fixed"}, tmp_path / "stock")
    folder, first_view, first = probe.backfill(*args, max_days=1, session_factory=session, kind="stock")
    assert first["kind"] == "stock" and first["acquired_days"] == 1
    assert first["quality_issue_days"]["2025-12-30"] == ["missing_or_nonfinite_market_fields"]
    raw = pd.read_parquet(first_view / "raw_daily.parquet")
    assert raw.loc[0, "volume"] == "" and raw.loc[0, "tradestatus"] == "0"
    saved = json.loads((folder / "receipts.json").read_text())
    saved["tasks"]["2025-12-30"]["quality"]["issues"].append("old_checker_issue")
    (folder / "receipts.json").write_text(json.dumps(saved))
    _, second_view, second = probe.backfill(*args, max_days=1, session_factory=session, kind="stock")
    assert second["status"] == "source_cache_complete" and second["rows"] == 2
    assert calls == ["2025-12-30", "2025-12-31"] and first_view != second_view
    assert "old_checker_issue" not in second["quality_issue_days"]["2025-12-30"]
    assert "old_checker_issue" in json.loads((second_view / "receipts.json").read_text())["tasks"]["2025-12-30"]["quality"]["issues"]
    assert second["execution_admitted"] is second["strategy_validated"] is False
    with pytest.raises(ValueError, match="scope changed"):
        probe.backfill(*args, session_factory=session, kind="etf")
    receipt = saved["tasks"]["2025-12-30"]
    with pytest.raises(ValueError, match="identity changed"):
        probe.cached_daily(folder, receipt)
    assert calls == ["2025-12-30", "2025-12-31"]


def test_newest_first_changes_acquisition_not_sorted_panel_or_scope(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": ["2025-12-29", "2025-12-30", "2025-12-31"]}).to_csv(calendar, index=False)
    calls = []

    @contextmanager
    def session():
        def query(date):
            calls.append(date)
            return SimpleNamespace(error_code="0", error_msg="success", day=date)
        yield SimpleNamespace(query_daily_history_k_AStock=query)

    def payload(result):
        value = frame()
        value.loc[0, ["date", "code"]] = [result.day, "sh.600000"]
        return value

    monkeypatch.setattr(probe, "provider_frame", payload)
    monkeypatch.setattr(probe.time, "sleep", lambda duration: None)
    monkeypatch.setattr(probe.pd, "concat", lambda *args, **kwargs: pytest.fail("Must not merge full stock history in memory"))
    args = (calendar, "2025-12-29", "2025-12-31", {"sha256": "fixed"}, tmp_path / "cache")
    folder, _, first = probe.backfill(*args, max_days=1, session_factory=session, kind="stock", newest_first=True)
    assert first["missing_days"] == ["2025-12-29", "2025-12-30"]
    _, view, result = probe.backfill(*args, max_days=2, session_factory=session, kind="stock", newest_first=True)
    assert calls == ["2025-12-31", "2025-12-30", "2025-12-29"]
    assert result["acquisition_order"] == "newest_first" and result["rows"] == 3
    assert pd.read_parquet(view / "raw_daily.parquet").date.tolist() == ["2025-12-29", "2025-12-30", "2025-12-31"]
    assert pq.ParquetFile(view / "raw_daily.parquet").metadata.num_row_groups == 3
    with pytest.raises(ValueError, match="acquisition order changed"):
        probe.backfill(*args, session_factory=session, kind="stock")
    assert len(calls) == 3


def test_cli_brief_keeps_full_result_and_stock_scope(tmp_path, monkeypatch, capsys):
    summary = {"status": "partial_backfill", "kind": "stock", "rows": 42, "execution_admitted": False,
               "missing_days": ["2026-01-05"], "empty_days": [], "quality_issue_days": {"2026-01-06": ["missing"]},
               "current_quality": {"2026-01-06": {"rows": 42}}}
    calls = []
    monkeypatch.setattr(probe, "select_sdk", lambda wheel: {})
    def backfill(*args, **kwargs):
        calls.append(kwargs)
        return tmp_path, tmp_path / "view", summary
    monkeypatch.setattr(probe, "backfill", backfill)
    assert probe.backfill_main(["--sdk-wheel", "reviewed.whl", "--calendar", "calendar.csv", "--start", "2023-01-01",
                                "--end", "2026-09-30", "--kind", "stock", "--newest-first", "--brief"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["rows"] == 42 and output["missing_day_count"] == output["quality_issue_day_count"] == 1
    assert output["empty_day_count"] == 0 and "current_quality" not in output
    assert calls == [{"kind": "stock", "newest_first": True, "timeout_seconds": 30}]
    assert summary["missing_days"] == ["2026-01-05"] and summary["current_quality"]


@pytest.mark.parametrize("fail", [False, True])
def test_backfill_explicit_bounded_timeout_restored_on_success_or_failure(tmp_path, monkeypatch, fail):
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": ["2025-12-31"]}).to_csv(calendar, index=False)
    original = probe.socket.getdefaulttimeout()

    @contextmanager
    def session():
        assert probe.socket.getdefaulttimeout() == 60
        def query(date):
            assert probe.socket.getdefaulttimeout() == 60
            if fail:
                raise TimeoutError("delayed daily source")
            return SimpleNamespace(error_code="0", error_msg="success")
        yield SimpleNamespace(query_daily_history_k_ETF=query)

    monkeypatch.setattr(probe, "provider_frame", lambda response: frame())
    _, view, result = probe.backfill(calendar, "2025-12-31", "2025-12-31", {"sha256": "fixed"},
                                     tmp_path / "cache", session_factory=session, timeout_seconds=60)
    assert probe.socket.getdefaulttimeout() == original
    assert result["request_timeout_seconds"] == 60
    assert result["status"] == ("failed_backfill" if fail else "source_cache_complete")
    assert result["acquired_days"] == (0 if fail else 1)
    assert len(json.loads((view / "receipts.json").read_text())["failures"]) == int(fail)


@pytest.mark.parametrize("seconds", [0, 121])
def test_backfill_timeout_bound_precedes_connection_or_artifact(tmp_path, seconds):
    with pytest.raises(ValueError, match="bounded"):
        probe.backfill(tmp_path / "missing.csv", "2025-12-31", "2025-12-31", {}, tmp_path / "cache",
                       timeout_seconds=seconds)
    assert not (tmp_path / "cache").exists()


def adjustment_frame(day="2025-07-16"):
    return pd.DataFrame([dict(zip(probe.ADJUSTMENT_FIELDS,
                                 ["sh.600000", day, "1.000000", "2.123456", "2.123456"]))])


@pytest.mark.parametrize("field,value,issue", [
    ("dividOperateDate", "2025-07-15", "response_date_mismatch"),
    ("code", "bj.830001", "invalid_or_duplicate_identity"),
    ("adjustFactor", "", "missing_nonfinite_or_nonpositive_factor"),
    ("foreAdjustFactor", "0", "missing_nonfinite_or_nonpositive_factor"),
    ("backAdjustFactor", "inf", "missing_nonfinite_or_nonpositive_factor"),
])
def test_adjustment_quality_preserves_invalid_source(field, value, issue):
    payload = adjustment_frame()
    payload.at[0, field] = value
    assert issue in probe.quality(payload, "2025-07-16", "adjustment")["issues"]
    assert payload.at[0, field] == value


def test_adjustment_probe_is_not_price_or_cash_admission(tmp_path, monkeypatch):
    @contextmanager
    def session():
        yield SimpleNamespace(query_daily_adjust_factor=lambda date:
                              SimpleNamespace(day=date, error_code="0", error_msg="success"))

    monkeypatch.setattr(probe, "provider_frame", lambda result: adjustment_frame(result.day))
    monkeypatch.setattr(probe.time, "sleep", lambda duration: None)
    folder, receipt = probe.collect(["2025-07-16"], ["adjustment"], {}, tmp_path / "probe", session)
    assert receipt["status"] == "completed_probe" and receipt["execution_admitted"] is False
    item = receipt["entries"][0]
    assert item["method"] == "query_daily_adjust_factor" and item["quality"]["issues"] == []
    assert item["quality"]["factor_semantics_verified"] is False
    assert probe.cached_daily(folder, item, "adjustment").equals(adjustment_frame())


def test_adjustment_backfill_retains_empty_events_and_separate_panel(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": ["2025-07-15", "2025-07-16"]}).to_csv(calendar, index=False)
    calls = []
    @contextmanager
    def session():
        def query(date):
            calls.append(date)
            return SimpleNamespace(day=date, error_code="0", error_msg="success")
        yield SimpleNamespace(query_daily_adjust_factor=query)

    def payload(result):
        value = adjustment_frame(result.day)
        return value.iloc[:0] if result.day == "2025-07-15" else value
    monkeypatch.setattr(probe, "provider_frame", payload)
    monkeypatch.setattr(probe.time, "sleep", lambda duration: None)
    args = (calendar, "2025-07-15", "2025-07-16", {"sha256": "fixed"}, tmp_path / "cache")
    folder, _, first = probe.backfill(*args, max_days=1, session_factory=session, kind="adjustment", newest_first=True)
    assert first["acquired_days"] == 1 and first["rows"] == 1
    _, view, result = probe.backfill(*args, max_days=1, session_factory=session, kind="adjustment", newest_first=True)
    assert result["status"] == "source_cache_complete" and result["acquired_days"] == 2
    assert result["empty_days"] == ["2025-07-15"]
    assert result["panel_file"] == "adjustment_events.parquet" and "raw_daily_sha256" not in result
    assert pd.read_parquet(view / result["panel_file"]).equals(adjustment_frame())
    assert not (view / "raw_daily.parquet").exists()
    assert calls == ["2025-07-16", "2025-07-15"]
    assert result["execution_admitted"] is result["strategy_validated"] is False
    with pytest.raises(ValueError, match="identity changed"):
        probe.cached_daily(folder, json.loads((folder / "receipts.json").read_text())["tasks"]["2025-07-16"], "stock")
