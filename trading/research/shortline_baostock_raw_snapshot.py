"""Capture a bounded BaoStock raw-bar path for a shortline research replay.

The capture is a read-only independent source.  It never changes StockDB, a
Ledger, a broker or a THS payload; consumers must keep the source boundary in
their receipt and cannot treat this as PIT or StockDB replacement evidence.
"""

from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any, Callable, ContextManager

import pandas as pd

from a_stock_committee.market_data import BAOSTOCK_FIELDS, baostock_session
from ..symbols import normalize_symbol


SCHEMA = "shortline_baostock_raw_snapshot_v1"
STATUS = "COMPLETED_RESEARCH_ONLY_RAW_PATH"
OUTPUT_COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "amount",
                  "pct_chg", "is_st", "pre_close"]


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _days(start: str, end: str) -> tuple[str, str]:
    left, right = date.fromisoformat(start).isoformat(), date.fromisoformat(end).isoformat()
    if left != start or right != end or left > right:
        raise ValueError("start/end must be canonical ordered dates")
    return left, right


def _symbols(values: list[str]) -> list[str]:
    if not values or len(values) != len(set(values)):
        raise ValueError("at least one distinct symbol is required")
    canonical = []
    for value in values:
        info = normalize_symbol(value)
        if info.vt_symbol != value or not info.code.startswith(("0", "3", "6")):
            raise ValueError(f"ordinary canonical A-share symbol required: {value}")
        canonical.append(value)
    return canonical


def _frame(result: Any) -> pd.DataFrame:
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock query failed: {result.error_msg}")
    frame = result.get_data()
    if list(frame.columns) != BAOSTOCK_FIELDS.split(","):
        raise ValueError("BaoStock daily schema changed")
    return frame


def capture(symbols: list[str], start: str, end: str, output: str | Path,
            *, session_factory: Callable[[], ContextManager[Any]] = baostock_session,
            sleep_seconds: float = 0.5) -> dict[str, Any]:
    start, end = _days(start, end)
    symbols = _symbols(symbols)
    if sleep_seconds < 0:
        raise ValueError("sleep_seconds must be non-negative")
    root = Path(output).resolve()
    if root.exists():
        raise FileExistsError("raw snapshot output must be new")
    root.mkdir(parents=True)
    refs: list[dict[str, Any]] = []
    frames: list[pd.DataFrame] = []
    with session_factory() as client:
        for symbol in symbols:
            info = normalize_symbol(symbol)
            result = client.query_history_k_data_plus(
                info.baostock_code, BAOSTOCK_FIELDS, start_date=start, end_date=end,
                frequency="d", adjustflag="3")
            frame = _frame(result)
            path = root / f"{symbol}.csv"
            frame.to_csv(path, index=False)
            refs.append({"symbol": symbol, "provider_code": info.baostock_code,
                         "rows": int(len(frame)), "path": str(path), "sha256": _digest(path),
                         "dates": frame["date"].tolist()})
            frames.append(frame)
            if sleep_seconds:
                time.sleep(sleep_seconds)
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=BAOSTOCK_FIELDS.split(","))
    combined = combined.rename(columns={"pctChg": "pct_chg", "isST": "is_st", "preclose": "pre_close"})
    combined["code"] = combined["code"].astype(str).str.split(".").str[1].str.zfill(6)
    combined["date"] = pd.to_datetime(combined["date"], errors="raise").dt.strftime("%Y%m%d")
    combined = combined[OUTPUT_COLUMNS].sort_values(["date", "code"], kind="stable")
    parquet = root / "raw_daily.parquet"
    combined.to_parquet(parquet, index=False)
    manifest = {
        "schema": SCHEMA, "status": STATUS, "source": "BaoStock public daily history API",
        "requested_symbols": symbols, "requested_start": start, "requested_end": end,
        "adjustflag": "3", "files": refs,
        "parquet": {"path": str(parquet), "sha256": _digest(parquet), "rows": int(len(combined))},
        "stockdb_replacement": False, "execution_admitted": False,
        "broker_interacted": False, "pit_verified": False, "corporate_actions_verified": False,
        "limitations": [
            "Independent raw-bar source is bounded to the requested symbols and dates; it does not replace StockDB.",
            "No model refit, labels, broker/THS state or account mutation was used.",
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = capture([value.strip() for value in args.symbols.split(",") if value.strip()],
                     args.start, args.end, args.out)
    print(json.dumps({"status": result["status"], "rows": result["parquet"]["rows"],
                      "output": str(args.out.resolve()), "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
