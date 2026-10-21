import pandas as pd

from trading.research.stockdb_baostock_extension import _load


def test_baostock_extension_normalizes_and_gates(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    pd.DataFrame([
        {"date": "2023-01-03", "code": "sh.600000", "open": "7", "high": "8", "low": "6.5", "close": "7.5", "preclose": "7", "volume": "100", "amount": "750", "tradestatus": "1", "pctChg": "7.14", "isST": "0"},
        {"date": "2023-01-04", "code": "sh.600000", "open": "7.5", "high": "8", "low": "7", "close": "7.8", "preclose": "7.5", "volume": "100", "amount": "780", "tradestatus": "1", "pctChg": "4", "isST": "0"},
        {"date": "2023-01-03", "code": "sz.000001", "open": "10", "high": "10", "low": "10", "close": "10", "preclose": "10", "volume": "0", "amount": "0", "tradestatus": "0", "pctChg": "0", "isST": "0"},
    ]).to_csv(source / "2023-01-03_stock.csv", index=False)
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"date": ["2023-01-03", "2023-01-04"]}).to_csv(calendar, index=False)
    frame, details = _load(source, calendar, "2023-01-01", "2023-12-31")
    assert frame.symbol.tolist() == ["000001.SZSE", "600000.SSE", "600000.SSE"]
    assert details["quality"]["duplicate_code_date_rows"] == 0
    assert details["quality"]["off_calendar_rows"] == 0
    assert details["quality"]["alias_rows"] == 0
    assert details["quality"]["missing_required_value_rows"] == 0
    assert details["quality"]["missing_required_active_rows"] == 0
