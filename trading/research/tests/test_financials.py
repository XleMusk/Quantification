from contextlib import contextmanager
from hashlib import sha256
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading.research.financials import (
    FIELDS, PERIODS, align_market, asof_panel, build_panel, collect, create, import_probe, normalize,
)
from trading.config import StrategyConfig
from trading.research.factors import research_eligibility


def report(pub="2024-04-30", stat="2024-03-31"):
    return pd.DataFrame([{**{name: "" for name in FIELDS}, "code": "sh.600000", "pubDate": pub,
                          "statDate": stat, "roeAvg": "0.023738", "netProfit": "17660000000.000000"}])


def test_normalization_does_not_convert_units_or_fill_missing_bank_fields():
    frame = normalize(report(), "sh.600000", 2024, 1)
    assert frame.netProfit.iloc[0] == 17660000000
    assert frame.roeAvg.iloc[0] == .023738
    assert pd.isna(frame.gpMargin.iloc[0]) and pd.isna(frame.MBRevenue.iloc[0])


@pytest.mark.parametrize("field,value", [("pubDate", ""), ("pubDate", "2024-03-01"),
                                          ("statDate", "2024-06-30"), ("roeAvg", "inf")])
def test_bad_dates_quarter_identity_and_nonfinite_values_fail(field, value):
    frame = report()
    frame.loc[0, field] = value
    with pytest.raises(ValueError):
        normalize(frame, "sh.600000", 2024, 1)


def test_unknown_intraday_publication_time_never_admits_same_day_or_stat_date():
    reports = normalize(report(), "sh.600000", 2024, 1)
    calendar = ["2024-03-29", "2024-04-29", "2024-04-30", "2024-05-06"]
    panel = asof_panel(reports, calendar, ["600000.SSE", "600004.SSE"])
    bank = panel.loc[panel.symbol.eq("600000.SSE")]
    assert bank.source_pub_date.tolist() == ["", "", "", "2024-04-30"]
    assert bank.roeAvg_provider_value.notna().tolist() == [False, False, False, True]
    assert panel.loc[panel.symbol.eq("600004.SSE"), "netProfit_provider_value"].isna().all()


def test_late_older_quarter_does_not_replace_newer_available_period_and_stale_expires():
    older = normalize(report("2024-06-01", "2023-12-31"), "sh.600000", 2023, 4)
    newer = normalize(report(), "sh.600000", 2024, 1)
    panel = asof_panel(pd.concat([older, newer]), ["2024-05-06", "2024-06-03", "2025-06-03"], ["600000.SSE"])
    assert panel.source_stat_date.tolist() == ["2024-03-31", "2024-03-31", ""]
    assert pd.isna(panel.netProfit_provider_value.iloc[-1])
    with pytest.raises(ValueError, match="revisions"):
        asof_panel(pd.concat([newer, newer]), ["2024-05-06"], ["600000.SSE"])


def test_collector_resume_and_actual_panel_consumer_keep_all_empty_reports(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({"status": "ready", "symbols": ["600000.SSE"], "end": "2026-09-24"}))
    pd.DataFrame({"symbol": ["600000.SSE"], "code": ["sh.600000"], "ipoDate": ["1999-11-10"], "outDate": [""]}).to_csv(snapshot / "instruments.csv", index=False)
    pd.DataFrame({"date": ["2024-04-30", "2024-05-06"]}).to_csv(snapshot / "calendar.csv", index=False)
    folder = create(snapshot, tmp_path / "financials")
    calls = []

    def query(code, year, quarter):
        calls.append((code, year, quarter))
        frame = report() if (year, quarter) == (2024, 1) else pd.DataFrame(columns=FIELDS)
        return SimpleNamespace(error_code="0", error_msg="", fields=list(FIELDS), get_data=lambda: frame)

    @contextmanager
    def session():
        yield SimpleNamespace(query_profit_data=query)

    monkeypatch.setattr("trading.research.financials.baostock_session", session)
    state = collect(folder, 1)
    assert len(calls) == len(PERIODS) == 15
    assert state["complete_symbols"] == 1 and state["report_rows"] == 1
    assert sum(row["status"] == "empty_provider_result" for row in state["tasks"].values()) == 14
    collect(folder, 1)
    assert len(calls) == 15
    view, result = build_panel(folder)
    assert result["daily_rows"] == 2 and result["rows_with_available_report"] == 1
    assert result["declared_symbols"] == result["symbols_with_reports"] == 1
    assert result["empty_quarter_results"] == 14
    assert sha256((view / "source_receipts.json").read_bytes()).hexdigest() == result["source_receipts_sha256"]
    assert not result["revision_safe_pit_verified"] and not result["execution_admitted"]
    panel = pd.read_parquet(view / "publication_lagged.parquet")
    assert not {"score", "target_weight", "tradestatus"} & set(panel)
    entry = state["tasks"]["600000.SSE:2024Q1"]
    (folder / entry["file"]).write_text("tampered")
    with pytest.raises(ValueError, match="receipt changed"):
        build_panel(folder)


