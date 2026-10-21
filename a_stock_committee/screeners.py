"""Decision-support screeners for A-share stocks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from .market_data import ensure_daily_data, normalize_symbol
from .radar_config import RadarConfig


REPORT_DIR = Path("decision_reports")


@dataclass
class StockDecision:
    vt_symbol: str
    qlib_code: str
    latest_date: str
    close: float
    score: float
    label: str
    strategy_tags: list[str]
    risk_flags: list[str]
    highlights: list[str]
    metrics: dict[str, float | str | None]


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add common trend, momentum, valuation, and liquidity indicators."""
    data = df.sort_values("date").copy()
    for window in [20, 50, 120, 200]:
        data[f"ma{window}"] = data["close"].rolling(window).mean()
        data[f"amount_ma{window}"] = data["amount"].rolling(window).mean()

    for window in [20, 60, 120]:
        data[f"ret_{window}"] = data["close"].pct_change(window)

    data["high_120"] = data["high"].rolling(120).max()
    data["low_120"] = data["low"].rolling(120).min()
    data["close_to_high_120"] = data["close"] / data["high_120"]
    data["range_15"] = (
        data["high"].rolling(15).max() - data["low"].rolling(15).min()
    ) / data["close"]
    return data


def analyze_stock(
    df: pd.DataFrame,
    config: RadarConfig | None = None,
    external_risk_flags: list[str] | None = None,
) -> StockDecision:
    """Analyze one stock and produce a decision-support object."""
    data = add_indicators(df)
    latest = data.iloc[-1]
    vt_symbol = str(latest["vt_symbol"])
    qlib_code = str(latest["qlib_code"])

    trend_score = _trend_score(latest)
    momentum_score = _momentum_score(latest)
    valuation_score = _valuation_score(latest)
    liquidity_score = _liquidity_score(latest)
    risk_flags = _risk_flags(latest, config)
    if external_risk_flags:
        risk_flags.extend(external_risk_flags)
    risk_penalty = min(35, len([x for x in risk_flags if x.startswith("BLOCK")]) * 25 + len(risk_flags) * 4)

    score = (
        trend_score * 0.35
        + momentum_score * 0.25
        + valuation_score * 0.20
        + liquidity_score * 0.20
        - risk_penalty
    )
    score = round(max(0, min(100, score)), 1)
    label = _label(score, risk_flags)
    tags = _strategy_tags(latest, trend_score, momentum_score, valuation_score, liquidity_score, risk_flags)
    highlights = _highlights(latest, trend_score, momentum_score, valuation_score, liquidity_score)

    metrics = {
        "trend_score": round(trend_score, 1),
        "momentum_score": round(momentum_score, 1),
        "valuation_score": round(valuation_score, 1),
        "liquidity_score": round(liquidity_score, 1),
        "close": _safe_round(latest.get("close")),
        "ma20": _safe_round(latest.get("ma20")),
        "ma50": _safe_round(latest.get("ma50")),
        "ma120": _safe_round(latest.get("ma120")),
        "ma200": _safe_round(latest.get("ma200")),
        "ret_20_pct": _safe_pct(latest.get("ret_20")),
        "ret_60_pct": _safe_pct(latest.get("ret_60")),
        "ret_120_pct": _safe_pct(latest.get("ret_120")),
        "close_to_high_120_pct": _safe_pct(latest.get("close_to_high_120")),
        "range_15_pct": _safe_pct(latest.get("range_15")),
        "amount_ma20_yuan": _safe_round(latest.get("amount_ma20"), 0),
        "turn": _safe_round(latest.get("turn")),
        "pe_ttm": _safe_round(latest.get("peTTM")),
        "pb_mrq": _safe_round(latest.get("pbMRQ")),
        "is_st": int(latest.get("isST")) if pd.notna(latest.get("isST")) else None,
        "trade_status": int(latest.get("tradestatus")) if pd.notna(latest.get("tradestatus")) else None,
        "security_type": str(latest.get("security_type", "stock")),
    }
    if config is not None:
        metrics.update(_account_metrics(latest, config))

    return StockDecision(
        vt_symbol=vt_symbol,
        qlib_code=qlib_code,
        latest_date=pd.Timestamp(latest["date"]).strftime("%Y-%m-%d"),
        close=float(latest["close"]),
        score=score,
        label=label,
        strategy_tags=tags,
        risk_flags=risk_flags,
        highlights=highlights,
        metrics=metrics,
    )


