from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research.stockdb_close_package import load_complete
from trading.research.stockdb_close_package_builder import BLOCKED_STATUS, build_package


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _snapshot(tmp_path: Path, *, complete: bool = True) -> Path:
    root = tmp_path / ("snapshot_complete" if complete else "snapshot_partial")
    root.mkdir()
    symbols = ["000001.SZSE", "600001.SSE"]
    daily_symbols = symbols if complete else symbols[:1]
    daily = pd.DataFrame([{"symbol": s, "date": "2026-10-26", "close": "10.0"} for s in daily_symbols])
    availability = pd.DataFrame([
        {"symbol": s, "date": "2026-10-26", "status": "available" if complete or s == symbols[0] else "missing"}
        for s in symbols
    ])
    daily.to_csv(root / "daily.csv", index=False)
    availability.to_csv(root / "availability.csv", index=False)
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({
        "schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "open_sessions": ["2026-10-26"], "closed_ranges": [],
    }), encoding="utf-8")
    snapshot = {
        "schema": "stockdb_research_snapshot_v1", "capture_complete": True,
        "execution_admitted": False, "effective_session_dates": ["2026-10-26"],
        "calendar_basis": str(calendar), "requested_symbols": symbols,
        "files": {
            "daily": {"path": "daily.csv", "sha256": _sha(root / "daily.csv")},
            "availability": {"path": "availability.csv", "sha256": _sha(root / "availability.csv")},
        },
    }
    (root / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return root


def test_builder_publishes_a_loader_compatible_complete_package(tmp_path: Path) -> None:
    output = tmp_path / "package"
    receipt = build_package(_snapshot(tmp_path), output)
    assert receipt["status"] == "complete_stockdb_close_package_research_only"
    raw, metadata = load_complete(output)
    assert len(raw) == 2
    assert metadata["read_only_verified"] is True
    assert receipt["execution_admitted"] is False


def test_builder_publishes_blocked_receipt_without_a_complete_package(tmp_path: Path) -> None:
    output = tmp_path / "blocked"
    receipt = build_package(_snapshot(tmp_path, complete=False), output)
    assert receipt["status"] == BLOCKED_STATUS
    assert len(receipt["missing_symbols"]) == 1
    assert (output / "receipt.json").is_file()
    assert not (output / "plan.json").exists()
    assert not (output / "raw_daily.csv").exists()
    with pytest.raises(FileNotFoundError):
        load_complete(output)


def test_builder_refuses_to_overwrite_existing_evidence(tmp_path: Path) -> None:
    output = tmp_path / "package"
    output.mkdir()
    (output / "receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        build_package(_snapshot(tmp_path), output)
