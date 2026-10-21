"""Register completed local daily inventories without uploading an entire market.

Only receipts discovered below the configured trading-data directory are
accepted. Registration copies a verified projection into immutable web storage;
subsequent charts and research never read a moving provider directory.
"""

from hashlib import sha256
import csv
import itertools
import json
from pathlib import Path
import shutil
import sqlite3
import uuid

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trading import settings as trading_settings
from . import data, settings, store

VERSION = "local_daily_inventory_v1"
MAX_FOLDERS = 1200
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
BATCH_ROWS = 50000
ALIASES = {"turnover": "turnover_rate_pct", "turn": "turnover_rate_pct", "preclose": "pre_close",
           "isST": "is_st", "pctChg": "pct_chg", "peTTM": "pe_ttm", "pbMRQ": "pb"}
WARNINGS = [
    "历史研究库存，证券目录不能证明历史可投资股票池",
    "保留来源缺口；缺失记录不推定停牌，不补零或前值",
    "估值、ST、前收盘等来源字段不代表已核验历史可得性或复权连续性",
]


def _hash(path):
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path):
    if path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ValueError("本地库存清单过大")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _inside(folder, name):
    path = (folder / name).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError("库存引用超出所在目录或文件不存在")
    return path


def _folders():
    """Inspect folder names only, at bounded depth, never the StockDB data files."""
    root = (trading_settings.DATA_ROOT / "research").resolve()
    if not root.is_dir():
        return [], False
    queue, result = [(root, 0)], []
    while queue and len(result) < MAX_FOLDERS:
        folder, depth = queue.pop(0)
        if not folder.is_relative_to(root):
            continue
        result.append(folder)
        if depth < 4:
            try:
                children = sorted((p.resolve() for p in folder.iterdir()
                                   if p.is_dir() and not p.name.startswith(".")), reverse=True)
            except OSError:
                continue
            if depth == 0:
                preferred = {"stockdb_snapshots", "full_market", "stock_daily_bulk", "native_history"}
                children.sort(key=lambda p: (p.name not in preferred, p.name))
            queue.extend((p, depth + 1) for p in children[:MAX_FOLDERS])
    return result, bool(queue)


def _daily_fields(names):
    mapped = {ALIASES.get(name, name) for name in names}
    if "code" in mapped:
        mapped.add("symbol")
    return [field for field in [*data.COLUMNS, *data.OPTIONAL_FIELDS] if field in mapped]


def _snapshot_fields(folder, value):
    path = _inside(folder, value["files"]["daily"]["path"])
    with path.open(encoding="utf-8-sig", newline="") as stream:
        fields = set(next(csv.reader(stream)))
    decoded = value.get("files", {}).get("decoded_daily")
    if decoded:
        with _inside(folder, decoded["path"]).open(encoding="utf-8") as stream:
            # Bounded schema hint; registration computes exact full coverage.
            for _ in range(32):
                line = stream.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    raise ValueError("解码日线记录超过单条大小限制")
                record = json.loads(line)
                if isinstance(record.get("payload"), dict):
                    fields.update(record["payload"])
    return _daily_fields(fields)


