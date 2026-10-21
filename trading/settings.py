from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("QUANT_TRADING_HOME", "D:/QuantificationData/trading")).resolve()
DATASET = DATA_ROOT / "datasets" / "hs300_20231229_pilot"
DB_PATH = DATA_ROOT / "trading.sqlite3"


def prepare():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "results").mkdir(exist_ok=True)
