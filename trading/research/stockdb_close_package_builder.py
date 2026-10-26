"""Build a strict dated StockDB close package from a saved local snapshot.

The builder is deliberately read-only with respect to StockDB.  It publishes a
complete ``stockdb_close_package_v1`` only when the snapshot covers the exact
requested roster and day.  An incomplete snapshot produces a blocked receipt
without plan/raw package files, so a caller cannot accidentally feed it to
``fixed_daily`` as a complete source.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .stockdb_close_package import SCHEMA, STATUS as COMPLETE_STATUS


BLOCKED_STATUS = "BLOCKED_INCOMPLETE_STOCKDB_SNAPSHOT"
FALSE_FLAGS = {
    "source_verified": False,
    "pit_verified": False,
    "strategy_validated": False,
    "selection_enabled": False,
    "execution_admitted": False,
    "stockdb_modified": False,
    "ledger_mutated": False,
}


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


def _ref(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": _digest(path)}


def _resolve_snapshot(value: str | Path) -> tuple[Path, dict[str, Any]]:
    root = Path(value).expanduser().resolve()
    path = root / "snapshot.json" if root.is_dir() else root
    if not path.is_file():
        raise FileNotFoundError(f"Missing StockDB snapshot.json: {path}")
    snapshot = _read_json(path)
    if snapshot.get("schema") != "stockdb_research_snapshot_v1":
        raise ValueError(f"Unsupported StockDB snapshot schema: {path}")
    if snapshot.get("capture_complete") is not True or snapshot.get("execution_admitted") is not False:
        raise ValueError("StockDB snapshot is not a complete unadmitted capture")
    return path, snapshot


def _source_file(snapshot_path: Path, snapshot: dict[str, Any], name: str) -> Path:
    reference = snapshot.get("files", {}).get(name)
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise ValueError(f"StockDB snapshot has no {name} file reference")
    path = Path(reference["path"])
    if not path.is_absolute():
        path = snapshot_path.parent / path
    path = path.resolve()
    if not path.is_file() or _digest(path) != reference.get("sha256"):
        raise ValueError(f"StockDB snapshot {name} hash changed: {path}")
    return path


def _calendar(value: str | Path | None, snapshot: dict[str, Any]) -> tuple[Path, str]:
    chosen = Path(value).expanduser() if value is not None else Path(str(snapshot.get("calendar_basis", "")))
    if not chosen.is_file():
        raise FileNotFoundError(f"Missing pinned exchange calendar: {chosen}")
    payload = _read_json(chosen)
    if payload.get("schema") != "cn_a_share_exchange_calendar_v1":
        raise ValueError(f"Unsupported exchange calendar: {chosen}")
    return chosen.resolve(), _digest(chosen)


def _prepare_output(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Refusing to overwrite existing close-package evidence: {output}")
    output.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_package(
    snapshot: str | Path,
    output: str | Path,
    *,
    day: str | None = None,
    calendar: str | Path | None = None,
) -> dict[str, Any]:
    snapshot_path, metadata = _resolve_snapshot(snapshot)
    requested_day = date.fromisoformat(day or str(metadata.get("effective_session_dates", [""])[-1])).isoformat()
    if requested_day not in {str(value)[:10] for value in metadata.get("effective_session_dates", [])}:
        raise ValueError(f"Snapshot does not contain requested close day: {requested_day}")
    expected = metadata.get("requested_symbols")
    if not isinstance(expected, list) or not expected or expected != sorted(set(str(value) for value in expected)):
        raise ValueError("StockDB snapshot requested_symbols must be sorted and unique")
    calendar_path, calendar_sha = _calendar(calendar, metadata)
    daily_path = _source_file(snapshot_path, metadata, "daily")
    availability_path = _source_file(snapshot_path, metadata, "availability")
    daily = pd.read_csv(daily_path, dtype=str, keep_default_na=False)
    availability = pd.read_csv(availability_path, dtype=str, keep_default_na=False)
    required_daily = {"symbol", "date", "close"}
    if not required_daily.issubset(daily.columns) or not {"symbol", "date", "status"}.issubset(availability.columns):
        raise ValueError("StockDB snapshot daily/availability schema is incomplete")
    daily["symbol"] = daily["symbol"].astype(str).str.strip()
    daily["date"] = daily["date"].astype(str).str[:10]
    availability["symbol"] = availability["symbol"].astype(str).str.strip()
    availability["date"] = availability["date"].astype(str).str[:10]
    available = availability.loc[
        availability["date"].eq(requested_day) & availability["status"].eq("available"), "symbol"
    ].tolist()
    available_set = set(available)
    day_rows = daily.loc[daily["date"].eq(requested_day)].copy()
    duplicate = day_rows.duplicated(["date", "symbol"]).any()
    actual_set = set(day_rows["symbol"])
    missing = sorted(set(expected) - available_set | set(expected) - actual_set)
    extra = sorted(actual_set - set(expected))
    output_path = Path(output).expanduser().resolve()
    _prepare_output(output_path)
    base = {
        "schema": SCHEMA,
        "day": requested_day,
        "source_snapshot": _ref(snapshot_path),
        "snapshot_daily_sha256": _digest(daily_path),
        "snapshot_availability_sha256": _digest(availability_path),
        "calendar_sha256": calendar_sha,
        "requested_symbol_count": len(expected),
        "available_symbol_count": len(expected) - len(missing),
        "missing_symbols": missing,
        "extra_symbols": extra,
        "duplicate_rows": bool(duplicate),
        **FALSE_FLAGS,
    }
    if missing or extra or duplicate:
        receipt = {
            **base,
            "status": BLOCKED_STATUS,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "full_dated_roster_inventory_complete": False,
            "read_only_verified": True,
            "reason": "exact requested roster/date coverage is incomplete; no plan or raw package was published",
        }
        _write_json(output_path / "receipt.json", receipt)
        return receipt
    close = day_rows.sort_values("symbol").reset_index(drop=True)
    numeric_close = pd.to_numeric(close["close"], errors="coerce")
    if numeric_close.isna().any() or (numeric_close <= 0).any():
        raise ValueError("StockDB close values must be finite and positive")
    roster = pd.DataFrame({"symbol": expected, "status": ["available"] * len(expected)})
    roster.to_csv(output_path / "roster.csv", index=False)
    close.to_csv(output_path / "raw_daily.csv", index=False)
    roster_sha = _digest(output_path / "roster.csv")
    raw_sha = _digest(output_path / "raw_daily.csv")
    plan = {
        **base,
        "roster_symbols": expected,
        "roster_sha256": roster_sha,
        "raw_daily_sha256": raw_sha,
        "full_dated_roster_inventory_complete": True,
        "calendar_path": str(calendar_path),
    }
    receipt = {
        **base,
        "status": COMPLETE_STATUS,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "full_dated_roster_inventory_complete": True,
        "roster_sha256": roster_sha,
        "raw_daily_sha256": raw_sha,
        "read_only_verified": True,
    }
    _write_json(output_path / "plan.json", plan)
    _write_json(output_path / "receipt.json", receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--day")
    parser.add_argument("--calendar", type=Path)
    args = parser.parse_args(argv)
    receipt = build_package(args.snapshot, args.output, day=args.day, calendar=args.calendar)
    print(json.dumps({"status": receipt["status"], "output": str(Path(args.output).resolve()),
                      "day": receipt["day"], "missing_symbols": len(receipt.get("missing_symbols", [])),
                      "execution_admitted": False}, ensure_ascii=False))
    return 0 if receipt["status"] == COMPLETE_STATUS else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BLOCKED_STATUS", "build_package", "main"]
