"""Synthetic dated inputs only; no actual collection, publication or replay."""
from datetime import datetime, timedelta
import json

import pandas as pd
import pytest

from trading.research import leader_entry_confirmation_inputs as m
from trading.research.baostock_bulk_extras import FLAGS as EXTRA_FLAGS, SCHEMA as EXTRA_SCHEMA

NOW = datetime.fromisoformat("2026-10-16T19:00:00+08:00")
DAY, PREVIOUS = "2026-10-16", "2026-10-15"


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf8")


def inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_now", lambda: NOW)
    calendar = tmp_path / "calendar.json"
    save(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                    "closed_ranges": ["2026-01-01/2026-01-03"], "open_sessions": []})
    units = tmp_path / "units.json"
    save(units, {"status": "official_provider_document_read", "turn_unit": "percent",
                 "retrieved_at": "2026-10-14T12:00:00+08:00"})

    def extras(day, turns, halted, st):
        directory = tmp_path / day
        directory.mkdir()
        package = directory / "package"
        package.mkdir()
        symbols = [f"60000{i}.SSE" for i in range(len(turns))]
        rows = pd.DataFrame({"date": [day]*len(turns), "code": [f"sh.60000{i}" for i in range(len(turns))],
            "symbol": symbols, "high": ["10" if i in halted or i == 1 else "11" for i in range(len(turns))],
            "low": ["10"]*len(turns), "volume": ["" if i in halted else "100" for i in range(len(turns))],
            "amount": ["" if i in halted else "1000" for i in range(len(turns))],
            "turn": turns, "turn_numeric": pd.array([None if x == "" else float(x) for x in turns], dtype="Float64"),
            "tradestatus": ["0" if i in halted else "1" for i in range(len(turns))],
            "isST": ["1" if i in st else "0" for i in range(len(turns))], "adjustflag": ["3"]*len(turns)})
        data = directory / "dated_extras.parquet"
        rows.to_parquet(data, index=False)
        raw_hash = ("a" if day == PREVIOUS else "b")*64
        plan = {"schema": "baostock_bulk_exact_dated_close_inventory_v1", "day": day,
            "calendar_sha256": m._digest(calendar), "ordinary_symbols": len(rows), "symbols": symbols,
            "execution_admitted": False}
        save(package / "plan.json", plan)
        save(package / "receipt.json", {"day": day, "status": "complete_full_dated_close_inventory_not_admitted",
            "raw_daily_sha256": raw_hash, "completed_rows": len(rows), "execution_admitted": False})
        record = {"schema": EXTRA_SCHEMA, "status": "completed_dated_bulk_extras_research_only", **EXTRA_FLAGS,
            "day": day, "package": str(package), "package_schema": plan["schema"],
            "package_plan_sha256": m._digest(package / "plan.json"),
            "package_receipt_sha256": m._digest(package / "receipt.json"),
            "ordinary_roster_complete": True, "raw_strings_preserved": True, "ordinary_rows": len(rows),
            "calendar_sha256": m._digest(calendar), "turn_numeric_unit": "percent", "turn_scaled": False,
            "source_inputs_available_at": day+"T18:00:00+08:00", "actual_derived_at": day+"T18:05:00+08:00",
            "available_at": day+"T18:05:00+08:00", "data_file": str(data), "data_sha256": m._digest(data),
            "unit_evidence_file": str(units), "unit_evidence_sha256": m._digest(units), "unit_evidence": m._read(units)}
        path = directory / "result.json"
        save(path, record)
        return path, {"day": day, "raw_daily_sha256": raw_hash, "roles": ["requested_current"]}

    previous, previous_proof = extras(PREVIOUS, ["2", "2", "1", "", "3"], {3}, {4})
    current, current_proof = extras(DAY, ["3", "1", "", "2", "4", "2"], {2}, {4})
    return current, previous, calendar, [previous_proof, current_proof]


def build(tmp_path, monkeypatch):
    current, previous, calendar, proofs = inputs(tmp_path, monkeypatch)
    manifest, record = m.build_confirmation(current, previous, calendar, day=DAY, source_day_proofs=proofs, out=tmp_path/"new")
    return current, previous, calendar, proofs, manifest, record