def screen_symbols(
    symbols: Iterable[str],
    start_date: str | None = None,
    end_date: str | None = None,
    strategy: str = "all",
    top: int = 30,
    force: bool = False,
    config: RadarConfig | None = None,
    external_risk_flags: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    """Fetch/analyze multiple symbols and return ranked candidates."""
    rows = []
    for symbol in symbols:
        df = ensure_daily_data(symbol, start_date=start_date, end_date=end_date, force=force)
        decision = analyze_stock(
            df,
            config=config,
            external_risk_flags=_external_flags_for_symbol(df, symbol, external_risk_flags),
        )
        if strategy != "all" and strategy not in decision.strategy_tags:
            continue
        row = asdict(decision)
        row.update(decision.metrics)
        row["strategy_tags"] = ",".join(decision.strategy_tags)
        row["risk_flags"] = " | ".join(decision.risk_flags)
        row["highlights"] = " | ".join(decision.highlights)
        rows.append(row)

    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).sort_values(["score", "latest_date"], ascending=[False, False])
    return result.head(top).reset_index(drop=True)


def generate_stock_report(decision: StockDecision) -> str:
    """Generate a Markdown decision-support report."""
    lines = [
        f"# A股决策支持报告：{decision.vt_symbol}",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 数据日期：{decision.latest_date}",
        f"- 收盘价：{decision.close:.2f}",
        f"- 决策分：**{decision.score:.1f}/100**",
        f"- 标签：{decision.label}",
        f"- 策略命中：{', '.join(decision.strategy_tags) if decision.strategy_tags else '无'}",
        "",
        "> 这是决策支持报告，不是买卖指令。真实交易前还要查公告、财报、停复牌、涨跌停、账户和流动性。",
        "",
        "## 关键亮点",
        "",
    ]
    lines.extend([f"- {item}" for item in decision.highlights] or ["- 暂无明显亮点。"])

    lines.extend(["", "## 风险红灯", ""])
    lines.extend([f"- {item}" for item in decision.risk_flags] or ["- 暂未触发基础红灯。"])

    lines.extend(["", "## 指标快照", ""])
    for key, value in decision.metrics.items():
        lines.append(f"- {key}: {value}")

    lines.extend(
        [
            "",
            "## 投委会追问",
            "",
            "- 这只股票的主动优势是什么，为什么不买指数？",
            "- 最近一份财报是否支持当前趋势和估值？",
            "- 如果买入，哪一个价格或事实出现就说明判断错了？",
            "- 计划仓位是否能在止损触发时把单笔亏损控制在预算内？",
            "- 当前是否存在公告、解禁、减持、问询函、业绩预告等事件风险？",
            "",
        ]
    )
    return "\n".join(lines)


def save_stock_report(decision: StockDecision, output_dir: str | Path = REPORT_DIR) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{decision.latest_date}_{decision.vt_symbol.replace('.', '_')}_decision.md"
    path.write_text(generate_stock_report(decision), encoding="utf-8")
    return path


def save_screen_result(df: pd.DataFrame, output_dir: str | Path = REPORT_DIR) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"screen_{stamp}.csv"
    md_path = out / f"screen_{stamp}.md"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(_screen_markdown(df), encoding="utf-8")
    return csv_path, md_path


def _screen_markdown(df: pd.DataFrame) -> str:
    if df.empty:
        return "# A股筛选结果\n\n无候选。\n"
    cols = ["vt_symbol", "latest_date", "close", "score", "label", "strategy_tags", "risk_flags"]
    table = df[cols].to_markdown(index=False)
    return f"# A股筛选结果\n\n{table}\n"


def stock_report_for_symbol(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
    config: RadarConfig | None = None,
    external_risk_flags: dict[str, list[str]] | None = None,
) -> tuple[StockDecision, Path]:
    normalize_symbol(symbol)
    df = ensure_daily_data(symbol, start_date=start_date, end_date=end_date, force=force)
    decision = analyze_stock(
        df,
        config=config,
        external_risk_flags=_external_flags_for_symbol(df, symbol, external_risk_flags),
    )
    return decision, save_stock_report(decision)


