"""Shared configuration for the Qlib-vnpy bridge."""
from pathlib import Path

# === Paths ===
ROOT = Path(r"D:\Quantification")
BRIDGE_DIR = ROOT / "bridge"
SIGNALS_DIR = BRIDGE_DIR / "signals"
QLIB_DATA_DIR = ROOT / "qlib_data" / "cn_data"
VN_TRADER_DIR = Path.home() / ".vntrader"

# === Qlib time ranges (bundled data: 2005-01-04 ~ 2021-06-11) ===
QLIB_DATA_START = "2005-01-04"
QLIB_DATA_END = "2021-06-11"
TRAIN_START = "2008-01-01"
TRAIN_END = "2014-12-31"
VALID_START = "2015-01-01"
VALID_END = "2016-12-31"
TEST_START = "2019-01-01"
TEST_END = "2020-12-31"

# === Trading parameters ===
DEFAULT_CAPITAL = 1_000_000       # 100w CNY
DEFAULT_TOPK = 30
DEFAULT_N_DROP = 5
DEFAULT_RATE = 0.0003             # stamp tax + broker
DEFAULT_SLIPPAGE = 0.01           # 0.01 CNY per share
DEFAULT_MAX_POS = 1000            # shares per stock
