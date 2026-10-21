"""Core review engine for the A-share stock committee learning system."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .profiles import COMMITTEE_PROFILES


DEFAULT_THESIS: dict[str, Any] = {
    "date": "",
    "action": "buy",
    "stock_name": "",
    "code": "",
    "time_horizon": "3-12 months",
    "current_price": None,
    "account_equity": None,
    "planned_capital_pct": None,
    "planned_stop_loss_pct": None,
    "business": "",
    "buy_reason": "",
    "field_research": "",
    "market_context": "",
    "consensus_view": "",
    "variant_view": "",
    "benchmark": "沪深300/中证500/行业指数",
    "edge": "",
    "catalysts": [],
    "valuation": {
        "pe": None,
        "pb": None,
        "growth_assumption": "",
        "comparison": "",
    },
    "fundamentals": {
        "revenue_growth": "",
        "profit_growth": "",
        "roe": "",
        "debt": "",
        "cashflow": "",
    },
    "technical": {
        "trend": "",
        "relative_strength": "",
        "volume": "",
        "key_levels": "",
    },
    "risks": [],
    "sell_rules": {
        "wrong": "",
        "expensive": "",
        "changed": "",
        "time_stop": "",
    },
    "risk_limits": {
        "max_single_position_pct": 5,
        "max_total_stock_pct": 80,
        "cash_reserve_pct": 10,
        "max_loss_per_trade_pct": 0.5,
        "max_daily_loss_pct": 1,
    },
    "a_share_constraints": {
        "st": None,
        "halted": None,
        "limit_status": "unknown",
        "board_permission": "",
        "liquidity": "",
        "t_plus_one_notes": "",
    },
    "data_checked": [],
    "notes": "",
}


SAMPLE_THESIS: dict[str, Any] = {
    **deepcopy(DEFAULT_THESIS),
    "date": datetime.now().strftime("%Y-%m-%d"),
    "action": "watch",
    "stock_name": "示例股份",
    "code": "600000.SSE",
    "current_price": 10.0,
    "account_equity": 100000,
    "planned_capital_pct": 3,
    "planned_stop_loss_pct": 8,
    "business": "示例：银行业务，主要收入来自净利息收入和手续费收入。",
    "buy_reason": "示例：估值处于历史较低区间，股息率有吸引力，价格重新站上中期均线。",
    "field_research": "示例：只做了公开资料检查，还没有一线调研。",
    "market_context": "示例：低利率环境下，高股息资产受到关注。",
    "consensus_view": "示例：市场担心净息差收窄和地产风险。",
    "variant_view": "示例：认为资产质量风险已经部分反映在估值中。",
    "edge": "示例：暂时没有明显信息优势，只能作为低仓位观察。",
    "catalysts": ["年报分红方案", "资产质量改善", "行业估值修复"],
    "valuation": {
        "pe": 6.5,
        "pb": 0.55,
        "growth_assumption": "低增长，高分红。",
        "comparison": "PB低于过去五年中位数，也低于部分同业。",
    },
    "fundamentals": {
        "revenue_growth": "低个位数增长。",
        "profit_growth": "利润增速稳定但不高。",
        "roe": "约10%。",
        "debt": "银行需重点看资本充足率和不良率。",
        "cashflow": "金融企业现金流指标需与普通制造业区别看待。",
    },
    "technical": {
        "trend": "中期横盘后尝试向上。",
        "relative_strength": "相对沪深300略强。",
        "volume": "突破日成交温和放大。",
        "key_levels": "9.2为失效位，11.5附近观察压力。",
    },
    "risks": ["净息差继续下行", "地产链风险暴露", "股价长期低估不修复"],
    "sell_rules": {
        "wrong": "跌破9.2且无法收回，或财报显示资产质量恶化。",
        "expensive": "估值回到五年高位且股息赔率下降。",
        "changed": "分红政策、资产质量或监管环境显著恶化。",
        "time_stop": "6个月内没有兑现催化且跑输基准，重新评估。",
    },
    "a_share_constraints": {
        "st": False,
        "halted": False,
        "limit_status": "normal",
        "board_permission": "主板普通权限",
        "liquidity": "日均成交额充足，当前计划仓位影响很小。",
        "t_plus_one_notes": "买入后当天不可卖出，按次日风控处理。",
    },
    "data_checked": ["年报", "季报", "估值分位", "行业指数", "成交量"],
}


def create_empty_thesis() -> dict[str, Any]:
    """Return a fresh thesis template."""
    thesis = deepcopy(DEFAULT_THESIS)
    thesis["date"] = datetime.now().strftime("%Y-%m-%d")
    return thesis


def create_sample_thesis() -> dict[str, Any]:
    """Return a filled example thesis."""
    return deepcopy(SAMPLE_THESIS)


def load_thesis(path: str | Path) -> dict[str, Any]:
    """Load a thesis JSON file."""
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    return merge_defaults(data)


def save_thesis(thesis: dict[str, Any], path: str | Path) -> Path:
    """Save a thesis JSON file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        json.dump(thesis, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return target


def merge_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Merge user data onto the default thesis shape."""
    base = create_empty_thesis()
    return _deep_merge(base, data)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def generate_review_markdown(thesis: dict[str, Any]) -> str:
    """Generate the full committee review as Markdown."""
    thesis = merge_defaults(thesis)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = _display_title(thesis)
    completeness = evaluate_completeness(thesis)
    red_flags = evaluate_red_flags(thesis)
    position = calculate_position_plan(thesis, red_flags)
    profile_reviews = [review_profile(profile, thesis) for profile in COMMITTEE_PROFILES]
    decision = make_decision(completeness, red_flags, position, profile_reviews)

    lines: list[str] = [
        f"# A股大师投委会审查报告：{title}",
        "",
        f"- 生成时间：{now}",
        f"- 动作：{thesis.get('action') or '未填写'}",
        f"- 周期：{thesis.get('time_horizon') or '未填写'}",
        f"- 结论：**{decision['label']}**",
        "",
        "> 这是学习和复盘工具，不是投资建议。它的工作是帮你暴露盲区、定义风险和训练纪律。",
        "",
        "## 一页结论",
        "",
        decision["summary"],
        "",
        "## 红灯与硬约束",
        "",
    ]

    if red_flags:
        lines.extend([f"- {flag}" for flag in red_flags])
    else:
        lines.append("- 暂未发现必须立即否决的硬约束，但仍需完成下方证据清单。")

    lines.extend(
        [
            "",
            "## 仓位与风险预算",
            "",
            position["markdown"],
            "",
            "## 资料完整度",
            "",
            f"- 完整度：{completeness['score']:.0f}/100",
        ]
    )

    if completeness["missing"]:
        lines.append("- 缺失项：")
        lines.extend([f"  - {item}" for item in completeness["missing"]])
    else:
        lines.append("- 核心字段已填写。")

    if completeness["weak"]:
        lines.append("- 证据偏弱项：")
        lines.extend([f"  - {item}" for item in completeness["weak"]])

    lines.extend(["", "## 六席投委会审查", ""])
    for item in profile_reviews:
        lines.extend(
            [
                f"### {item['name']}",
                "",
                f"- 流派：{item['inspired_by']}",
                f"- 评分：{item['score']}/5",
                f"- 追问：{item['main_question']}",
            ]
        )
        if item["missing"]:
            lines.append("- 必补证据：")
            lines.extend([f"  - {x}" for x in item["missing"]])
        else:
            lines.append("- 必补证据：这一席的基础材料已经比较完整。")
        lines.append("")

    lines.extend(
        [
            "## 买入前检查单",
            "",
            *[f"- [ ] {item}" for item in build_pre_trade_checklist(thesis)],
            "",
            "## 卖出纪律",
            "",
            f"- 错了卖：{_text_or_placeholder(_get(thesis, 'sell_rules.wrong'))}",
            f"- 贵了卖：{_text_or_placeholder(_get(thesis, 'sell_rules.expensive'))}",
            f"- 变了卖：{_text_or_placeholder(_get(thesis, 'sell_rules.changed'))}",
            f"- 时间止损：{_text_or_placeholder(_get(thesis, 'sell_rules.time_stop'))}",
            "",
            "## 复盘问题",
            "",
            "- 如果这笔交易亏钱，最可能是哪一条假设错了？",
            "- 如果这笔交易赚钱，是判断正确、赔率补偿，还是运气？",
            "- 这笔主动交易相比指数的优势是否可以复用？",
            "- 下一次遇到类似机会，哪些证据必须提前准备？",
        ]
    )

    return "\n".join(lines).rstrip() + "\n"


def evaluate_completeness(thesis: dict[str, Any]) -> dict[str, Any]:
    """Score how complete the thesis is."""
    required = [
        ("stock_name", "股票名称"),
        ("code", "证券代码"),
        ("business", "商业模式"),
        ("buy_reason", "买入理由"),
        ("catalysts", "催化剂"),
        ("valuation.comparison", "估值对比"),
        ("fundamentals.revenue_growth", "收入增速"),
        ("fundamentals.profit_growth", "利润增速"),
        ("technical.trend", "趋势状态"),
        ("technical.key_levels", "关键价位"),
        ("risks", "风险清单"),
        ("sell_rules.wrong", "错了卖规则"),
        ("planned_capital_pct", "计划仓位"),
        ("planned_stop_loss_pct", "预设止损"),
        ("edge", "主动优势"),
        ("benchmark", "比较基准"),
    ]
    missing: list[str] = []
    weak: list[str] = []
    total = len(required)
    points = 0.0
    presence_only_paths = {
        "stock_name",
        "code",
        "planned_capital_pct",
        "planned_stop_loss_pct",
        "benchmark",
    }

    for path, label in required:
        value = _get(thesis, path)
        if path in presence_only_paths:
            strength = 0 if _is_blank(value) else 2
        else:
            strength = _evidence_strength(value)
        if strength == 0:
            missing.append(label)
        elif strength == 1:
            weak.append(label)
            points += 0.5
        else:
            points += 1.0

    return {
        "score": max(0.0, min(100.0, points / total * 100)),
        "missing": missing,
        "weak": weak,
    }


def evaluate_red_flags(thesis: dict[str, Any]) -> list[str]:
    """Return hard constraints that should block or pause a trade."""
    flags: list[str] = []
    planned_pct = _parse_pct(thesis.get("planned_capital_pct"))
    stop_pct = _parse_pct(thesis.get("planned_stop_loss_pct"))
    max_single_pct = _parse_pct(_get(thesis, "risk_limits.max_single_position_pct"))
    max_loss_pct = _parse_pct(_get(thesis, "risk_limits.max_loss_per_trade_pct"))

    if _get(thesis, "a_share_constraints.st") is True:
        flags.append("标的为ST/风险警示状态：学习阶段默认禁止买入。")
    if _get(thesis, "a_share_constraints.halted") is True:
        flags.append("标的处于停牌状态：无法执行交易。")

    limit_status = str(_get(thesis, "a_share_constraints.limit_status") or "").lower()
    if any(x in limit_status for x in ["limit_up", "涨停"]):
        flags.append("当前涨停：买入成交和次日流动性风险需要额外确认。")
    if any(x in limit_status for x in ["limit_down", "跌停"]):
        flags.append("当前跌停：卖出可能无法成交，禁止把止损假设写得过于理想。")

    if stop_pct is None or stop_pct <= 0:
        flags.append("没有预设止损百分比：不能进入实盘买入流程。")
    if _is_blank(_get(thesis, "sell_rules.wrong")):
        flags.append("没有写清“错了卖”规则：不能进入实盘买入流程。")

    if planned_pct is not None and max_single_pct is not None and planned_pct > max_single_pct:
        flags.append(
            f"计划仓位{planned_pct * 100:.2f}%超过单票上限{max_single_pct * 100:.2f}%。"
        )

    if planned_pct is not None and stop_pct is not None and max_loss_pct is not None:
        trade_loss_pct = planned_pct * stop_pct
        if trade_loss_pct > max_loss_pct:
            flags.append(
                "按计划仓位和止损计算，单笔亏损预算约"
                f"{trade_loss_pct * 100:.2f}%，超过上限{max_loss_pct * 100:.2f}%。"
            )

    return flags


def calculate_position_plan(thesis: dict[str, Any], red_flags: Iterable[str]) -> dict[str, Any]:
    """Create a conservative position sizing plan."""
    planned_pct = _parse_pct(thesis.get("planned_capital_pct"))
    stop_pct = _parse_pct(thesis.get("planned_stop_loss_pct"))
    risk_budget_pct = _parse_pct(_get(thesis, "risk_limits.max_loss_per_trade_pct")) or 0.005
    single_cap_pct = _parse_pct(_get(thesis, "risk_limits.max_single_position_pct")) or 0.05
    equity = _parse_float(thesis.get("account_equity"))
    price = _parse_float(thesis.get("current_price"))

    blocking = list(red_flags)
    if stop_pct is None or stop_pct <= 0:
        max_by_risk = 0.0
    else:
        max_by_risk = risk_budget_pct / stop_pct

    max_initial_pct = min(single_cap_pct, max_by_risk)
    if planned_pct is not None:
        max_initial_pct = min(max_initial_pct, planned_pct)

    if blocking:
        suggested_pct = 0.0
        status = "有红灯约束，建议只观察或纸面演练，不进入真实买入。"
    else:
        suggested_pct = max_initial_pct
        status = "仅按风险预算给出保守上限，仍需人工确认。"

    shares_text = "未填写账户资产或价格，无法换算股数。"
    if equity and price and suggested_pct > 0:
        raw_shares = equity * suggested_pct / price
        lot_shares = math.floor(raw_shares / 100) * 100
        amount = lot_shares * price
        shares_text = (
            f"按{equity:,.0f}资金、{price:.2f}价格估算，"
            f"不超过{lot_shares:,}股，约{amount:,.0f}元。"
        )

    planned_text = "未填写" if planned_pct is None else f"{planned_pct * 100:.2f}%"
    stop_text = "未填写" if stop_pct is None else f"{stop_pct * 100:.2f}%"

    markdown = "\n".join(
        [
            f"- 状态：{status}",
            f"- 计划仓位：{planned_text}",
            f"- 预设止损：{stop_text}",
            f"- 单笔最大亏损预算：{risk_budget_pct * 100:.2f}%",
            f"- 风险预算反推仓位上限：{max_by_risk * 100:.2f}%",
            f"- 单票硬上限：{single_cap_pct * 100:.2f}%",
            f"- 本系统建议初始仓位上限：**{suggested_pct * 100:.2f}%**",
            f"- A股100股整数手估算：{shares_text}",
        ]
    )
    return {
        "suggested_pct": suggested_pct,
        "max_by_risk": max_by_risk,
        "markdown": markdown,
    }


def review_profile(profile: dict[str, Any], thesis: dict[str, Any]) -> dict[str, Any]:
    """Review a thesis from one committee lens."""
    missing: list[str] = []
    points = 0.0
    required_paths = profile["required_paths"]
    for path, label in required_paths:
        strength = _evidence_strength(_get(thesis, path))
        if strength == 0:
            missing.append(label)
        elif strength == 1:
            points += 0.5
        else:
            points += 1.0

    score = round(points / len(required_paths) * 5, 1)
    if missing:
        main_question = f"先补齐：{missing[0]}。"
    else:
        main_question = profile["questions"][0]

    return {
        "name": profile["name"],
        "inspired_by": profile["inspired_by"],
        "score": score,
        "main_question": main_question,
        "missing": missing,
    }


def make_decision(
    completeness: dict[str, Any],
    red_flags: list[str],
    position: dict[str, Any],
    profile_reviews: list[dict[str, Any]],
) -> dict[str, str]:
    """Convert review facts into a high-level learning decision."""
    average_profile = sum(item["score"] for item in profile_reviews) / len(profile_reviews)
    if red_flags:
        return {
            "label": "红灯：只许观察/复盘，暂不进入真实买入",
            "summary": "这份交易想法触发了硬约束。先把红灯问题解决，再讨论赔率和仓位。",
        }
    if completeness["score"] < 60:
        return {
            "label": "资料不足：先补研究，不做交易",
            "summary": "核心证据还不够。现在最大的收益不是买入，而是把缺失数据补齐。",
        }
    if average_profile < 3.2:
        return {
            "label": "弱通过：只能纸面跟踪或极小仓试错",
            "summary": "没有硬红灯，但多个流派评分偏低。若要训练，只能用非常小的风险预算。",
        }
    if position["suggested_pct"] <= 0:
        return {
            "label": "等待：风险预算不支持买入",
            "summary": "研究材料尚可，但当前止损和仓位组合不允许形成正经买入计划。",
        }
    return {
        "label": "可进入观察清单：仍需人工确认",
        "summary": "这份想法可以进入观察清单或纸面交易。真实买入前仍要重新检查价格、流动性、公告和账户风险。",
    }


def build_pre_trade_checklist(thesis: dict[str, Any]) -> list[str]:
    """Build a practical checklist for A-share trading preparation."""
    return [
        "确认最新公告、财报、业绩预告和监管问询。",
        "确认不是ST、退市风险、停牌、涨跌停或权限受限标的。",
        "确认日均成交额足以承接计划仓位，避免小票冲击成本。",
        "确认买入价、失效价、卖出规则和复盘日期已经写下。",
        "确认计划仓位没有超过单票上限和单笔亏损预算。",
        "确认同类持仓和行业暴露没有过度集中。",
        "确认这笔主动交易相对基准指数的优势说得清楚。",
        "确认下单后当天T+1限制下无法卖出也能承受。",
    ]


def build_ai_prompt(thesis: dict[str, Any]) -> str:
    """Build a prompt that can be pasted into an AI mentor session."""
    thesis = merge_defaults(thesis)
    compact = json.dumps(thesis, ensure_ascii=False, indent=2)
    return f"""请你作为一个A股股票学习投委会来审查我的交易想法。

要求：
1. 不要直接给“必买/必卖”的神谕式结论。
2. 分别用价值投资、成长股研究、趋势交易、宏观赔率、指数化怀疑、A股执行风控六个视角审查。
3. 优先指出我哪里证据不足、哪里可能自欺、哪里风险预算不合理。
4. 必须给出买入前需要补的数据、失效条件、卖出条件和保守仓位建议。
5. 语气要像严厉但负责的导师。

我的交易想法如下：

```json
{compact}
```
"""


def default_review_path(thesis: dict[str, Any], output_dir: str | Path = "committee_reviews") -> Path:
    """Return a stable default review path."""
    code = str(thesis.get("code") or "unknown").replace(".", "_")
    date = str(thesis.get("date") or datetime.now().strftime("%Y-%m-%d"))
    stamp = datetime.now().strftime("%H%M%S")
    return Path(output_dir) / f"{date}_{code}_{stamp}_review.md"


def _get(data: dict[str, Any], dotted_path: str) -> Any:
    cur: Any = data
    for part in dotted_path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _evidence_strength(value: Any) -> int:
    if _is_blank(value):
        return 0
    if isinstance(value, str):
        return 1 if len(value.strip()) < 12 else 2
    if isinstance(value, (list, tuple, set)):
        return 1 if len(value) == 1 else 2
    return 2


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if math.isnan(float(value)) or math.isinf(float(value)):
            return None
        return float(value)
    text = str(value).strip().replace(",", "").replace("元", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def _parse_pct(value: Any) -> float | None:
    if value is None or value == "":
        return None
    raw_text = str(value).strip()
    explicit_percent = raw_text.endswith("%")
    number = _parse_float(value)
    if number is None:
        return None

    if explicit_percent or number > 1:
        return number / 100.0
    # In this project the user-facing convention is: 3 means 3%, while
    # decimal fractions such as 0.08 still mean 8%. Values like 0.5 are
    # usually intended as 0.5%, so treat decimals above 20% as percent points.
    if number > 0.2:
        return number / 100.0
    return number


def _text_or_placeholder(value: Any) -> str:
    if _is_blank(value):
        return "未填写"
    if isinstance(value, list):
        return "；".join(str(x) for x in value)
    return str(value)


def _display_title(thesis: dict[str, Any]) -> str:
    name = thesis.get("stock_name") or "未命名股票"
    code = thesis.get("code") or "未填写代码"
    return f"{name}（{code}）"