def _external_flags_for_symbol(
    df: pd.DataFrame,
    raw_symbol: str,
    external_risk_flags: dict[str, list[str]] | None,
) -> list[str]:
    if not external_risk_flags:
        return []
    keys = {normalize_symbol(raw_symbol).code}
    if not df.empty:
        latest = df.iloc[-1]
        vt_symbol = str(latest.get("vt_symbol", ""))
        qlib_code = str(latest.get("qlib_code", ""))
        if vt_symbol:
            keys.add(vt_symbol)
            keys.add(vt_symbol.split(".")[0])
        if qlib_code:
            keys.add(qlib_code)
            keys.add(qlib_code[-6:])
    flags: list[str] = []
    for key in keys:
        flags.extend(external_risk_flags.get(key, []))
    return list(dict.fromkeys(flags))


def _trend_score(row: pd.Series) -> float:
    checks = [
        (row["close"] > row.get("ma20"), 15),
        (row["close"] > row.get("ma50"), 20),
        (row["close"] > row.get("ma120"), 20),
        (row["close"] > row.get("ma200"), 20),
        (row.get("ma50") > row.get("ma120"), 15),
        (row.get("ma120") > row.get("ma200"), 10),
    ]
    return _score_checks(checks)


def _momentum_score(row: pd.Series) -> float:
    close_to_high = row.get("close_to_high_120")
    checks = [
        (row.get("ret_20") > 0, 20),
        (row.get("ret_60") > 0.05, 25),
        (row.get("ret_120") > 0.10, 25),
        (close_to_high is not None and close_to_high > 0.90, 20),
        (row.get("range_15") is not None and row.get("range_15") < 0.15, 10),
    ]
    return _score_checks(checks)


def _valuation_score(row: pd.Series) -> float:
    pe = row.get("peTTM")
    pb = row.get("pbMRQ")
    score = 0.0
    if pd.notna(pe) and pe > 0:
        if pe <= 15:
            score += 45
        elif pe <= 30:
            score += 30
        elif pe <= 60:
            score += 15
    if pd.notna(pb) and pb > 0:
        if pb <= 1.5:
            score += 35
        elif pb <= 3:
            score += 25
        elif pb <= 6:
            score += 10
    if pd.notna(pe) and pe > 0 and pd.notna(pb) and pb > 0:
        score += 20
    return min(100, score)


def _liquidity_score(row: pd.Series) -> float:
    amount = row.get("amount_ma20")
    if pd.isna(amount):
        return 0
    if amount >= 1_000_000_000:
        return 100
    if amount >= 300_000_000:
        return 80
    if amount >= 100_000_000:
        return 60
    if amount >= 50_000_000:
        return 35
    return 10


def _risk_flags(row: pd.Series, config: RadarConfig | None = None) -> list[str]:
    flags = []
    security_type = str(row.get("security_type", "stock"))
    is_stock = security_type == "stock"

    if is_stock and row.get("isST") == 1:
        flags.append("BLOCK: ST/风险警示股票")
    if row.get("tradestatus") == 0:
        flags.append("BLOCK: 当前停牌或不可交易")
    if pd.notna(row.get("amount_ma20")) and row.get("amount_ma20") < 50_000_000:
        flags.append("WARN: 20日均成交额低于5000万，流动性偏弱")
    if pd.notna(row.get("ma200")) and row.get("close") < row.get("ma200"):
        flags.append("WARN: 收盘价低于200日均线，长期趋势偏弱")
    if is_stock and pd.notna(row.get("peTTM")) and row.get("peTTM") <= 0:
        flags.append("WARN: PE为负或无效，可能亏损或估值不可比")

    if config is not None:
        vt_symbol = str(row.get("vt_symbol"))
        close = float(row.get("close")) if pd.notna(row.get("close")) else 0
        amount_ma20 = row.get("amount_ma20")
        lot_cost = close * config.min_trade_lot
        max_single_value = config.account_equity * config.max_single_position_pct / 100

        if is_stock and config.exclude_st and row.get("isST") == 1:
            flags.append("BLOCK: 小资金配置过滤ST")
        if config.exclude_suspended and row.get("tradestatus") == 0:
            flags.append("BLOCK: 小资金配置过滤停牌")
        if config.exclude_bj and vt_symbol.endswith(".BJ"):
            flags.append("BLOCK: 小资金配置过滤北交所")
        if config.exclude_star_if_no_permission and not config.has_star_permission and vt_symbol.startswith("688"):
            flags.append("BLOCK: 未开通科创板权限")
        if config.exclude_chinext_if_no_permission and not config.has_chinext_permission and vt_symbol.startswith("300"):
            flags.append("BLOCK: 未开通创业板权限")
        if close > config.max_price:
            flags.append(f"BLOCK: 股价{close:.2f}超过小资金价格上限{config.max_price:.2f}")
        if close < config.min_price:
            flags.append(f"WARN: 股价{close:.2f}低于价格下限{config.min_price:.2f}，注意低价股风险")
        if lot_cost > max_single_value:
            flags.append(
                f"BLOCK: 100股约{lot_cost:.0f}元，超过单票预算{max_single_value:.0f}元"
            )
        if pd.notna(amount_ma20) and amount_ma20 < config.min_20d_amount:
            flags.append(
                f"BLOCK: 20日均成交额低于{config.min_20d_amount / 100000000:.1f}亿元"
            )
    return flags


