"""Market regime classifier for A-share event trading."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from .trend_calibration import KNOWN_TARGETS, TrendResult


REPORT_DIR = Path("committee_reviews")
BROAD_IDS = {"overall", "all_a", "chinext", "star50"}
TECH_TAGS = {
    "semiconductor",
    "advanced_chip",
    "communication",
    "cpo",
    "copper_interconnect",
    "consumer_electronics",
    "electronics",
    "ai_hardware",
    "server_odm",
}


@dataclass
class MarketRegime:
    label: str
    score: float
    broad_score: float
    tech_score: float
    broad_positive_count: int
    tech_positive_count: int
    risk_gate: str
    execution_bias: str
    diagnostics: dict[str, float | int | str]


def classify_market_regime(results: list[TrendResult]) -> MarketRegime:
    """Classify whether the current market is weak repair, strong repair, or defensive."""
    valid = [result for result in results if result.error is None]
    if len(valid) < 5:
        return MarketRegime(
            label="数据不足",
            score=0.0,
            broad_score=0.0,
            tech_score=0.0,
            broad_positive_count=0,
            tech_positive_count=0,
            risk_gate="不输出环境结论，先补足历史数据",
            execution_bias="只允许人工复核，不使用环境闸门",
            diagnostics={
                "valid_targets": len(valid),
                "required_valid_targets": 5,
                "error_targets": len(results) - len(valid),
            },
        )
    target_map = {target.target_id: target for target in KNOWN_TARGETS}
    broad = [result for result in valid if result.target_id in BROAD_IDS]
    tech = [
        result
        for result in valid
        if TECH_TAGS.intersection(target_map.get(result.target_id, target_map["overall"]).tags)
        and result.target_id not in BROAD_IDS
    ]
    broad_score = _avg([result.score for result in broad])
    tech_score = _avg([result.score for result in tech])
    broad_positive = len([result for result in broad if result.signal == "看多"])
    tech_positive = len([result for result in tech if result.signal == "看多"])
    broad_negative = len([result for result in broad if result.signal == "看空"])
    tech_negative = len([result for result in tech if result.signal == "看空"])
    composite = broad_score * 0.55 + tech_score * 0.45
    label = _regime_label(broad_score, tech_score, broad_positive, tech_positive, broad_negative, tech_negative)
    risk_gate, execution_bias = _risk_gate(label)
    diagnostics = {
        "valid_targets": len(valid),
        "broad_count": len(broad),
        "tech_count": len(tech),
        "broad_negative_count": broad_negative,
        "tech_negative_count": tech_negative,
    }
    return MarketRegime(
        label=label,
        score=round(composite, 3),
        broad_score=round(broad_score, 3),
        tech_score=round(tech_score, 3),
        broad_positive_count=broad_positive,
        tech_positive_count=tech_positive,
        risk_gate=risk_gate,
        execution_bias=execution_bias,
        diagnostics=diagnostics,
    )


def save_market_regime_report(
    regime: MarketRegime,
    results: list[TrendResult],
    output_dir: str | Path = REPORT_DIR,
) -> tuple[Path, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out / f"market_regime_{stamp}.json"
    md_path = out / f"market_regime_{stamp}.md"
    payload = {
        "regime": asdict(regime),
        "targets": [asdict(result) for result in results],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(market_regime_markdown(regime, results), encoding="utf-8")
    return json_path, md_path


def load_latest_market_regime(report_dir: str | Path = REPORT_DIR) -> MarketRegime | None:
    paths = sorted(Path(report_dir).glob("market_regime_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        return None
    try:
        payload = json.loads(paths[0].read_text(encoding="utf-8"))
        data = payload.get("regime", {})
        return MarketRegime(
            label=str(data.get("label", "")),
            score=float(data.get("score", 0.0)),
            broad_score=float(data.get("broad_score", 0.0)),
            tech_score=float(data.get("tech_score", 0.0)),
            broad_positive_count=int(data.get("broad_positive_count", 0)),
            tech_positive_count=int(data.get("tech_positive_count", 0)),
            risk_gate=str(data.get("risk_gate", "")),
            execution_bias=str(data.get("execution_bias", "")),
            diagnostics=dict(data.get("diagnostics", {})),
        )
    except Exception:
        return None


def market_regime_markdown(regime: MarketRegime, results: list[TrendResult]) -> str:
    lines = [
        "# A股市场环境闸门",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 环境：{regime.label}",
        f"- 综合分：{regime.score:.3f}，宽基分：{regime.broad_score:.3f}，科技分：{regime.tech_score:.3f}",
        f"- 风险闸门：{regime.risk_gate}",
        f"- 执行偏向：{regime.execution_bias}",
        "",
        "## 目标明细",
        "",
    ]
    rows = [
        {
            "标的": result.name,
            "代码": result.symbol,
            "信号": result.signal,
            "分数": result.score,
            "训练命中": result.train_accuracy,
            "最新日": result.latest_date,
            "错误": result.error or "",
        }
        for result in results
    ]
    lines.append(pd.DataFrame(rows).to_markdown(index=False))
    lines.append("")
    return "\n".join(lines)


def _regime_label(
    broad_score: float,
    tech_score: float,
    broad_positive: int,
    tech_positive: int,
    broad_negative: int,
    tech_negative: int,
) -> str:
    if broad_score <= -0.08 and tech_score <= -0.05 and broad_negative >= 2:
        return "退潮防守"
    if broad_score >= 0.10 and tech_score >= 0.10 and broad_positive >= 2 and tech_positive >= 4:
        return "强修复"
    if broad_score >= -0.02 and tech_score >= 0.12 and tech_positive >= 4:
        return "结构强修复"
    if tech_score >= 0.04 and tech_positive >= 3:
        return "弱修复"
    if broad_negative >= 2 or tech_negative >= 4:
        return "分歧震荡"
    return "中性震荡"


def _risk_gate(label: str) -> tuple[str, str]:
    if label == "强修复":
        return "允许小仓位进攻，仍禁止无承接追高", "优先主线ETF和低拥挤确认分支"
    if label == "结构强修复":
        return "允许结构性进攻，宽基不配合时降低仓位", "只做科技主线中资金确认最强的分支"
    if label == "弱修复":
        return "降低追涨权限，优先等分歧低吸", "强催化可观察，避免大涨后追高"
    if label == "分歧震荡":
        return "只做试错，不做连续加仓", "重视日内承接和次日反馈"
    if label == "退潮防守":
        return "防守为主，事件反弹也按反抽处理", "控制仓位，优先现金和复盘"
    return "中性闸门，等待方向选择", "只跟踪最清晰主线"


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)
