"""Compatibility adapter for the read-only release projection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from trading.research.release_projection import SCHEMA, latest_release as _latest_release


RELEASE_ROOT = (
    Path(os.environ.get("QUANT_TRADING_HOME", r"D:\QuantificationData\trading"))
    / "research"
    / "releases"
).resolve()


def latest_release(root: str | Path | None = None) -> dict[str, Any]:
    """Preserve the webapp import and test seam while delegating read logic."""

    return _latest_release(RELEASE_ROOT if root is None else root)
