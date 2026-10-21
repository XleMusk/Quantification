"""
Qlib Signal Bridge Strategy for vnpy
-------------------------------------
A TargetPosTemplate subclass that reads external Qlib signals from
a Parquet file and sets target positions for the configured instrument.

Works with:
- vnpy BacktestingEngine (backtest mode)
- vnpy CtaEngine (live/paper trading with a gateway)

Strategy parameters:
- signal_file : str    — path to signals.parquet
- max_pos     : int    — maximum position size (shares)
- direction_filter: int — 0=both, 1=long-only, -1=short-only
"""

import sys
from pathlib import Path
from datetime import date
from typing import Optional

# Ensure D:\Quantification is on Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from vnpy_ctastrategy import (
    TargetPosTemplate,
    BarData,
    TickData,
    TradeData,
    OrderData,
    StopOrder,
)


class QlibSignalBridgeStrategy(TargetPosTemplate):
    """
    Reads daily signals from a shared Parquet file and adjusts
    target position for the configured instrument.
    """

    author = "Qlib-vnpy Bridge"

    signal_file: str = ""
    max_pos: int = 1000
    direction_filter: int = 0

    parameters = ["signal_file", "max_pos", "direction_filter"]
    variables = ["target_pos", "signal_score", "signal_direction"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.signal_score: float = 0.0
        self.signal_direction: int = 0
        self._signal_cache: Optional[pd.DataFrame] = None
        self._last_bar_date: Optional[date] = None

    def on_init(self):
        """Load signal file into memory cache."""
        if not self.signal_file:
            self.write_log("signal_file not configured")
            return

        signal_path = Path(self.signal_file)
        if not signal_path.exists():
            self.write_log(f"Signal file not found: {signal_path}")
            return

        self._signal_cache = pd.read_parquet(signal_path)
        self._signal_cache["datetime"] = pd.to_datetime(
            self._signal_cache["datetime"]
        ).dt.date
        self.write_log(f"Loaded {len(self._signal_cache)} signal rows for {self.vt_symbol}")

        # Load historical bars for warm-up
        self.load_bar(10)

    def on_start(self):
        self.write_log(f"QlibSignalBridge started for {self.vt_symbol}")

    def on_stop(self):
        self.write_log(f"QlibSignalBridge stopped for {self.vt_symbol}")

    def on_bar(self, bar: BarData):
        """On each daily bar, look up signal and set target position."""
        super().on_bar(bar)

        bar_date = bar.datetime.date()

        # Only process once per day
        if self._last_bar_date == bar_date:
            return
        self._last_bar_date = bar_date

        if self._signal_cache is None:
            return

        if not hasattr(bar, "close_price"):
            return

        # Find signal for this symbol + date
        match = self._signal_cache[
            (self._signal_cache["vt_symbol"] == self.vt_symbol)
            & (self._signal_cache["datetime"] == bar_date)
        ]

        if match.empty:
            # No signal today -> flat
            self.signal_score = 0.0
            self.signal_direction = 0
            self.set_target_pos(0)
            return

        row = match.iloc[0]
        direction = int(row["direction"])
        score = float(row["score"])

        self.signal_score = score
        self.signal_direction = direction

        # Apply direction filter
        if self.direction_filter == 1 and direction <= 0:
            target = 0
        elif self.direction_filter == -1 and direction >= 0:
            target = 0
        elif direction > 0:
            target = self.max_pos
        elif direction < 0:
            target = -self.max_pos
        else:
            target = 0

        self.set_target_pos(target)

    def on_tick(self, tick: TickData):
        """Tick callback (not used in daily bar mode)."""
        super().on_tick(tick)

    def on_trade(self, trade: TradeData):
        """Publish update events for UI tracking."""
        self.put_event()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_stop_order(self, stop_order: StopOrder):
        pass
