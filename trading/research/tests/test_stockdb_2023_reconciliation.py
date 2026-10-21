import pandas as pd

from trading.research.stockdb_2023_reconciliation import reconcile


def test_reconciliation_retains_conflict_stop_reason(tmp_path):
    extension = tmp_path / "extension"
    extension.mkdir()
    ext = pd.DataFrame([
        {"code": "600000", "symbol": "600000.SSE", "date": "2023-01-03", "open": 7.0, "high": 8.0, "low": 6.5, "close": 7.5, "pre_close": 7.0, "volume": 100, "amount": 750, "pct_chg": 7.14, "tradestatus": 1, "is_st": False},
    ])
    ext.to_parquet(extension / "raw_daily.parquet", index=False)
    (extension / "manifest.json").write_text('{"raw_file":"' + str((extension / "raw_daily.parquet")).replace("\\", "\\\\") + '"}\n', encoding="utf-8")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    pd.DataFrame([
        {"code": "600000", "date": "2023-01-03", "open": 7.5, "high": 8.0, "low": 6.5, "close": 7.5, "pre_close": 7.0, "volume": 100, "amount": 750, "pct_chg": 7.14, "is_st": False},
    ]).to_parquet(legacy / "2023-01.parquet", index=False)
    result_path, result = reconcile(extension, legacy, tmp_path / "out")
    assert result_path.is_file()
    assert result["field_checks"]["open"]["conflict_rows"] == 1
    assert result["promotion_allowed"] is False
    assert result["execution_admitted"] is False
