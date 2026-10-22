"""Bounded, read-only StockDB minute capture and immutable research snapshots.

The installed MessagePack endpoint is queried by exact symbol/date/minute keys.
An attempt covers the requested clock grid, never a global key enumeration. A
finished capture can still have missing/error rows; those remain in its receipt.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import shutil
import uuid
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import settings
from .stockdb_source import ENDPOINT, SYMBOL_PATTERN, local_get, source_minute


SCHEMA = "stockdb_minute_snapshot_v1"
TZ = "Asia/Shanghai"
TIMEZONE = ZoneInfo(TZ)
PRICE_FIELDS = ["open", "high", "low", "close"]
MINUTE_FIELDS = ["timestamp", "symbol", *PRICE_FIELDS, "volume", "amount", "session_date", "session", "auction", "amount_usable", "gap_before"]
MAX_SYMBOLS, MAX_DAYS = 10, 5
WARNINGS = [
    "分钟快照仅包含实际返回记录；缺失分钟不补零、不前值填充，不推定停牌",
    "保留供应商时间标签，按分钟结束标签分桶；安装版本的区间标签含义尚未独立核验",
    "成交额按元、成交量按股保存；检查量价一致性，不进行猜测换算",
    "分钟 OHLCV 不是逐笔成交，不能还原真实价位成交分布或订单流",
]


def snapshot_root():
    return settings.DATA_ROOT / "research" / "minute_snapshots"


def now():
    return datetime.now(TIMEZONE).isoformat()


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_json_float": str(value)}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def file_hash(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clock_grid(day, include_opening_auction=False):
    """240 requested end labels; no artificial lunch/overnight minute slots."""
    value = pd.Timestamp(day)
    if pd.isna(value) or value.tz is not None or value != value.normalize():
        raise ValueError("分钟采集日期须为 YYYY-MM-DD")
    day = value.strftime("%Y-%m-%d")
    ends = [*pd.date_range(f"{day} 09:31", f"{day} 11:30", freq="min", tz=TZ),
            *pd.date_range(f"{day} 13:01", f"{day} 15:00", freq="min", tz=TZ)]
    if include_opening_auction:
        ends.insert(0, pd.Timestamp(f"{day} 09:25", tz=TZ))
    return pd.DatetimeIndex(ends)


def _timestamps(values):
    text = values.astype(str)
    if text.str.fullmatch(r"\d{14}").all():
        parsed = pd.to_datetime(text, format="%Y%m%d%H%M%S", errors="raise").dt.tz_localize(TZ)
    else:
        # CSV imports explicitly use Shanghai local time; aware inputs retain
        # their absolute time and are then displayed in Shanghai.
        parsed_values = []
        for value in values:
            item = pd.Timestamp(value)
            if pd.isna(item):
                raise ValueError("分钟时间不能缺失")
            parsed_values.append(item.tz_localize(TZ) if item.tz is None else item.tz_convert(TZ))
        parsed = pd.Series(pd.DatetimeIndex(parsed_values), index=values.index)
    if not parsed.eq(parsed.dt.floor("min")).all():
        raise ValueError("分钟时间必须精确到整分钟")
    return parsed


def normalize_minutes(frame):
    frame = frame.copy()
    if "timestamp" not in frame and "date" in frame:
        frame = frame.rename(columns={"date": "timestamp"})
    absent = {"timestamp", "symbol", *PRICE_FIELDS, "volume"} - set(frame)
    if absent:
        raise ValueError("分钟数据缺少字段: " + ", ".join(sorted(absent)))
    frame["timestamp"] = _timestamps(frame.timestamp)
    frame["symbol"] = frame.symbol.astype(str).str.strip().str.upper()
    if not frame.symbol.map(lambda value: bool(SYMBOL_PATTERN.fullmatch(value))).all():
        raise ValueError("分钟证券须为六位代码加 .SSE / .SZSE")
    if frame.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("分钟数据存在重复的证券与时间")
    for field in [*PRICE_FIELDS, "volume"]:
        frame[field] = pd.to_numeric(frame[field], errors="raise").astype(float)
    prices = frame[PRICE_FIELDS]
    if (not np.isfinite(frame[[*PRICE_FIELDS, "volume"]]).all().all()
            or prices.le(0).any().any() or frame.volume.lt(0).any()
            or frame.high.lt(prices.max(axis=1) - 2e-6 * prices.max(axis=1)).any()
            or frame.low.gt(prices.min(axis=1) + 2e-6 * prices.max(axis=1)).any()):
        raise ValueError("分钟 OHLC / 成交量不合格")
    frame["amount"] = pd.to_numeric(frame.get("amount", pd.Series(np.nan, index=frame.index)), errors="raise").astype(float)
    if (~np.isfinite(frame.amount) & frame.amount.notna()).any() or frame.amount.lt(0).any():
        raise ValueError("分钟成交额须为非负有限值或空值")
    times = frame.timestamp.dt.strftime("%H:%M")
    morning = times.between("09:31", "11:30")
    afternoon = times.between("13:01", "15:00")
    opening = times.eq("09:25")
    if not (morning | afternoon | opening).all():
        raise ValueError("分钟时间不在所支持的 A 股结束标签时段")
    frame["session_date"] = frame.timestamp.dt.strftime("%Y-%m-%d")
    frame["session"] = np.select([morning, afternoon], ["morning", "afternoon"], default="opening_auction")
    frame["auction"] = opening | times.between("14:58", "15:00")
    unit_price = frame.amount.div(frame.volume.where(frame.volume.gt(0)))
    frame["amount_usable"] = ((frame.volume.eq(0) & frame.amount.eq(0))
                              | (frame.volume.gt(0) & frame.amount.gt(0)
                                 & unit_price.between(frame.low - .02, frame.high + .02)))
    frame = frame.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    # This count only uses expected clock labels before this observed row.
    # A later missing minute cannot change an earlier replay prefix.
    previous = frame.groupby(["symbol", "session_date", "session"]).timestamp.shift(1)
    origins = pd.to_datetime(frame.session_date + frame.session.map(
        {"morning": " 09:30", "afternoon": " 13:00", "opening_auction": " 09:24"})).dt.tz_localize(TZ)
    frame["gap_before"] = ((frame.timestamp - previous.fillna(origins)).dt.total_seconds() / 60 - 1).astype(int)
    return frame[MINUTE_FIELDS]


def _coverage(frame, symbols, days, include_opening_auction, receipts=None):
    by_day = []
    receipts = receipts if receipts is not None else pd.DataFrame()
    for symbol in symbols:
        for day in days:
            grid = clock_grid(day, include_opening_auction)
            rows = frame.loc[frame.symbol.eq(symbol) & frame.session_date.eq(day)]
            observed = set(rows.timestamp)
            query = receipts.loc[receipts.symbol.eq(symbol) & receipts.session_date.eq(day)] if len(receipts) else receipts
            errors = int(query.status.eq("error").sum()) if len(query) else 0
            invalid = int(query.status.eq("invalid").sum()) if len(query) else 0
            by_day.append({"symbol": symbol, "session_date": day, "requested": len(grid), "observed": len(rows),
                           "missing": len(grid) - len(observed), "errors": errors, "invalid": invalid,
                           "complete_clock_grid": len(observed) == len(grid),
                           "missing_timestamps": [stamp.isoformat() for stamp in grid if stamp not in observed]})
    requested = sum(item["requested"] for item in by_day)
    return {"requested": requested, "observed": len(frame), "missing": requested - len(frame),
            "errors": sum(item["errors"] for item in by_day), "invalid": sum(item["invalid"] for item in by_day),
            "status": "complete_clock_grid" if len(frame) == requested else "partial",
            "calendar_verified": False, "by_symbol_day": by_day}


def save_snapshot(frame, *, name, source="imported minute observations", symbols=None, days=None,
                  include_opening_auction=False, kind="historical", receipts=None, source_records=None):
    """Save imported/captured rows once. HTTP callers never supply a disk path."""
    if not name.strip() or len(name) > 100 or kind not in {"historical", "synthetic"}:
        raise ValueError("分钟快照名称或数据类型无效")
    normalized = normalize_minutes(frame) if len(frame) else pd.DataFrame({
        "timestamp": pd.Series([], dtype=f"datetime64[ns, {TZ}]"), "symbol": pd.Series([], dtype=str),
        **{field: pd.Series([], dtype=float) for field in [*PRICE_FIELDS, "volume", "amount"]},
        "session_date": pd.Series([], dtype=str), "session": pd.Series([], dtype=str),
        "auction": pd.Series([], dtype=bool), "amount_usable": pd.Series([], dtype=bool),
        "gap_before": pd.Series([], dtype=int)})
    symbols = sorted(set(symbols if symbols is not None else normalized.symbol))
    days = sorted(set(str(pd.Timestamp(day).date()) for day in (days if days is not None else normalized.session_date)))
    if not symbols or not days or not 1 <= len(symbols) <= MAX_SYMBOLS or not 1 <= len(days) <= MAX_DAYS:
        raise ValueError("分钟快照每次限 1–10 证券、1–5 个明确日期")
    if not all(SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise ValueError("分钟证券代码格式无效")
    if not normalized.symbol.isin(symbols).all() or not normalized.session_date.isin(days).all():
        raise ValueError("分钟记录超出请求证券或日期")
    allowed = {stamp for day in days for stamp in clock_grid(day, include_opening_auction)}
    if not normalized.timestamp.isin(allowed).all():
        raise ValueError("分钟记录超出声明的请求时间格点")
    coverage = _coverage(normalized, symbols, days, include_opening_auction, receipts)
    root = snapshot_root()
    root.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    staging, target = root / (identifier + ".partial"), root / identifier
    staging.mkdir()
    try:
        normalized.to_parquet(staging / "minutes.parquet", index=False, compression="zstd", row_group_size=1024)
        files = {"minutes": {"path": "minutes.parquet", "sha256": file_hash(staging / "minutes.parquet")}}
        if receipts is not None:
            receipts.to_parquet(staging / "query_receipts.parquet", index=False)
            files["receipts"] = {"path": "query_receipts.parquet", "sha256": file_hash(staging / "query_receipts.parquet")}
        if source_records is not None:
            (staging / "decoded_source_records.jsonl").write_text("".join(
                json.dumps(_json_safe(record), ensure_ascii=False, allow_nan=False) + "\n" for record in source_records), encoding="utf-8")
            files["decoded_source"] = {"path": "decoded_source_records.jsonl", "sha256": file_hash(staging / "decoded_source_records.jsonl")}
        metadata = {"schema": SCHEMA, "id": identifier, "name": name.strip(), "kind": kind,
                    "source": source, "price_basis": "raw", "source_period": "1m", "timezone": TZ,
                    "timestamp_convention": "provider_end_labels_unverified", "timestamp_semantics_verified": False,
                    "include_opening_auction": include_opening_auction, "symbols": symbols,
                    "start": min(days), "end": max(days), "session_dates": days, "rows": len(normalized),
                    "captured_at": now(), "coverage": coverage, "fields": [*PRICE_FIELDS, "volume", "amount"],
                    "field_units": {**dict.fromkeys(PRICE_FIELDS, "CNY"), "volume": "share", "amount": "CNY"},
                    "amount_unusable_rows": int((~normalized.amount_usable).sum()),
                    "warnings": (["合成分钟数据，仅用于接入、复盘与计算验收"] if kind == "synthetic" else []) + list(WARNINGS),
                    "files": files, "capture_complete": True, "execution_admitted": False,
                    "payload_representation": "decoded_messagepack_json_not_original_transport_bytes" if source_records is not None else "imported_rows"}
        metadata["fingerprint"] = sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        (staging / "snapshot.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        staging.rename(target)
        return metadata
    except BaseException:
        shutil.rmtree(staging)
        raise


def capture_snapshot(symbols, days, *, name="StockDB 分钟快照", include_opening_auction=False, workers=4, _get=None, _progress=None):
    """Query a bounded requested grid once; failures are recorded, not retried."""
    symbols = sorted(set(symbols))
    days = sorted(set(str(pd.Timestamp(day).date()) for day in days))
    if not 1 <= len(symbols) <= MAX_SYMBOLS or not 1 <= len(days) <= MAX_DAYS or not 1 <= workers <= 8:
        raise ValueError("采集限 1–10 证券、1–5 日期、1–8 并发")
    if not all(SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise ValueError("分钟证券代码格式无效")
    tasks = [(symbol, stamp) for symbol in symbols for day in days for stamp in clock_grid(day, include_opening_auction)]

    def query(task):
        symbol, stamp = task
        captured = {"symbol": symbol, "session_date": stamp.strftime("%Y-%m-%d"), "timestamp": stamp.isoformat(),
                    "key": f"分钟k:{symbol[:6]}:{stamp.strftime('%Y%m%d%H%M%S')}", "started_at": now()}
        payloads = []
        try:
            def getter(key):
                value = (_get or local_get)(key)
                payloads.append(value)
                return value
            value = source_minute(symbol, captured["session_date"], stamp.strftime("%H%M%S"), _get=getter)
            if value is None:
                captured["status"] = "missing"
                row = None
            else:
                row = {"timestamp": stamp, "symbol": symbol, **{field: value.get(field) for field in [*PRICE_FIELDS, "volume", "amount"]}}
                try:
                    normalize_minutes(pd.DataFrame([row]))
                    captured["status"] = "observed"
                except (TypeError, ValueError) as error:
                    captured.update(status="invalid", error=str(error))
                    row = None
        except Exception as error:
            captured.update(status="invalid" if payloads and isinstance(error, (TypeError, ValueError)) else "error",
                            error=f"{type(error).__name__}: {error}")
            row = None
        captured["finished_at"] = now()
        return row, captured, {**captured, "payload": payloads[0] if payloads else None}

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        # Small submission batches permit bounded fail-fast behavior instead
        # of queuing thousands of five-second failures when the service is down.
        for offset in range(0, len(tasks), workers * 2):
            chunk = list(executor.map(query, tasks[offset:offset + workers * 2]))
            results.extend(chunk)
            if _progress:
                _progress(len(results), len(tasks))
            if chunk and all(receipt["status"] == "error" for _, receipt, _ in chunk):
                for symbol, stamp in tasks[len(results):]:
                    receipt = {"symbol": symbol, "session_date": stamp.strftime("%Y-%m-%d"),
                               "timestamp": stamp.isoformat(), "status": "not_queried", "error": "capture_stopped_after_transport_errors"}
                    results.append((None, receipt, {**receipt, "payload": None}))
                break
    rows = [row for row, _, _ in results if row is not None]
    receipts = pd.DataFrame([receipt for _, receipt, _ in results])
    return save_snapshot(pd.DataFrame(rows), name=name, symbols=symbols, days=days,
                         source=f"local StockDB HTTP/MessagePack {ENDPOINT}",
                         include_opening_auction=include_opening_auction, receipts=receipts,
                         source_records=[record for _, _, record in results])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture immutable minute research data from local StockDB; no account changes")
    parser.add_argument("--symbols", required=True, help="600000.SSE,000001.SZSE; at most 10")
    parser.add_argument("--days", required=True, help="Explicit YYYY-MM-DD dates, comma separated; at most 5")
    parser.add_argument("--name", default="StockDB 分钟快照")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--include-opening-auction", action="store_true")
    args = parser.parse_args(argv)
    result = capture_snapshot(args.symbols.split(","), args.days.split(","), name=args.name,
                              workers=args.workers, include_opening_auction=args.include_opening_auction)
    print(json.dumps({key: result[key] for key in ("id", "name", "rows", "coverage", "warnings")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
