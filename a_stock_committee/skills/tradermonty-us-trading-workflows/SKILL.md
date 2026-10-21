---
name: tradermonty-us-trading-workflows
description: TraderMonty 美股交易流程 skill wrapper。用于把 tradermonty/claude-trading-skills 中的市场宽度、技术图表、VCP/CANSLIM、突破交易计划、暴露度管理、复盘和回测设计能力接入本项目，作为 A股攻防系统的海外市场与交易流程补充。
---

# TraderMonty US Trading Workflows

上游仓库安装在：

```text
D:\Quantification\vendor\tradermonty-claude-trading-skills
```

本次接入版本：

```text
20be823
```

## When To Use

- 用户问美股市场环境、QQQ/SPY/科技股风险、泡沫、市场宽度、趋势参与度。
- 用户提供周线/日线图，要求技术分析、支撑阻力、概率情景、失效位。
- 用户想做美股 VCP、CANSLIM、突破交易计划、仓位暴露、交易复盘或回测假设设计。
- 用户想把美股/海外风险映射回 A股主题，例如 AI、半导体、机器人、CPO、创新药。

## Selected Upstream Skills

优先使用这些上游 skill，而不是整包触发：

```text
skills/technical-analyst
skills/market-breadth-analyzer
skills/uptrend-analyzer
skills/exposure-coach
skills/market-environment-analysis
skills/us-market-bubble-detector
skills/macro-regime-detector
skills/vcp-screener
skills/canslim-screener
skills/breakout-trade-planner
skills/scenario-analyzer
skills/backtest-expert
skills/signal-postmortem
```

读取上游技能时，从以下目录打开对应 `SKILL.md`：

```text
D:\Quantification\vendor\tradermonty-claude-trading-skills\skills\<skill-name>\SKILL.md
```

## Project Integration

这个 wrapper 不替代本项目 A股命令，只补充海外和流程视角。

1. 如果问题是 A股进攻/防守，先按 `serenity-ict-attack-defense` 判定 A股市场状态和主题卡点。
2. 如果问题涉及海外映射，再读取 TraderMonty 的市场环境、技术或泡沫检测 skill。
3. 如果 TraderMonty 输出了美股交易计划，只能作为海外资产或主题映射参考；A股落地仍必须重新计算 T+1、涨跌停、流动性、ST/退市和 100 股整数手。
4. 如果上游 skill 需要 FMP/FINVIZ/Alpaca 等 API，且本地没有配置，就改用无 API 的手工/CSV/图像路径，不临时把密钥要求变成结论。

## Output Discipline

- 把分析分成“海外信号”“对 A股主题的映射”“本地交易约束”三层。
- 图表分析必须给 2-4 个概率情景、关键价位和 invalidation，不用单一路径叙事。
- 回测和筛选结果必须注明数据源、样本期、幸存者偏差、交易成本和是否样本外验证。
- 不输出自动下单指令，不把海外美股规则机械套到 A股。

## Useful Pairings

- 与 `uzi-stock-deep-analyzer`：UZI 看个股评委分歧，TraderMonty 看图表/美股市场环境。
- 与 `serenity-ict-attack-defense`：TraderMonty 提供海外 beta 和风险映射，Serenity+ICT 做 A股攻防落地。
- 与 `position-sizer`：美股突破计划只能给思路，A股实际仓位仍由本项目计算。
