"""Recent A-share market trend calibration and validation.

This module is intentionally lightweight: it uses current index/ETF daily bars
as tradable proxies for broad market and technology/sector themes, calibrates a
small directional score on recent history, and can evaluate yesterday's forecast
against today's closed bar without lookahead.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import json
from pathlib import Path
import time
from typing import Iterable

import numpy as np
import pandas as pd


REPORT_DIR = Path("committee_reviews")
TREND_HISTORY_DIR = Path("market_data") / "a_share" / "trend_history"
TREND_PARAM_DIR = Path("market_data") / "a_share" / "trend_params"
DEFAULT_HISTORY_YEARS = 5


@dataclass(frozen=True)
class TrendTarget:
    """A market proxy to fetch and score."""

    target_id: str
    name: str
    kind: str
    symbol: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrendParams:
    """Weights used by the recent directional score."""

    polarity: float = 1.0
    momentum_weight: float = 0.45
    trend_weight: float = 0.30
    reversal_weight: float = 0.15
    volume_weight: float = 0.10
    risk_weight: float = 0.20
    threshold: float = 0.05


@dataclass
class TrendResult:
    target_id: str
    name: str
    symbol: str
    latest_date: str
    prediction_date: str | None
    score: float
    signal: str
    actual_ret_pct: float | None
    correct: bool | None
    train_accuracy: float
    params: dict[str, float]
    diagnostics: dict[str, float | str | None]
    event_adjustment: dict[str, float | str] | None = None
    error: str | None = None


@dataclass
class TrendHistoryCacheEntry:
    target_id: str
    name: str
    symbol: str
    kind: str
    rows: int
    start: str
    end: str
    path: str
    refreshed: bool
    error: str = ""


KNOWN_TARGETS = [
    TrendTarget("overall", "上证指数", "index", "sh000001", ("broad",)),
    TrendTarget("all_a", "中证全指", "index", "sh000985", ("broad",)),
    TrendTarget("chinext", "创业板指", "index", "sz399006", ("growth", "broad")),
    TrendTarget("star50", "科创50", "index", "sh000688", ("growth", "semiconductor")),
    TrendTarget("star_chip_etf", "科创芯片ETF", "etf", "588200", ("semiconductor", "advanced_chip")),
    TrendTarget("semiconductor_etf", "半导体ETF", "etf", "512480", ("semiconductor",)),
    TrendTarget("chip_etf", "芯片ETF", "etf", "159995", ("semiconductor", "advanced_chip")),
    TrendTarget("communication_etf", "通信ETF国泰", "etf", "515880", ("communication", "cpo", "copper_interconnect")),
    TrendTarget("communication_hx_etf", "通信ETF华夏", "etf", "515050", ("communication", "cpo", "copper_interconnect")),
    TrendTarget("consumer_electronics_hx_etf", "消费电子ETF华夏", "etf", "159732", ("consumer_electronics", "electronics_manufacturing", "ai_hardware")),
    TrendTarget("consumer_electronics_e_etf", "消费电子ETF易方达", "etf", "562950", ("consumer_electronics", "electronics_manufacturing", "ai_hardware")),
    TrendTarget("electronic50_etf", "电子50ETF", "etf", "515320", ("electronics", "mlcc", "pcb_proxy")),
    TrendTarget("electronics_etf", "电子ETF", "etf", "515260", ("electronics", "pcb_proxy", "mlcc")),
    TrendTarget("ai_etf", "人工智能ETF", "etf", "159819", ("ai_hardware", "server_odm", "cpo")),
    TrendTarget("cloud_computing_etf", "云计算ETF", "etf", "159611", ("server_odm", "ai_hardware")),
    TrendTarget("star_chip_alt_etf", "科创芯片ETF备选", "etf", "588290", ("semiconductor", "advanced_chip")),
]

DEFAULT_TARGETS = [
    target
    for target in KNOWN_TARGETS
    if target.target_id
    in {
        "overall",
        "chinext",
        "star50",
        "star_chip_etf",
        "semiconductor_etf",
        "chip_etf",
        "communication_etf",
        "communication_hx_etf",
        "consumer_electronics_hx_etf",
        "electronic50_etf",
        "electronics_etf",
        "ai_etf",
    }
]


EVENT_PROFILES = {
    "nvl72_bom": {
        "name": "NVL72/Rubin机柜BOM重估",
        "tag_boosts": {
            "pcb_proxy": 0.28,
            "consumer_electronics": 0.24,
            "electronics_manufacturing": 0.20,
            "mlcc": 0.20,
            "communication": 0.18,
            "cpo": 0.18,
            "copper_interconnect": 0.18,
            "server_odm": 0.14,
            "ai_hardware": 0.14,
            "semiconductor": 0.06,
        },
        "overheat_penalty": 0.35,
    },
}


PARAM_GRID = [
    TrendParams(polarity=p, momentum_weight=m, trend_weight=t, reversal_weight=r, volume_weight=v, risk_weight=q, threshold=th)
    for p in (1.0, -1.0)
    for m in (0.25, 0.45, 0.65)
    for t in (0.20, 0.35)
    for r in (0.05, 0.15)
    for v in (0.05,)
    for q in (0.20, 0.50, 0.80)
    for th in (0.03, 0.08)
]


def default_history_start(years: int = DEFAULT_HISTORY_YEARS) -> str:
    return (date.today() - timedelta(days=365 * years)).strftime("%Y-%m-%d")


def default_history_end() -> str:
    return date.today().strftime("%Y-%m-%d")


def fetch_target_history(target: TrendTarget, start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch and normalize one target's daily history via AkShare."""
    import akshare as ak

    if target.kind == "index":
        try:
            raw = _call_with_retries(ak.stock_zh_index_daily, symbol=target.symbol)
        except Exception:
            raw = _call_with_retries(ak.stock_zh_index_daily_em, symbol=target.symbol)
        df = _normalize_index_frame(raw)
    elif target.kind == "etf":
        try:
            raw = _call_with_retries(ak.fund_etf_hist_sina, symbol=_sina_etf_symbol(target.symbol))
            if raw.empty:
                raise ValueError(f"empty Sina ETF history for {target.symbol}")
        except Exception:
            raw = _call_with_retries(
                ak.fund_etf_hist_em,
                symbol=target.symbol,
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="",
            )
        df = _normalize_etf_frame(raw)
    else:
        raise ValueError(f"unsupported target kind: {target.kind}")

    df = df.sort_values("date").reset_index(drop=True)
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
    if df.empty:
        raise ValueError(f"no data for {target.name} {target.symbol} from {start_date} to {end_date}")
    df["target_id"] = target.target_id
    df["target_name"] = target.name
    df["symbol"] = target.symbol
    return df.reset_index(drop=True)


