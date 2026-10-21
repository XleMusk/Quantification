import json

import pandas as pd
import pytest

from trading.research.stockdb_forward_compare import compare


def test_forward_compare_ignores_reconciliation_summary_rows(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    pd.DataFrame([
        {"symbol": "000001.SZSE", "date": "2026-09-23", "close": 10.0},
        {"symbol": "000001.SZSE", "date": "2026-09-24", "close": 11.0},
        {"symbol": "000001.SZSE", "date": "2026-09-25", "close": 12.0},
    ]).to_csv(snapshot / "daily.csv", index=False)
    (snapshot / "snapshot.json").write_text(json.dumps({
        "schema": "stockdb_research_snapshot_v1",
        "research_only": True,
        "execution_admitted": False,
        "requested_dates": ["2026-09-23", "2026-09-25"],
        "files": {"daily": {"path": "daily.csv"}},
    }), encoding="utf-8")
    target = tmp_path / "targets.csv"
    pd.DataFrame([
        {"row_type": "order", "symbol": "000001.SZSE", "target_qty": 100, "active_target": True},
        {"row_type": "summary", "symbol": None, "target_qty": None, "active_target": None},
    ]).to_csv(target, index=False)

    result = compare(snapshot, [("candidate", target)], selection_cutoff="2026-09-23")

    assert result["candidates"][0]["status"] == "complete"
    assert result["candidates"][0]["target_symbols"] == 1
    assert result["candidates"][0]["coverage"] == 1.0


def test_forward_compare_still_rejects_non_boolean_order_rows(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    pd.DataFrame([
        {"symbol": "000001.SZSE", "date": "2026-09-23", "close": 10.0},
        {"symbol": "000001.SZSE", "date": "2026-09-24", "close": 11.0},
        {"symbol": "000001.SZSE", "date": "2026-09-25", "close": 12.0},
    ]).to_csv(snapshot / "daily.csv", index=False)
    (snapshot / "snapshot.json").write_text(json.dumps({
        "schema": "stockdb_research_snapshot_v1",
        "research_only": True,
        "execution_admitted": False,
        "requested_dates": ["2026-09-23", "2026-09-25"],
        "files": {"daily": {"path": "daily.csv"}},
    }), encoding="utf-8")
    target = tmp_path / "targets.csv"
    pd.DataFrame([
        {"row_type": "order", "symbol": "000001.SZSE", "target_qty": 100, "active_target": "unknown"},
    ]).to_csv(target, index=False)

    with pytest.raises(ValueError, match="non-boolean active_target"):
        compare(snapshot, [("candidate", target)], selection_cutoff="2026-09-23")
