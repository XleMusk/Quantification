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
OPTIONAL_NUMERIC_FIELDS = ["pre_close", "amount", "turnover_rate_pct", "pct_chg", "amplitude",
                           "vol_ratio", "total_share", "float_share", "total_mv", "float_mv", "pe_ttm", "pb"]
OPTIONAL_FIELDS = [*OPTIONAL_NUMERIC_FIELDS, "is_st", "name"]
FIELD_UNITS = {**dict.fromkeys([*PRICE_FIELDS, "pre_close", "amount", "total_mv", "float_mv"], "CNY"),
               **dict.fromkeys(["volume", "total_share", "float_share"], "share"),
               **dict.fromkeys(["turnover_rate_pct", "pct_chg", "amplitude"], "percent"),
               **dict.fromkeys(["vol_ratio", "pe_ttm", "pb"], "ratio"), "is_st": "boolean"}
PIT_FIELDS = {"pe_ttm", "pb", "total_mv", "float_mv", "total_share", "float_share", "is_st", "name"}
PIT_WARNING = "估值、市值、股本、ST 与名称保留供应商原值；历史可得时间及修订记录未核验，不能直接视为当时已知信息"


def records(frame):
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def normalize_daily_fields(frame):
    """Normalize a daily chunk without applying the interactive CSV size caps.

    StockDB aliases belong in its source adapter: its ``turnover`` is a
    percentage, whereas the chart library uses that name for cash turnover.
    Missing optional observations remain null and are never filled from prices.
    """
    absent = sorted(set(COLUMNS) - set(frame.columns))
    if absent:
        raise ValueError("缺少字段: " + ", ".join(absent))
    if "turnover" in frame:
        raise ValueError("turnover 含义不明确：成交额请使用 amount（元），换手率请使用 turnover_rate_pct（百分数）")
    frame = frame[[*COLUMNS, *[field for field in OPTIONAL_FIELDS if field in frame]]].copy()
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
    for field in OPTIONAL_NUMERIC_FIELDS:
        if field not in frame:
            continue
        values = pd.to_numeric(frame[field], errors="raise").astype(float)
        if (~np.isfinite(values) & values.notna()).any():
            raise ValueError(f"{field} 不能包含无穷值")
        # Negative PE/PB and reported percentage returns can be meaningful.
        if field not in {"pe_ttm", "pb", "pct_chg"} and values.lt(0).any():
            raise ValueError(f"{field} 不能为负数；缺失值请保留为空")
        frame[field] = values
    if "is_st" in frame:
        def st_flag(value):
            if pd.isna(value):
                return pd.NA
            if value is True or value == 1 or (isinstance(value, str) and value.strip().lower() in {"1", "true"}):
                return True
            if value is False or value == 0 or (isinstance(value, str) and value.strip().lower() in {"0", "false"}):
                return False
            raise ValueError("is_st 只允许布尔值、0、1 或空值")
        frame["is_st"] = frame.is_st.map(st_flag).astype("boolean")
    if "name" in frame:
        frame["name"] = frame.name.astype("string").str.strip()
        if frame.name.str.len().gt(120).any() or frame.name.str.contains("\x00", regex=False, na=False).any():
            raise ValueError("name 最长 120 字符，不能包含空字符")
        frame["name"] = frame.name.mask(frame.name.eq(""), pd.NA)
    return frame, invalid


def field_metadata(frame):
    fields = [field for field in [*COLUMNS, *OPTIONAL_FIELDS] if field in frame]
    result = {"fields": fields, "field_units": {field: FIELD_UNITS[field] for field in fields if field in FIELD_UNITS},
              "field_coverage": {field: float(frame[field].notna().mean()) if len(frame) else 0.0 for field in fields}}
    if PIT_FIELDS.intersection(fields):
        result["pit_status"] = "not_verified"
    return result


def validate_panel(frame, calendar=None):
    if not 1 <= len(frame) <= 100000:
        raise ValueError("数据行数须在 1 到 100000 之间")
    frame, invalid = normalize_daily_fields(frame)
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
    if PIT_FIELDS.intersection(frame.columns):
        warnings.append(PIT_WARNING)
    meta = {"kind": kind, "source": source, "adjustment": adjustment,
            "rows": len(frame), "symbols": sorted(frame.symbol.unique().tolist()),
            "start": str(dates[0].date()), "end": str(dates[-1].date()),
            "quality": quality, "warnings": warnings, **field_metadata(frame)}
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
        pd.DataFrame(latest_observations(frame)).to_parquet(folder / "latest.parquet", index=False)
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


def read_bars(identifier, *, symbol=None, start=None, end=None, columns=None):
    if store.dataset(identifier) is None:
        raise ValueError("数据集不存在")
    filters = []
    if symbol is not None:
        filters.append(("symbol", "=", symbol))
    for value, operator in ((start, ">="), (end, "<=")):
        if value is not None:
            day = pd.Timestamp(value)
            if pd.isna(day) or day.tz is not None or day != day.normalize():
                raise ValueError("查询日期须为不含时区或时分秒的日频日期")
            filters.append(("date", operator, day))
    return pd.read_parquet(STORAGE / "datasets" / identifier / "bars.parquet",
                           filters=filters or None, columns=columns)


def read_latest(identifier):
    item = store.dataset(identifier)
    if item is None:
        raise ValueError("数据集不存在")
    path = STORAGE / "datasets" / identifier / "latest.parquet"
    if path.is_file():
        return records(pd.read_parquet(path))
    # Legacy CSV datasets are bounded by validate_panel; a large registered
    # inventory must have its compact cache rather than forcing a full scan.
    if item.get("rows", 0) > 100000:
        raise ValueError("大型数据集缺少最新观测缓存，请重新登记该数据集")
    return latest_observations(read_bars(identifier))


def latest_observations(frame):
    """Latest stored row per symbol, compared only with its previous observed row.

    Missing sessions are not filled, so the previous row need not be the prior
    exchange session. These are research snapshot values, never live quotes.
    """
    if frame.empty:
        return []
    frame = frame.sort_values(["symbol", "date"]).copy()
    groups = frame.groupby("symbol", sort=False)
    frame["previous_date"] = groups.date.shift(1)
    frame["previous_close"] = groups.close.shift(1)
    comparable = (frame.close.gt(0) & frame.previous_close.gt(0)
                  & np.isfinite(frame.close) & np.isfinite(frame.previous_close))
    frame["change_pct"] = (frame.close / frame.previous_close - 1).where(comparable)
    return records(frame.groupby("symbol", sort=False).tail(1)[
        [*COLUMNS, *[field for field in OPTIONAL_FIELDS if field in frame],
         "previous_date", "previous_close", "change_pct"]])


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