def _account_metrics(row: pd.Series, config: RadarConfig) -> dict[str, float | str | None]:
    close = row.get("close")
    if pd.isna(close):
        return {}
    lot_cost = float(close) * config.min_trade_lot
    max_single_value = config.account_equity * config.max_single_position_pct / 100
    max_lots = int(max_single_value // lot_cost) if lot_cost > 0 else 0
    max_shares = max_lots * config.min_trade_lot
    return {
        "lot_cost": round(lot_cost, 2),
        "small_account_max_single_value": round(max_single_value, 2),
        "small_account_max_lots": max_lots,
        "small_account_max_shares": max_shares,
        "small_account_profile": config.profile_name,
    }


def _strategy_tags(
    row: pd.Series,
    trend_score: float,
    momentum_score: float,
    valuation_score: float,
    liquidity_score: float,
    risk_flags: list[str],
) -> list[str]:
    if any(flag.startswith("BLOCK") for flag in risk_flags):
        return []
    tags = []
    close_to_high = row.get("close_to_high_120")
    range_15 = row.get("range_15")
    if trend_score >= 70 and momentum_score >= 60 and liquidity_score >= 60:
        tags.append("canslim-lite")
    if trend_score >= 70 and pd.notna(close_to_high) and close_to_high >= 0.88 and pd.notna(range_15) and range_15 <= 0.16:
        tags.append("vcp-lite")
    if valuation_score >= 70 and liquidity_score >= 35:
        tags.append("value-lite")
    if trend_score >= 70 and liquidity_score >= 60:
        tags.append("trend-watch")
    return tags


def _highlights(
    row: pd.Series,
    trend_score: float,
    momentum_score: float,
    valuation_score: float,
    liquidity_score: float,
) -> list[str]:
    items = [
        f"趋势分 {trend_score:.1f}，动量分 {momentum_score:.1f}，估值分 {valuation_score:.1f}，流动性分 {liquidity_score:.1f}。",
    ]
    if pd.notna(row.get("ret_60")):
        items.append(f"60日涨跌幅约 {row.get('ret_60') * 100:.2f}%。")
    if pd.notna(row.get("close_to_high_120")):
        items.append(f"当前价格约为120日高点的 {row.get('close_to_high_120') * 100:.2f}%。")
    if pd.notna(row.get("amount_ma20")):
        items.append(f"20日均成交额约 {row.get('amount_ma20') / 100_000_000:.2f} 亿元。")
    if pd.notna(row.get("peTTM")) and pd.notna(row.get("pbMRQ")):
        items.append(f"PE(TTM) {row.get('peTTM'):.2f}，PB(MRQ) {row.get('pbMRQ'):.2f}。")
    return items


def _score_checks(checks: list[tuple[bool, float]]) -> float:
    score = 0.0
    for passed, points in checks:
        try:
            if bool(passed):
                score += points
        except Exception:
            continue
    return min(100, score)


def _label(score: float, risk_flags: list[str]) -> str:
    if any(flag.startswith("BLOCK") for flag in risk_flags):
        return "红灯：不进入买入流程"
    if score >= 75:
        return "重点观察：可进入投委会深审"
    if score >= 60:
        return "观察：需要补基本面和卖出规则"
    if score >= 45:
        return "谨慎观察：证据不足或结构一般"
    return "暂不考虑：等待更好证据"


def _safe_round(value, digits: int = 2):
    if pd.isna(value):
        return None
    return round(float(value), digits)


def _safe_pct(value):
    if pd.isna(value):
        return None
    return round(float(value) * 100, 2)