def ensure_target_history(
    target: TrendTarget,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
    cache_dir: str | Path = TREND_HISTORY_DIR,
) -> pd.DataFrame:
    """Return target daily history from a local multi-year cache, fetching if needed."""
    start_date = start_date or default_history_start()
    end_date = end_date or default_history_end()
    path = trend_history_cache_path(target, cache_dir=cache_dir)
    if path.exists() and not force:
        cached = read_trend_history_cache(path)
        if _covers_dates(cached, start_date, end_date):
            return _slice_history_dates(cached, start_date, end_date)

    fetched = fetch_target_history(target, start_date=start_date, end_date=end_date)
    write_trend_history_cache(fetched, path)
    return fetched


def backfill_trend_history(
    targets: Iterable[TrendTarget] = DEFAULT_TARGETS,
    start_date: str | None = None,
    end_date: str | None = None,
    years: int = DEFAULT_HISTORY_YEARS,
    force: bool = False,
    cache_dir: str | Path = TREND_HISTORY_DIR,
) -> list[TrendHistoryCacheEntry]:
    """Fetch and persist several years of trend-proxy histories."""
    start_date = start_date or default_history_start(years)
    end_date = end_date or default_history_end()
    entries: list[TrendHistoryCacheEntry] = []
    for target in targets:
        path = trend_history_cache_path(target, cache_dir=cache_dir)
        try:
            existed = path.exists()
            df = ensure_target_history(
                target,
                start_date=start_date,
                end_date=end_date,
                force=force,
                cache_dir=cache_dir,
            )
            entries.append(_cache_entry(target, df, path, refreshed=force or not existed))
        except Exception as exc:
            entries.append(
                TrendHistoryCacheEntry(
                    target_id=target.target_id,
                    name=target.name,
                    symbol=target.symbol,
                    kind=target.kind,
                    rows=0,
                    start=start_date,
                    end=end_date,
                    path=str(path),
                    refreshed=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return entries


def trend_history_cache_summary(
    cache_dir: str | Path = TREND_HISTORY_DIR,
    targets: Iterable[TrendTarget] = KNOWN_TARGETS,
) -> list[TrendHistoryCacheEntry]:
    target_map = {target.target_id: target for target in targets}
    entries: list[TrendHistoryCacheEntry] = []
    for path in sorted(Path(cache_dir).glob("*.csv")):
        target_id = path.stem.split("__", 1)[0]
        target = target_map.get(target_id, TrendTarget(target_id, target_id, "unknown", ""))
        try:
            df = read_trend_history_cache(path)
            entries.append(_cache_entry(target, df, path, refreshed=False))
        except Exception as exc:
            entries.append(
                TrendHistoryCacheEntry(
                    target_id=target.target_id,
                    name=target.name,
                    symbol=target.symbol,
                    kind=target.kind,
                    rows=0,
                    start="",
                    end="",
                    path=str(path),
                    refreshed=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return entries


def trend_history_cache_markdown(entries: list[TrendHistoryCacheEntry]) -> str:
    lines = [
        "# A股趋势历史缓存",
        "",
        f"- 生成时间：{date.today().strftime('%Y-%m-%d')}",
        "- 用途：为市场环境、趋势校准、事件复盘提供近几年宽基和科技代理日线。",
        "",
    ]
    if not entries:
        lines.append("暂无趋势历史缓存。")
        return "\n".join(lines) + "\n"
    rows = [
        {
            "标的": entry.name,
            "代码": entry.symbol,
            "类型": entry.kind,
            "行数": entry.rows,
            "开始": entry.start,
            "结束": entry.end,
            "刷新": "是" if entry.refreshed else "否",
            "异常": entry.error,
        }
        for entry in entries
    ]
    lines.append(pd.DataFrame(rows).to_markdown(index=False))
    lines.append("")
    return "\n".join(lines)


def trend_history_cache_path(target: TrendTarget, cache_dir: str | Path = TREND_HISTORY_DIR) -> Path:
    safe_symbol = target.symbol.replace("/", "_").replace("\\", "_")
    return Path(cache_dir) / f"{target.target_id}__{safe_symbol}.csv"


def read_trend_history_cache(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def write_trend_history_cache(df: pd.DataFrame, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    saved = df.copy()
    saved["date"] = pd.to_datetime(saved["date"]).dt.strftime("%Y-%m-%d")
    saved.to_csv(out, index=False, encoding="utf-8-sig")
    return out


def evaluate_targets(
    targets: Iterable[TrendTarget] = DEFAULT_TARGETS,
    start_date: str | None = None,
    end_date: str | None = None,
    evaluate_date: str | None = None,
    lookback: int = 160,
    event: str | None = None,
    use_cache: bool = True,
    force: bool = False,
    use_param_cache: bool = True,
    force_params: bool = False,
) -> list[TrendResult]:
    """Evaluate/predict a group of targets with recent calibrated parameters.

    If evaluate_date is provided, the score is generated from the previous bar
    and then compared with evaluate_date's actual close-to-close return.
    Without evaluate_date, the score is generated from the latest available bar
    and actual_ret_pct/correct are left empty.
    """
    start_date = start_date or default_history_start()
    end_date = end_date or default_history_end()
    results: list[TrendResult] = []
    for target in targets:
        try:
            if use_cache:
                df = ensure_target_history(target, start_date=start_date, end_date=end_date, force=force)
            else:
                df = fetch_target_history(target, start_date=start_date, end_date=end_date)
            features = add_trend_features(df)
            result = evaluate_target_frame(
                target,
                features,
                evaluate_date=evaluate_date,
                lookback=lookback,
                event=event,
                use_param_cache=use_param_cache,
                force_params=force_params,
            )
            results.append(result)
        except Exception as exc:
            results.append(_error_result(target, end_date=end_date, evaluate_date=evaluate_date, exc=exc))
    return results


def evaluate_target_frame(
    target: TrendTarget,
    features: pd.DataFrame,
    evaluate_date: str | None = None,
    lookback: int = 160,
    event: str | None = None,
    use_param_cache: bool = False,
    force_params: bool = False,
) -> TrendResult:
    """Evaluate one already-normalized target frame."""
    if len(features) < 80:
        raise ValueError(f"not enough bars for {target.name}: {len(features)}")

    features = features.sort_values("date").reset_index(drop=True)
    if evaluate_date:
        eval_ts = pd.Timestamp(evaluate_date)
        matches = features.index[features["date"] == eval_ts].tolist()
        if not matches:
            raise ValueError(f"evaluate_date {evaluate_date} not found for {target.name}")
        eval_idx = matches[0]
        pred_idx = eval_idx - 1
        if pred_idx < 40:
            raise ValueError(f"not enough pre-evaluation history for {target.name} on {evaluate_date}")
    else:
        eval_idx = None
        pred_idx = len(features) - 1

    train_end = pred_idx - 1 if evaluate_date else pred_idx
    train_start = max(0, train_end - lookback + 1)
    train = features.iloc[train_start : train_end + 1].copy()
    params, train_accuracy = cached_calibrate_params(
        target,
        train,
        lookback=lookback,
        enabled=use_param_cache,
        force=force_params,
    )

    pred_row = features.iloc[pred_idx]
    raw_score = trend_score(pred_row, params)
    raw_score, event_adjustment = apply_event_overlay(raw_score, pred_row, target, event)
    signal = signal_from_score(raw_score, params.threshold)

    actual_ret_pct = None
    correct = None
    prediction_date = None
    if eval_idx is not None:
        actual_ret = float(features.iloc[eval_idx]["close"] / pred_row["close"] - 1.0)
        actual_ret_pct = round(actual_ret * 100, 2)
        correct = direction_correct(signal, actual_ret)
        prediction_date = pd.Timestamp(features.iloc[eval_idx]["date"]).strftime("%Y-%m-%d")

    diagnostics = {
        "pred_base_date": pd.Timestamp(pred_row["date"]).strftime("%Y-%m-%d"),
        "close": _round(pred_row["close"]),
        "ret_1_pct": _pct(pred_row["ret_1"]),
        "ret_5_pct": _pct(pred_row["ret_5"]),
        "ret_20_pct": _pct(pred_row["ret_20"]),
        "volume_ratio_5_20": _round(pred_row["volume_ratio_5_20"]),
        "volatility_20_pct": _pct(pred_row["volatility_20"]),
        "upper_shadow_pct": _pct(pred_row["upper_shadow"]),
        "exhaustion_risk": _round(_exhaustion_risk(pred_row)),
        "close_location": _round(pred_row["close_location"]),
        "intraday_return_pct": _pct(pred_row["intraday_return"]),
    }
    return TrendResult(
        target_id=target.target_id,
        name=target.name,
        symbol=target.symbol,
        latest_date=pd.Timestamp(features.iloc[-1]["date"]).strftime("%Y-%m-%d"),
        prediction_date=prediction_date,
        score=round(raw_score, 4),
        signal=signal,
        actual_ret_pct=actual_ret_pct,
        correct=correct,
        train_accuracy=round(train_accuracy, 4),
        params=asdict(params),
        diagnostics=diagnostics,
        event_adjustment=event_adjustment,
    )


def add_trend_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add trailing-only features used by the calibrator."""
    data = df.sort_values("date").copy()
    for col in ("open", "high", "low", "close", "volume", "amount"):
        data[col] = pd.to_numeric(data[col], errors="coerce")

    data["ret_1"] = data["close"].pct_change(1)
    data["ret_3"] = data["close"].pct_change(3)
    data["ret_5"] = data["close"].pct_change(5)
    data["ret_20"] = data["close"].pct_change(20)

    for window in (5, 10, 20, 60):
        data[f"ma{window}"] = data["close"].rolling(window).mean()
    data["vol_ma5"] = data["volume"].rolling(5).mean()
    data["vol_ma20"] = data["volume"].rolling(20).mean()
    data["volume_ratio_5_20"] = data["vol_ma5"] / data["vol_ma20"]
    data["volatility_20"] = data["ret_1"].rolling(20).std()

    day_range = (data["high"] - data["low"]).replace(0, np.nan)
    data["close_location"] = ((data["close"] - data["low"]) / day_range).clip(0, 1)
    data["intraday_return"] = (data["close"] / data["open"] - 1.0).replace([np.inf, -np.inf], np.nan)
    data["upper_shadow"] = ((data["high"] - data[["open", "close"]].max(axis=1)) / data["close"]).clip(lower=0)
    return data.dropna().reset_index(drop=True)


def calibrate_params(train: pd.DataFrame) -> tuple[TrendParams, float]:
    """Grid-search the score parameters on recent one-day direction accuracy."""
    if train.empty:
        return TrendParams(), 0.0

    best_params = TrendParams()
    best_accuracy = -1.0
    for params in PARAM_GRID:
        accuracy = score_accuracy(train, params)
        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_params = params
    return best_params, max(0.0, best_accuracy)


def cached_calibrate_params(
    target: TrendTarget,
    train: pd.DataFrame,
    lookback: int,
    enabled: bool = True,
    force: bool = False,
    cache_dir: str | Path = TREND_PARAM_DIR,
) -> tuple[TrendParams, float]:
    """Cache the expensive parameter grid search for one target/date/lookback."""
    if not enabled:
        return calibrate_params(train)
    path = trend_param_cache_path(target, train, lookback=lookback, cache_dir=cache_dir)
    if path.exists() and not force:
        cached = load_trend_param_cache(path)
        if cached is not None:
            return cached
    params, accuracy = calibrate_params(train)
    save_trend_param_cache(path, target, train, lookback, params, accuracy)
    return params, accuracy


def trend_param_cache_path(
    target: TrendTarget,
    train: pd.DataFrame,
    lookback: int,
    cache_dir: str | Path = TREND_PARAM_DIR,
) -> Path:
    end_date = pd.to_datetime(train["date"].iloc[-1]).strftime("%Y%m%d")
    return Path(cache_dir) / f"{target.target_id}__{end_date}__lb{lookback}.json"


def load_trend_param_cache(path: str | Path) -> tuple[TrendParams, float] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return TrendParams(**payload["params"]), float(payload["train_accuracy"])
    except Exception:
        return None


def save_trend_param_cache(
    path: str | Path,
    target: TrendTarget,
    train: pd.DataFrame,
    lookback: int,
    params: TrendParams,
    train_accuracy: float,
) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "target_id": target.target_id,
        "symbol": target.symbol,
        "lookback": lookback,
        "train_start": pd.to_datetime(train["date"].iloc[0]).strftime("%Y-%m-%d"),
        "train_end": pd.to_datetime(train["date"].iloc[-1]).strftime("%Y-%m-%d"),
        "rows": len(train),
        "params": asdict(params),
        "train_accuracy": train_accuracy,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def score_accuracy(frame: pd.DataFrame, params: TrendParams) -> float:
    """Accuracy of today's signal versus next day's close direction."""
    if len(frame) < 20:
        return 0.0
    scored = frame.copy()
    scored["next_ret"] = scored["close"].pct_change().shift(-1)
    scored = scored.dropna(subset=["next_ret"])
    if scored.empty:
        return 0.0
    predictions = scored.apply(lambda row: signal_from_score(trend_score(row, params), params.threshold), axis=1)
    checks = [direction_correct(signal, actual) for signal, actual in zip(predictions, scored["next_ret"])]
    return float(np.mean(checks)) if checks else 0.0


def trend_score(row: pd.Series, params: TrendParams) -> float:
    """Directional score: positive is bullish, negative is defensive."""
    momentum = (
        _safe(row.get("ret_3")) * 0.25
        + _safe(row.get("ret_5")) * 0.35
        + _safe(row.get("ret_20")) * 0.40
    ) * 10.0
    trend = _trend_alignment(row) - 0.5
    reversal = (_safe(row.get("close_location")) - 0.5) + _safe(row.get("intraday_return")) * 5.0
    volume = np.log(max(_safe(row.get("volume_ratio_5_20"), 1.0), 0.2))
    risk = _safe(row.get("volatility_20")) * 10.0 + _safe(row.get("upper_shadow")) * 8.0 + _exhaustion_risk(row)
    raw = (
        params.momentum_weight * momentum
        + params.trend_weight * trend
        + params.reversal_weight * reversal
        + params.volume_weight * volume
        - params.risk_weight * risk
    ) * params.polarity
    return _apply_a_share_regime_guard(raw, row, params.threshold)


def _exhaustion_risk(row: pd.Series) -> float:
    """Extra penalty for A-share blow-off moves that often reverse the next day."""
    ma20 = _safe(row.get("ma20"), np.nan)
    close = _safe(row.get("close"), np.nan)
    close_ma20_extension = 0.0
    if pd.notna(ma20) and ma20 > 0 and pd.notna(close):
        close_ma20_extension = max(close / ma20 - 1.0, 0.0)

    return (
        max(_safe(row.get("ret_1")) - 0.025, 0.0) * 14.0
        + max(_safe(row.get("ret_5")) - 0.04, 0.0) * 12.0
        + max(_safe(row.get("ret_20")) - 0.18, 0.0) * 10.0
        + max(close_ma20_extension - 0.12, 0.0) * 8.0
        + max(_safe(row.get("volume_ratio_5_20"), 1.0) - 1.25, 0.0) * 0.4
    )


def _apply_a_share_regime_guard(score: float, row: pd.Series, threshold: float) -> float:
    """Defensive overrides for short-term A-share overheating and rollover."""
    ret_1 = _safe(row.get("ret_1"))
    ret_5 = _safe(row.get("ret_5"))
    ret_20 = _safe(row.get("ret_20"))
    upper_shadow = _safe(row.get("upper_shadow"))
    volatility_20 = _safe(row.get("volatility_20"))
    exhaustion = _exhaustion_risk(row)

    if ret_20 >= 0.12 and exhaustion >= 0.9 and (ret_1 >= 0.02 or upper_shadow >= 0.006 or volatility_20 >= 0.02):
        return min(score, -max(threshold * 1.5, 0.06))
    if ret_20 >= 0.10 and upper_shadow >= 0.006 and volatility_20 >= 0.02:
        return min(score, -max(threshold * 1.5, 0.06))
    if ret_20 >= 0.05 and ret_5 < 0 and upper_shadow >= 0.005 and volatility_20 >= 0.015:
        return min(score, -max(threshold * 1.5, 0.06))
    if ret_1 < 0 and ret_5 < 0 and score < threshold:
        return min(score, -max(threshold * 1.2, 0.04))
    return score


def apply_event_overlay(
    score: float,
    row: pd.Series,
    target: TrendTarget,
    event: str | None,
) -> tuple[float, dict[str, float | str] | None]:
    """Adjust score for explicit event catalysts such as NVL72 BOM repricing."""
    if not event:
        return score, None
    profile = EVENT_PROFILES.get(event)
    if profile is None:
        raise ValueError(f"unknown event profile: {event}")

    tag_boosts = profile["tag_boosts"]
    matched = [tag for tag in target.tags if tag in tag_boosts]
    gross_boost = sum(float(tag_boosts[tag]) for tag in matched)
    if gross_boost == 0:
        return score, {
            "event": str(profile["name"]),
            "matched_tags": "",
            "gross_boost": 0.0,
            "penalty": 0.0,
            "net_boost": 0.0,
        }

    overheat = max(_safe(row.get("ret_1")) - 0.06, 0.0) * float(profile["overheat_penalty"]) * 10.0
    upper_shadow_penalty = max(_safe(row.get("upper_shadow")) - 0.01, 0.0) * 2.0
    penalty = overheat + upper_shadow_penalty
    net_boost = max(gross_boost - penalty, 0.0)
    adjusted = score + net_boost
    return adjusted, {
        "event": str(profile["name"]),
        "matched_tags": ",".join(matched),
        "gross_boost": round(gross_boost, 4),
        "penalty": round(penalty, 4),
        "net_boost": round(net_boost, 4),
    }


def signal_from_score(score: float, threshold: float) -> str:
    if score > threshold:
        return "看多"
    if score < -threshold:
        return "看空"
    return "震荡"


def direction_correct(signal: str, actual_ret: float, neutral_band: float = 0.0025) -> bool:
    if abs(actual_ret) <= neutral_band:
        return signal == "震荡"
    if actual_ret > 0:
        return signal == "看多"
    return signal == "看空"


def save_market_trend_review(results: list[TrendResult], output_dir: str | Path = REPORT_DIR) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    latest = max(result.latest_date for result in results)
    stem = f"market_trend_validation_{latest.replace('-', '')}"
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    json_path.write_text(
        json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(market_trend_markdown(results), encoding="utf-8")
    return json_path, md_path


def market_trend_markdown(results: list[TrendResult]) -> str:
    if not results:
        return "# A股近期趋势核验\n\n无结果。\n"

    total = len([r for r in results if r.correct is not None])
    hits = len([r for r in results if r.correct])
    hit_rate = hits / total if total else None
    latest = max(result.latest_date for result in results)
    rows = []
    for result in results:
        rows.append(
            {
                "标的": result.name,
                "代码": result.symbol,
                "基准日": result.diagnostics.get("pred_base_date", ""),
                "预测日": result.prediction_date or "下一交易日",
                "信号": result.signal,
                "分数": result.score,
                "实际涨跌%": result.actual_ret_pct,
                "命中": "" if result.correct is None else ("是" if result.correct else "否"),
                "训练命中率": f"{result.train_accuracy:.1%}",
                "事件调整": _format_event_adjustment(result.event_adjustment),
                "异常": result.error or "",
            }
        )
    table = pd.DataFrame(rows).to_markdown(index=False)
    summary = "暂无可核验实际涨跌。" if hit_rate is None else f"可核验 {total} 个标的，命中 {hits} 个，命中率 {hit_rate:.1%}。"
    return "\n".join(
        [
            "# A股近期趋势核验",
            "",
            f"- 最新数据日期：{latest}",
            f"- 核验摘要：{summary}",
            "- 数据源：AkShare / 新浪与东方财富指数、ETF 日线接口。",
            "- 说明：这是趋势校准与复盘工具，不是买卖建议。",
            "",
            table,
            "",
            "## 参数",
            "",
            "每个标的用最近窗口网格搜索得到参数，目标是下一交易日方向命中率；预测只使用预测基准日及之前的数据。",
            "可选事件覆盖层会按标的产业标签调分，并对单日过热和上影线做扣分。",
            "",
        ]
    )


def select_targets(ids_or_symbols: str) -> list[TrendTarget]:
    if not ids_or_symbols:
        return list(DEFAULT_TARGETS)
    wanted = {item.strip().lower() for item in ids_or_symbols.split(",") if item.strip()}
    selected = [
        target
        for target in KNOWN_TARGETS
        if target.target_id.lower() in wanted or target.symbol.lower() in wanted
    ]
    if not selected:
        raise ValueError(f"no known targets selected from: {ids_or_symbols}")
    return selected


def _covers_dates(df: pd.DataFrame, start_date: str, end_date: str) -> bool:
    if df.empty:
        return False
    available_start = pd.to_datetime(df["date"]).min().strftime("%Y-%m-%d")
    available_end = pd.to_datetime(df["date"]).max().strftime("%Y-%m-%d")
    return available_start <= start_date and available_end >= end_date


def _slice_history_dates(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    return df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)


def _cache_entry(target: TrendTarget, df: pd.DataFrame, path: Path, refreshed: bool) -> TrendHistoryCacheEntry:
    if df.empty:
        start = ""
        end = ""
    else:
        start = pd.to_datetime(df["date"]).min().strftime("%Y-%m-%d")
        end = pd.to_datetime(df["date"]).max().strftime("%Y-%m-%d")
    return TrendHistoryCacheEntry(
        target_id=target.target_id,
        name=target.name,
        symbol=target.symbol,
        kind=target.kind,
        rows=len(df),
        start=start,
        end=end,
        path=str(path),
        refreshed=refreshed,
        error="",
    )


def _format_event_adjustment(event_adjustment: dict[str, float | str] | None) -> str:
    if not event_adjustment:
        return ""
    net = event_adjustment.get("net_boost", 0.0)
    tags = event_adjustment.get("matched_tags", "")
    return f"{event_adjustment.get('event', '')}:{tags}:{net}"


def _normalize_index_frame(raw: pd.DataFrame) -> pd.DataFrame:
    renamed = raw.rename(columns={col: str(col).lower() for col in raw.columns}).copy()
    if "amount" not in renamed.columns:
        renamed["amount"] = renamed.get("volume", 0)
    return renamed[["date", "open", "close", "high", "low", "volume", "amount"]].assign(
        date=lambda df: pd.to_datetime(df["date"])
    )


def _normalize_etf_frame(raw: pd.DataFrame) -> pd.DataFrame:
    lower_cols = {col: str(col).lower() for col in raw.columns}
    if {"date", "open", "high", "low", "close", "volume", "amount"}.issubset(set(lower_cols.values())):
        renamed = raw.rename(columns=lower_cols)
        return renamed[["date", "open", "close", "high", "low", "volume", "amount"]].assign(
            date=lambda df: pd.to_datetime(df["date"])
        )

    if {"日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"}.issubset(raw.columns):
        renamed = raw.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "成交额": "amount",
            }
        )
        return renamed[["date", "open", "close", "high", "low", "volume", "amount"]].assign(
            date=lambda df: pd.to_datetime(df["date"])
        )

    cols = list(raw.columns)
    if len(cols) < 7:
        raise ValueError(f"ETF history has unexpected columns: {cols}")
    renamed = raw.rename(
        columns={
            cols[0]: "date",
            cols[1]: "open",
            cols[2]: "close",
            cols[3]: "high",
            cols[4]: "low",
            cols[5]: "volume",
            cols[6]: "amount",
        }
    )
    return renamed[["date", "open", "close", "high", "low", "volume", "amount"]].assign(
        date=lambda df: pd.to_datetime(df["date"])
    )


def _sina_etf_symbol(symbol: str) -> str:
    return f"sh{symbol}" if symbol.startswith(("5", "6")) else f"sz{symbol}"


def _call_with_retries(func, retries: int = 1, timeout: int = 12, **kwargs) -> pd.DataFrame:
    import requests

    last_exc: Exception | None = None
    original_request = requests.sessions.Session.request

    def request_with_timeout(session, method, url, **request_kwargs):
        request_kwargs.setdefault("timeout", timeout)
        return original_request(session, method, url, **request_kwargs)

    for attempt in range(retries + 1):
        try:
            requests.sessions.Session.request = request_with_timeout
            return func(**kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
        finally:
            requests.sessions.Session.request = original_request
    assert last_exc is not None
    raise last_exc


def _error_result(target: TrendTarget, end_date: str, evaluate_date: str | None, exc: Exception) -> TrendResult:
    return TrendResult(
        target_id=target.target_id,
        name=target.name,
        symbol=target.symbol,
        latest_date=end_date,
        prediction_date=evaluate_date,
        score=0.0,
        signal="数据失败",
        actual_ret_pct=None,
        correct=None,
        train_accuracy=0.0,
        params={},
        diagnostics={"error": f"{type(exc).__name__}: {exc}"},
        error=f"{type(exc).__name__}: {exc}",
    )


def _trend_alignment(row: pd.Series) -> float:
    checks = [
        row.get("close") > row.get("ma5"),
        row.get("close") > row.get("ma10"),
        row.get("close") > row.get("ma20"),
        row.get("ma5") > row.get("ma10"),
        row.get("ma10") > row.get("ma20"),
        row.get("ma20") > row.get("ma60"),
    ]
    valid = [bool(check) for check in checks if pd.notna(check)]
    return sum(valid) / len(valid) if valid else 0.5


def _safe(value, default: float = 0.0) -> float:
    if value is None or pd.isna(value):
        return default
    return float(value)


def _round(value, digits: int = 4) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def _pct(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value) * 100, 2)
