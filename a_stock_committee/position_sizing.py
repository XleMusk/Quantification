"""A-share aware position sizing utilities."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class PositionSizingResult:
    schema_version: str
    generated_at: str
    account_equity: float
    entry_price: float
    stop_price: float
    risk_pct: float
    risk_per_share: float
    risk_budget: float
    max_position_value: float
    raw_shares_by_risk: int
    final_shares: int
    final_position_value: float
    final_risk_amount: float
    final_risk_pct: float
    binding_constraint: str
    notes: list[str]


def calculate_a_share_position(
    account_equity: float,
    entry_price: float,
    stop_price: float | None = None,
    stop_pct: float | None = None,
    risk_pct: float = 0.5,
    max_position_pct: float = 5,
    lot_size: int = 100,
) -> PositionSizingResult:
    """Calculate a conservative long-stock position for A-shares."""
    if account_equity <= 0:
        raise ValueError("account_equity must be positive")
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")

    risk_fraction = parse_pct(risk_pct)
    max_position_fraction = parse_pct(max_position_pct)
    if risk_fraction <= 0:
        raise ValueError("risk_pct must be positive")
    if max_position_fraction <= 0:
        raise ValueError("max_position_pct must be positive")

    if stop_price is None:
        if stop_pct is None:
            raise ValueError("provide stop_price or stop_pct")
        stop_price = entry_price * (1 - parse_pct(stop_pct))
    if stop_price <= 0 or stop_price >= entry_price:
        raise ValueError("stop_price must be below entry_price for a long trade")

    risk_per_share = entry_price - stop_price
    risk_budget = account_equity * risk_fraction
    raw_shares_by_risk = math.floor(risk_budget / risk_per_share)

    max_position_value = account_equity * max_position_fraction
    raw_shares_by_position = math.floor(max_position_value / entry_price)

    shares_before_lot = min(raw_shares_by_risk, raw_shares_by_position)
    final_shares = math.floor(shares_before_lot / lot_size) * lot_size
    final_position_value = final_shares * entry_price
    final_risk_amount = final_shares * risk_per_share
    final_risk_pct = final_risk_amount / account_equity if account_equity else 0.0

    if raw_shares_by_risk <= raw_shares_by_position:
        binding = "risk_budget"
    else:
        binding = "max_position_pct"
    if final_shares == 0:
        binding = "lot_size_or_capital"

    notes = [
        "A股买入按100股整数手向下取整。",
        "该计算只处理多头股票，不处理融资融券、ETF期权或可转债细节。",
        "真实下单前仍需检查ST、停牌、涨跌停、可用资金和流动性。",
    ]

    return PositionSizingResult(
        schema_version="a-share-position-sizing/v1",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        account_equity=round(account_equity, 2),
        entry_price=round(entry_price, 4),
        stop_price=round(stop_price, 4),
        risk_pct=round(risk_fraction * 100, 4),
        risk_per_share=round(risk_per_share, 4),
        risk_budget=round(risk_budget, 2),
        max_position_value=round(max_position_value, 2),
        raw_shares_by_risk=raw_shares_by_risk,
        final_shares=final_shares,
        final_position_value=round(final_position_value, 2),
        final_risk_amount=round(final_risk_amount, 2),
        final_risk_pct=round(final_risk_pct * 100, 4),
        binding_constraint=binding,
        notes=notes,
    )


def parse_pct(value: float | int | str) -> float:
    """Parse user-facing percent values.

    Convention: 5 means 5%, "5%" means 5%, 0.05 means 5%, and 0.5 means 0.5%.
    """
    text = str(value).strip()
    explicit = text.endswith("%")
    if explicit:
        text = text[:-1]
    number = float(text)
    if explicit or number > 1 or number > 0.2:
        return number / 100.0
    return number


def result_to_markdown(result: PositionSizingResult) -> str:
    """Format a position sizing result."""
    return "\n".join(
        [
            "# A股仓位计算",
            "",
            f"- 账户资产：{result.account_equity:,.2f}",
            f"- 入场价：{result.entry_price:.4f}",
            f"- 止损价：{result.stop_price:.4f}",
            f"- 单笔风险预算：{result.risk_pct:.4f}%",
            f"- 单股风险：{result.risk_per_share:.4f}",
            f"- 风险预算金额：{result.risk_budget:,.2f}",
            f"- 单票仓位金额上限：{result.max_position_value:,.2f}",
            f"- 风险预算允许股数：{result.raw_shares_by_risk:,}",
            f"- 最终股数：**{result.final_shares:,}股**",
            f"- 最终持仓金额：{result.final_position_value:,.2f}",
            f"- 最终亏损风险：{result.final_risk_amount:,.2f}（{result.final_risk_pct:.4f}%）",
            f"- 约束来源：{result.binding_constraint}",
            "",
            "## 注意",
            "",
            *[f"- {note}" for note in result.notes],
            "",
        ]
    )


def save_position_report(result: PositionSizingResult, output_dir: str | Path) -> tuple[Path, Path]:
    """Save JSON and Markdown reports."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out / f"position_sizer_{stamp}.json"
    md_path = out / f"position_sizer_{stamp}.md"
    json_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(result_to_markdown(result), encoding="utf-8")
    return json_path, md_path


def load_result(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

