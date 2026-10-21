"""Provider time compatibility without contacting a market-data endpoint."""
import sys
from types import SimpleNamespace

import pandas as pd

from a_stock_committee.alpha_stack import fetch_etf_snapshots
from a_stock_committee.trend_calibration import TrendTarget


def test_etf_quote_time_uses_current_sdk_column_and_preserves_unknown(tmp_path, monkeypatch):
    rows = pd.DataFrame([
        {"代码": "588200", "更新时间": pd.Timestamp("2026-10-15T09:53:00+08:00"), "数据时间": "old"},
        {"代码": "512480", "更新时间": pd.NaT, "数据时间": "2026-10-15 09:53:01"},
        {"代码": "159995", "更新时间": float("nan"), "数据时间": float("nan")},
    ])
    calls = []
    def source():
        calls.append(True)
        return rows
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(fund_etf_spot_em=source))
    targets = [TrendTarget(symbol, symbol, "etf", symbol) for symbol in rows["代码"]]
    snapshots = fetch_etf_snapshots(targets, force=True, cache_dir=tmp_path)
    assert calls == [True]
    assert [item.data_time for item in snapshots] == [
        "2026-10-15T09:53:00+08:00", "2026-10-15 09:53:01", ""]
