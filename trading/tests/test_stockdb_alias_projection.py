from pathlib import Path

import pandas as pd

from trading.research.stockdb_alias_projection import diagnose


def _panel(path: Path, rows):
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_alias_projection_prefers_canonical_before_cutoff_and_preserves_after(tmp_path: Path):
    source = tmp_path / "legacy.parquet"
    rows = [
        {"code": "sz.300114", "date": "2025-02-14", "open": 1, "high": 2, "low": 1, "close": 1.5, "preclose": 1.4},
        {"code": "sz.302132", "date": "2025-02-14", "open": 1, "high": 2, "low": 1, "close": 1.5, "preclose": 1.4},
        {"code": "sz.300114", "date": "2025-02-17", "open": None, "high": None, "low": None, "close": None, "preclose": None},
        {"code": "sz.302132", "date": "2025-02-17", "open": 2, "high": 3, "low": 1.5, "close": 2.5, "preclose": 2},
    ]
    _panel(source, rows)
    out, result = diagnose([source], out=tmp_path / "out", artifact=tmp_path / "artifact")
    panel = result["source_panels"][0]
    projection = panel["projection"]
    assert projection["historical_alias_rows"] == 1
    assert projection["post_cutoff_alias_rows_preserved"] == 1
    assert projection["duplicate_rows_removed_after_projection"] == 1
    assert projection["classification_counts"]["exact_duplicate"] == 1
    assert result["promotion"] is False
    assert result["source_panels_modified"] is False
    assert Path(out).is_file()


def test_alias_projection_reports_field_conflict(tmp_path: Path):
    source = tmp_path / "legacy.parquet"
    _panel(source, [
        {"code": "300114", "date": "2025-02-14", "open": 1, "high": 2, "low": 1, "close": 1.5, "preclose": 1.4},
        {"code": "302132", "date": "2025-02-14", "open": 1, "high": 2.2, "low": 1, "close": 1.5, "preclose": 1.4},
    ])
    _, result = diagnose([source], out=tmp_path / "out", artifact=tmp_path / "artifact")
    counts = result["source_panels"][0]["projection"]["classification_counts"]
    assert counts["field_conflict"] == 1


def test_pairwise_reports_exact_and_tolerance_pre_close(tmp_path: Path):
    rows = [{"code": "300114", "date": "2023-01-03", "open": 1, "high": 2,
             "low": 1, "close": 1.5, "preclose": 1.40, "volume": 10, "amount": 15, "pctChg": 1}]
    left, right = tmp_path / "left.parquet", tmp_path / "right.parquet"
    _panel(left, rows)
    rows[0] = {**rows[0], "preclose": 1.41}
    _panel(right, rows)
    _, result = diagnose([left, right], out=tmp_path / "out", artifact=tmp_path / "artifact")
    pair = result["pairwise_reconciliation"][0]
    assert pair["pre_close_exact_conflict_rows"] == 1
    assert pair["pre_close_tolerance_0_02_equal_rows"] == 1
    assert pair["ohlc_conflict_rows"] == 0
