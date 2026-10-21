"""Build a read-only 2023 BaoStock daily-bar extension receipt.

This consumer deliberately stays outside the canonical StockDB snapshot.  It
normalizes the existing resumable BaoStock CSV cache, records coverage and
field-level quality issues, and writes a separate raw parquet view that can be
reconciled with the quality-aligned StockDB/Qlib lineage later.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT


REQUIRED = ["date", "code", "open", "high", "low", "close", "preclose",
            "volume", "amount", "tradestatus", "pctChg", "isST"]
OUTPUT_FIELDS = ["code", "symbol", "date", "open", "high", "low", "close",
                 "pre_close", "volume", "amount", "pct_chg", "tradestatus", "is_st"]


def _stamp() -> str:
    return datetime.now().astimezone().isoformat()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _symbol(code: str) -> str | None:
    text = str(code).strip().lower()
    if text.startswith("sh.") and text[3:].isdigit() and len(text[3:]) == 6:
        return text[3:] + ".SSE"
    if text.startswith("sz.") and text[3:].isdigit() and len(text[3:]) == 6:
        return text[3:] + ".SZSE"
    return None


def _load(source_root: Path, calendar_path: Path, start: str, end: str) -> tuple[pd.DataFrame, dict]:
    files = sorted(source_root.glob("*_stock.csv"))
    if not files:
        raise ValueError(f"No BaoStock daily CSV files found: {source_root}")
    pieces = []
    for path in files:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        missing = sorted(set(REQUIRED) - set(frame.columns))
        if missing:
            raise ValueError(f"{path.name} missing fields: {missing}")
        pieces.append(frame[REQUIRED].assign(source_file=path.name))
    raw = pd.concat(pieces, ignore_index=True)
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw = raw.loc[raw["date"].between(start, end)].copy()
    raw["date"] = raw["date"].dt.strftime("%Y-%m-%d")
    raw["source_code"] = raw["code"]
    raw["symbol"] = raw["code"].map(_symbol)
    raw["code"] = raw["code"].astype(str).str.extract(r"(\d{6})$")[0]
    numeric = ["open", "high", "low", "close", "preclose", "volume", "amount", "pctChg"]
    for field in numeric:
        raw[field] = pd.to_numeric(raw[field], errors="coerce")
    raw["tradestatus"] = pd.to_numeric(raw["tradestatus"], errors="coerce")
    raw["isST"] = pd.to_numeric(raw["isST"], errors="coerce")
    expected_calendar = pd.read_csv(calendar_path, dtype=str)
    expected_calendar["date"] = pd.to_datetime(expected_calendar["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    expected = sorted(expected_calendar.loc[expected_calendar["date"].between(start, end), "date"].dropna().unique())
    active = raw["tradestatus"].eq(1)
    prices = raw[["open", "high", "low", "close", "preclose"]]
    invalid_ohlc = active & (prices.isna().any(axis=1) | prices.le(0).any(axis=1)
                             | prices["high"].lt(prices[["open", "close", "low"]].max(axis=1))
                             | prices["low"].gt(prices[["open", "close", "high"]].min(axis=1)))
    bad_status = ~raw["tradestatus"].isin([0, 1]) | ~raw["isST"].isin([0, 1])
    duplicate = raw.duplicated(["code", "date"], keep=False)
    off_calendar = ~raw["date"].isin(expected)
    output = raw.assign(is_st=raw["isST"].eq(1), pre_close=raw["preclose"], pct_chg=raw["pctChg"])
    output = output[OUTPUT_FIELDS].sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
    coverage = output.groupby("date", as_index=False).agg(rows=("code", "size"), symbols=("code", "nunique"))
    missing_required = raw[["open", "high", "low", "close", "preclose", "volume", "amount"]].isna().any(axis=1)
    missing_liquidity = raw[["volume", "amount"]].isna().any(axis=1)
    missing_by_status = {
        str(key): int(value)
        for key, value in raw.loc[missing_required, "tradestatus"].value_counts(dropna=False).items()
    }
    missing_by_st = {
        str(key): int(value)
        for key, value in raw.loc[missing_required, "isST"].value_counts(dropna=False).items()
    }
    quality = {
        "schema": "stockdb_baostock_extension_quality_v1",
        "status": "partial_backfill",
        "source": "BaoStock local resumable daily CSV cache",
        "source_provenance": "separate_stock_daily_bulk_cache; collection receipt is not merged into canonical StockDB",
        "source_root": str(source_root.resolve()),
        "calendar": str(calendar_path.resolve()),
        "start": start, "end": end,
        "source_files": len(files), "source_rows": int(len(raw)),
        "rows": int(len(output)), "symbols": int(output.symbol.nunique()),
        "sessions": int(output.date.nunique()), "expected_sessions": len(expected),
        "first_date": output.date.min(), "last_date": output.date.max(),
        "duplicate_code_date_rows": int(duplicate.sum()),
        "off_calendar_rows": int(off_calendar.sum()),
        "invalid_ohlc_rows": int(invalid_ohlc.sum()),
        "invalid_status_rows": int(bad_status.sum()),
        "nonpositive_volume_rows": int((active & raw.volume.le(0)).sum()),
        "negative_amount_rows": int((active & raw.amount.lt(0)).sum()),
        "alias_rows": int(raw.symbol.isna().sum()),
        "missing_required_value_rows": int(missing_required.sum()),
        "missing_required_by_tradestatus": missing_by_status,
        "missing_required_by_is_st": missing_by_st,
        "missing_required_inactive_rows": int((missing_required & raw["tradestatus"].eq(0)).sum()),
        "missing_required_active_rows": int((missing_required & raw["tradestatus"].eq(1)).sum()),
        "missing_liquidity_fields_rows": int(missing_liquidity.sum()),
        "session_coverage_min_rows": int(coverage.rows.min()) if len(coverage) else 0,
        "session_coverage_max_rows": int(coverage.rows.max()) if len(coverage) else 0,
        "quality_gate": "diagnostic_only",
        "coverage_interpretation": "partial_backfill; source cache completeness and PIT universe are not certified",
        "execution_admitted": False,
        "future_labels_used": False,
        "model_refit": False,
        "ledger_mutated": False,
        "ths_payload_generated": False,
    }
    return output, {"quality": quality, "coverage": coverage, "expected_sessions": expected}


def run(source_root: Path, calendar_path: Path, out: Path | None = None,
        start: str = "2023-01-01", end: str = "2023-12-31") -> tuple[Path, dict]:
    source_root, calendar_path = Path(source_root).resolve(), Path(calendar_path).resolve()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    extension = Path(out).resolve() if out else DATA_ROOT / "research" / "stockdb_baostock_extensions" / identifier
    report_root = ROOT / "outputs" / "stockdb_baostock_extensions" / identifier
    extension.mkdir(parents=True, exist_ok=False); report_root.mkdir(parents=True, exist_ok=False)
    frame, details = _load(source_root, calendar_path, start, end)
    frame.to_parquet(extension / "raw_daily.parquet", index=False)
    q = details["quality"]
    manifest = {
        "schema": "stockdb_baostock_extension_manifest_v1", "status": "partial_backfill",
        "created_at": _stamp(), "source": "BaoStock", "start": start, "end": end,
        "fields": OUTPUT_FIELDS, "raw_file": str((extension / "raw_daily.parquet").resolve()),
        "rows": len(frame), "symbols": int(frame.symbol.nunique()), "sessions": int(frame.date.nunique()),
        "quality_receipt": str((report_root / "quality_receipt.json").resolve()),
        "execution_admitted": False, "research_only": True,
    }
    _write_json(extension / "manifest.json", manifest)
    _write_json(report_root / "quality_receipt.json", q)
    _write_json(report_root / "manifest.json", manifest)
    details["coverage"].to_csv(report_root / "session_coverage.csv", index=False)
    report = ["# 2023 BaoStock extension quality receipt", "",
              f"Status: `{q['status']}`; quality gate: `{q['quality_gate']}`.",
              f"Rows: `{q['rows']}`; symbols: `{q['symbols']}`; sessions: `{q['sessions']}/{q['expected_sessions']}`.",
              f"Date range: `{q['first_date']}..{q['last_date']}`.",
              f"Duplicate code/date rows: `{q['duplicate_code_date_rows']}`; invalid OHLC: `{q['invalid_ohlc_rows']}`; aliases: `{q['alias_rows']}`.",
              "This is a separate partial BaoStock extension; it does not alter the canonical StockDB snapshot or admit signals.",
              "All execution, model-refit, future-label, Ledger and THS flags are false.", "",
              f"Extension: `{extension}`", f"Receipt: `{report_root / 'quality_receipt.json'}`"]
    (report_root / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return report_root / "quality_receipt.json", {**q, "extension": str(extension), "report_root": str(report_root)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2023-12-31")
    args = parser.parse_args(argv)
    path, result = run(args.source_root, args.calendar, args.out, args.start, args.end)
    print(json.dumps({"receipt": str(path), "status": result["status"], "rows": result["rows"],
                      "symbols": result["symbols"], "sessions": result["sessions"],
                      "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
