import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT.parent
STORAGE = Path(os.environ.get("QUANT_WEB_STORAGE", ROOT / "storage")).resolve()
UI_ORIGIN = os.environ.get("QUANT_UI_ORIGIN", "http://127.0.0.1:5175")
MAX_UPLOAD = 12 * 1024 * 1024


def prepare_storage():
    for folder in (STORAGE, STORAGE / "datasets", STORAGE / "results"):
        folder.mkdir(parents=True, exist_ok=True)