def _descriptor(path):
    if not path.resolve().is_relative_to(trading_settings.DATA_ROOT.resolve()):
        raise ValueError("库存清单超出配置的数据目录")
    value, folder = _json(path), path.parent
    if (folder / "INVALID.json").exists():
        return None
    if (path.name == "snapshot.json" and value.get("schema") == "stockdb_research_snapshot_v1"
            and value.get("capture_complete") is True and value.get("execution_admitted") is False):
        kind = "stockdb_snapshot"
        if not value.get("effective_session_dates") or not value.get("requested_symbols"):
            raise ValueError("快照缺少原始请求证券和有效日期")
        start, end = min(value["effective_session_dates"]), max(value["effective_session_dates"])
        rows = value.get("rows", {}).get("available_daily", 0)
        fields = _snapshot_fields(folder, value)
        symbols = value.get("rows", {}).get("available_unique_symbols")
        sessions = len(value["effective_session_dates"])
        if sessions == 1:
            symbols = rows
    elif (path.name == "manifest.json" and value.get("status") == "complete_inventory_only"
          and value.get("execution_admitted") is False
          and len(value.get("chunks", [])) == value.get("chunks_expected")):
        kind = "stockdb_bulk"
        start, end = value["start"], value["end"]
        rows = sum(chunk["rows"] for chunk in value["chunks"])
        fields = _daily_fields(value["fields"])
        symbols, sessions = None, None
    elif (path.name == "result.json" and value.get("status") == "source_cache_complete"
          and value.get("kind") == "stock" and value.get("panel_file") == "raw_daily.parquet"
          and value.get("execution_admitted") is False and value.get("missing_days") == []):
        kind = "native_raw_cache"
        plan = _json(_inside(folder, "plan.json"))
        days = plan.get("days", [])
        if not days:
            raise ValueError("原始缓存缺少固定日期计划")
        start, end = days[0], days[-1]
        rows = value.get("rows") or value.get("total_rows")
        fields = _daily_fields(pq.read_schema(_inside(folder, "raw_daily.parquet")).names)
        symbols, sessions = value.get("symbols"), len(days)
    else:
        return None
    identifier = sha256((str(path.relative_to(trading_settings.DATA_ROOT.resolve())) + "\n" + _hash(path)).encode()).hexdigest()
    item = {"id": identifier, "name": folder.name, "kind": "historical", "format": kind,
            "start": start, "end": end, "rows": rows, "symbols": symbols,
            "sessions": sessions, "fields": sorted(set(fields)), "warnings": list(WARNINGS),
            "registered_dataset_id": None}
    if kind == "stockdb_snapshot":
        item.update(requested_symbols=len(value["requested_symbols"]), missing_symbols=value.get("rows", {}).get("missing_daily") if sessions == 1 else None,
                    missing_rows=value.get("rows", {}).get("missing_daily"))
    return item, path, value


def _discover():
    folders, truncated = _folders()
    found, warnings = [], []
    for folder in folders:
        for name in ("snapshot.json", "manifest.json", "result.json"):
            path = folder / name
            if not path.is_file():
                continue
            try:
                item = _descriptor(path)
                if item:
                    found.append(item)
            except (ValueError, KeyError, TypeError, OSError) as error:
                warnings.append(f"跳过库存 {folder.name}/{name}: {error}")
    if truncated:
        warnings.append("本地目录数量超过本次发现范围，请将完成的库存放在 trading/research 下四级目录内")
    return sorted(found, key=lambda record: (record[0]["end"], record[0]["name"]), reverse=True), warnings


def inventory():
    found, warnings = _discover()
    existing = {item.get("inventory_id"): item for item in store.datasets()}
    for item, _, _ in found:
        registered = existing.get(item["id"])
        if registered:
            item.update(registered_dataset_id=registered["id"], symbols=len(registered["symbols"]),
                        sessions=registered["quality"]["sessions"], fields=registered["fields"])
    return {"items": [item for item, _, _ in found], "warnings": warnings}


def _source_files(kind, folder, manifest):
    if kind == "stockdb_snapshot":
        names = ["daily", "availability"]
        if manifest.get("files", {}).get("decoded_daily"):
            names.append("decoded_daily")
        return [(_inside(folder, manifest["files"][name]["path"]), manifest["files"][name]["sha256"])
                for name in names]
    if kind == "stockdb_bulk":
        return [(_inside(folder, chunk["parquet_file"]), chunk["parquet_sha256"])
                for chunk in manifest["chunks"]]
    return [(_inside(folder, "raw_daily.parquet"), manifest.get("raw_daily_sha256") or manifest["panel_sha256"]),
            (_inside(folder, "plan.json"), manifest["plan_sha256"]),
            (_inside(folder, "receipts.json"), manifest["receipts_sha256"])]


