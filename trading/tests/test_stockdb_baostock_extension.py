from pathlib import Path

import pandas as pd

from trading.research.stockdb_baostock_extension import build, normalize_code


def test_normalize_code_keeps_stockdb_six_digit_identity():
    assert normalize_code("sh.600000") == "600000"
    assert normalize_code("600000.SSE") == "600000"
    assert normalize_code("SZ000001") == "000001"
    assert normalize_code("bj.920000") == "920000"
    assert normalize_code("fund.123456") == ""


def test_build_reports_quality_and_bounded_partial_coverage(tmp_path: Path):
    source = tmp_path / "csv"
    source.mkdir()
    rows = [
        {"date": "2023-01-03", "code": "sh.600000", "open": 10, "high": 11, "low": 9, "close": 10.5,
         "preclose": 10, "volume": 100, "amount": 1050, "tradestatus": 1, "pctChg": 5, "isST": 0},
        {"date": "2023-01-03", "code": "600000.SSE", "open": 10, "high": 11, "low": 9, "close": 10.5,
         "preclose": 10, "volume": 100, "amount": 1050, "tradestatus": 1, "pctChg": 5, "isST": 0},
        {"date": "2023-01-04", "code": "sz.000001", "open": 8, "high": 7, "low": 8.5, "close": 8,
         "preclose": 8, "volume": 0, "amount": 0, "tradestatus": 0, "pctChg": 0, "isST": 0},
    ]
    for day in ("2023-01-03", "2023-01-04"):
        pd.DataFrame([row for row in rows if row["date"] == day]).to_csv(
            source / f"{day}_stock.csv", index=False
        )
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": ["2023-01-03", "2023-01-04", "2023-01-05"]}).to_csv(calendar, index=False)
    coverage = tmp_path / "coverage.csv"
    pd.DataFrame({"symbol": ["600000.SSE", "000001.SZSE"], "raw_available": [True, True]}).to_csv(coverage, index=False)

    result_path, result = build(source, calendar, coverage, start="2023-01-01", end="2023-01-05",
                                out=tmp_path / "out", artifact=tmp_path / "artifact")
    assert result_path.name == "result.json"
    assert result["status"] == "partial_backfill"
    assert result["coverage_status"].startswith("partial_backfill")
    assert result["quality"]["duplicate_key_rows"] == 2
    assert result["quality"]["invalid_ohlc_rows"] == 1
    assert result["quality"]["missing_expected_date_count"] == 1
    assert result["research_only"] is True
    assert result["execution_admitted"] is False
    panel = pd.read_parquet(tmp_path / "artifact" / "raw_daily.parquet")
    assert list(panel.columns) == ["code", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "is_st", "pre_close"]
    assert len(panel) == 2
