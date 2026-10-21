from hashlib import sha256
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd

from . import store
from .settings import PROJECT, STORAGE

PRICE_FIELDS = ["open", "high", "low", "close"]
COLUMNS = ["date", "symbol", *PRICE_FIELDS, "volume"]


def records(frame):
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def validate_panel(frame, calendar=None):
    absent = sorted(set(COLUMNS) - set(frame.columns))
    if absent:
        raise ValueError("缺少字段: " + ", ".join(absent))
    if not 1 <= len(frame) <= 100000:
        raise ValueError("数据行数须在 1 到 100000 之间")
    frame = frame[COLUMNS].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame.date.isna().any() or frame.date.dt.tz is not None or not frame.date.eq(frame.date.dt.normalize()).all():
        raise ValueError("date 必须为不含时区或时分秒的日频日期")
    if frame.symbol.isna().any():
        raise ValueError("证券代码不能缺失")
    frame["symbol"] = frame.symbol.astype(str).str.strip().str.upper()
    if not frame.symbol.str.fullmatch(r"[A-Z0-9.]{1,24}").all():
        raise ValueError("证券代码只允许字母、数字和小数点，最长 24 位")
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("存在重复的 date + symbol，未自动覆盖或去重")
    for field in PRICE_FIELDS + ["volume"]:
        frame[field] = pd.to_numeric(frame[field], errors="raise").astype(float)
    prices = frame[PRICE_FIELDS]
    if not np.isfinite(frame[PRICE_FIELDS + ["volume"]]).all().all():
        raise ValueError("价格和成交量不能含缺失值或无穷值；缺失交易日请保留为空行缺口")
    invalid = (prices.le(0).any(axis=1) | frame.volume.lt(0)
               | (frame.high + prices.max(axis=1) * 2e-6 < prices.max(axis=1))
               | (frame.low - prices.max(axis=1) * 2e-6 > prices.min(axis=1)))
    symbols = sorted(frame.symbol.unique().tolist())
    if len(symbols) > 100:
        raise ValueError("首版每个数据集最多 100 个证券")
    calendar_kind = "provided" if calendar is not None else "observed_union"
    dates = pd.DatetimeIndex(pd.to_datetime(calendar if calendar is not None else sorted(frame.date.unique())), name="date")
    if dates.empty or dates.hasnans or dates.tz is not None or not dates.is_unique or not dates.is_monotonic_increasing or not dates.equals(dates.normalize()):
        raise ValueError("交易日历必须为有序、唯一的日频日期")
    if not frame.date.isin(dates).all():
        raise ValueError("行情含交易日历之外的日期")
    if len(dates) * len(symbols) > 150000:
        raise ValueError("日历 × 证券数量超过首版 150000 格点限制")
    quality = {"invalid_rows": int(invalid.sum()), "zero_volume_rows": int(frame.volume.eq(0).sum()),
               "missing_grid_rows": len(dates) * len(symbols) - len(frame),
               "sessions": len(dates), "calendar_kind": calendar_kind}
    return frame.sort_values(["date", "symbol"]).reset_index(drop=True), dates, quality


def register(frame, name, kind, source, adjustment, calendar=None, warnings=None):
    if not name.strip() or not source.strip():
        raise ValueError("数据集名称和来源不能为空")
    frame, dates, quality = validate_panel(frame, calendar)
    warnings = list(warnings or [])
    if quality["calendar_kind"] == "observed_union":
        warnings.append("日历来自观测日期并集，未独立核验；全市场共同缺失日无法识别")
    if kind == "historical":
        warnings.append("历史数据，不代表当前行情；历史证券范围与复权口径尚未独立核验")
    else:
        warnings.append("合成数据，仅验证计算和流程，不构成市场证据")
    if quality["invalid_rows"]:
        warnings.append("OHLC 或成交量不合格，因子任务将被阻断")
    if quality["zero_volume_rows"]:
        warnings.append("零成交量记录在因子计算中视为缺失，不推定可成交")
    meta = {"kind": kind, "source": source, "adjustment": adjustment,
            "rows": len(frame), "symbols": sorted(frame.symbol.unique().tolist()),
            "start": str(dates[0].date()), "end": str(dates[-1].date()),
            "quality": quality, "warnings": warnings}
    content = (frame.to_csv(index=False) + "\n".join(dates.strftime("%Y-%m-%d"))
               + json.dumps({"name": name, **meta}, sort_keys=True, ensure_ascii=False)).encode("utf-8")
    fingerprint = sha256(content).hexdigest()
    with store.connection() as db:
        previous = db.execute("SELECT * FROM datasets WHERE fingerprint=?", (fingerprint,)).fetchone()
        if previous:
            return store.decode_dataset(previous)
    identifier = uuid.uuid4().hex
    folder = STORAGE / "datasets" / identifier
    if not folder.resolve().is_relative_to((STORAGE / "datasets").resolve()):
        raise ValueError("Invalid storage path")
    folder.mkdir()
    try:
        frame.to_parquet(folder / "bars.parquet", index=False)
        pd.DataFrame({"date": dates}).to_parquet(folder / "calendar.parquet", index=False)
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO datasets VALUES(?,?,?,?,?)",
                       (identifier, fingerprint, name, json.dumps(meta, ensure_ascii=False), store.now()))
            row = db.execute("SELECT * FROM datasets WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row["id"] != identifier:
            shutil.rmtree(folder)
        return store.decode_dataset(row)
    except Exception:
        shutil.rmtree(folder)
        raise


def read_bars(identifier):
    if store.dataset(identifier) is None:
        raise ValueError("数据集不存在")
    return pd.read_parquet(STORAGE / "datasets" / identifier / "bars.parquet")


def sync_local_cache():
    from a_stock_committee.market_data import normalize_symbol

    frames = []
    for path in sorted((PROJECT / "market_data/a_share/daily").glob("*.parquet")):
        frame = pd.read_parquet(path)
        if frame.empty or not {"vt_symbol", "adjustflag", *PRICE_FIELDS, "date", "volume"}.issubset(frame.columns):
            continue
        if "security_type" in frame:
            if not frame.security_type.eq("stock").all():
                continue
        else:
            # Older BaoStock caches predate the explicit security_type field.
            try:
                symbols = [normalize_symbol(value) for value in frame.vt_symbol.unique()]
            except (ValueError, AttributeError):
                continue
            if not all(info.security_type == "stock" and info.code.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")) for info in symbols):
                continue
        if not frame.adjustflag.astype(str).eq("2").all():
            continue
        frames.append(frame.rename(columns={"vt_symbol": "symbol"})[COLUMNS])
    if not frames:
        raise ValueError("未发现可用的前复权股票缓存")
    return register(pd.concat(frames, ignore_index=True), "本地日线缓存", "historical", "BaoStock / 已有本地缓存", "forward",
                    warnings=["仅为本地保存的候选股票样本，不是历史全市场股票池"])


def seed_demo():
    from quant_learning_starter.factor_lab import synthetic_panel
    frame, calendar = synthetic_panel()
    return register(frame, "合成教学数据", "synthetic", "本地生成 / 固定随机种子 20260909", "synthetic", calendar)
