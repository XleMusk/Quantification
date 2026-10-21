"""Tiny real native/full-close components; no live data, model fit or Engine."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import baostock_all_stock_probe as roster_module
from trading.research import baostock_bulk_close as bulk
from trading.research import native_history_chain_source as m
from trading.research import shortline_native_event_panel as producer
from trading.research.baostock_daily_slice import FIELDS, digest
from trading.research.baostock_native_history import RAW_FIELDS
from trading.research.tests.test_native_history_source import save, read, write_panel


BASE_DAYS = ["2026-09-28", "2026-09-29", "2026-09-30"]
EXT_DAYS = ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"]
CLOSE_DAYS = ["2026-10-14", "2026-10-15"]
DAYS = BASE_DAYS + EXT_DAYS + CLOSE_DAYS
A, B, C, U, H = "000001.SZSE", "300114.SZSE", "302132.SZSE", "600001.SSE", "000002.SZSE"
NOW = datetime.fromisoformat("2026-10-16T10:00:00+08:00")


def _rows(day, *, strict=False):
    closes = [10., 10., 10., 11., 12.1, 13.31, 14.64, 14.6, 16.06]
    i = DAYS.index(day)
    rows = []
    for code in ("sz.000001", "sz.000002", "sz.300114", "sz.302132", "sh.600001"):
        close = closes[i] if code == "sz.000001" else 10.
        previous = closes[i - 1] if code == "sz.000001" and i else 10.
        row = {"date": day, "code": code, "open": str(close), "high": str(close + .02),
            "low": str(close - .02), "close": str(close), "preclose": str(previous),
            "volume": "1000", "amount": str(close * 1000), "adjustflag": "3", "turn": "2.345600",
            "tradestatus": "1", "pctChg": str((close / previous - 1) * 100),
            "peTTM": "-12.340000", "pbMRQ": "1.0000", "psTTM": "2.0000", "pcfNcfTTM": "", "isST": "0"}
        if code == "sz.000002":
            row.update(tradestatus="0", open="10.0", high="10.0", low="10.0", close="10.0",
                volume="", amount="", pctChg="", turn="")
        elif code == "sh.600001" and not strict:
            row.update(tradestatus="", isST="", turn="")
        rows.append(row)
    return rows


def _native(root, days, available):
    root.mkdir()
    calendar = root / "source_calendar.csv"
    pd.DataFrame({"date": days}).to_csv(calendar, index=False)
    groups, tasks = [], {}
    for day in days:
        rows = _rows(day)
        groups.append(rows)
        csv = root / (day + "_stock.csv")
        pd.DataFrame(rows, columns=list(RAW_FIELDS)).to_csv(csv, index=False)
        tasks[day] = {"date": day, "kind": "stock", "method": "query_daily_history_k_AStock",
            "file": csv.name, "fields": ",".join(RAW_FIELDS), "sha256": digest(csv), "error_code": "0",
            "quality": {"rows": len(rows), "symbols": len(rows)}}
    raw = root / "raw_daily.parquet"
    write_panel(raw, groups)
    save(root / "plan.json", {"kind": "stock", "days": days, "calendar_source": str(calendar),
        "calendar_sha256": digest(calendar), "requested_start": days[0], "requested_end": days[-1],
        "execution_admitted": False, "historical_universe_independently_verified": False})
    save(root / "receipts.json", {"status": "source_cache_complete", "execution_admitted": False,
        "expected_days": len(days), "updated_at": available, "tasks": tasks, "failures": []})
    save(root / "result.json", {"status": "source_cache_complete", "kind": "stock",
        "expected_days": len(days), "acquired_days": len(days), "rows": sum(map(len, groups)), "symbols": 5,
        "missing_days": [], "raw_daily_sha256": digest(raw), "panel_file": "raw_daily.parquet",
        "panel_sha256": digest(raw), "plan_sha256": digest(root / "plan.json"),
        "receipts_sha256": digest(root / "receipts.json"), "execution_admitted": False,
        "selection_enabled": False, "strategy_validated": False})
    return root


def _close(tmp_path, day, calendar, sdk, actual):
    root = tmp_path / day
    root.mkdir()
    probe, roster = root / "probe", root / "roster"
    probe.mkdir()
    roster.mkdir()
    raw = pd.DataFrame(_rows(day, strict=True), columns=list(RAW_FIELDS))
    file = probe / (day + "_stock.csv")
    raw.to_csv(file, index=False)
    save(probe / "receipt.json", {"status": "completed_probe",
        "created_at": (actual - timedelta(seconds=5)).isoformat(),
        "finished_at": (actual - timedelta(seconds=3)).isoformat(), "sdk": sdk,
        "implementation_sha256": digest(bulk.daily_source_probe.__file__), "days": [day], "kinds": ["stock"],
        "execution_admitted": False, "full_history_verified": False,
        "entries": [{"date": day, "kind": "stock", "method": "query_daily_history_k_AStock", "error_code": "0",
            "file": file.name, "sha256": digest(file),
            "quality": {"rows": 5, "symbols": 5, "issues": ["missing_or_nonfinite_market_fields"]}}]})
    roster_rows = pd.DataFrame([[row.code, row.tradestatus, row.code] for row in raw.itertuples()],
        columns=["code", "tradeStatus", "code_name"])
    roster_file = roster / ("query_all_stock_" + day + ".csv")
    roster_rows.to_csv(roster_file, index=False)
    save(roster / "receipt.json", {"schema": "baostock_historical_all_stock_status_probe_v1", "day": day,
        "response_day": day, "method": "query_all_stock", "kwargs": {"day": day}, "sdk": sdk,
        "status": "completed_provider_status_list", "quality_issues": [], "provider_error_code": "0",
        "fields_returned": list(roster_rows), "execution_admitted": False, "price_bars_generated": False,
        "raw_csv": str(roster_file), "raw_sha256": digest(roster_file), "rows_preserved": 5,
        "completed_at": (actual - timedelta(seconds=1)).isoformat()})
    destination = root / "package"
    bulk.import_bulk_close(probe, roster, calendar, destination, now=actual)
    return destination


@pytest.fixture
def chain(tmp_path, monkeypatch):
    calendar = tmp_path / "exchange_2026.json"
    save(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    wheel = tmp_path / "synthetic.whl"
    wheel.write_bytes(b"synthetic SDK provenance only, never installed or called")
    sdk_hash = digest(wheel)
    monkeypatch.setattr(bulk, "WHEEL_SHA256", sdk_hash)
    monkeypatch.setattr(roster_module, "WHEEL_SHA256", sdk_hash)
    sdk = {"wheel": str(wheel), "sha256": sdk_hash, "distribution_version": "0.9.4"}
    base = _native(tmp_path / "base", BASE_DAYS, "2026-10-15T18:00:00+08:00")
    extension = _native(tmp_path / "extension", EXT_DAYS, "2026-10-15T18:10:00+08:00")
    closes = [_close(tmp_path, day, calendar, sdk,
        datetime.fromisoformat("2026-10-15T" + hour + ":00:00+08:00"))
        for day, hour in zip(CLOSE_DAYS, ("20", "21"))]
    return {"base": base, "extension": extension, "closes": closes, "calendar": calendar}


def _source(chain, *, extensions=None, closes=None):
    return m.NativeHistoryChainSource(chain["base"], [chain["extension"]] if extensions is None else extensions,
        chain["closes"] if closes is None else closes, calendar=chain["calendar"])


def test_exact_holiday_component_calendar_original_codes_status_and_missing_extras(chain):
    with _source(chain) as source:
        assert source.calendar == DAYS
        assert source.calendar[source.calendar.index("2026-10-08") - 1] == "2026-09-30"
        native = source.frame("2026-10-13")
        assert {B, C} <= set(native.symbol)
        assert set(native.loc[native.symbol.isin([B, C]), "code"]) == {"sz.300114", "sz.302132"}
        unknown = native.loc[native.symbol.eq(U)].iloc[0]
        assert unknown.tradestatus == unknown.isST == ""
        assert pd.isna(unknown.tradestatus_value) and pd.isna(unknown.isST_value)
        assert native.loc[native.symbol.eq(A), "turn"].item() == "2.345600"
        assert native.loc[native.symbol.eq(A), "peTTM"].item() == "-12.340000"
        close = source.frame("2026-10-14")
        assert list(close.columns[:13]) == FIELDS.split(",")
        assert not {"turn", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"}.intersection(close.columns)
        halted = close.loc[close.symbol.eq(H)].iloc[0]
        assert halted.tradestatus == "0" and halted.tradestatus_value == 0
        assert halted.volume == halted.amount == ""
        assert set(close.symbol) == {A, B, C, U, H}
        assert source.frame("2026-10-15", [B, C]).symbol.tolist() == sorted([B, C])
        with pytest.raises(ValueError):
            source.frame("2026-10-07")


def test_source_proofs_retain_physical_owner_and_component_actual_clocks(chain):
    with _source(chain) as source:
        proof = source.provenance
        assert proof["source_available_at"] == "2026-10-15T21:00:00+08:00"
        assert [row["day"] for row in proof["day_sources"]] == DAYS
        assert [row["logical_session_index"] for row in proof["day_sources"]] == list(range(len(DAYS)))
        native = source.frame("2026-10-13").attrs["provenance"]
        close = source.frame("2026-10-14").attrs["provenance"]
        assert native["row_group"] == 3 and native["close_file"] is None
        assert native["source_available_at"] == "2026-10-15T18:10:00+08:00"
        assert close["row_group"] is None and close["close_file"] == str(chain["closes"][0] / "raw_daily.csv")
        assert close["source_available_at"] == "2026-10-15T20:00:00+08:00"
        assert len(native["original_raw_fields"]) == 18 and len(close["original_raw_fields"]) == 13
        proof["day_sources"].clear()
        assert len(source.provenance["day_sources"]) == len(DAYS)
        assert source.provenance["missing_extras_filled"] is False
        assert source.provenance["source_components_rewritten"] is False


def test_real_producer_crosses_storage_boundary_with_exact_witness_and_prefix_features(chain, monkeypatch):
    monkeypatch.setattr(producer, "_now", lambda: NOW)
    with _source(chain) as source:
        prefix = producer.produce(source, through="2026-10-13", observed_at=NOW)
        complete = producer.produce(source, observed_at=NOW)
    before = [row for row in prefix["events"] if row["symbol"] == A]
    after = [row for row in complete["events"] if row["symbol"] == A and row["date"] <= "2026-10-13"]
    assert [row["stage"] for row in before] == [1, 2, 3]
    assert all(row["label_witness_day"] is None and row["hit_4"] is None for row in before)
    assert all(row["run_end_date"] == "2026-10-13" for row in after)
    assert all(row["label_witness_day"] == "2026-10-14" and row["hit_4"] == 1 and row["hit_5"] == 0 for row in after)
    assert all(row["label_witness_source_available_at"] == "2026-10-15T20:00:00+08:00" for row in after)
    assert all(row["label_available_at"] == NOW.isoformat() for row in after)
    assert prefix["event_features"] == [row for row in complete["event_features"] if row["date"] <= "2026-10-13"]
    assert not set(producer._LABEL_FIELDS).intersection(prefix["event_features"][0])
    assert next(row for row in complete["events"] if row["symbol"] == A and row["date"] == "2026-10-15")["stage"] == 1


@pytest.mark.parametrize("fault", ["duplicate_view", "gap", "reverse_closes", "duplicate_close"])
def test_duplicate_gap_or_reordered_components_are_not_silently_reconciled(chain, tmp_path, fault):
    extensions, closes = [chain["extension"]], chain["closes"]
    if fault == "duplicate_view":
        extensions = [chain["extension"], chain["extension"]]
    elif fault == "gap":
        extensions = [_native(tmp_path / "gap", EXT_DAYS[1:], "2026-10-15T18:10:00+08:00")]
    elif fault == "reverse_closes":
        closes = list(reversed(closes))
    else:
        closes = closes + closes[-1:]
    with pytest.raises(ValueError):
        _source(chain, extensions=extensions, closes=closes)


@pytest.mark.parametrize("fault", ["calendar", "raw_native", "close_receipt_clock"])
def test_calendar_source_or_future_completion_changes_are_rejected(chain, fault):
    if fault == "close_receipt_clock":
        receipt_file = chain["closes"][0] / "receipt.json"
        receipt = read(receipt_file)
        receipt["completed_at"] = receipt["updated_at"] = "2099-10-15T20:00:00+08:00"
        save(receipt_file, receipt)
        with pytest.raises(ValueError):
            _source(chain)
    else:
        with _source(chain) as source:
            if fault == "calendar":
                file = chain["calendar"]
                payload = read(file)
                payload["closed_ranges"].append("2026-10-08/2026-10-08")
                save(file, payload)
            else:
                with (chain["extension"] / "raw_daily.parquet").open("ab") as handle:
                    handle.write(b"synthetic changed native bytes")
            with pytest.raises(ValueError):
                source.frame("2026-10-13")
