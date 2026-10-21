"""Immutable minute snapshots, bounded capture jobs and causal chart queries."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import uuid

from fastapi import HTTPException
import pandas as pd

from trading import stockdb_minutes as source


PERIODS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
ID = re.compile(r"^[0-9a-f]{32}$")
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
# The local workbench runs one API process. A new process must never report an
# old in-memory background task as healthy or silently resume its acquisitions.
PROCESS_INSTANCE = uuid.uuid4().hex


def _load(path):
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("分钟快照清单过大")
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot(identifier, verify=True):
    if not ID.fullmatch(identifier):
        raise HTTPException(404, "分钟快照不存在")
    folder = (source.snapshot_root() / identifier).resolve()
    root = source.snapshot_root().resolve()
    if not folder.is_relative_to(root) or not (folder / "snapshot.json").is_file():
        raise HTTPException(404, "分钟快照不存在")
    item = _load(folder / "snapshot.json")
    if (item.get("schema") != source.SCHEMA or item.get("id") != identifier
            or item.get("capture_complete") is not True or item.get("price_basis") != "raw"
            or item.get("timezone") != source.TZ):
        raise ValueError("分钟快照清单不完整或格式不支持")
    fingerprint = sha256(json.dumps({key: value for key, value in item.items() if key != "fingerprint"},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if item.get("fingerprint") != fingerprint or "minutes" not in item.get("files", {}):
        raise ValueError("分钟快照清单版本已变化")
    for record in item.get("files", {}).values():
        path = (folder / record["path"]).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise ValueError("分钟快照引用不在本地保存目录内")
        if verify and source.file_hash(path) != record["sha256"]:
            raise ValueError("分钟快照文件已变化，请保留原始版本并重新采集")
    return item, folder


def _summary(item):
    result = {key: item.get(key) for key in ("id", "name", "kind", "source", "symbols", "start", "end", "rows", "warnings",
                                             "timezone", "timestamp_convention", "captured_at", "fingerprint", "price_basis")}
    result["coverage"] = {key: value for key, value in item["coverage"].items() if key != "by_symbol_day"}
    result["coverage"]["by_symbol_day"] = [
        {key: value for key, value in row.items() if key != "missing_timestamps"}
        for row in item["coverage"]["by_symbol_day"]]
    return result


def list_minute_datasets():
    root = source.snapshot_root()
    if not root.is_dir():
        return []
    items = []
    # A fixed, single-level directory prevents root database discovery/scanning.
    for folder in sorted(root.iterdir(), reverse=True):
        if len(items) >= 200:
            break
        if not folder.is_dir() or not ID.fullmatch(folder.name):
            continue
        try:
            item, _ = _snapshot(folder.name, verify=False)
            items.append(_summary(item))
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return sorted(items, key=lambda item: item.get("captured_at", ""), reverse=True)


def parse_cutoff(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", value):
        raise ValueError("分钟截止时间须为 ISO 日期时间，可使用 +08:00 时区")
    try:
        parsed = pd.Timestamp(value)
        if pd.isna(parsed):
            raise ValueError
        return parsed.tz_localize(source.TZ) if parsed.tz is None else parsed.tz_convert(source.TZ)
    except (TypeError, ValueError) as error:
        raise ValueError("分钟截止时间无效") from error


def _record(row):
    return {"timestamp": int(row.timestamp.timestamp() * 1000), "date": row.timestamp.isoformat(),
            "symbol": row.symbol, **{field: float(getattr(row, field)) for field in source.PRICE_FIELDS},
            "volume": float(row.volume), "amount": None if pd.isna(row.amount) else float(row.amount),
            "turnover": float(row.amount) if row.amount_usable else None,
            "amount_usable": bool(row.amount_usable), "session_date": row.session_date, "session": row.session,
            "auction": bool(row.auction), "gap_before": int(row.gap_before),
            "expected_minutes": 1, "observed_minutes": 1, "is_partial": False}


def aggregate_minutes(frame, period="1m", as_of=None):
    """Aggregate only revealed source rows, with independent morning/afternoon buckets."""
    if period not in PERIODS:
        raise ValueError("分钟周期仅支持 1m / 5m / 15m / 30m / 60m")
    end = parse_cutoff(as_of) if isinstance(as_of, str) else as_of
    rows = frame.loc[frame.timestamp.le(end)] if end is not None else frame
    rows = rows.sort_values("timestamp").copy()
    if rows.empty:
        return []
    if rows.symbol.nunique() != 1:
        raise ValueError("分钟图表聚合须选择单只证券")
    if period == "1m":
        return [_record(row) for row in rows.itertuples(index=False)]
    minutes = PERIODS[period]
    opening = pd.to_datetime(rows.session_date + rows.session.map(
        {"morning": " 09:30", "afternoon": " 13:00", "opening_auction": " 09:24"})).dt.tz_localize(source.TZ)
    position = ((rows.timestamp - opening).dt.total_seconds() / 60).astype(int)
    rows["bucket"] = (position - 1) // minutes
    records = []
    for (_, session, bucket), group in rows.groupby(["session_date", "session", "bucket"], sort=True):
        first, last = group.iloc[0], group.iloc[-1]
        point = _record(next(group.tail(1).itertuples(index=False)))
        expected = 1 if session == "opening_auction" else minutes
        observed = len(group)
        amount_complete = bool(group.amount_usable.all())
        point.update(open=float(first.open), high=float(group.high.max()), low=float(group.low.min()),
                     close=float(last.close), volume=float(group.volume.sum()),
                     amount=float(group.amount.sum()) if group.amount.notna().all() else None,
                     turnover=float(group.amount.sum()) if amount_complete else None,
                     amount_usable=amount_complete, auction=bool(group.auction.any()),
                     gap_before=int(first.gap_before), expected_minutes=expected, observed_minutes=observed,
                     is_partial=observed != expected or bool(group.gap_before.iloc[1:].gt(0).any()))
        # Date stays at the last actual revealed minute. A partial 10:17 candle
        # is never stamped 10:30 and never contains that later close.
        point["bucket_end"] = (opening.loc[group.index[0]] + pd.Timedelta(minutes=(int(bucket) + 1) * minutes)).isoformat() if session != "opening_auction" else last.timestamp.isoformat()
        records.append(point)
    return sorted(records, key=lambda row: row["timestamp"])


def minute_chart(identifier, symbol=None, period="1m", as_of=None):
    item, folder = _snapshot(identifier)
    symbol = symbol or item["symbols"][0]
    if symbol not in item["symbols"]:
        raise ValueError("证券不在分钟快照中")
    end = parse_cutoff(as_of)
    filters = [("symbol", "=", symbol)]
    if end is not None:
        filters.append(("timestamp", "<=", end))
    frame = pd.read_parquet(folder / item["files"]["minutes"]["path"], filters=filters)
    prices = aggregate_minutes(frame, period, end)
    expected = [stamp for day in item["session_dates"] for stamp in source.clock_grid(day, item["include_opening_auction"])
                if end is None or stamp <= end]
    observed = set(frame.timestamp)
    by_day = []
    for day in item["session_dates"]:
        grid = [stamp for stamp in expected if str(stamp.date()) == day]
        if not grid:
            continue
        count = int(frame.session_date.eq(day).sum())
        by_day.append({"symbol": symbol, "session_date": day, "requested": len(grid), "observed": count,
                       "missing": len(grid) - count, "complete_clock_grid": count == len(grid)})
    coverage = {"requested": len(expected), "observed": len(frame), "missing": len(expected) - len(frame),
                "status": ("complete_clock_grid" if len(expected) == len(frame) else "partial") if expected else "empty",
                "calendar_verified": False, "missing_timestamps": [stamp.isoformat() for stamp in expected if stamp not in observed],
                "by_symbol_day": by_day}
    return {"dataset": _summary(item), "symbol": symbol, "symbols": item["symbols"], "prices": prices,
            "calendar": [day for day in item["session_dates"] if end is None or day <= str(end.date())],
            "requested_days": item["session_dates"], "source_period": "1m", "period": period,
            "price_basis": "raw", "source_kind": "minute_snapshot", "timezone": source.TZ,
            "timestamp_convention": item["timestamp_convention"], "timestamp_semantics_verified": False,
            "fields": [*source.PRICE_FIELDS, "volume", "amount", "turnover"],
            "available_fields": [*source.PRICE_FIELDS, "volume", "amount", "turnover"],
            "turnover_available": True, "turnover_unit": "CNY", "volume_unit": "share",
            "as_of": end.isoformat() if end is not None else None, "coverage": coverage,
            "warnings": item["warnings"], "realtime": False, "execution_admitted": False}


def _capture_folder(identifier):
    if not ID.fullmatch(identifier):
        raise HTTPException(404, "分钟采集任务不存在")
    return source.snapshot_root() / "captures" / identifier


def _save_job(folder, value):
    temp = folder / f"job.{uuid.uuid4().hex}.tmp"
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temp, folder / "job.json")


def create_capture(symbols, start, end, name=None):
    symbols = sorted(set(symbols))
    if not 1 <= len(symbols) <= 5 or not all(source.SYMBOL_PATTERN.fullmatch(value) for value in symbols):
        raise ValueError("每次分钟采集限 1–5 个六位 .SSE / .SZSE 证券")
    for day in (start, end):
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("采集日期须为 YYYY-MM-DD")
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    if first > last or (last - first).days > 4:
        raise ValueError("每次分钟采集限顺序排列的 1–5 个日历日期")
    days = pd.date_range(first, last, freq="D").strftime("%Y-%m-%d").tolist()
    label = (name or "StockDB 分钟快照").strip()
    if not label or len(label) > 100:
        raise ValueError("分钟采集名称须在 1–100 字符之间")
    identifier = uuid.uuid4().hex
    folder = _capture_folder(identifier)
    folder.mkdir(parents=True, exist_ok=False)
    job = {"id": identifier, "status": "queued", "symbols": symbols, "start": start, "end": end,
           "days": days, "name": label, "requested": len(days) * len(symbols) * 240,
           "processed": 0, "snapshot_id": None, "error": None, "created_at": source.now(), "updated_at": source.now(),
           "owner_pid": os.getpid(), "owner_instance": PROCESS_INSTANCE,
           "warnings": ["按所选日历日期逐分钟查询；交易日历未独立核验，闭市日期将保留为未返回记录"],
           "live_trading": False}
    _save_job(folder, job)
    return job


def capture_status(identifier):
    path = _capture_folder(identifier) / "job.json"
    if not path.is_file():
        raise HTTPException(404, "分钟采集任务不存在")
    job = _load(path)
    if job["status"] in {"queued", "running"} and (job.get("owner_pid") != os.getpid()
            or job.get("owner_instance") != PROCESS_INSTANCE):
        return {**job, "recorded_status": job["status"], "status": "interrupted",
                "error": "原采集所属 API 进程已变化；保存的部分版本可单独查看，请明确新建采集任务"}
    return job


def run_capture(identifier):
    folder = _capture_folder(identifier)
    job = capture_status(identifier)
    if job["status"] in {"completed", "partial", "failed", "interrupted"}:
        return job
    lock = folder / "running.lock"
    try:
        handle = lock.open("x")
    except FileExistsError:
        return capture_status(identifier)
    try:
        handle.close()
        job.update(status="running", owner_pid=os.getpid(), owner_instance=PROCESS_INSTANCE, updated_at=source.now())
        _save_job(folder, job)
        def progress(processed, requested):
            job.update(processed=processed, requested=requested, updated_at=source.now())
            _save_job(folder, job)
        result = source.capture_snapshot(job["symbols"], job["days"], name=job["name"], workers=2, _progress=progress)
        job.update(status="partial" if result["coverage"]["status"] != "complete_clock_grid" else "completed",
                   snapshot_id=result["id"], coverage=result["coverage"], updated_at=source.now())
        _save_job(folder, job)
    except Exception as error:
        job.update(status="failed", error=f"{type(error).__name__}: {error}", updated_at=source.now())
        _save_job(folder, job)
    finally:
        lock.unlink(missing_ok=True)
    return job
