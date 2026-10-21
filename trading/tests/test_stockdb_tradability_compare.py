import json

import pandas as pd

from trading.research import stockdb_tradability_compare as module


def test_tradability_overlap_receipt_keeps_source_boundaries(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({"execution_admitted": False}), encoding="utf-8")
    stockdb = pd.DataFrame([{
        "code": "000001", "date": 20260930, "open": 1., "high": 1., "low": 1.,
        "close": 1., "volume": 10, "amount": 20., "is_st": False,
    }])
    monkeypatch.setattr(module, "verify_chunks", lambda *_: None)
    monkeypatch.setattr(module, "load_rows", lambda *_: stockdb.copy())
    raw = tmp_path / "raw"
    raw.mkdir()
    pd.DataFrame([{"code": "sz.000001", "date": "2026-09-30", "tradestatus": "1", "isST": "0"}]).to_parquet(raw / "raw_daily.parquet")
    out, result = module.compare(snapshot, raw, "2026-09-30", tmp_path / "out")
    assert result["tradestatus_disagreement_rows"] == 0
    assert result["is_st_disagreement_rows"] == 0
    assert result["execution_admitted"] is False
    assert json.loads(out.read_text(encoding="utf-8"))["paper_orders_generated"] is False


def test_tradability_compare_accepts_daily_probe_csv(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({"execution_admitted": False}), encoding="utf-8")
    stockdb = pd.DataFrame([{
        "code": "600000", "date": 20261013, "open": 1., "high": 1., "low": 1.,
        "close": 1., "volume": 10, "amount": 20., "is_st": False,
    }])
    monkeypatch.setattr(module, "verify_chunks", lambda *_: None)
    monkeypatch.setattr(module, "load_rows", lambda *_: stockdb.copy())
    raw = tmp_path / "probe"
    raw.mkdir()
    pd.DataFrame([{"code": "sh.600000", "date": "2026-10-13", "tradestatus": "1", "isST": "0"}]).to_csv(raw / "2026-10-13_stock.csv", index=False)
    _, result = module.compare(snapshot, raw, "2026-10-13", tmp_path / "out")
    assert result["paired_rows"] == 1
    assert result["raw_file"].endswith("2026-10-13_stock.csv")

