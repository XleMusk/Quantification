"""
vnpy Backtest Runner for Qlib Signal Bridge
--------------------------------------------
Runs a single-instrument vnpy backtest using QlibSignalBridgeStrategy.

Usage:
    python bridge/backtest_runner.py
"""

import sys
from pathlib import Path
from datetime import datetime

# Ensure D:\Quantification is on Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from vnpy.trader.constant import Interval
from vnpy_ctastrategy.backtesting import BacktestingEngine
from vnpy_ctastrategy.base import BacktestingMode

from bridge.vnpy_strategy import QlibSignalBridgeStrategy
from bridge.config import (
    SIGNALS_DIR, DEFAULT_CAPITAL, DEFAULT_RATE,
    DEFAULT_SLIPPAGE, DEFAULT_MAX_POS,
)


def run_single_stock_backtest(
    vt_symbol: str = "600000.SSE",
    signal_file: str = None,
    start_date: str = "2019-06-01",
    end_date: str = "2020-06-30",
    max_pos: int = DEFAULT_MAX_POS,
    capital: int = DEFAULT_CAPITAL,
) -> dict:
    """Run a single-stock backtest with the Qlib signal strategy."""

    if signal_file is None:
        signal_file = str(SIGNALS_DIR / "signals_latest.parquet")

    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=vt_symbol,
        interval=Interval.DAILY,
        start=datetime.strptime(start_date, "%Y-%m-%d"),
        end=datetime.strptime(end_date, "%Y-%m-%d"),
        rate=DEFAULT_RATE,
        slippage=DEFAULT_SLIPPAGE,
        size=1,
        pricetick=0.01,
        capital=capital,
        mode=BacktestingMode.BAR,
    )

    setting = {
        "signal_file": signal_file,
        "max_pos": max_pos,
        "direction_filter": 1,  # long-only for A-shares
    }

    engine.add_strategy(QlibSignalBridgeStrategy, setting)
    engine.load_data()
    engine.run_backtesting()

    daily_df = engine.calculate_result()
    statistics = engine.calculate_statistics(output=True)

    return {
        "statistics": statistics,
        "daily_df": daily_df,
        "vt_symbol": vt_symbol,
    }


def main():
    """Run backtest for a few representative stocks."""
    signal_file = str(SIGNALS_DIR / "signals_latest.parquet")

    test_stocks = ["600000.SSE", "600036.SSE", "000001.SZSE"]

    for vt_symbol in test_stocks:
        print(f"\n{'=' * 55}")
        print(f"  Backtesting: {vt_symbol}")
        print(f"{'=' * 55}")
        try:
            result = run_single_stock_backtest(
                vt_symbol=vt_symbol,
                signal_file=signal_file,
            )
            stats = result["statistics"]
            if stats:
                print(f"  Total Return : {stats.get('total_return', 0):.2f}%")
                print(f"  Sharpe Ratio : {stats.get('sharpe_ratio', 0):.2f}")
                print(f"  Max Drawdown : {stats.get('max_ddpercent', 0):.2f}%")
                print(f"  Trade Count  : {stats.get('total_trade_count', 0)}")
                print(f"  End Balance  : {stats.get('end_balance', 0):.0f}")
            else:
                print("  No statistics (possibly no trades)")
        except Exception as e:
            print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
