import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.research import lowvol_supplement_append as module


def setup(monkeypatch, tmp_path, *, old_day="2026-10-13", old_close="10.00"):
    fields = ["date", "code", "close", "volume", "tradestatus", "turn"]
    table = pa.Table.from_pylist([{"date": old_day, "code": "sh.600000", "close": old_close,
                                  "volume": "000100", "tradestatus": "1", "turn": ""}],
                                schema=pa.schema([(field, pa.string()) for field in fields]))
    base = tmp_path / "original.parquet"
    pq.write_table(table, base)
    raw = pd.DataFrame([{"date": "2026-10-14", "code": "sh.600000", "close": "10.00", "volume": "000100", "tradestatus": "1"}])
    source = {"plan": {"day": "2026-10-14", "calendar_sha256": "calendar", "roster_raw_sha256": "roster"},
              "receipt": {"day": "2026-10-14", "status": "complete_full_dated_close_inventory_not_admitted",
                          "raw_daily_sha256": "raw", "updated_at": "2026-10-15T09:50:42+08:00",
                          "full_dated_roster_inventory_complete": True},
              "source_folder": "strict_source", "read_only_verified": True}
    monkeypatch.setattr(module, "load_complete", lambda path: (raw, source))
    return base, table


def test_append_preserves_exact_entire_prefix_and_nulls_missing_fields(monkeypatch, tmp_path):
    base, before = setup(monkeypatch, tmp_path)
    old_bytes = base.read_bytes()
    result = module.append_sources(base, [tmp_path / "source"], tmp_path / "derived")
    after = pq.read_table(result["raw_daily_file"])
    assert after.slice(0, 1).equals(before) and base.read_bytes() == old_bytes
    assert after["turn"].to_pylist() == ["", None]
    assert after["volume"].to_pylist() == ["000100", "000100"]
    assert result["appended_rows"] == 1 and result["execution_admitted"] is False


def test_any_conflicting_overlap_stops_without_creating_output(monkeypatch, tmp_path):
    base, _ = setup(monkeypatch, tmp_path, old_day="20261014", old_close="10")
    with pytest.raises(ValueError, match="Overlapping"):
        module.append_sources(base, [tmp_path / "source"], tmp_path / "derived")
    assert not (tmp_path / "derived").exists()


def test_stockdb_close_package_loader_is_normalized_for_append(monkeypatch, tmp_path):
    package = tmp_path / "stockdb-package"
    package.mkdir()
    (package / "plan.json").write_text(json.dumps({"schema": "stockdb_close_package_v1"}), encoding="utf-8")
    raw = pd.DataFrame([{"symbol": "600000.SSE", "date": "2026-10-14", "close": "10.00"}])
    source = {
        "plan": {"day": "2026-10-14", "calendar_sha256": "calendar", "roster_sha256": "roster"},
        "receipt": {"day": "2026-10-14", "status": "complete_stockdb_close_package_research_only",
                    "raw_daily_sha256": "raw", "completed_at": "2026-10-15T09:50:42+08:00",
                    "full_dated_roster_inventory_complete": True},
        "source_folder": str(package), "read_only_verified": True,
        "source_schema": "stockdb_close_package_v1",
    }
    monkeypatch.setattr(module, "load_stockdb_complete", lambda _path: (raw, source))
    loaded, observed = module._load_complete_package(package)
    assert list(loaded.columns) == ["code", "date", "close"]
    assert loaded.iloc[0]["code"] == "600000.SSE"
    proof = module._stockdb_source_proof(loaded, observed, "calendar")
    assert proof["roster_raw_sha256"] == "roster"
    assert proof["source_schema"] == "stockdb_close_package_v1"
