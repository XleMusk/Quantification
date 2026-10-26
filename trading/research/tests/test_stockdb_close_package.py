from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research.stockdb_close_package import load_complete


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _package(tmp_path: Path, *, missing_snapshot_symbol: bool = False) -> Path:
    root, snapshot = tmp_path / "package", tmp_path / "snapshot"
    root.mkdir(); snapshot.mkdir()
    symbols = ["000001.SZSE", "600001.SSE"]
    snapshot_symbols = symbols[:1] if missing_snapshot_symbol else symbols
    snapshot_daily = pd.DataFrame([
        {"symbol": symbol, "date": "2026-10-26", "close": "10.0"}
        for symbol in snapshot_symbols
    ])
    snapshot_daily.to_csv(snapshot / "daily.csv", index=False)
    snapshot_json = snapshot / "snapshot.json"
    snapshot_json.write_text(json.dumps({
        "schema": "stockdb_research_snapshot_v1",
        "capture_complete": True,
        "execution_admitted": False,
        "effective_session_dates": ["2026-10-26"],
        "files": {"daily": {"path": "daily.csv", "sha256": _sha(snapshot / "daily.csv")}},
    }), encoding="utf-8")
    roster = pd.DataFrame({"symbol": symbols, "status": ["available", "available"]})
    roster.to_csv(root / "roster.csv", index=False)
    raw = pd.DataFrame([
        {"symbol": symbol, "date": "2026-10-26", "close": "10.0"}
        for symbol in symbols
    ])
    raw.to_csv(root / "raw_daily.csv", index=False)
    digest = _sha(root / "raw_daily.csv")
    roster_digest = _sha(root / "roster.csv")
    plan = {
        "schema": "stockdb_close_package_v1", "day": "2026-10-26",
        "roster_symbols": symbols, "roster_sha256": roster_digest,
        "raw_daily_sha256": digest,
        "source_snapshot": {"path": str(snapshot_json), "sha256": _sha(snapshot_json)},
        "source_verified": False, "pit_verified": False, "strategy_validated": False,
        "selection_enabled": False, "execution_admitted": False,
        "stockdb_modified": False, "ledger_mutated": False,
    }
    receipt = {
        **plan, "status": "complete_stockdb_close_package_research_only",
        "completed_at": "2026-10-26T23:00:00+08:00",
        "full_dated_roster_inventory_complete": not missing_snapshot_symbol,
    }
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return root


def test_load_complete_binds_exact_snapshot_and_roster(tmp_path: Path) -> None:
    raw, metadata = load_complete(_package(tmp_path))
    assert len(raw) == 2
    assert metadata["read_only_verified"] is True
    assert metadata["receipt"]["status"] == "complete_stockdb_close_package_research_only"


def test_incomplete_native_snapshot_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not cover the exact package roster"):
        load_complete(_package(tmp_path, missing_snapshot_symbol=True))


def test_changed_raw_hash_is_rejected(tmp_path: Path) -> None:
    package = _package(tmp_path)
    (package / "raw_daily.csv").write_text("symbol,date,close\n000001.SZSE,2026-10-26,10.0\n600001.SSE,2026-10-26,11.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="raw daily hash changed"):
        load_complete(package)
