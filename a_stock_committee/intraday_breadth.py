"""Fast intraday repair-strength classifier from ETF spot snapshots."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from .alpha_stack import MarketSnapshot


REPORT_DIR = Path("committee_reviews")


@dataclass
class IntradayBreadth:
    label: str
    total: int
    positive_count: int
    strong_count: int
    hot_count: int
    negative_flow_count: int
    positive_ratio: float
    avg_pct_change: float
    median_pct_change: float
    total_amount: float
    main_net_inflow: float | None
    risk_gate: str
    diagnostics: dict[str, float | int | str]


def classify_intraday_breadth(snapshots: list[MarketSnapshot]) -> IntradayBreadth:
    total = len(snapshots)
    if total == 0:
        return IntradayBreadth(
            label="数据不足",
            total=0,
            positive_count=0,
            strong_count=0,
            hot_count=0,
            negative_flow_count=0,
            positive_ratio=0.0,
            avg_pct_change=0.0,
            median_pct_change=0.0,
            total_amount=0.0,
            main_net_inflow=None,
            risk_gate="没有实时快照，不输出盘中修复强度",
            diagnostics={},
        )
    pct_values = [snapshot.pct_change for snapshot in snapshots]
    positive_count = len([value for value in pct_values if value > 0])
    strong_count = len([value for value in pct_values if value >= 3.0])
    hot_count = len([value for value in pct_values if value >= 5.0])
    negative_flow_count = len(
        [
            snapshot
            for snapshot in snapshots
            if snapshot.main_net_inflow is not None and snapshot.main_net_inflow < 0 and snapshot.pct_change > 2.0
        ]
    )
    flows = [snapshot.main_net_inflow for snapshot in snapshots if snapshot.main_net_inflow is not None]
    total_flow = None if not flows else sum(flows)
    positive_ratio = positive_count / total
    avg_pct = sum(pct_values) / total
    median_pct = float(pd.Series(pct_values).median())
    total_amount = sum(snapshot.amount for snapshot in snapshots)
    label = _breadth_label(positive_ratio, avg_pct, strong_count, hot_count, negative_flow_count, total)
    risk_gate = _breadth_risk_gate(label)
    return IntradayBreadth(
        label=label,
        total=total,
        positive_count=positive_count,
        strong_count=strong_count,
        hot_count=hot_count,
        negative_flow_count=negative_flow_count,
        positive_ratio=round(positive_ratio, 3),
        avg_pct_change=round(avg_pct, 2),
        median_pct_change=round(median_pct, 2),
        total_amount=round(total_amount, 0),
        main_net_inflow=None if total_flow is None else round(total_flow, 0),
        risk_gate=risk_gate,
        diagnostics={
            "hot_ratio": round(hot_count / total, 3),
            "strong_ratio": round(strong_count / total, 3),
            "negative_flow_ratio": round(negative_flow_count / total, 3),
        },
    )


def save_intraday_breadth_report(
    breadth: IntradayBreadth,
    snapshots: list[MarketSnapshot],
    output_dir: str | Path = REPORT_DIR,
) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"intraday_breadth_{stamp}.csv"
    md_path = out / f"intraday_breadth_{stamp}.md"
    pd.DataFrame([asdict(snapshot) for snapshot in snapshots]).to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(intraday_breadth_markdown(breadth, snapshots), encoding="utf-8")
    return csv_path, md_path


def intraday_breadth_markdown(breadth: IntradayBreadth, snapshots: list[MarketSnapshot]) -> str:
    lines = [
        "# A股盘中修复强度",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 结论：{breadth.label}",
        f"- 上涨占比：{breadth.positive_ratio:.1%}，平均涨幅：{breadth.avg_pct_change:.2f}%，中位涨幅：{breadth.median_pct_change:.2f}%",
        f"- 强涨数量：{breadth.strong_count}/{breadth.total}，高热数量：{breadth.hot_count}/{breadth.total}",
        f"- 风险闸门：{breadth.risk_gate}",
        "",
    ]
    if snapshots:
        rows = [
            {
                "标的": snapshot.name,
                "代码": snapshot.symbol,
                "涨跌%": snapshot.pct_change,
                "成交额": round(snapshot.amount / 100000000, 2),
                "主力净流入": None
                if snapshot.main_net_inflow is None
                else round(snapshot.main_net_inflow / 100000000, 2),
                "换手": snapshot.turnover,
            }
            for snapshot in sorted(snapshots, key=lambda item: item.pct_change, reverse=True)
        ]
        lines.append(pd.DataFrame(rows).to_markdown(index=False))
        lines.append("")
    return "\n".join(lines)


def _breadth_label(
    positive_ratio: float,
    avg_pct: float,
    strong_count: int,
    hot_count: int,
    negative_flow_count: int,
    total: int,
) -> str:
    if positive_ratio >= 0.75 and avg_pct >= 2.5 and strong_count >= max(3, total // 3):
        if hot_count >= max(3, total // 4) or negative_flow_count >= max(2, total // 5):
            return "强修复但高拥挤"
        return "强修复"
    if positive_ratio >= 0.55 and avg_pct >= 0.8 and strong_count >= 1:
        return "弱修复"
    if positive_ratio >= 0.45 and avg_pct > -0.2:
        return "分歧震荡"
    return "退潮/弱反抽"


def _breadth_risk_gate(label: str) -> str:
    if label == "强修复":
        return "允许跟踪前排主线，但仍用回踩承接确认"
    if label == "强修复但高拥挤":
        return "强度够但拥挤高，禁止追高，等分歧换手"
    if label == "弱修复":
        return "只按弱修复处理，优先低吸和观察补涨"
    if label == "分歧震荡":
        return "不扩大仓位，等待方向选择"
    if label == "数据不足":
        return "没有实时快照，不输出盘中修复强度"
    return "防守优先，反弹按反抽处理"
