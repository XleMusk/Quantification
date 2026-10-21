"""Tiny original-string cache fixtures; no actual history, network or replay."""
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import baostock_native_history as m
from trading.research.baostock_daily_slice import digest

DAYS = ["2023-01-03", "2023-01-04", "2023-01-05"]
OUTSIDE = "2024-01-02"
CODES = ["sz.300114", "sz.302132", "sh.600001", "sz.000002"]


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


@pytest.fixture
def cached(tmp_path):
    root = tmp_path / "synthetic_daily_cache"
    root.mkdir()
    calendar = tmp_path / "quality_calendar.csv"
    pd.DataFrame({"date": DAYS}).to_csv(calendar, index=False)
    plan_calendar = tmp_path / "cache_calendar.csv"
    pd.DataFrame({"date": [*DAYS, OUTSIDE]}).to_csv(plan_calendar, index=False)
    tasks, sources = {}, {}
    for index, day in enumerate(DAYS):
        records = []
        for code in CODES:
            row = {"date": day, "code": code, "open": "10.000000", "high": "10.500000",
                "low": "9.500000", "close": "10.200000", "preclose": "9.170000" if index == 0 else "7.770000",
                "volume": "1000000", "amount": "10200000.000000", "adjustflag": "3",
                "turn": "2.345600", "tradestatus": "1", "pctChg": "11.232279",
                "peTTM": "-12.340000", "pbMRQ": "1.000000", "psTTM": "2.100000", "pcfNcfTTM": "", "isST": "0"}
            if code == "sh.600001":
                row.update(tradestatus="", isST="", volume="", amount="", turn="")
            elif code == "sz.302132":
                row.update(isST="1")
            elif code == "sz.000002":
                row.update(tradestatus="0", open="", high="", low="", close="", volume="", amount="", pctChg="", turn="")
            records.append(row)
        file = root / (day + "_stock.csv")
        pd.DataFrame(records, columns=list(m.RAW_FIELDS)).to_csv(file, index=False)
        sources[day] = file
        tasks[day] = {"date": day, "kind": "stock", "method": "query_daily_history_k_AStock",
            "file": file.name, "sha256": digest(file), "fields": ",".join(m.RAW_FIELDS),
            "error_code": "0", "error_msg": "", "quality": {"rows": 4, "symbols": 4},
            "started_at": "2026-10-15T17:00:00+08:00", "completed_at": "2026-10-15T17:01:00+08:00"}
    plan = {"kind": "stock", "days": [*DAYS, OUTSIDE], "calendar_source": str(plan_calendar),
        "calendar_sha256": digest(plan_calendar), "requested_start": "2023-01-01", "requested_end": OUTSIDE,
        "historical_universe_independently_verified": False, "execution_admitted": False}
    receipts = {"status": "source_cache_complete", "execution_admitted": False, "expected_days": 4,
        "updated_at": "2026-10-15T17:05:00+08:00", "tasks": tasks,
        "failures": [{"query": {"date": OUTSIDE}, "error_code": "999", "error_msg": "synthetic retained outside-window failure"}]}
    save(root / "plan.json", plan)
    save(root / "receipts.json", receipts)
    quality = {"schema": "stockdb_baostock_extension_quality_v1", "status": "partial_backfill",
        "source": "BaoStock local resumable daily CSV cache", "source_root": str(root), "calendar": str(calendar),
        "start": "2023-01-01", "end": "2023-12-31", "source_files": 3, "source_rows": 12, "rows": 12,
        "symbols": 4, "sessions": 3, "expected_sessions": 3, "first_date": DAYS[0], "last_date": DAYS[-1],
        "quality_gate": "diagnostic_only", "coverage_interpretation": "partial_backfill_not_dated_roster_or_PIT",
        "invalid_status_rows": 3, "missing_required_value_rows": 6,
        **{key: False for key in ("execution_admitted", "future_labels_used", "model_refit", "ledger_mutated", "ths_payload_generated")}}
    receipt = tmp_path / "quality_receipt.json"
    save(receipt, quality)
    return {"root": root, "quality": receipt, "sources": sources, "out": tmp_path / "new_native_view"}