def _frames(kind, folder, manifest):
    if kind == "stockdb_snapshot":
        decoded = manifest.get("files", {}).get("decoded_daily")
        if decoded:
            with _inside(folder, decoded["path"]).open(encoding="utf-8") as stream:
                while True:
                    lines = list(itertools.islice(stream, BATCH_ROWS))
                    if not lines:
                        break
                    rows = []
                    for line in lines:
                        record = json.loads(line)
                        payload = record.get("payload")
                        if record.get("status") != "decoded":
                            raise ValueError("已完成快照中存在失败的日线查询")
                        if payload == []:
                            continue
                        if not isinstance(payload, dict) or str(payload.get("code")) != str(record.get("symbol", ""))[:6]:
                            raise ValueError("解码记录证券与请求不一致")
                        if str(payload.get("date")) != str(record["date"]).replace("-", ""):
                            raise ValueError("解码记录日期与请求不一致")
                        rows.append({**payload, "symbol": record["symbol"], "date": record["date"]})
                    if rows:
                        yield pd.DataFrame(rows)
        else:
            yield from pd.read_csv(_inside(folder, manifest["files"]["daily"]["path"]),
                                   dtype={"code": str, "symbol": str}, chunksize=BATCH_ROWS)
    else:
        paths = [folder / "raw_daily.parquet"] if kind == "native_raw_cache" else [
            _inside(folder, item["parquet_file"]) for item in manifest["chunks"]]
        for path in paths:
            with pq.ParquetFile(path) as parquet:
                for batch in parquet.iter_batches(batch_size=BATCH_ROWS):
                    yield batch.to_pandas()


def _canonical(frame, kind):
    frame = frame.copy()
    frame = frame.replace("", pd.NA)
    for old, new in ALIASES.items():
        if old in frame:
            if new in frame:
                raise ValueError(f"重复的来源字段口径: {old}/{new}")
            frame = frame.rename(columns={old: new})
    if kind == "native_raw_cache":
        if "adjustflag" not in frame or not frame.adjustflag.astype(str).eq("3").all():
            raise ValueError("原始缓存的未复权口径不一致")
    if "symbol" not in frame:
        codes = frame.code.astype(str)
        native = codes.str.fullmatch(r"(?:sh|sz)\.\d{6}").all()
        if native:
            frame["symbol"] = codes.str[3:] + np.where(codes.str.startswith("sh."), ".SSE", ".SZSE")
        else:
            if not codes.str.fullmatch(r"\d{6}").all():
                raise ValueError("来源证券代码必须为原始六位代码")
            frame["symbol"] = codes + np.where(codes.str.startswith(("5", "6", "9")), ".SSE", ".SZSE")
    dates = frame.date.astype(str)
    if dates.str.fullmatch(r"\d{8}").all():
        frame["date"] = pd.to_datetime(dates, format="%Y%m%d", errors="raise")
    else:
        frame["date"] = pd.to_datetime(dates, errors="raise")
    omissions = pd.DataFrame(columns=["date", "symbol", "reason", "source_record_json"])
    if "tradestatus" in frame:
        status = pd.to_numeric(frame.tradestatus, errors="raise")
        required = frame[[*data.PRICE_FIELDS, "volume"]].apply(pd.to_numeric, errors="coerce")
        incomplete = ~np.isfinite(required).all(axis=1) | required[data.PRICE_FIELDS].le(0).any(axis=1)
        skip = (status.eq(0) | status.isna()) & incomplete
        if skip.any():
            omitted_rows = frame.loc[skip]
            omissions = omitted_rows[["date", "symbol"]].copy()
            omissions["reason"] = np.where(status.loc[skip].eq(0), "explicit_suspension_incomplete_required_fields", "unknown_status_incomplete_required_fields")
            omissions["source_record_json"] = [json.dumps(row, ensure_ascii=False) for row in data.records(omitted_rows)]
        frame = frame.loc[~skip].copy()
    if frame.empty:
        return frame, omissions
    frame, invalid = data.normalize_daily_fields(frame)
    if invalid.any():
        raise ValueError("本地日线库存存在不合格 OHLC / 成交量，请先处理来源异常")
    return frame, omissions