def test_collection_stops_on_invalid_source_not_silently_empty(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({"status": "ready", "symbols": ["600000.SSE"], "end": "2026-09-24"}))
    pd.DataFrame({"symbol": ["600000.SSE"], "code": ["sh.600000"], "ipoDate": ["1999-11-10"], "outDate": [""]}).to_csv(snapshot / "instruments.csv", index=False)
    folder = create(snapshot, tmp_path / "financials")

    @contextmanager
    def session():
        yield SimpleNamespace(query_profit_data=lambda *args: SimpleNamespace(error_code="1", error_msg="failed"))

    monkeypatch.setattr("trading.research.financials.baostock_session", session)
    with pytest.raises(RuntimeError):
        collect(folder, 1)
    state = json.loads((folder / "receipts.json").read_text())
    assert state["status"] == "stopped_source_error" and not state["tasks"]
    assert state["failed_task"] == "600000.SSE:2022Q4"

    @contextmanager
    def recovered_session():
        yield SimpleNamespace(query_profit_data=lambda *args: SimpleNamespace(
            error_code="0", error_msg="", fields=list(FIELDS),
            get_data=lambda: pd.DataFrame(columns=FIELDS)))

    monkeypatch.setattr("trading.research.financials.baostock_session", recovered_session)
    recovered = collect(folder, 1)
    assert recovered["status"] == "complete_acquisition" and recovered["complete_symbols"] == 1
    assert recovered["last_error"] is None and recovered["failed_task"] is None
    assert len(recovered["tasks"]) == len(PERIODS) and recovered["report_rows"] == 0
    assert all(entry["status"] == "empty_provider_result" for entry in recovered["tasks"].values())


def test_failed_login_records_terminal_source_state_and_current_counts(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({"status": "ready", "symbols": ["600000.SSE"], "end": "2026-09-24"}))
    pd.DataFrame({"symbol": ["600000.SSE"], "code": ["sh.600000"], "ipoDate": ["1999-11-10"], "outDate": [""]}).to_csv(snapshot / "instruments.csv", index=False)
    folder = create(snapshot, tmp_path / "financials")

    @contextmanager
    def session():
        raise ConnectionError("peer closed")
        yield

    monkeypatch.setattr("trading.research.financials.baostock_session", session)
    with pytest.raises(ConnectionError, match="peer closed"):
        collect(folder, 1)
    state = json.loads((folder / "receipts.json").read_text())
    assert state["status"] == "stopped_source_error" and state["failed_task"] is None
    assert state["complete_symbols"] == state["report_rows"] == 0
    assert state["expected_symbols"] == 1 and not state["tasks"]


