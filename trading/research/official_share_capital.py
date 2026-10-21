"""Offline preservation of the official SZSE A-share-list capital literals.

The source has no explicit unit caption or statistical date.  Integer parsing
does not verify shares, free float, historical PIT or a strategy denominator.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any

import openpyxl
import pandas as pd

from ..contracts import MARKET_TZ


SCHEMA = "official_szse_reported_capital_literals_v1"
SOURCE_URL = "https://www.szse.cn/api/report/ShowReport"
REQUIRED_HEADERS = ("A股代码", "A股简称", "A股上市日期", "A股总股本", "A股流通股本")
FLAGS = {
    "research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "validated_denominator": False,
    "historical_pit_verified": False, "full_market_coverage_verified": False,
    "free_float_verified": False, "unit_verified": False,
    "source_asof_verified": False, "network_accessed": False,
    "account_accessed": False, "ledger_mutated": False,
}


def _stamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("A timezone-aware clock is required")
    return stamp.astimezone(MARKET_TZ)


def _now() -> datetime:
    return datetime.now(MARKET_TZ)


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _source(source: Path) -> tuple[dict, bytes]:
    receipt_path, response_path = source / "receipt.json", source / "response.bin"
    receipt = _read(receipt_path)
    params = receipt.get("params") or {}
    if (receipt.get("source_url") != SOURCE_URL
            or receipt.get("status_code") != 200 or receipt.get("http_success") is not True
            or receipt.get("execution_admitted") is not False
            or receipt.get("account_accessed") is not False
            or any(params.get(key) != value for key, value in
                   {"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1"}.items())):
        raise ValueError("Successful official SZSE A-share-list receipt required")
    if _stamp(receipt["completed_at"]) < _stamp(receipt["started_at"]):
        raise ValueError("Source receipt clocks are reversed")
    response = response_path.read_bytes()
    if receipt.get("byte_count") != len(response) or receipt.get("sha256") != sha256(response).hexdigest():
        raise ValueError("Source response bytes changed or differ from receipt")
    return receipt, response


def _raw_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return {"excel_value_type": type(value).__name__, "value": value.isoformat()}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError(f"Unsupported original cell value type: {type(value).__name__}")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, (datetime, date)) else str(value)


def _reported_integer(value: Any) -> int | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a reported capital integer")
    text = str(value).strip()
    if not re.fullmatch(r"(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)", text):
        raise ValueError(f"Unrecognized reported capital integer: {text!r}")
    value = int(text.replace(",", ""))
    if value > 2**63 - 1:
        raise ValueError("Reported integer exceeds the supported literal range")
    return value


def _listing_day(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return date.fromisoformat(value.strip()).isoformat()
    return None


def collect(out_dir: str | Path) -> dict:
    """Save one fixed official request, with no retry, redirect or normalization."""
    import requests

    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    params = {"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1",
        "random": "0.6935816432433362"}
    receipt = {
        "source_url": SOURCE_URL, "params": params, "started_at": _now().isoformat(),
        "automatic_retry": False, "automatic_redirect": False,
        "timeout_seconds": [10, 20], "account_accessed": False,
        "execution_admitted": False, "status_code": None, "final_url": None,
        "response_headers": None, "byte_count": None, "sha256": None,
        "http_success": False,
    }
    try:
        response = requests.get(SOURCE_URL, params=params, timeout=(10, 20), allow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/market/product/stock/list/index.html"})
        body = response.content
        with (out / "response.bin").open("xb") as handle:
            handle.write(body)
        receipt.update({
            "status_code": response.status_code, "final_url": response.url,
            "response_headers": {key: response.headers.get(key) for key in (
                "Content-Type", "Content-Disposition", "Date", "Last-Modified", "Content-Length")}, "byte_count": len(body),
            "sha256": sha256(body).hexdigest(), "http_success": response.status_code == 200,
        })
        if response.status_code != 200:
            receipt["error"] = "http_status_not_200_no_retry"
    except requests.RequestException as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    receipt["completed_at"] = _now().isoformat()
    _write(out / "receipt.json", receipt)
    return receipt


def normalize(source_dir: str | Path, out_dir: str | Path) -> dict:
    """Preserve a successful saved response; never download or infer units."""
    source, out = Path(source_dir).resolve(), Path(out_dir).resolve()
    if out == source or out.is_relative_to(source):
        raise ValueError("Output must be outside the original source directory")
    if out.exists():
        raise FileExistsError("Output must be a new exclusive directory")
    receipt, response = _source(source)
    book = openpyxl.load_workbook(BytesIO(response), read_only=True, data_only=False)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("Expected the single official A-share-list worksheet")
        sheet = book.worksheets[0]
        declared_dimension = sheet.calculate_dimension()
        # The real export declares A1 even when its XML has thousands of rows.
        sheet.reset_dimensions()
        iterator = sheet.iter_rows(values_only=True)
        header = next(iterator, ())
        if len(header) != 22 or len(set(header)) != 22 or not all(isinstance(x, str) for x in header):
            raise ValueError("Expected the original 22 distinct source column captions")
        if not set(REQUIRED_HEADERS).issubset(header):
            raise ValueError("Official A-share-list required captions are missing")
        positions = {name: header.index(name) for name in REQUIRED_HEADERS}
        raw_rows, normalized, seen = [], [], set()
        for row_number, values in enumerate(iterator, start=2):
            if len(values) > len(header):
                raise ValueError("Source row is wider than the original header")
            values = tuple(values) + (None,) * (len(header) - len(values))
            code = values[positions["A股代码"]]
            if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
                raise ValueError("Original A-share code must remain a six-digit string; no aliases or padding")
            if code in seen:
                raise ValueError(f"Duplicate original A-share code: {code}")
            seen.add(code)
            raw_rows.append({"worksheet_row": row_number, "values": [_raw_value(x) for x in values]})
            normalized.append({
                "symbol": code, "exchange": "SZSE", "worksheet_row": row_number,
                "a_name_raw": _text(values[positions["A股简称"]]),
                "a_listing_date_raw": _text(values[positions["A股上市日期"]]),
                "a_listing_date": _listing_day(values[positions["A股上市日期"]]),
                "total_a_raw": _text(values[positions["A股总股本"]]),
                "circulating_a_raw": _text(values[positions["A股流通股本"]]),
                "total_a_reported_integer": _reported_integer(values[positions["A股总股本"]]),
                "circulating_a_reported_integer": _reported_integer(values[positions["A股流通股本"]]),
                "unit": "unknown", "unit_status": "source_no_explicit_unit_caption",
                "source_asof": None,
            })
    finally:
        book.close()
    if not normalized:
        raise ValueError("The source worksheet has no observations")
    frame = pd.DataFrame(normalized)
    for field in ("total_a_reported_integer", "circulating_a_reported_integer"):
        frame[field] = pd.array(frame[field], dtype="Int64")
    source_available_at = _stamp(receipt["completed_at"]).isoformat()
    source_day = source_available_at[:10]
    quality = {
        "schema": "official_szse_reported_capital_quality_v1",
        "observed_rows": len(frame), "original_column_count": len(header),
        "declared_worksheet_dimension": declared_dimension, "worksheet_dimensions_reset": True,
        "total_a_unknown_count": int(frame.total_a_reported_integer.isna().sum()),
        "circulating_a_unknown_count": int(frame.circulating_a_reported_integer.isna().sum()),
        "total_a_explicit_zero_count": int(frame.total_a_reported_integer.eq(0).fillna(False).sum()),
        "circulating_a_explicit_zero_count": int(frame.circulating_a_reported_integer.eq(0).fillna(False).sum()),
        "listing_date_unknown_count": int(frame.a_listing_date.isna().sum()),
        "listing_after_source_local_date_count": int(frame.a_listing_date.map(
            lambda value: value is not None and value > source_day).sum()),
        "observed_rows_are_not_verified_listed_or_tradable_universe": True,
        **FLAGS,
    }
    out.mkdir(parents=True, exist_ok=False)
    raw_path, parquet_path, quality_path = out / "original_rows.jsonl", out / "normalized.parquet", out / "quality.json"
    with raw_path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in raw_rows:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    frame.to_parquet(parquet_path, index=False)
    _write(quality_path, quality)
    derived_available_at = _now().isoformat()
    result = {
        "schema": SCHEMA, "status": "normalized_reported_literals_units_and_asof_unknown",
        "source_directory": str(source), "source_receipt": receipt,
        "source_receipt_sha256": _digest(source / "receipt.json"),
        "source_response_sha256": sha256(response).hexdigest(),
        "source_available_at": source_available_at, "derived_available_at": derived_available_at,
        "normalized_at": derived_available_at, "source_asof": None,
        "unit": "unknown", "unit_status": "source_no_explicit_unit_caption",
        "source_column_headers": list(header), "observed_rows": len(frame),
        "files": {name: {"path": str(path), "sha256": _digest(path)} for name, path in
                  (("original_rows", raw_path), ("normalized", parquet_path), ("quality", quality_path))},
        "limitations": [
            "No explicit source statistical date or unit caption; parsed integers are not verified share counts.",
            "Only the observed SZSE A-share list, not full A-share market coverage or a verified tradable roster.",
            "Reported circulating A-share capital is not verified index free float.",
            "Actual acquisition and derivation clocks cannot be backfilled as historical PIT knowledge.",
            "No validated strategy denominator, turnover reconstruction, orders or Ledger changes are produced.",
        ], **FLAGS,
    }
    _write(out / "result.json", result)
    return result


def _requested_symbols(symbols: list[str]) -> list[tuple[str, str, str]]:
    output = []
    for text in symbols:
        for item in str(text).split(","):
            item = item.strip()
            match = re.fullmatch(r"([0-9]{6})(?:\.(SZSE|SSE))?", item)
            if not match:
                raise ValueError("Use an exact original six-digit code or a canonical .SZSE/.SSE symbol")
            if item in {requested for requested, _, _ in output}:
                raise ValueError("Duplicate requested symbol")
            output.append((item, match.group(1), match.group(2) or "SZSE"))
    if not output:
        raise ValueError("At least one explicit symbol is required")
    return output


def lookup(snapshot: str | Path, symbols: list[str], as_of: str) -> dict:
    """Read literal observations only after both actual acquisition and derivation."""
    path = Path(snapshot).resolve()
    result, when = _read(path), _stamp(as_of)
    requested = _requested_symbols(symbols)
    if (result.get("schema") != SCHEMA or result.get("unit") != "unknown"
            or result.get("source_asof") is not None or result.get("validated_denominator") is not False):
        raise ValueError("Expected the unchanged unknown-unit official literal snapshot")
    source_at, derived_at = _stamp(result["source_available_at"]), _stamp(result["derived_available_at"])
    if derived_at < source_at:
        raise ValueError("Derivation cannot precede source acquisition")
    status = ("unknown_source_not_available" if when < source_at else
              "unknown_derived_not_available" if when < derived_at else "literal_observations_only")
    rows = []
    if status != "literal_observations_only":
        rows = [{"requested_symbol": requested_symbol, "symbol": code,
                 "status": "unknown_outside_source_exchange" if exchange != "SZSE" else status,
                 "total_a_reported_integer": None, "circulating_a_reported_integer": None,
                 "unit": "unknown", "source_asof": None}
                for requested_symbol, code, exchange in requested]
    else:
        source = Path(result["source_directory"])
        receipt, response = _source(source)
        if (_digest(source / "receipt.json") != result["source_receipt_sha256"]
                or sha256(response).hexdigest() != result["source_response_sha256"]
                or _stamp(receipt["completed_at"]) != source_at):
            raise ValueError("Source evidence differs from the normalized snapshot")
        ref = result["files"]["normalized"]
        parquet = Path(ref["path"])
        if _digest(parquet) != ref["sha256"]:
            raise ValueError("Normalized snapshot bytes changed")
        frame = pd.read_parquet(parquet)
        if frame.symbol.duplicated().any():
            raise ValueError("Duplicate normalized symbol")
        by_symbol = frame.set_index("symbol", drop=False)
        for requested_symbol, code, exchange in requested:
            if exchange != "SZSE":
                rows.append({"requested_symbol": requested_symbol, "symbol": code,
                             "status": "unknown_outside_source_exchange", "unit": "unknown",
                             "source_asof": None, "total_a_reported_integer": None,
                             "circulating_a_reported_integer": None})
                continue
            if code not in by_symbol.index:
                rows.append({"requested_symbol": requested_symbol, "symbol": code,
                             "status": "unknown_not_observed_in_snapshot", "unit": "unknown",
                             "source_asof": None, "total_a_reported_integer": None,
                             "circulating_a_reported_integer": None})
                continue
            row = by_symbol.loc[code].to_dict()
            row = {key: None if pd.isna(value) else value for key, value in row.items()}
            for field in ("worksheet_row", "total_a_reported_integer", "circulating_a_reported_integer"):
                if row[field] is not None:
                    row[field] = int(row[field])
            rows.append({"requested_symbol": requested_symbol, "status": "observed_literal_unit_and_asof_unknown", **row})
    return {"schema": "official_szse_reported_capital_lookup_v1", "status": status,
            "snapshot": str(path), "as_of": when.isoformat(), "source_available_at": source_at.isoformat(),
            "derived_available_at": derived_at.isoformat(), "rows": rows, **FLAGS}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collector = sub.add_parser("collect")
    collector.add_argument("--out", required=True)
    normalizer = sub.add_parser("normalize")
    normalizer.add_argument("--source", required=True)
    normalizer.add_argument("--out", required=True)
    reader = sub.add_parser("lookup")
    reader.add_argument("--snapshot", required=True)
    reader.add_argument("--symbols", required=True, nargs="+")
    reader.add_argument("--as-of", required=True)
    args = parser.parse_args(argv)
    if args.command == "collect":
        value = collect(args.out)
    elif args.command == "normalize":
        value = normalize(args.source, args.out)
    else:
        value = lookup(args.snapshot, args.symbols, args.as_of)
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if args.command == "collect" and not value["http_success"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