def test_full_current_denominator_units_status_ST_and_unknown_precedence(tmp_path, monkeypatch):
    current, previous, calendar, proofs, manifest, record = build(tmp_path, monkeypatch)
    result = m.load_confirmation(manifest, day=DAY, calendar=calendar, source_day_proofs=proofs, observed_at=NOW)
    assert result["previous_day"] == PREVIOUS and result["counts"] == record["counts"]
    assert result["counts"]["member_count"] == 6 and len(result["by_symbol"]) == 6
    assert [r["status"] for r in result["rows"]] == ["known_true", "known_false", "unknown", "unknown", "known_true", "unknown"]
    assert result["rows"][0]["turn_percent"] == 3 and result["rows"][0]["turn_raw"] == "3"
    assert result["rows"][2]["turn_percent"] is None and result["rows"][2]["raw_range_nonzero"] is False
    assert result["rows"][4]["isST"] == "1" and result["rows"][4]["status"] == "known_true"
    assert result["rows"][5]["reason"] == "missing_symbol_in_exact_previous_own_roster"
    assert result["sourceproof"]["manifest"]["sha256"] == m._digest(manifest)
    assert record["available_at"] == NOW.isoformat() and result["execution_admitted"] is False


def test_missing_previous_is_full_unknown_and_missing_current_creates_nothing(tmp_path, monkeypatch):
    current, previous, calendar, proofs = inputs(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="No current"):
        m.build_confirmation(None, previous, calendar, day=DAY, source_day_proofs=proofs, out=tmp_path/"absent")
    assert not (tmp_path/"absent").exists()
    path, _ = m.build_confirmation(current, None, calendar, day=DAY, source_day_proofs=proofs, out=tmp_path/"unknown")
    result = m.load_confirmation(path, day=DAY, calendar=calendar, source_day_proofs=proofs, observed_at=NOW)
    assert len(result["rows"]) == 6 and all(r["status"] == "unknown" for r in result["rows"])
    assert all("previous_source_unknown" in r["unknown_reasons"] for r in result["rows"])


@pytest.mark.parametrize("fault", ["wrong_previous", "source_sha", "unit", "halt_zero"])
def test_wrong_previous_source_hash_unit_or_filled_halt_rejected_before_output(tmp_path, monkeypatch, fault):
    current, previous, calendar, proofs = inputs(tmp_path, monkeypatch)
    if fault == "wrong_previous":
        previous = current
    elif fault == "source_sha":
        proofs[0]["raw_daily_sha256"] = "c"*64
    elif fault == "unit":
        record = m._read(current); record["turn_numeric_unit"] = "fraction"; save(current, record)
    else:
        record = m._read(current); rows = pd.read_parquet(record["data_file"])
        rows.loc[2, ["turn", "turn_numeric"]] = ["0", 0]
        rows.to_parquet(record["data_file"], index=False)
        record["data_sha256"] = m._digest(record["data_file"]); save(current, record)
    with pytest.raises(ValueError):
        m.build_confirmation(current, previous, calendar, day=DAY, source_day_proofs=proofs, out=tmp_path/"rejected")
    assert not (tmp_path/"rejected").exists()


def test_actual_write_clock_and_early_or_future_use_refused(tmp_path, monkeypatch):
    current, previous, calendar, proofs = inputs(tmp_path, monkeypatch)
    real_write = m.write_json
    clock = [NOW]
    monkeypatch.setattr(m, "_now", lambda: clock[0])
    def write(path, value):
        real_write(path, value)
        if path.name == "confirmation_rows.json": clock[0] += timedelta(seconds=2)
    monkeypatch.setattr(m, "write_json", write)
    path, record = m.build_confirmation(current, previous, calendar, day=DAY, source_day_proofs=proofs, out=tmp_path/"timed")
    assert record["actual_derived_at"] == (NOW+timedelta(seconds=2)).isoformat()
    with pytest.raises(ValueError, match="availability"):
        m.load_confirmation(path, day=DAY, calendar=calendar, source_day_proofs=proofs, observed_at=NOW)
    with pytest.raises(ValueError, match="future"):
        m.load_confirmation(path, day=DAY, calendar=calendar, source_day_proofs=proofs, observed_at=NOW+timedelta(seconds=3))
    record["sources"]["current"]["available_at"] = NOW.isoformat(); save(path, record)
    with pytest.raises(ValueError, match="source"):
        m.load_confirmation(path, day=DAY, calendar=calendar, source_day_proofs=proofs, observed_at=clock[0])


def test_saved_rows_and_previous_mapping_cannot_change(tmp_path, monkeypatch):
    current, previous, calendar, proofs, path, record = build(tmp_path, monkeypatch)
    record["previous_day"] = "2026-10-14"; save(path, record)
    with pytest.raises(ValueError, match="previous"):
        m.load_confirmation(path, day=DAY, calendar=calendar, source_day_proofs=proofs, observed_at=NOW)
    record["previous_day"] = PREVIOUS
    rows = m._read(record["data_file"]); rows[0]["status"] = "known_false"; save(__import__('pathlib').Path(record["data_file"]), rows)
    record["data_sha256"] = m._digest(record["data_file"]); save(path, record)
    with pytest.raises(ValueError, match="facts"):
        m.load_confirmation(path, day=DAY, calendar=calendar, source_day_proofs=proofs, observed_at=NOW)
