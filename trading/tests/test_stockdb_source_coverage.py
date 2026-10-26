import json

import pandas as pd

from trading.research.stockdb_source_coverage import _load_stockdb_index, diagnose


def _snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    pd.DataFrame([
        {"code": "000001", "date": "20261026", "close": 10.0},
        {"code": "000002", "date": "20261026", "close": 20.0},
        {"code": "000001", "date": "20261027", "close": 10.1},
    ]).to_parquet(snapshot / "chunk.parquet", index=False)
    (snapshot / "manifest.json").write_text(json.dumps({
        "status": "complete_inventory_only",
        "execution_admitted": False,
        "chunks": [{"parquet_file": "chunk.parquet"}],
    }), encoding="utf-8")
    return snapshot


def test_stockdb_index_reads_each_requested_batch_once(tmp_path):
    snapshot = _snapshot(tmp_path)
    index = _load_stockdb_index(snapshot, ["000001", "000003"])
    assert [row["date"] for row in index["rows_by_code"]["000001"]] == ["20261026", "20261027"]
    assert index["rows_by_code"]["000003"] == []


def test_coverage_diagnostic_preserves_missing_rows(tmp_path):
    snapshot = _snapshot(tmp_path)
    output, record = diagnose(snapshot, ["000001.SZSE", "000003.SZSE"], ["2026-10-26"],
                               out_root=tmp_path / "artifacts")
    assert record["status"] == "coverage_gap_confirmed"
    assert record["missing_observations"] == 1
    assert (output / "run.json").is_file()
