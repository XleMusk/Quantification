"""One-command catalyst-to-alpha radar for A-share themes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from .alpha_stack import AlphaScore, MarketSnapshot, alpha_markdown, rank_event_reactions
from .catalyst_monitor import CatalystHit, catalyst_markdown
from .intraday_breadth import IntradayBreadth
from .market_regime import MarketRegime
from .trend_calibration import KNOWN_TARGETS, TrendTarget


REPORT_DIR = Path("committee_reviews")


def build_event_radar(
    hits: list[CatalystHit],
    snapshots: list[MarketSnapshot],
    targets: list[TrendTarget] | tuple[TrendTarget, ...] = KNOWN_TARGETS,
    top_events: int = 3,
) -> tuple[list[CatalystHit], list[AlphaScore]]:
    """Convert top catalysts into ranked ETF/theme alpha candidates."""
    selected_hits = hits[:top_events] if top_events > 0 else hits
    scores = rank_event_reactions(selected_hits, snapshots, targets=targets)
    return selected_hits, scores


def save_event_radar_report(
    hits: list[CatalystHit],
    scores: list[AlphaScore],
    regime: MarketRegime | None = None,
    breadth: IntradayBreadth | None = None,
    output_dir: str | Path = REPORT_DIR,
) -> tuple[Path, Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    hits_csv = out / f"event_radar_hits_{stamp}.csv"
    scores_csv = out / f"event_radar_scores_{stamp}.csv"
    md_path = out / f"event_radar_{stamp}.md"

    hits_df = pd.DataFrame([asdict(hit) for hit in hits])
    if not hits_df.empty:
        hits_df["matched_keywords"] = hits_df["matched_keywords"].apply(",".join)
        hits_df["affected_targets"] = hits_df["affected_targets"].apply(",".join)
        hits_df["confirmation_sources"] = hits_df["confirmation_sources"].apply(
            lambda value: ",".join(value or []) if isinstance(value, list) else ""
        )
    hits_df.to_csv(hits_csv, index=False, encoding="utf-8-sig")
    pd.DataFrame([asdict(score) for score in scores]).to_csv(scores_csv, index=False, encoding="utf-8-sig")
    md_path.write_text(event_radar_markdown(hits, scores, regime=regime, breadth=breadth), encoding="utf-8")
    return hits_csv, scores_csv, md_path


def event_radar_markdown(
    hits: list[CatalystHit],
    scores: list[AlphaScore],
    regime: MarketRegime | None = None,
    breadth: IntradayBreadth | None = None,
) -> str:
    lines = [
        "# A股事件雷达",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 流程：催化剂扫描 -> 来源/新鲜度/确认度加权 -> ETF代理实时反应 -> 拥挤惩罚。",
        "",
    ]
    if regime is not None:
        lines.extend(
            [
                "## 市场环境闸门",
                "",
                f"- 环境：{regime.label}",
                f"- 风险闸门：{regime.risk_gate}",
                f"- 执行偏向：{regime.execution_bias}",
                "",
            ]
        )
    if breadth is not None:
        lines.extend(
            [
                "## 盘中修复强度",
                "",
                f"- 结论：{breadth.label}",
                (
                    f"- 上涨占比：{breadth.positive_ratio:.1%}，"
                    f"平均涨幅：{breadth.avg_pct_change:.2f}%，"
                    f"强涨数量：{breadth.strong_count}/{breadth.total}"
                ),
                f"- 风险闸门：{breadth.risk_gate}",
                "",
            ]
        )
    lines.append("## 催化剂")
    lines.append("")
    if hits:
        lines.append(_top_catalysts_table(hits))
    else:
        lines.append("暂无高分催化剂。")
    lines.append("")
    lines.append("## Alpha候选")
    lines.append("")
    if scores:
        lines.append(_alpha_table(scores, regime=regime))
    else:
        lines.append("暂无Alpha评分。")
    lines.append("")
    lines.append("## 原始子报告")
    lines.append("")
    lines.append(catalyst_markdown(hits).strip())
    lines.append("")
    lines.append(alpha_markdown(scores).strip())
    lines.append("")
    return "\n".join(lines)


def _top_catalysts_table(hits: list[CatalystHit]) -> str:
    rows = [
        {
            "事件": hit.event,
            "分数": hit.score,
            "来源分": hit.source_score,
            "新鲜度": hit.freshness_score,
            "确认": hit.confirmation_count,
            "来源": hit.source,
            "标题": hit.title,
            "受益代理": ",".join(hit.affected_targets[:6]),
        }
        for hit in hits
    ]
    return pd.DataFrame(rows).to_markdown(index=False)


def _alpha_table(scores: list[AlphaScore], regime: MarketRegime | None = None) -> str:
    rows = [
        {
            "标的": score.name,
            "代码": score.symbol,
            "总分": score.total_score,
            "标签": score.label,
            "涨跌%": score.pct_change,
            "最新价": score.latest,
            "拥挤": score.crowding_penalty,
            "执行": score.playbook,
            "环境修正": regime_adjusted_playbook(score, regime),
            "理由": score.reason,
        }
        for score in scores[:12]
    ]
    return pd.DataFrame(rows).to_markdown(index=False)


def regime_adjusted_playbook(score: AlphaScore, regime: MarketRegime | None) -> str:
    if regime is None:
        return ""
    if regime.label == "弱修复":
        if "高位拥挤" in score.label:
            return "环境弱修复，继续等分歧，不做追高"
        if score.label in {"主线确认", "候选进攻"}:
            return "环境弱修复，进攻降一档，只等回踩承接"
        return "环境弱修复，观察优先"
    if regime.label == "强修复":
        if score.label in {"主线确认", "候选进攻"} and score.crowding_penalty < 0.85:
            return "环境强修复，可提高跟踪优先级"
        if "高位拥挤" in score.label:
            return "强修复但高拥挤，仍等分歧"
    if regime.label == "结构强修复":
        if score.label in {"主线确认", "候选进攻"}:
            return "结构强修复，只聚焦最强科技分支"
    if regime.label in {"退潮防守", "数据不足"}:
        return "环境不支持进攻，先防守或人工复核"
    return "按原执行剧本"
