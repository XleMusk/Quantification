"""Catalyst scanner for A-share theme reactions.

The trend model is deliberately backward-looking; this module adds a forward
looking layer by scanning news/research headlines for supply-chain catalysts and
mapping them to tradable ETF/theme proxies.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from .trend_calibration import EVENT_PROFILES, KNOWN_TARGETS


REPORT_DIR = Path("committee_reviews")


@dataclass
class NewsItem:
    title: str
    summary: str = ""
    published_at: str = ""
    source: str = ""
    url: str = ""


@dataclass
class CatalystHit:
    title: str
    published_at: str
    source: str
    url: str
    event: str
    score: float
    matched_keywords: list[str]
    affected_targets: list[str]
    summary: str
    source_score: float = 0.0
    freshness_score: float = 0.0
    confirmation_count: int = 1
    confirmation_sources: list[str] | None = None


KEYWORD_WEIGHTS = {
    "摩根士丹利": 3.0,
    "大摩": 3.0,
    "Morgan Stanley": 3.0,
    "NVL72": 3.0,
    "VR200": 2.5,
    "Rubin": 2.5,
    "BOM": 2.5,
    "物料清单": 2.5,
    "成本拆解": 2.5,
    "机柜": 1.5,
    "单柜": 1.5,
    "PCB": 1.5,
    "高阶PCB": 1.8,
    "PCB钻针": 1.8,
    "CCL": 1.4,
    "覆铜板": 1.4,
    "MLCC": 1.4,
    "ABF": 1.4,
    "载板": 1.2,
    "电源": 1.0,
    "液冷": 1.0,
    "CPO": 1.2,
    "光模块": 1.0,
    "铜连接": 1.2,
    "铜缆": 1.0,
    "消费电子": 1.0,
    "AI服务器": 1.2,
    "ODM": 1.0,
    "提前备货": 1.0,
    "供不应求": 1.0,
    "涨价": 1.0,
    "暴增": 1.0,
    "增长": 0.7,
    "提升": 0.7,
}


SOURCE_CREDIBILITY_WEIGHTS = {
    "摩根士丹利": 2.0,
    "大摩": 2.0,
    "Morgan Stanley": 2.0,
    "高盛": 1.8,
    "Goldman Sachs": 1.8,
    "中信证券": 1.4,
    "中金": 1.4,
    "CICC": 1.4,
    "华泰证券": 1.2,
    "财联社": 1.2,
    "证券时报": 1.0,
    "上海证券报": 1.0,
    "中国证券报": 1.0,
    "东方财富": 0.7,
    "雪球": 0.2,
    "股吧": -0.4,
    "谣传": -1.2,
    "小作文": -1.0,
}


TAG_KEYWORDS = {
    "pcb_proxy": ("PCB", "高阶PCB", "PCB钻针", "CCL", "覆铜板"),
    "consumer_electronics": ("消费电子", "电子制造", "组装"),
    "electronics_manufacturing": ("消费电子", "电子制造", "ODM", "组装"),
    "mlcc": ("MLCC", "被动元件", "陶瓷电容"),
    "communication": ("通信", "光模块", "铜连接", "铜缆"),
    "cpo": ("CPO", "光模块", "光通信"),
    "copper_interconnect": ("铜连接", "铜缆", "高速铜"),
    "server_odm": ("ODM", "AI服务器", "机柜", "服务器"),
    "ai_hardware": ("AI硬件", "AI服务器", "机柜", "Rubin", "NVL72"),
    "semiconductor": ("半导体", "芯片", "GPU", "HBM"),
}


def scan_catalysts(items: Iterable[NewsItem], threshold: float = 7.0) -> list[CatalystHit]:
    """Score news items and return high-conviction catalyst hits."""
    hits: list[CatalystHit] = []
    now = datetime.now()
    for item in items:
        text = f"{item.title}\n{item.summary}"
        score, matched, source_score, freshness = score_news_item(item, now=now)
        event = infer_event(text, score)
        if event and score >= threshold:
            hits.append(
                CatalystHit(
                    title=item.title,
                    published_at=item.published_at,
                    source=item.source,
                    url=item.url,
                    event=event,
                    score=round(score, 2),
                    matched_keywords=matched,
                    affected_targets=affected_targets(text, event),
                    summary=item.summary[:240],
                    source_score=round(source_score, 2),
                    freshness_score=round(freshness, 2),
                    confirmation_sources=[item.source] if item.source else [],
                )
            )
    return sorted(apply_confirmation_boosts(hits), key=lambda hit: hit.score, reverse=True)


def fetch_realtime_news(limit: int = 80) -> list[NewsItem]:
    """Fetch recent finance news from locally available AkShare sources."""
    import akshare as ak

    frames: list[tuple[str, pd.DataFrame]] = []
    for source, func in (
        ("财联社", ak.stock_info_global_cls),
        ("东方财富", ak.stock_info_global_em),
    ):
        try:
            frames.append((source, func()))
        except Exception:
            continue

    items: list[NewsItem] = []
    for source, frame in frames:
        for _, row in frame.head(limit).iterrows():
            normalized = {str(key): value for key, value in row.to_dict().items()}
            title = _first_present(normalized, ("标题", "title", "新闻标题", "内容"))
            summary = _first_present(normalized, ("摘要", "内容", "summary"))
            published = _first_present(normalized, ("发布时间", "时间", "发布日期"))
            url = _first_present(normalized, ("链接", "url", "网址"))
            if title:
                items.append(
                    NewsItem(
                        title=str(title),
                        summary=str(summary or ""),
                        published_at=str(published or ""),
                        source=source,
                        url=str(url or ""),
                    )
                )
    return items[:limit]


def save_catalyst_report(hits: list[CatalystHit], output_dir: str | Path = REPORT_DIR) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"catalyst_scan_{stamp}.csv"
    md_path = out / f"catalyst_scan_{stamp}.md"
    df = pd.DataFrame([asdict(hit) for hit in hits])
    if not df.empty:
        df["matched_keywords"] = df["matched_keywords"].apply(",".join)
        df["affected_targets"] = df["affected_targets"].apply(",".join)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(catalyst_markdown(hits), encoding="utf-8")
    return csv_path, md_path


def catalyst_markdown(hits: list[CatalystHit]) -> str:
    lines = [
        "# A股催化剂雷达",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 说明：扫描新闻/研报线索，映射到A股可交易代理，不是买卖建议。",
        "",
    ]
    if not hits:
        lines.append("暂无高分催化剂。")
        return "\n".join(lines) + "\n"

    rows = [
        {
            "事件": hit.event,
            "分数": hit.score,
            "来源分": hit.source_score,
            "新鲜度": hit.freshness_score,
            "确认": hit.confirmation_count,
            "时间": hit.published_at,
            "来源": hit.source,
            "标题": hit.title,
            "关键词": ",".join(hit.matched_keywords[:10]),
            "受益代理": ",".join(hit.affected_targets),
        }
        for hit in hits
    ]
    lines.append(pd.DataFrame(rows).to_markdown(index=False))
    lines.extend(
        [
            "",
            "## 事件处理规则",
            "",
            "- 若出现大行研报/BOM/涨价/备货线索，先看受益代理是否已有成交放大。",
            "- 前排涨停潮不追；优先找同产业链ETF或低拥挤分支确认。",
            "- 事件覆盖层可传给 `market-trend --event nvl72_bom` 做二次评分。",
            "",
        ]
    )
    return "\n".join(lines)


def score_text(text: str) -> tuple[float, list[str]]:
    matched = [keyword for keyword in KEYWORD_WEIGHTS if keyword.lower() in text.lower()]
    score = sum(KEYWORD_WEIGHTS[keyword] for keyword in matched)
    return score, matched


def score_news_item(item: NewsItem, now: datetime | None = None) -> tuple[float, list[str], float, float]:
    text = f"{item.title}\n{item.summary}"
    keyword_score, matched = score_text(text)
    source_score = source_quality_score(item)
    freshness = freshness_score(item.published_at, now=now)
    return keyword_score + source_score + freshness, matched, source_score, freshness


def source_quality_score(item: NewsItem) -> float:
    text = f"{item.source}\n{item.title}\n{item.summary}"
    score = 0.0
    for keyword, weight in SOURCE_CREDIBILITY_WEIGHTS.items():
        if keyword.lower() in text.lower():
            score += weight
    return max(min(score, 3.0), -2.0)


def freshness_score(published_at: str, now: datetime | None = None) -> float:
    parsed = parse_news_time(published_at, now=now)
    if parsed is None:
        return 0.0
    base = now or datetime.now()
    age = base - parsed
    if age < timedelta(minutes=-5):
        return 0.0
    if age <= timedelta(minutes=30):
        return 1.5
    if age <= timedelta(hours=2):
        return 1.0
    if age <= timedelta(hours=6):
        return 0.5
    if age <= timedelta(days=1):
        return 0.0
    if age <= timedelta(days=3):
        return -0.6
    return -1.2


def parse_news_time(value: str, now: datetime | None = None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    base = now or datetime.now()
    relative_map = {
        "刚刚": timedelta(minutes=0),
        "分钟前": None,
        "小时前": None,
        "昨天": timedelta(days=1),
    }
    if text == "刚刚":
        return base
    if "分钟前" in text:
        minutes = _leading_number(text)
        return None if minutes is None else base - timedelta(minutes=minutes)
    if "小时前" in text:
        hours = _leading_number(text)
        return None if hours is None else base - timedelta(hours=hours)
    if text.startswith("昨天"):
        clock = text.replace("昨天", "").strip()
        parsed_clock = _parse_clock(clock)
        if parsed_clock is None:
            return base - relative_map["昨天"]
        return (base - timedelta(days=1)).replace(hour=parsed_clock[0], minute=parsed_clock[1], second=0, microsecond=0)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
        except Exception:
            continue
    for fmt in ("%m-%d %H:%M", "%m/%d %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(year=base.year)
        except Exception:
            continue
    clock = _parse_clock(text)
    if clock is not None:
        return base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    return None


def apply_confirmation_boosts(hits: list[CatalystHit]) -> list[CatalystHit]:
    groups: dict[tuple[str, tuple[str, ...]], list[CatalystHit]] = {}
    for hit in hits:
        key = (hit.event, tuple(sorted(hit.affected_targets)))
        groups.setdefault(key, []).append(hit)
    for group in groups.values():
        sources = sorted({source for hit in group for source in (hit.confirmation_sources or [hit.source]) if source})
        count = max(len(sources), len(group))
        if count <= 1:
            continue
        boost = min((count - 1) * 0.8, 2.4)
        for hit in group:
            hit.confirmation_count = count
            hit.confirmation_sources = sources
            hit.score = round(hit.score + boost, 2)
    return hits


def infer_event(text: str, score: float) -> str:
    lower = text.lower()
    has_research_source = any(key.lower() in lower for key in ("摩根士丹利", "大摩", "morgan stanley"))
    has_rack_bom = any(key.lower() in lower for key in ("nvl72", "rubin", "vr200", "bom", "物料清单", "成本拆解", "机柜"))
    has_component = any(
        key.lower() in lower
        for key in (
            "pcb",
            "mlcc",
            "abf",
            "ccl",
            "覆铜板",
            "电源",
            "液冷",
            "cpo",
            "光模块",
            "铜连接",
            "消费电子",
            "ai服务器",
            "odm",
        )
    )
    if score >= 7.0 and has_rack_bom and (has_research_source or has_component):
        return "nvl72_bom"
    return ""


def affected_targets(text: str, event: str) -> list[str]:
    profile = EVENT_PROFILES.get(event)
    if profile is None:
        return []
    matched_tags = {
        tag
        for tag in profile["tag_boosts"]
        if tag in TAG_KEYWORDS and any(keyword.lower() in text.lower() for keyword in TAG_KEYWORDS[tag])
    }
    if not matched_tags:
        matched_tags = set(profile["tag_boosts"].keys())
    targets = [
        f"{target.name}({target.symbol})"
        for target in KNOWN_TARGETS
        if matched_tags.intersection(target.tags)
    ]
    return targets


def _first_present(row: dict[str, object], names: tuple[str, ...]):
    for name in names:
        if name in row and pd.notna(row[name]):
            return row[name]
    return ""


def _leading_number(text: str) -> int | None:
    digits = []
    for char in text:
        if char.isdigit():
            digits.append(char)
        elif digits:
            break
    if not digits:
        return None
    return int("".join(digits))


def _parse_clock(text: str) -> tuple[int, int] | None:
    parts = text.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None
