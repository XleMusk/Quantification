"""Strict reader for a complete StockDB dated close package.

This is a source boundary only.  It reads a package published from one frozen
StockDB snapshot, verifies the exact roster/date and source hashes, and returns
the same ``(raw_dataframe, metadata)`` shape as the existing full-close
loader.  An incomplete native snapshot is rejected; no values are imputed and
no account or StockDB state is changed.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pandas as pd


SCHEMA = "stockdb_close_package_v1"
STATUS = "complete_stockdb_close_package_research_only"
SNAPSHOT_SCHEMA = "stockdb_research_snapshot_v1"
FALSE_FLAGS = (
    "source_verified",
    "pit_verified",
    "strategy_validated",
    "selection_enabled",
    "execution_admitted",
    "stockdb_modified",
    "ledger_mutated",
)
REQUIRED_DAILY_COLUMNS = {"date", "symbol", "close"}


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _path_ref(value: Any, *, base: Path, field: str) -> Path:
    if isinstance(value, str):
        path = Path(value)
    elif isinstance(value, dict) and isinstance(value.get("path"), str):
        path = Path(value["path"])
        expected = value.get("sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError(f"{field} must retain a sha256 reference")
    else:
        raise ValueError(f"{field} must retain a path reference")
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if isinstance(value, dict) and _digest(path) != value["sha256"]:
        raise ValueError(f"{field} hash changed: {path}")
    return path


def _false_flags(payload: dict[str, Any], label: str) -> None:
    if any(payload.get(key) is not False for key in FALSE_FLAGS):
        raise ValueError(f"{label} boundary flags changed")


def _snapshot_daily(snapshot_json: Path, day: str, symbols: list[str]) -> pd.DataFrame:
    metadata = _read_json(snapshot_json)
    if (metadata.get("schema") != SNAPSHOT_SCHEMA
            or metadata.get("capture_complete") is not True
            or metadata.get("execution_admitted") is not False):
        raise ValueError("StockDB source snapshot is not a complete unadmitted capture")
    if day not in [str(value)[:10] for value in metadata.get("effective_session_dates", [])]:
        raise ValueError("StockDB source snapshot does not contain the package close day")
    daily_ref = metadata.get("files", {}).get("daily")
    daily_path = _path_ref(daily_ref, base=snapshot_json.parent, field="snapshot daily")
    frame = pd.read_csv(daily_path, dtype=str, keep_default_na=False)
    if not {"date", "symbol", "close"} <= set(frame.columns):
        raise ValueError("StockDB source snapshot daily.csv lacks date/symbol/close")
    frame["date"] = frame["date"].astype(str).str[:10]
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame = frame.loc[frame["date"].eq(day) & frame["symbol"].isin(symbols)].copy()
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("StockDB source snapshot contains duplicate date/symbol rows")
    if sorted(frame["symbol"].tolist()) != symbols:
        raise ValueError("StockDB source snapshot does not cover the exact package roster")
    return frame.sort_values("symbol").reset_index(drop=True)


def load_complete_close(folder: str | Path) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    """Read and revalidate a complete StockDB close package without I/O side effects."""
    root = Path(folder).expanduser().resolve()
    plan_path, receipt_path = root / "plan.json", root / "receipt.json"
    roster_path, daily_path = root / "roster.csv", root / "raw_daily.csv"
    for path in (plan_path, receipt_path, roster_path, daily_path):
        if not path.is_file():
            raise FileNotFoundError(f"StockDB close package missing {path.name}: {path}")
    plan, receipt = _read_json(plan_path), _read_json(receipt_path)
    if (plan.get("schema") != SCHEMA or receipt.get("schema") != SCHEMA
            or receipt.get("status") != STATUS or plan.get("day") != receipt.get("day")):
        raise ValueError("StockDB close package schema, day or status is not complete")
    _false_flags(plan, "StockDB close plan")
    _false_flags(receipt, "StockDB close receipt")
    day = date.fromisoformat(str(plan["day"])).isoformat()
    if day != plan["day"] or receipt["day"] != day:
        raise ValueError("StockDB close package day is not canonical ISO")
    if (not isinstance(plan.get("calendar_sha256"), str)
            or plan["calendar_sha256"] != receipt.get("calendar_sha256")
            or receipt.get("full_dated_roster_inventory_complete") is not True):
        raise ValueError("StockDB close package must retain the exact calendar and complete roster proof")
    symbols = plan.get("roster_symbols")
    if (not isinstance(symbols, list) or not symbols
            or symbols != sorted(set(str(value) for value in symbols))):
        raise ValueError("StockDB close package roster must be sorted and unique")
    roster_sha = _digest(roster_path)
    if plan.get("roster_sha256") != roster_sha or receipt.get("roster_sha256") != roster_sha:
        raise ValueError("StockDB close package roster hash changed")
    roster = pd.read_csv(roster_path, dtype=str, keep_default_na=False)
    if list(roster.columns) != ["symbol", "status"]:
        raise ValueError("StockDB close roster schema changed")
    roster["symbol"] = roster["symbol"].astype(str).str.strip()
    if (roster["symbol"].tolist() != symbols or roster["symbol"].duplicated().any()
            or not roster["status"].isin(["available"]).all()):
        raise ValueError("StockDB close roster does not cover the exact available symbols")
    raw_sha = _digest(daily_path)
    if plan.get("raw_daily_sha256") != raw_sha or receipt.get("raw_daily_sha256") != raw_sha:
        raise ValueError("StockDB close package raw daily hash changed")
    raw = pd.read_csv(daily_path, dtype=str, keep_default_na=False)
    missing = sorted(REQUIRED_DAILY_COLUMNS - set(raw.columns))
    if missing:
        raise ValueError(f"StockDB close package raw daily missing fields: {missing}")
    raw["date"] = raw["date"].astype(str).str[:10]
    raw["symbol"] = raw["symbol"].astype(str).str.strip()
    if (len(raw) != len(symbols) or raw["date"].nunique() != 1
            or raw["date"].iat[0] != day or sorted(raw["symbol"].tolist()) != symbols
            or raw.duplicated(["date", "symbol"]).any()):
        raise ValueError("StockDB close package raw daily does not match exact roster/date")
    close = pd.to_numeric(raw["close"], errors="coerce")
    if close.isna().any() or (close <= 0).any():
        raise ValueError("StockDB close package close values must be finite and positive")
    snapshot_json = _path_ref(plan.get("source_snapshot"), base=root, field="source snapshot")
    snapshot_rows = _snapshot_daily(snapshot_json, day, symbols)
    package_projection = raw[["symbol", "date", "close"]].sort_values("symbol").reset_index(drop=True)
    snapshot_projection = snapshot_rows[["symbol", "date", "close"]].sort_values("symbol").reset_index(drop=True)
    if not package_projection.equals(snapshot_projection):
        raise ValueError("StockDB close package rows differ from the pinned source snapshot")
    completed_at = receipt.get("completed_at") or receipt.get("updated_at")
    if not isinstance(completed_at, str) or datetime.fromisoformat(completed_at).tzinfo is None:
        raise ValueError("StockDB close package requires an aware completion clock")
    metadata = {
        "plan": plan,
        "receipt": receipt,
        "source_folder": str(root),
        "read_only_verified": True,
        "source_schema": SCHEMA,
        "source_snapshot": str(snapshot_json),
        "ordinary_roster": roster.to_dict("records"),
    }
    return metadata, raw, roster


def load_complete(folder: str | Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return ``(raw_dataframe, metadata)`` for fixed_daily-compatible consumers."""
    metadata, raw, roster = load_complete_close(folder)
    metadata["ordinary_roster"] = roster.to_dict("records")
    return raw, metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args(argv)
    raw, metadata = load_complete(args.package)
    result = {
        "status": STATUS,
        "schema": SCHEMA,
        "day": metadata["plan"]["day"],
        "rows": len(raw),
        "source_folder": metadata["source_folder"],
        "execution_admitted": False,
        "read_only_verified": True,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["load_complete", "load_complete_close", "main"]