def test_probe_import_preserves_values_and_rejects_changed_source(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({"status": "ready", "symbols": ["600000.SSE"], "end": "2026-09-24"}))
    pd.DataFrame({"symbol": ["600000.SSE"], "code": ["sh.600000"], "ipoDate": ["1999-11-10"], "outDate": [""]}).to_csv(snapshot / "instruments.csv", index=False)
    folder = create(snapshot, tmp_path / "financials")
    probe = tmp_path / "probe"
    probe.mkdir()
    path = probe / "sh.600000_2024Q1_profit.csv"
    report().to_csv(path, index=False)
    entry = {"file": path.name, "code": "sh.600000", "year": 2024, "quarter": 1,
             "rows": 1, "sha256": sha256(path.read_bytes()).hexdigest()}
    (probe / "receipt.json").write_text(json.dumps({"status": "completed_probe", "entries": [entry]}))
    state = import_probe(folder, probe)
    assert list(state["tasks"]) == ["600000.SSE:2024Q1"]
    assert pd.read_csv(folder / state["tasks"]["600000.SSE:2024Q1"]["file"]).netProfit.iloc[0] == 17660000000
    import_probe(folder, probe)
    path.write_text("tampered")
    with pytest.raises(ValueError, match="receipt changed"):
        import_probe(folder, probe)


def test_broader_historical_master_is_explicit_and_keeps_unacquired_codes(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({"status": "ready", "symbols": ["600000.SSE"], "end": "2026-09-24"}))
    source = pd.DataFrame({"symbol": ["600000.SSE"], "code": ["sh.600000"], "ipoDate": ["1999-11-10"], "outDate": [""]})
    source.to_csv(snapshot / "instruments.csv", index=False)
    master = tmp_path / "universe.csv"
    pd.concat([source, pd.DataFrame({"symbol": ["600004.SSE"], "code": ["sh.600004"], "ipoDate": ["2003-04-28"], "outDate": ["2026-07-01"]})]).to_csv(master, index=False)
    folder = create(snapshot, tmp_path / "financials", universe=master)
    plan = json.loads((folder / "plan.json").read_text())
    assert len(plan["symbols"]) == 2 and plan["symbols"][1]["outDate"] == "2026-07-01"
    master.write_text("tampered")
    from trading.research.financials import read_plan
    with pytest.raises(ValueError, match="universe source changed"):
        read_plan(folder)


def market_fixture(tmp_path):
    folder = tmp_path / "market"
    folder.mkdir()
    calendar = pd.bdate_range("2024-05-06", periods=25).strftime("%Y-%m-%d").tolist()
    symbols = ["600000.SSE", "600004.SSE", "600008.SSE"]
    rows = [{"date": day, "symbol": symbol, "close": 10., "volume": 3000000., "amount": 30000000.,
             "tradestatus": 1, "isST": 0} for day in calendar for symbol in symbols]
    raw = pd.DataFrame(rows)
    raw.loc[raw.symbol.eq(symbols[1]) & raw.date.eq(calendar[20]), "tradestatus"] = 0
    raw.loc[raw.symbol.eq(symbols[1]) & raw.date.eq(calendar[21]), "isST"] = 1
    raw = raw.loc[~(raw.symbol.eq(symbols[1]) & raw.date.eq(calendar[-1]))].copy()
    instruments = {symbol: {"ipoDate": "1999-11-10", "outDate": ""} for symbol in symbols}
    instruments[symbols[1]]["outDate"] = calendar[22]
    instruments[symbols[2]]["ipoDate"] = calendar[20]
    manifest = {"status": "ready", "selection_date": "2023-12-29", "symbols": symbols}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    raw.to_parquet(folder / "raw.parquet", index=False)
    raw.to_parquet(folder / "adjusted.parquet", index=False)
    pd.DataFrame({"date": calendar}).to_csv(folder / "calendar.csv", index=False)
    pd.DataFrame.from_dict(instruments, orient="index").to_csv(folder / "instruments.csv")
    pd.DataFrame(columns=["symbol"]).to_csv(folder / "actions.csv", index=False)
    return SimpleNamespace(folder=folder, raw=raw, calendar=calendar, instruments=instruments,
                           symbols=symbols, manifest=manifest)