def register_inventory(inventory_id, name=None):
    found, _ = _discover()
    match = next((record for record in found if record[0]["id"] == inventory_id), None)
    if match is None:
        raise ValueError("本地库存不存在、已变化或尚未完成；请刷新库存列表")
    item, receipt_path, manifest = match
    # A registered copy remains immutable even if its original source disappears.
    previous = next((value for value in store.datasets() if value.get("inventory_id") == inventory_id), None)
    if previous:
        return previous
    label = (name or item["name"]).strip()
    if not label or len(label) > 80:
        raise ValueError("数据集名称须在 1 到 80 个字符之间")
    folder, kind = receipt_path.parent, item["format"]
    files = [(receipt_path, _hash(receipt_path)), *_source_files(kind, folder, manifest)]
    signatures = {}
    for path, expected in files:
        stat = path.stat()
        signatures[path] = (stat.st_size, stat.st_mtime_ns)
        if _hash(path) != expected:
            raise ValueError("本地库存文件与完成清单不一致，请保留原始库存并修复来源")
    identifier = uuid.uuid4().hex
    target = settings.STORAGE / "datasets" / identifier
    target.mkdir()
    writer, omission_writer, keydb = None, None, None
    try:
        # SQLite checks keys across chunks without retaining millions of Python tuples.
        keydb = sqlite3.connect(target / "keys.sqlite3")
        keydb.execute("PRAGMA journal_mode=OFF")
        keydb.execute("PRAGMA synchronous=OFF")
        keydb.execute("CREATE TABLE keys(date TEXT, symbol TEXT, PRIMARY KEY(date,symbol)) WITHOUT ROWID")
        dates, symbols, counts = set(), set(), {}
        latest = pd.DataFrame()
        total, zero_volume, omitted = 0, 0, 0
        omission_counts = {}
        schema = None
        available = set()
        for raw in _frames(kind, folder, manifest):
            frame, omissions = _canonical(raw, kind)
            omitted += len(omissions)
            key_parts = [part[["date", "symbol"]] for part in (frame, omissions) if not part.empty]
            all_keys = pd.concat(key_parts, ignore_index=True)
            dates.update(all_keys.date.tolist())
            lower, upper = pd.Timestamp(item["start"]), pd.Timestamp(item["end"])
            if not all_keys.date.between(lower, upper).all():
                raise ValueError("本地日线记录超出库存声明的日期范围")
            try:
                keydb.executemany("INSERT INTO keys VALUES(?,?)", zip(all_keys.date.dt.strftime("%Y-%m-%d"), all_keys.symbol))
            except sqlite3.IntegrityError as error:
                raise ValueError("本地库存存在跨分片重复的 date + symbol") from error
            if not omissions.empty:
                omission_schema = pa.schema([("date", pa.timestamp("ns")), ("symbol", pa.string()), ("reason", pa.string()), ("source_record_json", pa.string())])
                if omission_writer is None:
                    omission_writer = pq.ParquetWriter(target / "omitted_source_rows.parquet", omission_schema, compression="zstd")
                omission_writer.write_table(pa.Table.from_pandas(omissions, schema=omission_schema, preserve_index=False))
                for reason, count in omissions.reason.value_counts().items():
                    omission_counts[reason] = omission_counts.get(reason, 0) + int(count)
            if frame.empty:
                continue
            available.update(frame.columns)
            # Stable types allow an optional field to be entirely missing in one chunk.
            fields = [*data.COLUMNS, *data.OPTIONAL_FIELDS]
            frame = frame.reindex(columns=fields)
            latest = pd.concat([latest, frame], ignore_index=True).sort_values(["symbol", "date"]).groupby("symbol", sort=False).tail(2)
            if schema is None:
                schema = pa.schema([(field, pa.timestamp("ns") if field == "date" else
                                     pa.string() if field in ("symbol", "name") else
                                     pa.bool_() if field == "is_st" else pa.float64()) for field in fields])
                writer = pq.ParquetWriter(target / "bars.parquet", schema, compression="zstd")
            dates.update(frame.date.tolist())
            symbols.update(frame.symbol.tolist())
            total += len(frame)
            zero_volume += int(frame.volume.eq(0).sum())
            for field in fields:
                counts[field] = counts.get(field, 0) + int(frame[field].notna().sum())
            writer.write_table(pa.Table.from_pandas(frame.sort_values(["symbol", "date"]), schema=schema, preserve_index=False), row_group_size=1024)
        if not total:
            raise ValueError("本地库存没有可登记的日线记录")
        writer.close()
        writer = None
        if omission_writer:
            omission_writer.close()
            omission_writer = None
        keydb.close()
        keydb = None
        (target / "keys.sqlite3").unlink()
        if kind == "stockdb_snapshot":
            if total + omitted != manifest["rows"]["available_daily"]:
                raise ValueError("实际日线记录数量与快照完成清单不一致")
            calendar = pd.DatetimeIndex(pd.to_datetime(manifest["effective_session_dates"]))
            requested = manifest["rows"]["requested_daily"]
            missing = manifest["rows"]["missing_daily"]
        elif kind == "native_raw_cache":
            if total + omitted != manifest["rows"]:
                raise ValueError("实际日线记录数量与原始缓存完成清单不一致")
            plan = _json(folder / "plan.json")
            calendar = pd.DatetimeIndex(pd.to_datetime(plan["days"]))
            requested, missing = None, None
        else:
            expected = sum(chunk["rows"] for chunk in manifest["chunks"])
            if total + omitted != expected:
                raise ValueError("实际日线记录数量与库存分片清单不一致")
            calendar = pd.DatetimeIndex(sorted(dates))
            requested, missing = None, None
        if (calendar.empty or not calendar.is_unique or not calendar.is_monotonic_increasing
                or calendar.tz is not None or not calendar.equals(calendar.normalize())
                or not pd.DatetimeIndex(sorted(dates)).isin(calendar).all()):
            raise ValueError("本地库存交易日历与实际行情不一致")
        pd.DataFrame({"date": calendar}).to_parquet(target / "calendar.parquet", index=False)
        pd.DataFrame(data.latest_observations(latest)).to_parquet(target / "latest.parquet", index=False)
        for path, signature in signatures.items():
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != signature:
                raise ValueError("本地库存登记过程中来源发生变化，请刷新后重试")
        source_refs = [{"file": str(path), "sha256": expected} for path, expected in files]
        metadata = {"kind": "historical", "source": "本地 StockDB 冻结库存" if kind.startswith("stockdb") else "本地 BaoStock 原始冻结库存",
                    "adjustment": "raw", "rows": total, "symbols": sorted(symbols),
                    "start": str(calendar[0].date()), "end": str(calendar[-1].date()),
                    "quality": {"invalid_rows": 0, "zero_volume_rows": zero_volume,
                                "missing_grid_rows": len(calendar) * len(symbols) - total,
                                "sessions": len(calendar), "calendar_kind": "provided" if kind != "stockdb_bulk" else "observed_union"},
                    "warnings": list(WARNINGS), "inventory_id": inventory_id,
                    "registration": VERSION, "source_format": kind,
                    "source_refs": source_refs, "source_captured_at": manifest.get("captured_at") or manifest.get("finished_at"),
                    "requested_daily_rows": requested, "source_missing_daily_rows": missing,
                    "omitted_source_rows": omitted, "omission_counts": omission_counts,
                    "omitted_suspended_rows_without_prices": omission_counts.get("explicit_suspension_incomplete_required_fields", 0),
                    "omitted_unknown_status_rows_without_prices": omission_counts.get("unknown_status_incomplete_required_fields", 0),
                    "fields": [field for field in [*data.COLUMNS, *data.OPTIONAL_FIELDS] if field in available],
                    "field_units": {field: unit for field, unit in data.FIELD_UNITS.items() if field in available},
                    "field_coverage": {field: count / total for field, count in counts.items() if field in available},
                    "research_only": True, "pit_status": "not_verified", "pit_verified": False, "execution_admitted": False}
        if omitted:
            metadata["warnings"].append("日线投影不含必需行情字段缺失的停牌或状态未知记录；原始记录与原因另存 omitted_source_rows.parquet，未知状态不推定停牌")
        fingerprint = sha256(json.dumps({"version": VERSION, "inventory_id": inventory_id,
                                        "sources": source_refs}, sort_keys=True).encode()).hexdigest()
        metadata["snapshot_fingerprint"] = fingerprint
        (target / "source_manifest.json").write_text(json.dumps({"fingerprint": fingerprint, **metadata}, ensure_ascii=False, indent=2), encoding="utf-8")
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO datasets VALUES(?,?,?,?,?)",
                       (identifier, fingerprint, label, json.dumps(metadata, ensure_ascii=False), store.now()))
            row = db.execute("SELECT * FROM datasets WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row["id"] != identifier:
            shutil.rmtree(target)
        return store.decode_dataset(row)
    except BaseException:
        if writer:
            writer.close()
        if omission_writer:
            omission_writer.close()
        if keydb:
            keydb.close()
        shutil.rmtree(target)
        raise
