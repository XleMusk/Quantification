"""Synthetic bulk/roster lineage checks; no provider calls or market claims."""
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from trading.research import baostock_all_stock_probe as roster_module
from trading.research import baostock_bulk_close as bulk
from trading.research import baostock_full_close as full

DAY = "2026-10-13"
NOW = datetime.fromisoformat("2026-10-15T10:55:00+08:00")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def sources(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    write_json(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026, "closed_ranges": [], "open_sessions": []})
    wheel = tmp_path / "fixture.whl"
    wheel.write_bytes(b"synthetic SDK bytes, never loaded")
    sdk_hash = bulk.digest(wheel)
    monkeypatch.setattr(bulk, "WHEEL_SHA256", sdk_hash)
    monkeypatch.setattr(roster_module, "WHEEL_SHA256", sdk_hash)
    sdk = {"wheel": str(wheel), "sha256": sdk_hash, "distribution_version": "0.9.4"}
    probe, roster = tmp_path/"bulk", tmp_path/"roster"
    probe.mkdir(); roster.mkdir()
    rows = []
    for code, status in (("sz.000001", "1"), ("sz.000002", "0"), ("sh.600001", "1"), ("sh.689009", "1")):
        row = {key: "1" for key in BAOSTOCK_FIELDS.split(",")}
        row.update(date=DAY, code=code, open="10", high="10", low="10", close="10", preclose="10",
            volume="100" if status == "1" else "", amount="1000" if status == "1" else "",
            pctChg="0" if status == "1" else "", adjustflag="3", tradestatus=status, isST="0")
        rows.append(row)
    frame = pd.DataFrame(rows, columns=BAOSTOCK_FIELDS.split(","))
    path = probe/f"{DAY}_stock.csv"
    frame.to_csv(path, index=False)
    write_json(probe/"receipt.json", {"status": "completed_probe", "created_at": "2026-10-14T03:33:36+08:00",
        "finished_at": "2026-10-14T03:33:53+08:00", "sdk": sdk,
        "implementation_sha256": bulk.digest(bulk.daily_source_probe.__file__), "days": [DAY], "kinds": ["stock"],
        "execution_admitted": False, "full_history_verified": False, "entries": [{"date": DAY, "kind": "stock",
            "method": "query_daily_history_k_AStock", "error_code": "0", "file": path.name,
            "sha256": bulk.digest(path), "quality": {"rows": 4, "symbols": 4, "issues": ["missing_or_nonfinite_market_fields"]}}]})
    roster_path = roster/f"query_all_stock_{DAY}.csv"
    status = pd.DataFrame([["sz.000001", "1", "name1"], ["sz.000002", "0", "halt"],
        ["sh.600001", "1", "name3"], ["sh.000001", "1", "index"]], columns=["code", "tradeStatus", "code_name"])
    status.to_csv(roster_path, index=False)
    write_json(roster/"receipt.json", {"schema": "baostock_historical_all_stock_status_probe_v1", "day": DAY,
        "response_day": DAY, "method": "query_all_stock", "kwargs": {"day": DAY}, "sdk": sdk,
        "status": "completed_provider_status_list", "quality_issues": [], "provider_error_code": "0",
        "fields_returned": list(status), "execution_admitted": False, "price_bars_generated": False,
        "raw_csv": str(roster_path), "raw_sha256": bulk.digest(roster_path), "rows_preserved": 4,
        "completed_at": "2026-10-15T10:53:59+08:00"})
    return probe, roster, calendar, tmp_path/"package"


def import_package(sources):
    return bulk.import_bulk_close(*sources, now=NOW)


def rewrite_bulk(probe, change):
    path = probe/f"{DAY}_stock.csv"
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    changed = change(frame)
    frame = changed if isinstance(changed, pd.DataFrame) else frame
    frame.to_csv(path, index=False)
    receipt = json.loads((probe/"receipt.json").read_text())
    receipt["entries"][0].update(sha256=bulk.digest(path), quality={"rows": len(frame), "symbols": frame.code.nunique(), "issues": ["missing_or_nonfinite_market_fields"]})
    write_json(probe/"receipt.json", receipt)


def test_import_preserves_real_bulk_nullable_halt_and_supports_uniform_readonly_loader(sources):
    probe, roster, calendar, target = sources
    before = {str(path): bulk.digest(path) for folder in (probe, roster) for path in folder.iterdir() if path.is_file()}
    folder, receipt = import_package(sources)
    assert folder == target and receipt["ordinary_symbols"] == receipt["completed_symbols"] == 3
    assert receipt["bulk_original_call"]["original_call_count"] == 1
    assert receipt["bulk_original_call"]["raw_rows"] == 4
    assert receipt["per_stock_price_calls"] == receipt["new_provider_calls_by_importer"] == 0
    assert receipt["excluded_nonordinary_codes"] == ["sh.689009"]
    assert receipt["nullable_suspension_rows"] == 1 and receipt["execution_bar_coverage_complete"] is False
    assert not (folder/"batches").exists()
    assert bulk.digest(folder/"bulk_source"/f"{DAY}_stock.csv") == bulk.digest(probe/f"{DAY}_stock.csv")
    files_before_load = {str(path): path.stat().st_mtime_ns for path in folder.rglob("*") if path.is_file()}
    raw, metadata = full.load_complete(folder)
    assert len(raw) == len(metadata["ordinary_roster"]) == 3
    assert raw.loc[raw.code.eq("sz.000002"), "volume"].item() == ""
    assert metadata["plan"]["schema"] == bulk.SCHEMA and metadata["read_only_verified"] is True
    assert files_before_load == {str(path): path.stat().st_mtime_ns for path in folder.rglob("*") if path.is_file()}
    assert before == {str(path): bulk.digest(path) for parent in (probe, roster) for path in parent.iterdir() if path.is_file()}
    with pytest.raises(FileExistsError):
        import_package(sources)


