"""Synthetic sidecar history only; no original case or real source is mutated."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.research import leader_type_case_context as m


NOW = "2026-10-15T20:00:00+08:00"
CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
SESSIONS, _, _ = m._calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def fixture(tmp_path, *, missing_native_day=None, invalid_native=False, missing_field=None):
    snapshot = tmp_path / "snapshot"; snapshot.mkdir()
    native = [{"code": "000001", "date": date, "open": 10., "high": 11., "low": 9., "close": 10.,
        "volume": 1234, "amount": 5678, "pct_chg": -2.5, "is_st": False, "pre_close": 999., "turnover": 450.}
        for date in [20261008, 20261009, 20261013] if date != missing_native_day]
    if invalid_native:
        native[0].update(high=8., close=0., volume=0)
    data = pd.DataFrame(native)
    if missing_field:
        data = data.drop(columns=[missing_field])
    part = snapshot / "native_0.parquet"; data.to_parquet(part, index=False)
    sm = {"status": "complete_inventory_only", "created_at": "2026-10-11T03:30:29+08:00",
        "source": "synthetic pinned source-native snapshot", "execution_admitted": False,
        "fields": m.RAW_FIELDS, "chunks_expected": 1,
        "limitations": ["units and adjustment unknown; pre_close not execution reference"],
        "chunks": [{"prefix": "0", "lower_inclusive": "20261008", "upper_inclusive": "20261013",
            "parquet_file": part.name, "parquet_sha256": m.digest(part), "rows": len(data),
            "raw_file": "original.msgpack", "raw_sha256": "declared-original-no-new-read"}]}
    write(snapshot / "manifest.json", sm)
    cases_folder = tmp_path / "cases"; cases_folder.mkdir()
    calendar = tmp_path / "calendar.json"; write(calendar, CALENDAR)
    anchor = {"day": "2026-10-09", "history_snapshot": str(snapshot),
        "original_history_manifest_sha256": m.digest(snapshot / "manifest.json"), "PIT_or_actions_verified": False}
    source = {"context_anchor": anchor, "IPO_qualification": {"calendar": str(calendar), "calendar_sha256": m.digest(calendar)}}
    entries, hashes = [], {}
    for number, (symbol, day) in enumerate([("000001.SZSE", "2026-10-12"),
            ("000001.SZSE", "2026-10-13"), ("600001.SSE", "2026-10-15")]):
        origin = SESSIONS.index(day); slots = []
        for offset in [-2, -1, 0, 1, 2]:
            date = SESSIONS[origin + offset]
            saved = "2026-10-12" <= date <= "2026-10-15"
            slots.append({"day": date, "offset_sessions": offset,
                "status": "observed_active_raw_bar" if saved else "missing_raw_row",
                "raw": {"date": date, "preclose": "10", "close": "10", "adjustflag": "3", "tradestatus": "1"} if saved else None})
        case = {"schema": m.CASE_SCHEMA, "case_id": "synthetic|" + str(number), "symbol": symbol,
            "day": day, "type_id": "trend_leader", "phase": "broad_weakness_snapshot",
            "source": deepcopy(source), "daily_slots": slots,
            "execution_admitted": False, "selection_enabled": False, "strategy_validated": False}
        file = cases_folder / f"case_{number}.json"; write(file, case)
        hashes[file] = m.digest(file)
        entries.append({"case_id": case["case_id"], "day": day, "symbol": symbol,
            "json_file": str(file), "case_sha256": m.digest(file)})
    manifest = cases_folder / "manifest.json"
    write(manifest, {"schema": m.CASE_MANIFEST_SCHEMA, "created_at": "2026-10-15T19:40:55+08:00",
        "source_days": ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"],
        "source": source, "cases": entries, "execution_admitted": False, "selection_enabled": False, "strategy_validated": False})
    registration = {"schema": m.PROTOCOL_SCHEMA, "method_id": m.METHOD_ID,
        "recorded_at": "2026-10-15T19:55:00+08:00", "status": "late_registered_historical_context_only",
        "cases_manifest": str(manifest), "cases_manifest_sha256": m.digest(manifest), "protocol": deepcopy(m.PROTOCOL)}
    protocol = tmp_path / "protocol.json"; write(protocol, registration)
    return {"manifest": manifest, "protocol": protocol, "snapshot": snapshot, "part": part,
        "cases": entries, "case_hashes": hashes, "registration": registration}


def build(files, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_now", lambda: datetime.fromisoformat(NOW))
    return m.build_context(cases_manifest=files["manifest"], protocol=files["protocol"], out=tmp_path / "new_context")


def test_only_original_missing_dedup_all_cases_native_units_unknown_and_no_original_write(tmp_path, monkeypatch):
    files = fixture(tmp_path); result = build(files, tmp_path, monkeypatch)
    counts = result["counts"]
    assert counts == {"original_cases": 3, "original_missing_slots": 5, "eligible_case_slot_references": 3,
        "unique_historical_symbol_days": 2, "found_native_rows": 2, "found_native_case_slot_references": 3,
        "valid_native_OHLC": 2, "invalid_or_unknown_native_OHLC": 0, "missing_source_rows": 0,
        "missing_source_case_slot_references": 0, "retained_outside_history_cutoff_slots": 2}
    assert [r["day"] for r in result["unique_context_rows"]] == ["2026-10-08", "2026-10-09"]
    assert result["unique_context_rows"][1]["case_slot_reference_count"] == 2
    row = result["unique_context_rows"][0]
    assert row["raw"]["volume"] == 1234 and row["raw"]["amount"] == 5678 and row["raw"]["turnover"] == 450.
    assert "adjustflag" not in row["raw"] and "tradestatus" not in row["raw"]
    assert row["trade_status"] is None and row["price_adjustment_basis"] == "unknown_source_native"
    assert set(row["field_units"].values()) == {"unknown_source_native_no_scaling"}
    assert len(result["cases"]) == 3 and result["cases"][-1]["original_nonmissing_slots_unchanged"] == 3
    assert [r["day"] for r in result["retained_outside_history_cutoff_missing"]] == ["2026-10-16", "2026-10-19"]
    assert all(r["raw"] is r["actual_future_publication"] is None for r in result["retained_outside_history_cutoff_missing"])
    proxy = result["cross_source_next_session_reference_proxy"][0]
    assert proxy["status"] == "consistent_within_one_percent_numeric_proxy"
    assert proxy["native_close"] == 10 and proxy["next_saved_Bao_preclose"] == "10"
    assert proxy["legacy_native_pre_close_used"] is False and row["raw"]["pre_close"] == 999
    assert result["source"]["snapshot_recorded_created_at_field"].startswith("2026-10-11")
    assert result["source"]["snapshot_created_at_is_not_verified_historical_publication_or_availability"] is True
    assert result["available_at"] == result["read_finished_at"] == NOW
    assert all(m.digest(file) == sha for file, sha in files["case_hashes"].items())
    assert result["execution_admitted"] is result["units_verified"] is result["price_adjustment_basis_verified"] is False
    assert (tmp_path / "new_context" / "result.json").exists()
    assert "原量字段" in (tmp_path / "new_context" / "CONTEXT.md").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        m.build_context(cases_manifest=files["manifest"], protocol=files["protocol"], out=tmp_path / "new_context")


def test_found_native_invalid_OHLC_and_missing_optional_field_are_not_absent_or_halt(tmp_path, monkeypatch):
    files = fixture(tmp_path, invalid_native=True, missing_field="turnover")
    result = build(files, tmp_path, monkeypatch)
    assert result["counts"]["found_native_rows"] == 2 and result["counts"]["valid_native_OHLC"] == 1
    assert result["counts"]["invalid_or_unknown_native_OHLC"] == 1 and result["counts"]["missing_source_rows"] == 0
    row = result["unique_context_rows"][0]
    assert row["state"] == "source_native_row_available" and row["raw"]["close"] == 0.
    assert row["raw"]["volume"] == 0 and row["trade_status"] is None
    assert row["raw"]["turnover"] is None and row["quality"]["missing_native_fields"] == ["turnover"]
    assert row["quality"]["valid_native_OHLC"] is False


def test_missing_exact_source_day_not_backfilled_from_nearby_or_future_row(tmp_path, monkeypatch):
    files = fixture(tmp_path, missing_native_day=20261008)
    result = build(files, tmp_path, monkeypatch)
    assert result["counts"]["found_native_rows"] == 1 and result["counts"]["missing_source_rows"] == 1
    assert result["counts"]["missing_source_case_slot_references"] == 1
    row = result["unique_context_rows"][0]
    assert row["day"] == "2026-10-08" and row["state"] == "missing_source_row" and row["raw"] is None
    assert row["unknown_reason"] == "exact_symbol_day_absent_in_original_pinned_partition"
    assert all(r["day"] <= "2026-10-09" for r in result["unique_context_rows"])


@pytest.mark.parametrize("target", ["case", "snapshot", "partition"])
def test_original_hash_pin_changes_are_rejected_without_output(tmp_path, monkeypatch, target):
    files = fixture(tmp_path)
    if target == "case":
        path = files["cases"][0]["json_file"]
    elif target == "snapshot":
        path = files["snapshot"] / "manifest.json"
    else:
        path = files["part"]
    with open(path, "ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="changed"):
        build(files, tmp_path, monkeypatch)
    assert not (tmp_path / "new_context").exists()


def test_protocol_actual_clock_manifest_and_admission_not_weakened(tmp_path, monkeypatch):
    files = fixture(tmp_path)
    for patch in ({"recorded_at": "2026-10-15T20:01:00+08:00"},
                  {"recorded_at": "2026-10-15T19:00:00+08:00"}, {"cases_manifest_sha256": "wrong"},
                  {"execution_admitted": True}):
        write(files["protocol"], {**files["registration"], **patch})
        with pytest.raises(ValueError):
            build(files, tmp_path, monkeypatch)
        assert not (tmp_path / "new_context").exists()


def test_original_exact_calendar_slot_not_replaced_with_previous_observation(tmp_path):
    files = fixture(tmp_path)
    manifest = m._json(files["manifest"])
    entry = manifest["cases"][0]
    case_file = entry["json_file"]; case = m._json(case_file)
    case["daily_slots"][0]["day"] = "2026-10-07"
    write(__import__("pathlib").Path(case_file), case)
    entry["case_sha256"] = m.digest(case_file); write(files["manifest"], manifest)
    with pytest.raises(ValueError, match="exact calendar"):
        m.load_case_requests(files["manifest"])
