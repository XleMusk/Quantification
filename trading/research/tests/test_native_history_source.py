"""Small pinned base-view fixtures; no real history scan, Engine or returns."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.research import native_history_source as m
from trading.research.baostock_daily_slice import digest
from trading.research.baostock_native_history import RAW_FIELDS
from trading.research.recent_alpha158_panel_replay import ExactSourceRawFeed

DAYS = ["2025-12-30", "2025-12-31", "2026-01-05", "2026-01-06"]
A, B, U, H = "300114.SZSE", "302132.SZSE", "600001.SSE", "000002.SZSE"
SYMBOLS = [A, B, U, H]
CODES = ["sz.300114", "sz.302132", "sh.600001", "sz.000002"]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_panel(path, groups):
    schema = pa.schema([(key, pa.string()) for key in RAW_FIELDS])
    with pq.ParquetWriter(path, schema) as writer:
        for group in groups:
            writer.write_table(pa.Table.from_pylist(group, schema=schema))


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "synthetic_base_raw_view"
    root.mkdir()
    calendar = tmp_path / "cross_year_calendar.csv"
    pd.DataFrame({"date": DAYS}).to_csv(calendar, index=False)
    groups, tasks = [], {}
    for index, day in enumerate(DAYS):
        rows = []
        for code in CODES:
            if day == DAYS[1] and code == "sz.302132":
                continue  # An actual missing symbol-day, never a zero placeholder.
            row = {"date": day, "code": code, "open": "10.000000", "high": "10.500000",
                "low": "9.500000", "close": "10.200000", "preclose": ["8.880000", "7.770000", "6.660000", "5.550000"][index],
                "volume": "1000000", "amount": "10200000.000000", "adjustflag": "3", "turn": "2.345600",
                "tradestatus": "1", "pctChg": "1.234500", "peTTM": "-12.340000", "pbMRQ": "1.000000",
                "psTTM": "2.100000", "pcfNcfTTM": "", "isST": "0"}
            if code == "sh.600001":
                row.update(tradestatus="", isST="", volume="", amount="", turn="")
            elif code == "sz.000002":
                row.update(tradestatus="0", open="", high="", low="", close="", volume="", amount="", turn="")
            rows.append(row)
        groups.append(rows)
        file = root / (day + "_stock.csv")
        pd.DataFrame(rows, columns=list(RAW_FIELDS)).to_csv(file, index=False)
        tasks[day] = {"date": day, "kind": "stock", "method": "query_daily_history_k_AStock", "file": file.name,
            "fields": ",".join(RAW_FIELDS), "sha256": digest(file), "error_code": "0",
            "quality": {"rows": len(rows), "symbols": len(rows)}}
    raw = root / "raw_daily.parquet"
    write_panel(raw, groups)
    plan = {"kind": "stock", "days": DAYS, "calendar_source": str(calendar), "calendar_sha256": digest(calendar),
        "requested_start": DAYS[0], "requested_end": DAYS[-1], "execution_admitted": False,
        "historical_universe_independently_verified": False}
    receipts = {"status": "source_cache_complete", "execution_admitted": False, "expected_days": 4,
        "updated_at": "2026-10-15T18:00:00+08:00", "tasks": tasks, "failures": []}
    save(root / "plan.json", plan)
    save(root / "receipts.json", receipts)
    result = {"status": "source_cache_complete", "kind": "stock", "expected_days": 4, "acquired_days": 4,
        "rows": sum(map(len, groups)), "symbols": 4, "missing_days": [],
        "raw_daily_sha256": digest(raw), "panel_file": "raw_daily.parquet", "panel_sha256": digest(raw),
        "plan_sha256": digest(root / "plan.json"), "receipts_sha256": digest(root / "receipts.json"),
        "execution_admitted": False, "selection_enabled": False, "strategy_validated": False}
    save(root / "result.json", result)
    return {"root": root, "raw": raw, "groups": groups, "result": result}


def test_cross_year_exact_window_never_reads_future_group_and_cache_stays_bounded(base, monkeypatch):
    accessed = []
    original = pq.ParquetFile.read_row_group
    def read_group(file, index, *args, **kwargs):
        accessed.append(index)
        assert index < 3, "A through-2026-01-05 read must never access the 2026-01-06 row group"
        return original(file, index, *args, **kwargs)
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", read_group)
    with m.NativeHistorySource(base["root"]) as source:
        assert list(source.calendar) == DAYS
        result = source.history(DAYS[2], SYMBOLS, 3)
        assert result["days"] == DAYS[:3]
        assert result["provenance"]["previous_session_required"][DAYS[2]] == DAYS[1]
        assert sorted(result["raw"].date.unique()) == DAYS[:3]
        assert list(source.calendar)[list(source.calendar).index(DAYS[2]) - 1] == DAYS[1]
        assert {index for index in accessed} == {0, 1, 2}
        assert len(source._cache) <= 2 and set(source._cache) == set(DAYS[1:3])
        source.frame(DAYS[0], [A])
        assert len(source._cache) <= 2 and DAYS[0] in source._cache and DAYS[1] not in source._cache
        short = source.history(DAYS[0], [A], 3)
        assert short["days"] == [DAYS[0]] and short["coverage"]["window_complete"] is False
        assert len(short["raw"]) == 1 and short["raw"].iloc[0]["date"] == DAYS[0]
    assert max(accessed) == 2


def test_native_unknown_blank_alias_extra_fields_and_missing_rows_reach_real_raw_feed(base):
    with m.NativeHistorySource(base["root"]) as source:
        result = source.history(DAYS[2], SYMBOLS, 3)
        raw = result["raw"]
        assert len(raw) == 11 and result["missing_keys"] == [{"day": DAYS[1], "symbol": B}]
        assert result["coverage"]["window_complete"] is False and result["coverage"]["missing_rows_imputed"] is False
        assert not ((raw.date == DAYS[1]) & (raw.symbol == B)).any()
        current = source.frame(DAYS[2])
        assert set(current.symbol) == set(SYMBOLS)
        assert set(current.loc[current.symbol.isin([A, B]), "code"]) == {"sz.300114", "sz.302132"}
        unknown = current.loc[current.symbol.eq(U)].iloc[0]
        assert unknown.tradestatus == unknown.isST == ""
        assert pd.isna(unknown.tradestatus_value) and pd.isna(unknown.isST_value)
        halted = current.loc[current.symbol.eq(H)].iloc[0]
        assert halted.volume == halted.amount == "" and halted.tradestatus == "0"
        a = current.loc[current.symbol.eq(A)].iloc[0]
        assert a.turn == "2.345600" and a.peTTM == "-12.340000" and a.pcfNcfTTM == ""
        assert a.preclose == "6.660000" and a.close == "10.200000"
        feed = ExactSourceRawFeed(raw, result["days"], SYMBOLS)
        assert feed.next_day(DAYS[1]) == DAYS[2]
        assert feed.rows[DAYS[2]][U]["tradestatus"] is None and feed.rows[DAYS[2]][U]["isST"] is None
        assert math.isnan(feed.rows[DAYS[2]][H]["volume"]) and math.isnan(feed.rows[DAYS[2]][H]["amount"])
        assert feed.rows[DAYS[2]][A]["preclose"] == 6.66
        assert B not in feed.rows[DAYS[1]] and A in feed.rows[DAYS[1]]
        assert {A, B} <= set(feed.rows[DAYS[2]])


def test_closed_or_absent_target_session_is_not_replaced_by_next_observation(base):
    with m.NativeHistorySource(base["root"]) as source:
        for closed in ("2026-01-01", "2026-01-02"):
            with pytest.raises(ValueError):
                source.frame(closed)
            with pytest.raises(ValueError):
                source.history(closed, [A], 2)


@pytest.mark.parametrize("fault", ["changed_raw_hash", "wrong_day_group", "wrong_source_reference", "not_base_view"])
def test_changed_or_nonbase_source_rejected_before_history_read(base, fault):
    root = base["root"]
    result = read(root / "result.json")
    if fault == "changed_raw_hash":
        with base["raw"].open("ab") as stream:
            stream.write(b"synthetic raw changed after pin")
    elif fault == "wrong_day_group":
        groups = deepcopy(base["groups"])
        groups[1][0]["date"] = DAYS[0]
        write_panel(base["raw"], groups)
        result["raw_daily_sha256"] = result["panel_sha256"] = digest(base["raw"])
        save(root / "result.json", result)
    elif fault == "wrong_source_reference":
        receipts = read(root / "receipts.json")
        receipts["tasks"][DAYS[0]]["date"] = DAYS[1]
        save(root / "receipts.json", receipts)
        result["receipts_sha256"] = digest(root / "receipts.json")
        save(root / "result.json", result)
    else:
        plan = read(root / "plan.json")
        plan["base_view"] = "synthetic_different_base_view"
        save(root / "plan.json", plan)
        result["plan_sha256"] = digest(root / "plan.json")
        save(root / "result.json", result)
    with pytest.raises(ValueError):
        m.NativeHistorySource(root)