@pytest.mark.parametrize("kind", ["missing", "extra", "status"])
def test_own_day_roster_requires_exact_bulk_membership_and_status(sources, kind):
    probe, _, _, target = sources
    def mutate(frame):
        if kind == "missing":
            return frame.loc[frame.code.ne("sz.000001")].copy()
        if kind == "extra":
            extra = frame.iloc[[0]].copy(); extra["code"] = "sz.000003"
            return pd.concat([frame, extra], ignore_index=True)
        frame.loc[frame.code.eq("sz.000002"), "tradestatus"] = "1"
    rewrite_bulk(probe, mutate)
    with pytest.raises(ValueError, match="code set|statuses disagree"):
        import_package(sources)
    assert not target.exists()


@pytest.mark.parametrize("field,value", [("adjustflag", "2"), ("date", "2026-10-14"), ("isST", ""),
    ("volume", ""), ("high", "9"), ("amount", "2000"), ("volume", "1.5")])
def test_bulk_schema_market_quality_cannot_be_repaired_or_silently_dropped(sources, field, value):
    probe, _, _, target = sources
    rewrite_bulk(probe, lambda frame: frame.loc.__setitem__((frame.code.eq("sz.000001"), field), value))
    with pytest.raises(ValueError):
        import_package(sources)
    assert not target.exists()


def test_halt_nonzero_activity_is_a_contradiction_not_a_nullable_exception(sources):
    probe, _, _, _ = sources
    rewrite_bulk(probe, lambda frame: frame.loc.__setitem__((frame.code.eq("sz.000002"), "pctChg"), "1"))
    with pytest.raises(ValueError, match="suspension payload"):
        import_package(sources)


def test_wrong_day_roster_and_changed_original_bytes_are_rejected(sources):
    probe, roster, _, _ = sources
    receipt = json.loads((roster/"receipt.json").read_text())
    receipt["day"] = "2026-10-14"
    write_json(roster/"receipt.json", receipt)
    with pytest.raises(ValueError, match="exact successful"):
        import_package(sources)
    receipt["day"] = DAY; write_json(roster/"receipt.json", receipt)
    (probe/f"{DAY}_stock.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="raw hash"):
        import_package(sources)


def test_projection_tampering_does_not_pass_even_if_its_aggregate_hash_is_rewritten(sources):
    folder, _ = import_package(sources)
    path = folder/"raw_daily.csv"
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.loc[frame.code.eq("sz.000001"), "close"] = "11"
    frame.to_csv(path, index=False)
    receipt = json.loads((folder/"receipt.json").read_text())
    receipt["raw_daily_sha256"] = bulk.digest(path); write_json(folder/"receipt.json", receipt)
    with pytest.raises(ValueError, match="projection differs"):
        full.load_complete(folder)


def test_loader_rejects_boundary_promotion_and_invented_per_stock_calls(sources):
    folder, _ = import_package(sources)
    path = folder/"receipt.json"
    receipt = json.loads(path.read_text())
    receipt["execution_admitted"] = True; write_json(path, receipt)
    with pytest.raises(ValueError, match="boundary"):
        full.load_complete(folder)
    receipt["execution_admitted"] = False; receipt["per_stock_price_calls"] = 3; write_json(path, receipt)
    with pytest.raises(ValueError, match="call provenance"):
        full.load_complete(folder)


def test_source_sdk_implementation_and_actual_roster_clock_are_pinned(sources):
    probe, roster, _, _ = sources
    path = probe/"receipt.json"
    receipt = json.loads(path.read_text())
    receipt["sdk"]["sha256"] = "changed"; write_json(path, receipt)
    with pytest.raises(ValueError, match="SDK"):
        import_package(sources)
    receipt["sdk"]["sha256"] = bulk.WHEEL_SHA256
    receipt["implementation_sha256"] = "changed"; write_json(path, receipt)
    with pytest.raises(ValueError, match="implementation"):
        import_package(sources)
    receipt["implementation_sha256"] = bulk.digest(bulk.daily_source_probe.__file__); write_json(path, receipt)
    roster_receipt = json.loads((roster/"receipt.json").read_text())
    roster_receipt["completed_at"] = "2026-10-15T11:00:00+08:00"
    write_json(roster/"receipt.json", roster_receipt)
    with pytest.raises(ValueError, match="roster completion clock"):
        import_package(sources)


def test_loader_rejects_future_completion_without_using_it_as_an_availability_clock(sources):
    folder, _ = import_package(sources)
    path = folder/"receipt.json"
    receipt = json.loads(path.read_text())
    receipt["completed_at"] = receipt["updated_at"] = "2099-10-15T10:55:00+08:00"
    write_json(path, receipt)
    with pytest.raises(ValueError, match="in the future"):
        bulk.load_complete_bulk(folder, now=NOW)


@pytest.mark.parametrize("kind", ["call", "publication", "imputation"])
def test_loader_pins_real_bulk_call_and_unverified_publication_boundary(sources, kind):
    folder, _ = import_package(sources)
    path = folder/"receipt.json"
    receipt = json.loads(path.read_text())
    if kind == "call":
        receipt["bulk_original_call"]["original_call_count"] = 5000
    else:
        receipt["actual_signal_publication_verified" if kind == "publication" else "raw_prices_imputed"] = True
    write_json(path, receipt)
    with pytest.raises(ValueError, match="original-call lineage|lineage boundary"):
        bulk.load_complete_bulk(folder, now=NOW)
