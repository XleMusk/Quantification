"""Frozen local inventory registration, source semantics and chunk integrity."""

from hashlib import sha256
import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading import settings as trading_settings
from webapp.backend.app import data, settings, stockdb_registry as registry, store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_settings, "DATA_ROOT", tmp_path / "trading")
    for module in (settings, data, store):
        monkeypatch.setattr(module, "STORAGE", tmp_path / "web")
    store.initialize()


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def provider_row(code="600000", day=20261019):
    return {"code": code, "date": day, "open": 12., "high": 12.5, "low": 11.5,
            "close": 12., "volume": 10000., "amount": 120000., "pre_close": 11.9,
            "turnover": .16, "pe_ttm": -5.1, "pb": .7, "is_st": False,
            "name": "浦发银行"}


def snapshot():
    folder = trading_settings.DATA_ROOT / "research/stockdb_snapshots/stockdb_20261019_full_v1"
    folder.mkdir(parents=True)
    day, symbol = "2026-10-19", "600000.SSE"
    row = provider_row()
    pd.DataFrame([{**row, "symbol": symbol, "date": day}]).drop(columns="turnover").to_csv(folder / "daily.csv", index=False)
    pd.DataFrame([{"symbol": symbol, "date": day, "status": "available"},
                  {"symbol": "000001.SZSE", "date": day, "status": "missing"}]).to_csv(folder / "availability.csv", index=False)
    records = [{"status": "decoded", "date": day, "symbol": symbol, "payload": row},
               {"status": "decoded", "date": day, "symbol": "000001.SZSE", "payload": []}]
    (folder / "decoded_daily_records.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    manifest = {"schema": "stockdb_research_snapshot_v1", "capture_complete": True,
                "execution_admitted": False, "effective_session_dates": [day],
                "requested_symbols": [symbol, "000001.SZSE"], "captured_at": day + "T17:39:00+08:00",
                "rows": {"requested_daily": 2, "available_daily": 1, "missing_daily": 1},
                "files": {name: {"path": filename, "sha256": digest(folder / filename)} for name, filename in
                          (("daily", "daily.csv"), ("availability", "availability.csv"), ("decoded_daily", "decoded_daily_records.jsonl"))}}
    write_json(folder / "snapshot.json", manifest)
    return folder


def bulk(rows, copies=1):
    folder = trading_settings.DATA_ROOT / "research/full_market/source"
    folder.mkdir(parents=True)
    chunks = []
    for i in range(copies):
        path = folder / f"chunk{i}.parquet"
        pd.DataFrame(rows).to_parquet(path, index=False)
        chunks.append({"parquet_file": path.name, "parquet_sha256": digest(path), "rows": len(rows)})
    manifest = {"status": "complete_inventory_only", "execution_admitted": False,
                "start": "2026-10-19", "end": "2026-10-20", "fields": list(rows[0]),
                "chunks_expected": copies, "chunks": chunks}
    write_json(folder / "manifest.json", manifest)
    return folder


def native_cache(rows):
    """The actual daily_source_probe producer writes 18 source string fields."""
    from trading.research.baostock_native_history import RAW_FIELDS
    folder = trading_settings.DATA_ROOT / "research/stock_daily_bulk/capture/views/completed"
    folder.mkdir(parents=True)
    defaults = dict(zip(RAW_FIELDS, [""] * len(RAW_FIELDS)))
    original = {**defaults, "date": "2026-10-19", "code": "sh.600000", "open": "12.000000", "high": "12.500000",
                "low": "11.500000", "close": "12.000000", "preclose": "11.900000", "volume": "10000",
                "amount": "120000.000000", "adjustflag": "3", "turn": "0.160000", "tradestatus": "1",
                "pctChg": "0.840300", "peTTM": "-5.100000", "pbMRQ": "0.700000", "isST": ""}
    values = [{**original, **row} for row in rows]
    schema = pa.schema([(field, pa.string()) for field in RAW_FIELDS])
    pq.write_table(pa.Table.from_pylist(values, schema=schema), folder / "raw_daily.parquet")
    days = sorted({row["date"] for row in values})
    write_json(folder / "plan.json", {"kind": "stock", "days": days, "requested_start": days[0],
                "requested_end": days[-1], "execution_admitted": False, "historical_universe_independently_verified": False})
    write_json(folder / "receipts.json", {"status": "source_cache_complete", "execution_admitted": False})
    write_json(folder / "result.json", {"status": "source_cache_complete", "kind": "stock", "panel_file": "raw_daily.parquet",
                "execution_admitted": False, "missing_days": [], "rows": len(values), "symbols": len(set(row["code"] for row in values)),
                "expected_days": len(days), "acquired_days": len(days),
                "raw_daily_sha256": digest(folder / "raw_daily.parquet"), "panel_sha256": digest(folder / "raw_daily.parquet"),
                "plan_sha256": digest(folder / "plan.json"), "receipts_sha256": digest(folder / "receipts.json")})
    return folder


def test_register_snapshot_retains_decoded_amount_rate_and_missing_evidence():
    folder = snapshot()
    discovered = registry.inventory()["items"]
    assert len(discovered) == 1
    assert discovered[0]["requested_symbols"] == 2 and discovered[0]["missing_rows"] == 1
    item = registry.register_inventory(discovered[0]["id"])
    frame = data.read_bars(item["id"], symbol="600000.SSE", start="2026-10-19", end="2026-10-19")
    assert frame.amount.iloc[0] == 120000
    assert frame.turnover_rate_pct.iloc[0] == .16
    assert frame.amount.iloc[0] / frame.volume.iloc[0] == 12
    assert "turnover" not in frame
    assert frame.name.iloc[0] == "浦发银行" and frame.pe_ttm.iloc[0] == -5.1
    assert item["source_missing_daily_rows"] == 1 and item["requested_daily_rows"] == 2
    assert item["pit_verified"] is False and item["execution_admitted"] is False
    assert item["field_units"]["amount"] == "CNY"
    assert item["field_coverage"]["amount"] == 1
    assert registry.register_inventory(discovered[0]["id"])["id"] == item["id"]
    assert registry.inventory()["items"][0]["registered_dataset_id"] == item["id"]
    # The registered copy never follows subsequent provider-file changes.
    (folder / "daily.csv").write_text("changed")
    assert data.read_bars(item["id"]).amount.iloc[0] == 120000
    assert data.read_latest(item["id"])[0]["amount"] == 120000


