from pathlib import Path
import hashlib
import json

import pandas as pd
import pytest

from trading.research.stockdb_untouched_forward_holdout import _held_symbols, _load_forward_bars, _load_forward_dates, _receipt_for_raw, _resolve_replay


def test_held_symbols_requires_positive_lots():
    assert _held_symbols({"positions": {"000001.SZSE": [{"qty": 100}]}}) == {"000001.SZSE"}
    with pytest.raises(ValueError, match="no holdings"):
        _held_symbols({"positions": {"000001.SZSE": [{"qty": 0}]}})


def test_forward_loader_normalizes_local_stockdb_codes(tmp_path: Path):
    view = tmp_path / "view"
    view.mkdir()
    rows = pd.DataFrame([
        {"date": "2026-10-14", "code": "sh.600000", "open": 10, "high": 11,
         "low": 9, "close": 10.5, "preclose": 10, "volume": 100, "amount": 10000000,
         "tradestatus": 1, "pctChg": 5, "isST": False},
    ])
    rows.to_parquet(view / "raw_daily.parquet", index=False)
    (view / "receipt.json").write_text(json.dumps({
        "status": "COMPLETE_LOCAL_STOCKDB_EXTENDED_VIEW", "execution_admitted": False
    }), encoding="utf-8")
    loaded = _load_forward_bars(view, {"600000.SSE"}, start="2026-10-14", end="2026-10-14")
    assert loaded.loc[0, "symbol"] == "600000.SSE"
    assert loaded.loc[0, "preclose_for_limits"] == 10


def test_forward_loader_rejects_missing_view(tmp_path: Path):
    with pytest.raises(ValueError, match="raw_daily missing"):
        _load_forward_bars(tmp_path, {"600000.SSE"}, start="2026-10-14", end="2026-10-14")


def test_forward_loader_accepts_native_stockdb_snapshot(tmp_path: Path):
    view = tmp_path / "native"
    view.mkdir()
    pd.DataFrame([
        {"symbol": "301686.SZSE", "date": "2026-10-14", "code": "301686",
         "open": 10, "high": 11, "low": 9, "close": 10.5, "pre_close": 10,
         "volume": 100, "amount": 10000000},
        {"symbol": "301686.SZSE", "date": "2026-10-15", "code": "301686",
         "open": 10.5, "high": 12, "low": 10, "close": 11, "pre_close": 10.5,
         "volume": 120, "amount": 12000000},
    ]).to_csv(view / "daily.csv", index=False, encoding="utf-8-sig")
    (view / "snapshot.json").write_text(json.dumps({
        "schema": "stockdb_research_snapshot_v1", "capture_complete": True,
        "execution_admitted": False,
    }), encoding="utf-8")
    receipt = _receipt_for_raw(view)
    assert receipt["status"] == "COMPLETED_NATIVE_STOCKDB_SNAPSHOT"
    identity = receipt["snapshot_identity"]
    assert identity["snapshot_sha256"] == hashlib.sha256((view / "snapshot.json").read_bytes()).hexdigest()
    assert identity["files"]["daily.csv"]["sha256"] == hashlib.sha256((view / "daily.csv").read_bytes()).hexdigest()
    assert _load_forward_dates(view, start="2026-10-14", end="2026-10-15") == ["2026-10-14", "2026-10-15"]
    loaded = _load_forward_bars(view, {"301686.SZSE"}, start="2026-10-14", end="2026-10-15")
    assert len(loaded) == 2 and loaded["symbol"].tolist() == ["301686.SZSE", "301686.SZSE"]


def test_resolve_replay_accepts_legacy_fixed_run_only_with_research_manifest(tmp_path: Path):
    root = tmp_path / "legacy" / "variant"
    root.mkdir(parents=True)
    (root.parent / "run.json").write_text(json.dumps({
        "status": "completed_research_only", "execution_admitted": False,
        "strategy_validated": False, "fresh_holdout_claimed": False,
    }), encoding="utf-8")
    replay = root / "replay.json"
    replay.write_text(json.dumps({"status": "completed", "last_completed_session": "2026-10-13"}), encoding="utf-8")
    (root / "account.json").write_text(json.dumps({"positions": {"600000.SSE": [{"qty": 100}]}}), encoding="utf-8")
    resolved, result, account = _resolve_replay(replay)
    assert resolved == replay.resolve() and result["end_date"] == "2026-10-13"
    assert account.name == "account.json"
