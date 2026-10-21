"""Composite alpha stack for catalyst-driven A-share themes.

This module combines three layers:
1. catalyst relevance from news/research,
2. live market confirmation from ETF snapshots,
3. crowding/risk penalties before any trade decision.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from .catalyst_monitor import CatalystHit, NewsItem, scan_catalysts
from .trend_calibration import KNOWN_TARGETS, TrendTarget


REPORT_DIR = Path("committee_reviews")
SPOT_CACHE_DIR = Path("market_data") / "a_share" / "spot_cache"
SIGNAL_LOG_DIR = Path("committee_state") / "signals"
ALPHA_MODEL_VERSION = "event_alpha_v0.3"
ALPHA_WEIGHTS = {
    "catalyst": 0.45,
    "reaction": 0.30,
    "liquidity": 0.15,
    "crowding": -0.25,
}


@dataclass
class MarketSnapshot:
    target_id: str
    name: str
    symbol: str
    pct_change: float
    amount: float
    open_price: float
    high: float
    low: float
    latest: float
    main_net_inflow: float | None = None
    turnover: float | None = None
    data_time: str = ""


@dataclass
class AlphaScore:
    target_id: str
    name: str
    symbol: str
    total_score: float
    catalyst_score: float
    reaction_score: float
    crowding_penalty: float
    liquidity_score: float
    label: str
    reason: str
    playbook: str = ""
    pct_change: float | None = None
    amount: float | None = None
    latest: float | None = None
    open_price: float | None = None
    high: float | None = None
    low: float | None = None
    main_net_inflow: float | None = None
    turnover: float | None = None
    data_time: str = ""


@dataclass
class AlphaOutcome:
    signal_id: str
    created_at: str
    target_id: str
    name: str
    symbol: str
    label: str
    original_score: float
    entry_price: float | None
    current_price: float | None
    return_pct: float | None
    original_pct_change: float | None
    current_pct_change: float | None
    outcome: str


@dataclass
class AlphaFeedback:
    label: str
    count: int
    avg_return_pct: float | None
    win_rate: float | None
    loss_rate: float | None
    verdict: str
    suggested_adjustment: str


def rank_event_reactions(
    hits: Iterable[CatalystHit],
    snapshots: Iterable[MarketSnapshot],
    targets: Iterable[TrendTarget] = KNOWN_TARGETS,
) -> list[AlphaScore]:
    """Rank targets by catalyst relevance plus live confirmation."""
    hit_list = list(hits)
    snapshot_map = {snapshot.symbol: snapshot for snapshot in snapshots}
    scores: list[AlphaScore] = []
    for target in targets:
        if target.kind != "etf":
            continue
        snapshot = snapshot_map.get(target.symbol)
        catalyst = catalyst_component(target, hit_list)
        reaction = reaction_component(snapshot)
        liquidity = liquidity_component(snapshot)
        crowding = crowding_penalty(snapshot)
        total = (
            catalyst * ALPHA_WEIGHTS["catalyst"]
            + reaction * ALPHA_WEIGHTS["reaction"]
            + liquidity * ALPHA_WEIGHTS["liquidity"]
            + crowding * ALPHA_WEIGHTS["crowding"]
        )
        label = classify_alpha(total, catalyst, reaction, crowding, snapshot)
        reason = build_reason(catalyst, reaction, liquidity, crowding, snapshot)
        playbook = execution_playbook(label, snapshot, catalyst, reaction, crowding)
        scores.append(
            AlphaScore(
                target_id=target.target_id,
                name=target.name,
                symbol=target.symbol,
                total_score=round(total, 3),
                catalyst_score=round(catalyst, 3),
                reaction_score=round(reaction, 3),
                crowding_penalty=round(crowding, 3),
                liquidity_score=round(liquidity, 3),
                label=label,
                reason=reason,
                playbook=playbook,
                pct_change=None if snapshot is None else round(snapshot.pct_change, 2),
                amount=None if snapshot is None else round(snapshot.amount, 0),
                latest=None if snapshot is None else round(snapshot.latest, 4),
                open_price=None if snapshot is None else round(snapshot.open_price, 4),
                high=None if snapshot is None else round(snapshot.high, 4),
                low=None if snapshot is None else round(snapshot.low, 4),
                main_net_inflow=None if snapshot is None or snapshot.main_net_inflow is None else round(snapshot.main_net_inflow, 0),
                turnover=None if snapshot is None or snapshot.turnover is None else round(snapshot.turnover, 2),
                data_time="" if snapshot is None else snapshot.data_time,
            )
        )
    return sorted(scores, key=lambda item: item.total_score, reverse=True)


def catalyst_component(target: TrendTarget, hits: list[CatalystHit]) -> float:
    """Score how directly a target maps to active catalysts."""
    if not hits:
        return 0.0
    total = 0.0
    label = f"{target.name}({target.symbol})"
    for hit in hits:
        direct = label in hit.affected_targets
        tag_like = any(tag in ",".join(hit.matched_keywords) for tag in target.tags)
        if direct or tag_like:
            total += min(hit.score / 30.0, 1.5)
    return min(total, 2.0)


def reaction_component(snapshot: MarketSnapshot | None) -> float:
    """Score live market confirmation."""
    if snapshot is None:
        return 0.0
    close_location = _close_location(snapshot)
    pct_score = max(min(snapshot.pct_change / 5.0, 1.4), -1.2)
    flow_score = 0.0
    if snapshot.main_net_inflow is not None and snapshot.amount > 0:
        flow_score = max(min(snapshot.main_net_inflow / snapshot.amount * 4.0, 0.8), -0.8)
    return pct_score * 0.55 + close_location * 0.25 + flow_score * 0.20


def liquidity_component(snapshot: MarketSnapshot | None) -> float:
    if snapshot is None:
        return 0.0
    if snapshot.amount >= 2_000_000_000:
        return 1.0
    if snapshot.amount >= 500_000_000:
        return 0.75
    if snapshot.amount >= 100_000_000:
        return 0.45
    if snapshot.amount >= 30_000_000:
        return 0.20
    return -0.2


def crowding_penalty(snapshot: MarketSnapshot | None) -> float:
    if snapshot is None:
        return 0.2
    penalty = 0.0
    if snapshot.pct_change >= 7.0:
        penalty += 1.0
    elif snapshot.pct_change >= 5.0:
        penalty += 0.55
    if snapshot.high > 0 and (snapshot.high - snapshot.latest) / snapshot.high >= 0.025:
        penalty += 0.45
    if snapshot.main_net_inflow is not None and snapshot.main_net_inflow < 0 and snapshot.pct_change > 3:
        penalty += 0.40
    if snapshot.turnover is not None and snapshot.turnover >= 20:
        penalty += 0.35
    return penalty


def classify_alpha(
    total: float,
    catalyst: float,
    reaction: float,
    crowding: float,
    snapshot: MarketSnapshot | None,
) -> str:
    if snapshot is None:
        return "数据缺失"
    if crowding >= 0.85:
        return "高位拥挤，等分歧"
    if total >= 0.95 and catalyst >= 0.8 and reaction >= 0.4:
        return "主线确认"
    if total >= 0.65 and catalyst >= 0.6:
        return "候选进攻"
    if catalyst >= 0.6 and reaction < 0.1:
        return "催化未确认"
    if total >= 0.35:
        return "观察"
    return "弱相关/回避"


def build_reason(
    catalyst: float,
    reaction: float,
    liquidity: float,
    crowding: float,
    snapshot: MarketSnapshot | None,
) -> str:
    if snapshot is None:
        return "未取到实时ETF快照"
    parts = [
        f"催化{catalyst:.2f}",
        f"反应{reaction:.2f}",
        f"流动性{liquidity:.2f}",
        f"拥挤{crowding:.2f}",
        f"涨跌{snapshot.pct_change:.2f}%",
    ]
    if snapshot.main_net_inflow is not None:
        parts.append(f"主力净流入{snapshot.main_net_inflow / 100000000:.2f}亿")
    return "，".join(parts)


def execution_playbook(
    label: str,
    snapshot: MarketSnapshot | None,
    catalyst: float,
    reaction: float,
    crowding: float,
) -> str:
    """Translate a label into a concrete, risk-aware operating script."""
    if snapshot is None:
        return "数据缺失，不做动作"
    if "高位拥挤" in label:
        return "不追高；只等放量分歧后仍能收回日内均线/前高再评估"
    if label == "主线确认":
        return "主线候选；只做分批试错，跌破日内关键承接位则退出观察"
    if label == "候选进攻":
        if snapshot.pct_change >= 5:
            return "涨幅偏大；只接受回落承接后的低吸试错"
        if snapshot.main_net_inflow is not None and snapshot.main_net_inflow > 0 and reaction >= 0.4:
            return "进攻候选；等回踩不破或放量突破确认，控制单笔风险"
        return "进攻候选但确认不足；等资金回流再动作"
    if label == "催化未确认":
        return "只观察催化扩散；没有量价确认不提前埋伏"
    if label == "观察":
        if catalyst >= 0.8 and reaction < 0.25:
            return "催化强但盘面未跟；放入补涨观察，不主动追"
        return "观察；等待相对强度或资金确认"
    return "弱相关或回避；不把反弹误当主线"


def _snapshot_source_time(row) -> str:
    """Keep the provider quote time; collection time is not a replacement."""
    for field in ("更新时间", "数据时间"):
        value = row.get(field)
        if value is None or pd.isna(value):
            continue
        if isinstance(value, (datetime, pd.Timestamp)):
            return value.isoformat()
        text = str(value).strip()
        if text and text.lower() not in {"nan", "nat", "none", "<na>"}:
            return text
    return ""


def fetch_etf_snapshots(
    targets: Iterable[TrendTarget],
    cache_seconds: int = 120,
    force: bool = False,
    cache_dir: str | Path = SPOT_CACHE_DIR,
) -> list[MarketSnapshot]:
    """Fetch ETF spot data for known targets via AkShare, with a short TTL cache."""
    import akshare as ak

    target_map = {target.symbol: target for target in targets if target.kind == "etf"}
    if not target_map:
        return []
    if not force and cache_seconds > 0:
        cached = load_cached_etf_snapshots(cache_dir=cache_dir, max_age_seconds=cache_seconds)
        if cached is not None:
            return [snapshot for snapshot in cached if snapshot.symbol in target_map]
    try:
        spot = ak.fund_etf_spot_em()
    except Exception as error:
        # Keep the intraday consumer usable when a public spot endpoint is
        # temporarily unavailable.  Missing market data stays unknown; it is
        # never converted into zeroes or stale quotes silently.
        error_path = Path(cache_dir) / "etf_spot_last_error.json"
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(
            json.dumps({
                "status": "unavailable",
                "observed_at": datetime.now().isoformat(timespec="seconds"),
                "source": "akshare.fund_etf_spot_em",
                "error": f"{type(error).__name__}: {error}",
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return []
    snapshots: list[MarketSnapshot] = []
    for _, row in spot.iterrows():
        symbol = str(row.get("代码", ""))
        target = target_map.get(symbol)
        if target is None:
            continue
        snapshots.append(
            MarketSnapshot(
                target_id=target.target_id,
                name=target.name,
                symbol=target.symbol,
                pct_change=_num(row.get("涨跌幅")),
                amount=_num(row.get("成交额")),
                open_price=_num(row.get("开盘价")),
                high=_num(row.get("最高价")),
                low=_num(row.get("最低价")),
                latest=_num(row.get("最新价")),
                main_net_inflow=_optional_num(row.get("主力净流入-净额")),
                turnover=_optional_num(row.get("换手率")),
                data_time=_snapshot_source_time(row),
            )
        )
    if snapshots:
        save_etf_snapshot_cache(snapshots, cache_dir=cache_dir)
        error_path = Path(cache_dir) / "etf_spot_last_error.json"
        if error_path.exists():
            error_path.unlink()
    return snapshots


def save_etf_snapshot_cache(
    snapshots: Iterable[MarketSnapshot],
    cache_dir: str | Path = SPOT_CACHE_DIR,
) -> Path:
    """Persist latest ETF spot snapshots for fast repeated event checks."""
    out_dir = Path(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "etf_spot_snapshots.json"
    snapshot_rows = [asdict(snapshot) for snapshot in snapshots]
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(snapshot_rows),
        "snapshots": snapshot_rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_cached_etf_snapshots(
    cache_dir: str | Path = SPOT_CACHE_DIR,
    max_age_seconds: int = 120,
) -> list[MarketSnapshot] | None:
    """Load ETF spot snapshots when the cache is still fresh enough."""
    path = Path(cache_dir) / "etf_spot_snapshots.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(str(payload.get("saved_at", "")))
        if (datetime.now() - saved_at).total_seconds() > max_age_seconds:
            return None
        return [_snapshot_from_dict(item) for item in payload.get("snapshots", [])]
    except Exception:
        return None


def etf_snapshot_cache_status(cache_dir: str | Path = SPOT_CACHE_DIR) -> dict[str, object]:
    path = Path(cache_dir) / "etf_spot_snapshots.json"
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "saved_at": "",
            "age_seconds": None,
            "count": 0,
            "error": "",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        saved_at_text = str(payload.get("saved_at", ""))
        saved_at = datetime.fromisoformat(saved_at_text)
        return {
            "exists": True,
            "path": str(path),
            "saved_at": saved_at_text,
            "age_seconds": round((datetime.now() - saved_at).total_seconds(), 1),
            "count": int(payload.get("count", len(payload.get("snapshots", [])))),
            "error": "",
        }
    except Exception as exc:
        return {
            "exists": True,
            "path": str(path),
            "saved_at": "",
            "age_seconds": None,
            "count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def append_alpha_log(
    scores: list[AlphaScore],
    hits: list[CatalystHit],
    event_text: str,
    log_dir: str | Path = SIGNAL_LOG_DIR,
    dedupe_minutes: int = 10,
) -> Path:
    """Append one event-alpha decision snapshot for later outcome evaluation."""
    out_dir = Path(log_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "event_alpha_log.jsonl"
    created_at = datetime.now().isoformat(timespec="seconds")
    event_hash = _event_hash(event_text)
    if dedupe_minutes > 0 and _has_recent_duplicate(path, event_hash, created_at, dedupe_minutes):
        return path
    signal_id = hashlib.sha1(f"{created_at}|{event_hash}".encode("utf-8")).hexdigest()[:12]
    record = {
        "signal_id": signal_id,
        "event_hash": event_hash,
        "created_at": created_at,
        "model_version": ALPHA_MODEL_VERSION,
        "weights": ALPHA_WEIGHTS,
        "event_text": event_text,
        "hits": [asdict(hit) for hit in hits],
        "scores": [asdict(score) for score in scores],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def evaluate_alpha_log(
    snapshots: Iterable[MarketSnapshot],
    log_path: str | Path = SIGNAL_LOG_DIR / "event_alpha_log.jsonl",
    limit: int = 5,
) -> list[AlphaOutcome]:
    """Evaluate logged event-alpha signals against current ETF snapshots."""
    path = Path(log_path)
    if not path.exists():
        return []
    snapshot_map = {snapshot.symbol: snapshot for snapshot in snapshots}
    records = _load_alpha_log_records(path)
    if limit > 0:
        records = records[-limit:]
    outcomes: list[AlphaOutcome] = []
    for record in records:
        for score in record.get("scores", []):
            symbol = str(score.get("symbol", ""))
            snapshot = snapshot_map.get(symbol)
            entry_price = _optional_num(score.get("latest"))
            current_price = None if snapshot is None else snapshot.latest
            return_pct = _return_pct(entry_price, current_price)
            label = str(score.get("label", ""))
            outcomes.append(
                AlphaOutcome(
                    signal_id=str(record.get("signal_id", "")),
                    created_at=str(record.get("created_at", "")),
                    target_id=str(score.get("target_id", "")),
                    name=str(score.get("name", "")),
                    symbol=symbol,
                    label=label,
                    original_score=_num(score.get("total_score")),
                    entry_price=None if entry_price is None else round(entry_price, 4),
                    current_price=None if current_price is None else round(current_price, 4),
                    return_pct=return_pct,
                    original_pct_change=_optional_num(score.get("pct_change")),
                    current_pct_change=None if snapshot is None else round(snapshot.pct_change, 2),
                    outcome=_classify_outcome(label, return_pct),
                )
            )
    return sorted(
        outcomes,
        key=lambda item: (-999.0 if item.return_pct is None else item.return_pct),
        reverse=True,
    )


def save_alpha_outcome_report(
    outcomes: list[AlphaOutcome],
    output_dir: str | Path = REPORT_DIR,
) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"alpha_outcome_{stamp}.csv"
    md_path = out / f"alpha_outcome_{stamp}.md"
    pd.DataFrame([asdict(outcome) for outcome in outcomes]).to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(alpha_outcome_markdown(outcomes), encoding="utf-8")
    return csv_path, md_path


def summarize_alpha_feedback(
    outcomes: list[AlphaOutcome],
    min_samples: int = 5,
) -> list[AlphaFeedback]:
    """Summarize logged outcome quality by alpha label."""
    groups: dict[str, list[AlphaOutcome]] = {}
    for outcome in outcomes:
        if outcome.return_pct is None:
            continue
        groups.setdefault(outcome.label, []).append(outcome)
    feedback: list[AlphaFeedback] = []
    for label, items in groups.items():
        returns = [item.return_pct for item in items if item.return_pct is not None]
        if not returns:
            continue
        avg_return = sum(returns) / len(returns)
        win_rate = len([value for value in returns if value > 0]) / len(returns)
        loss_rate = len([value for value in returns if value < 0]) / len(returns)
        verdict, adjustment = _feedback_decision(label, len(returns), avg_return, win_rate, loss_rate, min_samples)
        feedback.append(
            AlphaFeedback(
                label=label,
                count=len(returns),
                avg_return_pct=round(avg_return, 2),
                win_rate=round(win_rate, 3),
                loss_rate=round(loss_rate, 3),
                verdict=verdict,
                suggested_adjustment=adjustment,
            )
        )
    return sorted(feedback, key=lambda item: (item.count, item.avg_return_pct or 0), reverse=True)


def save_alpha_feedback_report(
    feedback: list[AlphaFeedback],
    output_dir: str | Path = REPORT_DIR,
) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"alpha_feedback_{stamp}.csv"
    md_path = out / f"alpha_feedback_{stamp}.md"
    pd.DataFrame([asdict(item) for item in feedback]).to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(alpha_feedback_markdown(feedback), encoding="utf-8")
    return csv_path, md_path


def alpha_feedback_markdown(feedback: list[AlphaFeedback]) -> str:
    lines = [
        "# A股事件Alpha参数反馈",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 模型版本：{ALPHA_MODEL_VERSION}",
        "- 说明：按历史信号标签统计后续收益，样本不足时只提示观察，不自动改参数。",
        "",
    ]
    if not feedback:
        lines.append("暂无可用反馈样本。")
        return "\n".join(lines) + "\n"
    rows = [
        {
            "标签": item.label,
            "样本": item.count,
            "均值%": item.avg_return_pct,
            "胜率": item.win_rate,
            "亏损率": item.loss_rate,
            "结论": item.verdict,
            "建议": item.suggested_adjustment,
        }
        for item in feedback
    ]
    lines.append(pd.DataFrame(rows).to_markdown(index=False))
    lines.append("")
    return "\n".join(lines)


def alpha_outcome_markdown(outcomes: list[AlphaOutcome]) -> str:
    lines = [
        "# A股事件信号结果评估",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 模型版本：{ALPHA_MODEL_VERSION}",
        "- 用已记录的信号价格对比当前ETF快照，判断进攻/拥挤标签是否需要调参。",
        "",
    ]
    if not outcomes:
        lines.append("无可评估信号。")
        return "\n".join(lines) + "\n"
    rows = [
        {
            "信号": outcome.signal_id,
            "时间": outcome.created_at,
            "标的": outcome.name,
            "代码": outcome.symbol,
            "原标签": outcome.label,
            "原分": outcome.original_score,
            "入价": outcome.entry_price,
            "现价": outcome.current_price,
            "收益%": outcome.return_pct,
            "结果": outcome.outcome,
        }
        for outcome in outcomes
    ]
    lines.append(pd.DataFrame(rows).to_markdown(index=False))
    lines.append("")
    return "\n".join(lines)


def score_manual_event(text: str, threshold: float = 7.0) -> list[CatalystHit]:
    return scan_catalysts([NewsItem(title=text, source="manual")], threshold=threshold)


def save_alpha_report(scores: list[AlphaScore], output_dir: str | Path = REPORT_DIR) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"alpha_stack_{stamp}.csv"
    md_path = out / f"alpha_stack_{stamp}.md"
    pd.DataFrame([asdict(score) for score in scores]).to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(alpha_markdown(scores), encoding="utf-8")
    return csv_path, md_path


def alpha_markdown(scores: list[AlphaScore]) -> str:
    lines = [
        "# A股事件信号栈",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 模型版本：{ALPHA_MODEL_VERSION}",
        "- 分数 = 催化剂相关性 + 市场确认 + 流动性 - 拥挤惩罚。",
        (
            "- 权重："
            f"催化{ALPHA_WEIGHTS['catalyst']:.2f}，"
            f"反应{ALPHA_WEIGHTS['reaction']:.2f}，"
            f"流动性{ALPHA_WEIGHTS['liquidity']:.2f}，"
            f"拥挤{ALPHA_WEIGHTS['crowding']:.2f}。"
        ),
        "",
    ]
    if not scores:
        lines.append("无评分结果。")
        return "\n".join(lines) + "\n"
    rows = [
        {
            "标的": score.name,
            "代码": score.symbol,
            "总分": score.total_score,
            "标签": score.label,
            "涨跌%": score.pct_change,
            "最新价": score.latest,
            "执行": score.playbook,
            "催化": score.catalyst_score,
            "反应": score.reaction_score,
            "拥挤": score.crowding_penalty,
            "理由": score.reason,
        }
        for score in scores
    ]
    lines.append(pd.DataFrame(rows).to_markdown(index=False))
    lines.append("")
    return "\n".join(lines)


def _close_location(snapshot: MarketSnapshot) -> float:
    if snapshot.high <= snapshot.low:
        return 0.0
    return max(min((snapshot.latest - snapshot.low) / (snapshot.high - snapshot.low), 1.0), 0.0)


def _snapshot_from_dict(item: dict) -> MarketSnapshot:
    return MarketSnapshot(
        target_id=str(item.get("target_id", "")),
        name=str(item.get("name", "")),
        symbol=str(item.get("symbol", "")),
        pct_change=_num(item.get("pct_change")),
        amount=_num(item.get("amount")),
        open_price=_num(item.get("open_price")),
        high=_num(item.get("high")),
        low=_num(item.get("low")),
        latest=_num(item.get("latest")),
        main_net_inflow=_optional_num(item.get("main_net_inflow")),
        turnover=_optional_num(item.get("turnover")),
        data_time=str(item.get("data_time", "")),
    )


def _load_alpha_log_records(path: Path) -> list[dict]:
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _event_hash(event_text: str) -> str:
    normalized = " ".join(str(event_text).split()).lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _has_recent_duplicate(path: Path, event_hash: str, created_at: str, dedupe_minutes: int) -> bool:
    if not path.exists():
        return False
    try:
        current_time = datetime.fromisoformat(created_at)
    except ValueError:
        current_time = datetime.now()
    for record in reversed(_load_alpha_log_records(path)[-50:]):
        if record.get("event_hash") != event_hash:
            continue
        try:
            previous_time = datetime.fromisoformat(str(record.get("created_at", "")))
        except ValueError:
            return True
        if abs((current_time - previous_time).total_seconds()) <= dedupe_minutes * 60:
            return True
    return False


def _return_pct(entry_price: float | None, current_price: float | None) -> float | None:
    if entry_price is None or current_price is None or entry_price <= 0:
        return None
    return round((current_price / entry_price - 1.0) * 100.0, 2)


def _classify_outcome(label: str, return_pct: float | None) -> str:
    if return_pct is None:
        return "数据缺失"
    if "高位拥挤" in label:
        if return_pct < 0:
            return "拥挤警报有效"
        if return_pct >= 2:
            return "可能惩罚过重"
        return "拥挤后震荡"
    if label in {"主线确认", "候选进攻"}:
        if return_pct >= 2:
            return "进攻有效"
        if return_pct <= -2:
            return "进攻失效"
        return "进攻待验证"
    if label == "催化未确认" and return_pct >= 2:
        return "低估未确认信号"
    if return_pct <= -2:
        return "回避有效"
    return "中性"


def _feedback_decision(
    label: str,
    count: int,
    avg_return: float,
    win_rate: float,
    loss_rate: float,
    min_samples: int,
) -> tuple[str, str]:
    if count < min_samples:
        return "样本不足", f"至少需要{min_samples}个样本后再调参"
    if abs(avg_return) < 0.2 and win_rate == 0 and loss_rate == 0:
        return "价格未变化", "等待下一交易日价格变化后再调参"
    if label in {"主线确认", "候选进攻"}:
        if avg_return > 1.0 and win_rate >= 0.55:
            return "进攻有效", "保持进攻阈值，重点复核容量和回撤"
        if avg_return < -0.8 or loss_rate >= 0.55:
            return "进攻偏松", "提高反应分阈值或加大拥挤惩罚"
        return "进攻中性", "保持参数，继续收样本"
    if "高位拥挤" in label:
        if avg_return < -0.8:
            return "拥挤惩罚有效", "保持或略提高高位惩罚权重"
        if avg_return > 1.0 and win_rate >= 0.55:
            return "惩罚可能过重", "降低高位惩罚或加入强趋势例外"
        return "拥挤待验证", "继续观察分歧后表现"
    if label in {"观察", "催化未确认"}:
        if avg_return > 1.0 and win_rate >= 0.55:
            return "低估观察信号", "提高催化未确认向候选进攻迁移的权重"
        return "观察有效", "维持观察标签"
    if avg_return < 0:
        return "回避有效", "维持低分过滤"
    return "回避待验证", "检查是否漏掉低位补涨信号"


def _num(value) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _optional_num(value) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None