def test_native_strings_nullable_status_halt_and_two_original_codes_are_preserved(cached):
    e = cached
    source_bytes = {path: path.read_bytes() for path in [e["quality"], *e["sources"].values(),
        e["root"] / "plan.json", e["root"] / "receipts.json"]}
    result = m.build(e["quality"], e["out"])
    assert result["rows"] == 12 and result["symbols"] == 4 and result["sessions"] == 3
    assert result["status"] == "completed_native_observed_history_not_admitted"
    assert result["schema"] == "baostock_native_string_history_v1"
    native = pd.read_parquet(result["native_daily_file"])
    statuses = pd.read_parquet(result["status_file"])
    assert list(native.columns[:18]) == list(m.RAW_FIELDS)
    assert len(native) == len(statuses) == 12
    unknown = statuses.loc[statuses.code.eq("sh.600001")]
    assert unknown.tradestatus_raw.eq("").all() and unknown.isST_raw.eq("").all()
    assert unknown.tradestatus_value.isna().all() and unknown.isST_value.isna().all()
    assert not unknown.tradestatus_known.any() and not unknown.isST_known.any()
    halt = native.loc[native.code.eq("sz.000002")]
    assert halt.volume.eq("").all() and halt.amount.eq("").all() and halt.tradestatus.eq("0").all()
    assert statuses.loc[statuses.code.eq("sz.000002"), "tradestatus_value"].eq(0).all()
    assert statuses.loc[statuses.code.eq("sz.302132"), "isST_value"].eq(1).all()
    pair = native.loc[native.date.eq(DAYS[0]) & native.code.isin(CODES[:2])]
    assert set(pair.code) == set(CODES[:2]) and set(pair.symbol) == {"300114.SZSE", "302132.SZSE"}
    assert pair.code.nunique() == len(pair) == 2
    assert all(Path(path).name == DAYS[0] + "_stock.csv" for path in pair.source_file)
    a = native.loc[native.code.eq("sz.300114")].set_index("date")
    assert a.loc[DAYS[0], "preclose"] == "9.170000"
    assert a.loc[DAYS[1], "preclose"] == "7.770000" != a.loc[DAYS[0], "close"]
    assert a.turn.eq("2.345600").all() and a.peTTM.eq("-12.340000").all() and a.pbMRQ.eq("1.000000").all()
    assert a.pcfNcfTTM.eq("").all() and native.adjustflag.eq("3").all()
    assert len(result["source_files"]) == 3
    assert result["source_quality_status"] == "partial_backfill"
    assert result["source_failure_history"] == read(e["root"] / "receipts.json")["failures"]
    assert result["requested_range_failure_history"] == []
    assert result["alias_comparison"]["projection_applied"] is False
    assert result["alias_comparison"]["deduplication_applied"] is False
    assert result["alias_comparison"]["overlap_session_count"] == 3
    quality_report = read(result["quality_report_file"])
    assert quality_report["source_quality_receipt"] == read(e["quality"])
    assert result["execution_admitted"] is False and result["corporate_actions_verified"] is False
    assert digest(result["native_daily_file"]) == result["native_daily_sha256"]
    assert digest(result["status_file"]) == result["status_sha256"]
    assert all(path.read_bytes() == before for path, before in source_bytes.items())


@pytest.mark.parametrize("fault", ["adjustflag", "wrong_date", "bad_code", "duplicate", "missing_status_column"])
def test_invalid_native_schema_or_identity_fails_without_output(cached, fault):
    e = cached
    file = e["sources"][DAYS[0]]
    rows = pd.read_csv(file, dtype=str, keep_default_na=False)
    if fault == "adjustflag": rows.loc[0, "adjustflag"] = "2"
    elif fault == "wrong_date": rows.loc[0, "date"] = "2023-01-99"
    elif fault == "bad_code": rows.loc[0, "code"] = "not-a-provider-code"
    elif fault == "duplicate": rows = pd.concat([rows, rows.iloc[:1]], ignore_index=True)
    else: rows = rows.drop(columns="isST")
    rows.to_csv(file, index=False)
    receipts = read(e["root"] / "receipts.json")
    receipts["tasks"][DAYS[0]]["sha256"] = digest(file)
    receipts["tasks"][DAYS[0]]["quality"]["rows"] = len(rows)
    save(e["root"] / "receipts.json", receipts)
    with pytest.raises(ValueError):
        m.build(e["quality"], e["out"])
    assert not e["out"].exists()


@pytest.mark.parametrize("fault", ["missing_file", "missing_task", "skipped_plan_day"])
def test_missing_registered_session_does_not_publish_false_complete_history(cached, fault):
    e = cached
    if fault == "missing_file":
        e["sources"][DAYS[1]].unlink()
    elif fault == "missing_task":
        receipts = read(e["root"] / "receipts.json")
        del receipts["tasks"][DAYS[1]]
        save(e["root"] / "receipts.json", receipts)
    else:
        plan = read(e["root"] / "plan.json")
        plan["days"].remove(DAYS[1])
        save(e["root"] / "plan.json", plan)
    before = e["quality"].read_bytes()
    with pytest.raises(ValueError):
        m.build(e["quality"], e["out"])
    assert not e["out"].exists() and e["quality"].read_bytes() == before
    assert read(e["quality"])["status"] == "partial_backfill"


def test_existing_output_is_not_overwritten(cached):
    e = cached
    e["out"].mkdir()
    sentinel = e["out"] / "existing.json"
    sentinel.write_text("original bytes", encoding="utf-8")
    before = sentinel.read_bytes()
    with pytest.raises(FileExistsError):
        m.build(e["quality"], e["out"])
    assert sentinel.read_bytes() == before and list(e["out"].iterdir()) == [sentinel]