def test_bulk_registration_bypasses_csv_symbol_cap_with_no_imputation():
    rows = [provider_row(f"{600000 + i:06d}", day) for i in range(101) for day in (20261019, 20261020)]
    bulk(rows)
    item = registry.register_inventory(registry.inventory()["items"][0]["id"])
    assert len(item["symbols"]) == 101 and item["rows"] == 202
    assert len(data.read_bars(item["id"], symbol="600000.SSE", end="2026-10-19")) == 1
    latest = data.read_latest(item["id"])
    assert len(latest) == 101 and latest[0]["previous_date"].startswith("2026-10-19")
    with pytest.raises(ValueError, match="100 个证券"):
        data.validate_panel(data.read_bars(item["id"]))


def test_mutated_source_and_cross_chunk_duplicates_are_rejected():
    folder = bulk([provider_row()], copies=2)
    identifier = registry.inventory()["items"][0]["id"]
    with pytest.raises(ValueError, match="跨分片重复"):
        registry.register_inventory(identifier)
    assert store.datasets() == []
    (folder / "chunk0.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="完成清单不一致"):
        registry.register_inventory(identifier)
    assert list((settings.STORAGE / "datasets").iterdir()) == []


def test_incomplete_and_outside_paths_cannot_register(tmp_path):
    folder = snapshot()
    manifest = json.loads((folder / "snapshot.json").read_text())
    manifest["capture_complete"] = False
    write_json(folder / "snapshot.json", manifest)
    assert registry.inventory()["items"] == []
    manifest["capture_complete"] = True
    manifest["files"]["daily"]["path"] = "../../../../outside.csv"
    write_json(folder / "snapshot.json", manifest)
    inventory = registry.inventory()
    assert inventory["items"] == [] and "超出" in inventory["warnings"][0]
    with pytest.raises(ValueError, match="不存在"):
        registry.register_inventory(str(tmp_path))


def test_native_cache_verifies_raw_basis_and_preserves_nullable_fields():
    native_cache([{}])
    item = registry.register_inventory(registry.inventory()["items"][0]["id"])
    assert item["source_format"] == "native_raw_cache"
    stored = data.read_bars(item["id"])
    assert stored.symbol.iloc[0] == "600000.SSE" and stored.is_st.isna().all()
    assert stored.turnover_rate_pct.iloc[0] == .16


def test_native_incomplete_suspension_and_unknown_rows_preserve_originals():
    native_cache([{}, {"code": "sz.000002", "tradestatus": "0", "volume": "", "amount": ""},
                  {"code": "sh.600001", "tradestatus": "", "volume": "", "amount": ""}])
    item = registry.register_inventory(registry.inventory()["items"][0]["id"])
    assert item["rows"] == 1 and item["omitted_source_rows"] == 2
    assert item["omission_counts"] == {"explicit_suspension_incomplete_required_fields": 1, "unknown_status_incomplete_required_fields": 1}
    original = pd.read_parquet(settings.STORAGE / "datasets" / item["id"] / "omitted_source_rows.parquet")
    assert len(original) == 2 and "600001.SSE" in set(original.symbol)
    assert json.loads(original.loc[original.symbol.eq("600001.SSE"), "source_record_json"].iloc[0])["tradestatus"] is None
    assert item["quality"]["sessions"] == 1


def test_native_manifest_count_and_bulk_dates_are_checked():
    folder = native_cache([{}])
    manifest = json.loads((folder / "result.json").read_text())
    manifest["rows"] = 2
    write_json(folder / "result.json", manifest)
    with pytest.raises(ValueError, match="数量"):
        registry.register_inventory(registry.inventory()["items"][0]["id"])
    bulk([provider_row(day=20261021)])
    inventory = registry.inventory()["items"]
    selected = next(item for item in inventory if item["format"] == "stockdb_bulk")
    with pytest.raises(ValueError, match="日期范围"):
        registry.register_inventory(selected["id"])


def test_snapshot_schema_does_not_advertise_unobserved_optional_fields():
    folder = snapshot()
    retained = [*data.COLUMNS, "amount", "pre_close", "code"]
    csv = pd.read_csv(folder / "daily.csv")
    csv[[field for field in retained if field in csv]].to_csv(folder / "daily.csv", index=False)
    records = [json.loads(line) for line in (folder / "decoded_daily_records.jsonl").read_text().splitlines()]
    for record in records:
        if isinstance(record["payload"], dict):
            record["payload"] = {key: value for key, value in record["payload"].items() if key in retained}
    (folder / "decoded_daily_records.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    manifest = json.loads((folder / "snapshot.json").read_text())
    for value in manifest["files"].values():
        value["sha256"] = digest(folder / value["path"])
    write_json(folder / "snapshot.json", manifest)
    discovered = registry.inventory()["items"][0]
    assert discovered["symbols"] == 1 and discovered["requested_symbols"] == 2
    assert "pe_ttm" not in discovered["fields"] and "turnover_rate_pct" not in discovered["fields"]
    item = registry.register_inventory(discovered["id"])
    assert "pe_ttm" not in item["fields"] and "amount" in item["fields"]
