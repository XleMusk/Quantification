"""
Qlib -> vnpy Data Converter
----------------------------
Reads daily bar data from Qlib and writes to vnpy's SQLite database
so the BacktestingEngine can load it.

Usage:
    python bridge/data_converter.py
"""

import sys
from pathlib import Path
import math

# Ensure D:\Quantification is on Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

import qlib
from qlib.constant import REG_CN
from qlib.data import D

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData
from vnpy.trader.database import get_database

from bridge.config import QLIB_DATA_DIR
from bridge.code_mapping import qlib_to_vnpy


def safe_float(val, default=0.0):
    """Convert a possibly-NaN value to a safe float."""
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


class QlibToVnpyConverter:
    """Read Qlib bar data, write to vnpy SQLite."""

    FIELDS = [
        "$open", "$high", "$low", "$close",
        "$volume", "$vwap",
    ]

    def __init__(self):
        self.database = get_database()

    def init_qlib(self):
        qlib.init(
            provider_uri=str(QLIB_DATA_DIR),
            region=REG_CN,
            joblib_backend="threading",
        )

    def convert_symbol(self, qlib_code: str, start: str, end: str) -> int:
        """Convert one symbol's daily data from Qlib to vnpy database."""
        vt_symbol = qlib_to_vnpy(qlib_code)
        symbol, exchange_str = vt_symbol.split(".")
        exchange = Exchange(exchange_str)

        df = D.features(
            [qlib_code],
            fields=self.FIELDS,
            start_time=start,
            end_time=end,
        )

        if df.empty:
            print(f"  {qlib_code} -> {vt_symbol}: NO DATA")
            return 0

        df = df.reset_index()

        bars = []
        for _, row in df.iterrows():
            dt = row["datetime"]
            if hasattr(dt, "to_pydatetime"):
                dt = dt.to_pydatetime()

            bar = BarData(
                symbol=symbol,
                exchange=exchange,
                datetime=dt,
                interval=Interval.DAILY,
                open_price=safe_float(row["$open"]),
                high_price=safe_float(row["$high"]),
                low_price=safe_float(row["$low"]),
                close_price=safe_float(row["$close"]),
                volume=safe_float(row["$volume"]),
                turnover=safe_float(row.get("$vwap")),
                open_interest=0.0,
                gateway_name="QLIB",
            )
            bars.append(bar)

        self.database.save_bar_data(bars)
        print(f"  {qlib_code} -> {vt_symbol}: {len(bars)} bars")
        return len(bars)


def main():
    """Convert a default set of CSI300 stocks for the test period."""
    converter = QlibToVnpyConverter()
    converter.init_qlib()

    instruments = D.instruments(market="csi300")
    all_symbols = D.list_instruments(instruments, as_list=True)

    # Convert top 20 stocks (enough for integration test)
    test_symbols = all_symbols[:20]

    print(f"Converting {len(test_symbols)} stocks from Qlib to vnpy...")
    total = 0
    for sym in test_symbols:
        total += converter.convert_symbol(sym, "2019-01-01", "2020-12-31")

    print(f"\nDone: {total} bar records across {len(test_symbols)} stocks")


if __name__ == "__main__":
    main()
