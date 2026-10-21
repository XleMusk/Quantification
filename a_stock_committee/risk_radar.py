"""A-share blowup risk radar.

This module is the defensive counterpart to the catalyst radar: it scans
announcements/news/research text for hard negative events before a position is
allowed into the trading workflow.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from .catalyst_monitor import NewsItem, fetch_realtime_news, freshness_score, source_quality_score


REPORT_DIR = Path("committee_reviews")


@dataclass
class RiskSignal:
    title: str
    published_at: str
    source: str
    url: str
    stock_name: str
    symbol: str
    score: float
    severity: str
    categories: list[str]
    matched_keywords: list[str]
    risk_type: str
    event_score: float
    baseline_score: float
    action: str
    summary: str
    source_score: float = 0.0
    freshness_score: float = 0.0


CRITICAL_KEYWORDS = {
    "退市风险警示",
    "*ST",
    "被实施退市风险警示",
    "无法表示意见",
    "否定意见",
    "证监会立案",
    "立案调查",
    "立案告知书",
    "涉嫌信息披露违法违规",
    "涉嫌信息披露误导性陈述",
    "财务造假",
    "资金占用",
    "违规担保",
    "破产清算",
    "重整",
}


BASELINE_STATUS_KEYWORDS = {"*ST", " ST", "ST"}


BENIGN_PHRASE_REPLACEMENTS = {
    "非经营性资金占用及其他关联方资金往来情况汇总表": "关联方往来情况汇总表",
    "控股股东及其他关联方资金占用情况的专项说明": "关联方往来情况专项说明",
    "非经营性资金占用及其他关联资金往来情况": "关联方资金往来情况",
    "资金占用及其他关联方资金往来": "关联方资金往来",
    "解除质押": "解除担保物",
    "部分股权解除质押": "部分股权担保物解除",
    "股权解除质押": "股权担保物解除",
    "解除冻结": "限制解除",
    "解除司法冻结": "司法限制解除",
    "停复牌": "交易安排",
    "转股停复牌": "转股交易安排",
}


RISK_KEYWORD_WEIGHTS = {
    "退市风险警示": 6.0,
    "其他风险警示": 4.0,
    "被实施退市风险警示": 6.0,
    "*ST": 5.0,
    " ST": 3.0,
    "披星戴帽": 4.5,
    "无法表示意见": 6.0,
    "否定意见": 6.0,
    "保留意见": 3.5,
    "内控审计报告": 2.0,
    "内部控制": 1.4,
    "证监会立案": 6.0,
    "立案调查": 6.0,
    "立案告知书": 5.5,
    "涉嫌信息披露违法违规": 6.0,
    "涉嫌信息披露误导性陈述": 6.0,
    "信息披露误导性陈述": 5.0,
    "通报批评": 3.5,
    "监管函": 2.5,
    "问询函": 2.0,
    "行政处罚": 4.5,
    "纪律处分": 3.5,
    "市场传闻": 1.5,
    "澄清公告": 2.5,
    "风险提示": 2.5,
    "不实传闻": 2.5,
    "连续跌停": 4.5,
    "一字跌停": 4.0,
    "跌停": 2.5,
    "停牌": 2.0,
    "大额亏损": 4.0,
    "巨亏": 4.0,
    "净利润亏损": 3.0,
    "投资损失": 3.0,
    "商誉减值": 3.0,
    "资产减值": 2.5,
    "控制权受限": 4.0,
    "供应中断": 3.5,
    "连续性造成影响": 3.0,
    "重大诉讼": 3.5,
    "仲裁": 2.0,
    "司法冻结": 3.5,
    "股权冻结": 3.0,
    "质押": 2.0,
    "强制平仓": 4.0,
    "债务逾期": 4.0,
    "资金占用": 6.0,
    "违规担保": 6.0,
    "财务造假": 6.0,
    "破产清算": 6.0,
    "重整": 4.5,
}


CATEGORY_KEYWORDS = {
    "ST/退市风险": ("退市风险警示", "其他风险警示", "*ST", " ST", "披星戴帽", "被实施退市风险警示"),
    "审计/内控非标": ("无法表示意见", "否定意见", "保留意见", "内控审计报告", "内部控制"),
    "监管立案/信披": ("证监会立案", "立案调查", "立案告知书", "信息披露", "误导性陈述", "通报批评", "监管函", "行政处罚"),
    "传闻澄清/预期证伪": ("市场传闻", "澄清公告", "风险提示", "不实传闻"),
    "跌停/流动性踩踏": ("连续跌停", "一字跌停", "跌停", "停牌"),
    "业绩/资产爆雷": ("大额亏损", "巨亏", "净利润亏损", "投资损失", "商誉减值", "资产减值"),
    "经营控制/供应风险": ("控制权受限", "供应中断", "连续性造成影响"),
    "诉讼/冻结/质押": ("重大诉讼", "仲裁", "司法冻结", "股权冻结", "质押", "强制平仓", "债务逾期"),
    "治理红线": ("资金占用", "违规担保", "财务造假", "破产清算", "重整"),
}


STOCK_NAME_HINTS = {
    "巨力索具": "002342",
    "闻泰科技": "600745",
    "*ST闻泰": "600745",
    "ST闻泰": "600745",
}


def scan_risk_items(items: Iterable[NewsItem], threshold: float = 5.0) -> list[RiskSignal]:
    signals: list[RiskSignal] = []
    now = datetime.now()
    for item in items:
        text = f"{item.title}\n{item.summary}"
        score, matched, source_score, fresh = score_risk_item(item, now=now)
        if score < threshold:
            continue
        event_score, baseline_score = split_keyword_scores(matched)
        categories = infer_risk_categories(text, matched)
        symbol = infer_symbol(text)
        stock_name = infer_stock_name(text)
        severity = classify_risk(score, matched, categories, event_score=event_score, baseline_score=baseline_score)
        risk_type = classify_risk_type(matched, categories, event_score, baseline_score)
        signals.append(
            RiskSignal(
                title=item.title,
                published_at=item.published_at,
                source=item.source,
                url=item.url,
                stock_name=stock_name,
                symbol=symbol,
                score=round(score, 2),
                severity=severity,
                categories=categories,
                matched_keywords=matched,
                risk_type=risk_type,
                event_score=round(event_score, 2),
                baseline_score=round(baseline_score, 2),
                action=risk_action(severity, categories),
                summary=item.summary[:300],
                source_score=round(source_score, 2),
                freshness_score=round(fresh, 2),
            )
        )
    return collapse_baseline_status_signals(sorted(signals, key=lambda signal: signal.score, reverse=True))


def score_risk_item(item: NewsItem, now: datetime | None = None) -> tuple[float, list[str], float, float]:
    text = f"{item.title}\n{item.summary}"
    keyword_score, matched = score_risk_text(text)
    source_score = max(source_quality_score(item), 0.0)
    fresh = max(freshness_score(item.published_at, now=now), 0.0)
    return keyword_score + source_score + fresh, matched, source_score, fresh


def score_risk_text(text: str) -> tuple[float, list[str]]:
    scan_text = risk_scan_text(text)
    lowered = scan_text.lower()
    matched = [keyword for keyword in RISK_KEYWORD_WEIGHTS if keyword.lower() in lowered]
    score = sum(RISK_KEYWORD_WEIGHTS[keyword] for keyword in matched)
    if re.search(r"(?<![A-Za-z])ST[A-Za-z\u4e00-\u9fff]*", scan_text) or "*ST" in scan_text:
        if " ST" not in matched and "*ST" not in matched:
            matched.append("ST")
            score += 3.0
    return score, matched


def risk_scan_text(text: str) -> str:
    scan_text = text
    for phrase, replacement in BENIGN_PHRASE_REPLACEMENTS.items():
        scan_text = scan_text.replace(phrase, replacement)
    return scan_text


def split_keyword_scores(matched: list[str]) -> tuple[float, float]:
    event_score = 0.0
    baseline_score = 0.0
    for keyword in matched:
        weight = 3.0 if keyword == "ST" else RISK_KEYWORD_WEIGHTS.get(keyword, 0.0)
        if keyword in BASELINE_STATUS_KEYWORDS:
            baseline_score += weight
        else:
            event_score += weight
    return event_score, baseline_score


def infer_risk_categories(text: str, matched: list[str]) -> list[str]:
    lowered = risk_scan_text(text).lower()
    categories = []
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords) or any(keyword in matched for keyword in keywords):
            categories.append(category)
    return categories or ["未分类风险"]


def classify_risk(
    score: float,
    matched: list[str],
    categories: list[str],
    event_score: float | None = None,
    baseline_score: float | None = None,
) -> str:
    event_score = event_score if event_score is not None else split_keyword_scores(matched)[0]
    baseline_score = baseline_score if baseline_score is not None else split_keyword_scores(matched)[1]
    critical_hits = [keyword for keyword in matched if keyword in CRITICAL_KEYWORDS]
    material_critical = [keyword for keyword in critical_hits if keyword not in BASELINE_STATUS_KEYWORDS]
    if material_critical or score >= 14 or "治理红线" in categories:
        return "红灯-回避/禁止买入"
    if baseline_score >= 3 and "ST/退市风险" in categories:
        return "红灯-回避/禁止买入"
    if event_score >= 6 or score >= 9:
        return "橙灯-降仓/只观察"
    if score >= 5:
        return "黄灯-必须核查"
    return "绿灯-未触发"


def classify_risk_type(
    matched: list[str],
    categories: list[str],
    event_score: float,
    baseline_score: float,
) -> str:
    if event_score >= 5 or any(keyword not in BASELINE_STATUS_KEYWORDS for keyword in matched):
        return "新增负面事件"
    if baseline_score > 0 or "ST/退市风险" in categories:
        return "存量风险状态"
    return "风险线索"


def risk_action(severity: str, categories: list[str]) -> str:
    if severity.startswith("红灯"):
        return "移出进攻池；已有仓位先按风险预案处理，禁止抄底和摊平"
    if severity.startswith("橙灯"):
        return "停止新增仓位；等待公告落地、跌停打开和流动性恢复后再复评"
    if severity.startswith("黄灯"):
        return "先核查交易所公告、审计意见和监管文件，未澄清前不做进攻"
    return "保持观察"


def collapse_baseline_status_signals(signals: list[RiskSignal]) -> list[RiskSignal]:
    """Keep one baseline-only ST/status row per symbol while preserving event rows."""
    result: list[RiskSignal] = []
    baseline_seen: set[str] = set()
    for signal in signals:
        if signal.risk_type != "存量风险状态":
            result.append(signal)
            continue
        key = signal.symbol or signal.stock_name or signal.title
        if key in baseline_seen:
            continue
        baseline_seen.add(key)
        result.append(signal)
    return result


def symbol_risk_flags(signals: Iterable[RiskSignal], max_per_symbol: int = 3) -> dict[str, list[str]]:
    """Return BLOCK flags keyed by 6-digit code for use by stock screeners."""
    flags: dict[str, list[str]] = {}
    for signal in signals:
        if not signal.symbol or not signal.severity.startswith("红灯"):
            continue
        bucket = flags.setdefault(signal.symbol, [])
        if len(bucket) >= max_per_symbol:
            continue
        categories = ",".join(signal.categories)
        bucket.append(f"BLOCK: 风险雷达红灯[{signal.risk_type}] {signal.title} ({categories})")
    return flags


def infer_symbol(text: str) -> str:
    for name, symbol in STOCK_NAME_HINTS.items():
        if name in text:
            return symbol
    match = re.search(r"(?<!\d)([036]\d{5})(?!\d)", text)
    return match.group(1) if match else ""


def infer_stock_name(text: str) -> str:
    for name in STOCK_NAME_HINTS:
        if name in text:
            return name
    for pattern in (r"证券简称[:：]\s*([*STＡ-ＺA-Z\u4e00-\u9fff]+)", r"股票简称[:：]\s*([*STＡ-ＺA-Z\u4e00-\u9fff]+)"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def fetch_symbol_risk_items(symbols: Iterable[str], limit_per_symbol: int = 20) -> list[NewsItem]:
    """Fetch per-symbol Eastmoney news when available; failures are skipped."""
    import akshare as ak

    items: list[NewsItem] = []
    for symbol in symbols:
        code = str(symbol).strip()
        if not code:
            continue
        try:
            frame = ak.stock_news_em(symbol=code)
        except Exception:
            continue
        for _, row in frame.head(limit_per_symbol).iterrows():
            normalized = {str(key): value for key, value in row.to_dict().items()}
            title = _first_present(normalized, ("新闻标题", "标题", "title"))
            content = _first_present(normalized, ("新闻内容", "内容", "摘要", "summary"))
            published = _first_present(normalized, ("发布时间", "时间", "发布日期"))
            url = _first_present(normalized, ("新闻链接", "链接", "url", "网址"))
            if title:
                items.append(
                    NewsItem(
                        title=str(title),
                        summary=str(content or ""),
                        published_at=str(published or ""),
                        source=f"东方财富个股新闻:{code}",
                        url=str(url or ""),
                    )
                )
    return items


def fetch_symbol_notice_items(
    symbols: Iterable[str],
    days: int = 120,
    limit_per_symbol: int = 30,
) -> list[NewsItem]:
    """Fetch recent per-stock Eastmoney announcements for hard risk detection."""
    import akshare as ak

    end = datetime.now()
    begin = end - timedelta(days=days)
    begin_text = begin.strftime("%Y%m%d")
    end_text = end.strftime("%Y%m%d")
    categories = ("风险提示", "重大事项", "信息披露", "财务报告")
    items: list[NewsItem] = []
    for symbol in symbols:
        code = str(symbol).strip()
        if not code:
            continue
        for category in categories:
            try:
                frame = ak.stock_individual_notice_report(
                    security=code,
                    symbol=category,
                    begin_date=begin_text,
                    end_date=end_text,
                )
            except Exception:
                continue
            for _, row in frame.head(limit_per_symbol).iterrows():
                normalized = {str(key): value for key, value in row.to_dict().items()}
                title = _first_present(normalized, ("公告标题", "标题", "title"))
                notice_type = _first_present(normalized, ("公告类型", "类型"))
                name = _first_present(normalized, ("名称", "证券简称"))
                published = _first_present(normalized, ("公告日期", "发布时间", "时间"))
                url = _first_present(normalized, ("网址", "链接", "url"))
                if title:
                    items.append(
                        NewsItem(
                            title=str(title),
                            summary=f"{name} {code}",
                            published_at=str(published or ""),
                            source=f"东方财富个股公告:{code}:{category}",
                            url=str(url or ""),
                        )
                    )
    return dedupe_news_items(items)


def dedupe_news_items(items: Iterable[NewsItem]) -> list[NewsItem]:
    seen: set[tuple[str, str, str]] = set()
    result: list[NewsItem] = []
    for item in items:
        key = (item.title, item.published_at, item.url)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def save_risk_report(signals: list[RiskSignal], output_dir: str | Path = REPORT_DIR) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"risk_radar_{stamp}.csv"
    md_path = out / f"risk_radar_{stamp}.md"
    df = pd.DataFrame([asdict(signal) for signal in signals])
    if not df.empty:
        df["categories"] = df["categories"].apply(",".join)
        df["matched_keywords"] = df["matched_keywords"].apply(",".join)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(risk_markdown(signals), encoding="utf-8")
    return csv_path, md_path


def risk_markdown(signals: list[RiskSignal]) -> str:
    lines = [
        "# A股暴雷风险雷达",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 规则：监管/退市/ST/非标审计/澄清证伪/连续跌停/大额亏损/诉讼质押等触发风险红灯。",
        "",
    ]
    if not signals:
        lines.append("暂无高风险信号。")
        return "\n".join(lines) + "\n"
    rows = [
        {
            "级别": signal.severity,
            "分数": signal.score,
            "名称": signal.stock_name,
            "代码": signal.symbol,
            "风险类型": signal.risk_type,
            "类别": ",".join(signal.categories),
            "时间": signal.published_at,
            "来源": signal.source,
            "标题": signal.title,
            "动作": signal.action,
        }
        for signal in signals
    ]
    lines.append(pd.DataFrame(rows).to_markdown(index=False))
    lines.extend(
        [
            "",
            "## 处理原则",
            "",
            "- 红灯先处理风险，不参与题材进攻排序。",
            "- 退市风险警示、无法表示意见、证监会立案、资金占用、违规担保优先级高于任何短线资金流。",
            "- 澄清公告和传闻证伪后，先看连续跌停/换手修复，不用上涨催化模型硬接。",
            "",
        ]
    )
    return "\n".join(lines)


def _first_present(row: dict[str, object], names: tuple[str, ...]):
    for name in names:
        if name in row and pd.notna(row[name]):
            return row[name]
    return ""
