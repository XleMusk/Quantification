import json

import pandas as pd

import trading.research.stockdb_qlib_provider as stockdb_provider
from trading.research.full_market_alpha158 import provider_rows


def test_stockdb_qlib_provider_rejects_incomplete_snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({"status": "incomplete"}), encoding="utf-8")
    try:
        stockdb_provider.build(snapshot, tmp_path / "out")
    except ValueError as exc:
        assert "complete" in str(exc)
    else:
        raise AssertionError("incomplete snapshot should be rejected")


def test_provider_rows_reports_source_quality_without_repairing_rows(tmp_path):
    frame = pd.DataFrame([
        # Invalid high and out-of-range VWAP, but still a live provider row.
        {"code": "000001", "date": 20240102, "open": 9., "high": 9., "low": 8.,
         "close": 10., "volume": 100., "amount": 2000., "pct_chg": 0., "is_st": False},
        {"code": "000002", "date": 20240102, "open": 9., "high": 11., "low": 8.,
         "close": 10., "volume": 100., "amount": 1000., "pct_chg": 0., "is_st": False},
    ])
    _, calendar, source = provider_rows(frame, tmp_path)
    assert calendar == [20240102]
    assert source["source_rows"] == 2
    assert source["source_live_rows"] == 2
    assert source["source_rows_excluded_from_provider"] == 0
    assert source["invalid_ohlc_rows"] == 1
    assert source["live_invalid_ohlc_rows"] == 1
    assert source["vwap_nonfinite_live_rows"] == 0
    assert source["vwap_outside_1pct_ohlc_rows"] == 1


def test_build_persists_source_quality_in_research_receipt(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(json.dumps({
        "status": "complete_inventory_only", "execution_admitted": False,
        "chunks": [], "chunks_expected": 0,
    }), encoding="utf-8")
    frame = pd.DataFrame([{"code": "000001", "date": 20240102}])
    source = {
        "codes": 1, "source_rows": 1, "source_live_rows": 1,
        "source_rows_excluded_from_provider": 0, "invalid_ohlc_rows": 0,
        "live_invalid_ohlc_rows": 0, "nonpositive_volume_rows": 0,
        "vwap_nonfinite_live_rows": 0, "vwap_outside_1pct_ohlc_rows": 0,
    }
    monkeypatch.setattr(stockdb_provider, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(stockdb_provider, "load_rows", lambda *_args, **_kwargs: frame)
    monkeypatch.setattr(stockdb_provider, "verify_chunks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stockdb_provider, "catalog", lambda: pd.DataFrame())
    monkeypatch.setattr(stockdb_provider, "provider_rows", lambda *_args, **_kwargs:
                        (tmp_path / "provider", [20240102], source))
    result_path, _ = stockdb_provider.build(snapshot, tmp_path / "out")
    receipt = json.loads(result_path.read_text(encoding="utf-8"))
    assert receipt["source_quality"] == {
        key: value for key, value in source.items() if key != "codes"
    } | {"quality_gate": "diagnostic_only"}