def financial_view_fixture(tmp_path, calendar):
    folder = tmp_path / "view"
    folder.mkdir()
    symbols = ["600000.SSE", "600004.SSE", "600008.SSE", "600009.SSE"]
    reports = []
    for symbol in symbols:
        frame = report()
        code = "sh." + symbol.split(".")[0]
        frame["code"] = code
        reports.append(normalize(frame, code, 2024, 1))
    panel = asof_panel(pd.concat(reports, ignore_index=True), calendar, symbols)
    path = folder / "publication_lagged.parquet"
    panel.to_parquet(path, index=False)
    (folder / "result.json").write_text(json.dumps({"status": "completed_publication_lagged_provider_view",
        "panel_sha256": sha256(path.read_bytes()).hexdigest(), "daily_rows": len(panel), "limitations": []}))
    return folder


def test_shared_pool_respects_dated_status_liquidity_and_no_future_dependency(tmp_path):
    feed = market_fixture(tmp_path)
    eligible = research_eligibility(feed, StrategyConfig())
    day = pd.Timestamp(feed.calendar[-1])
    assert eligible.loc[day, "600000.SSE"]
    assert not eligible.loc[day, "600008.SSE"]
    assert not eligible.loc[pd.Timestamp(feed.calendar[20]), "600004.SSE"]
    assert not eligible.loc[pd.Timestamp(feed.calendar[21]), "600004.SSE"]
    assert not eligible.loc[pd.Timestamp(feed.calendar[22]), "600004.SSE"]
    assert not eligible.loc[pd.Timestamp(feed.calendar[18]), "600000.SSE"]
    feed.raw.loc[feed.raw.date.eq(feed.calendar[-1]), "amount"] = 1.
    changed = research_eligibility(feed, StrategyConfig())
    pd.testing.assert_series_equal(eligible.loc[:pd.Timestamp(feed.calendar[-2])], changed.loc[:pd.Timestamp(feed.calendar[-2])])
    feed.raw.loc[feed.raw.symbol.eq("600000.SSE"), "amount"] = 100.
    assert not research_eligibility(feed, StrategyConfig()).xs("600000.SSE", level="symbol").any()


def test_alignment_keeps_missing_and_delisted_rows_but_masks_ineligible_financial_fields(tmp_path, monkeypatch):
    feed = market_fixture(tmp_path)
    view = financial_view_fixture(tmp_path, feed.calendar)
    monkeypatch.setattr("trading.research.financials.HistoricalFeed", lambda snapshot: feed)
    original = (view / "publication_lagged.parquet").read_bytes()
    destination, result = align_market(view, feed.folder)
    joined = pd.read_parquet(destination / "aligned.parquet")
    assert len(joined) == 100 and result["symbols"] == 4
    assert result["rows_without_market_observation"] == 26
    assert result["rows_usable_for_factor_research"] == 7
    assert result["available_reports_outside_pool"] == 93
    assert joined.loc[~joined.research_eligible, "netProfit_provider_value"].isna().all()
    assert joined.loc[joined.research_eligible, "netProfit_provider_value"].eq(17660000000).all()
    missing = joined.loc[joined.symbol.eq("600009.SSE")]
    assert missing.tradestatus.isna().all() and not missing.research_eligible.any()
    assert (view / "publication_lagged.parquet").read_bytes() == original
    assert not result["execution_admitted"] and not result["revision_safe_pit_verified"]
    assert not {"score", "target_weight", "label"} & set(joined)


def test_alignment_rejects_changed_source_or_mismatched_calendar(tmp_path, monkeypatch):
    feed = market_fixture(tmp_path)
    view = financial_view_fixture(tmp_path, feed.calendar)
    monkeypatch.setattr("trading.research.financials.HistoricalFeed", lambda snapshot: feed)
    feed.calendar = feed.calendar[:-1]
    with pytest.raises(ValueError, match="calendar or path mismatch"):
        align_market(view, feed.folder)
    (view / "publication_lagged.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Financial view source changed"):
        align_market(view, feed.folder)
