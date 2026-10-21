---
name: investskill-us-stock-frameworks
description: InvestSkill 美股机构分析框架 wrapper。用于调用 yennanliu/InvestSkill 的 21 个 prompt-only 美股研究框架，覆盖 stock-eval、基本面、DCF、可比估值、财报/电话会、期权、机构持仓、空头、组合和结果校验，作为 UZI 与 A股投委会之外的美股尽调模板。
---

# InvestSkill US Stock Frameworks

上游仓库安装在：

```text
D:\Quantification\vendor\InvestSkill
```

本次接入版本：

```text
d35608a
```

## When To Use

- 用户问美股个股、ADR、海外映射标的、竞争对手或供应链龙头。
- 用户需要 DCF、可比估值、10-K/10-Q、财报电话会、期权、机构持仓、空头数据等机构式框架。
- 用户想把美股龙头的财务/估值/业务变化映射到 A股产业链。
- 用户要求对已有分析做质量校验、方法论校验、信号一致性检查。

## Selected Upstream Skills

按任务读取以下上游 `SKILL.md`：

```text
plugins/us-stock-analysis/skills/stock-eval
plugins/us-stock-analysis/skills/fundamental-analysis
plugins/us-stock-analysis/skills/dcf-valuation
plugins/us-stock-analysis/skills/stock-valuation
plugins/us-stock-analysis/skills/financial-report-analyst
plugins/us-stock-analysis/skills/earnings-call-analysis
plugins/us-stock-analysis/skills/competitor-analysis
plugins/us-stock-analysis/skills/options-analysis
plugins/us-stock-analysis/skills/institutional-ownership
plugins/us-stock-analysis/skills/short-interest
plugins/us-stock-analysis/skills/portfolio-review
plugins/us-stock-analysis/skills/result-validator
plugins/us-stock-analysis/skills/research-bundle
```

读取路径：

```text
D:\Quantification\vendor\InvestSkill\plugins\us-stock-analysis\skills\<skill-name>\SKILL.md
```

## Data Rule

InvestSkill 是 prompt-only 框架，不自带可靠行情和财务数据。凡是涉及“最新”“今天”“当前估值”“最近财报”“机构持仓”“期权”等，必须先用实时来源、公司公告、SEC 文件、交易所/数据源或网页检索核验，再套框架。

## Project Integration

1. 用 InvestSkill 生成美股/海外公司的研究骨架。
2. 用 `serenity-skill` 或 `serenity-ict-attack-defense` 把海外变化映射到 A股产业链卡点。
3. 用本项目 `risk-radar` 和 `position-sizer` 处理 A股候选，不直接照搬美股买卖建议。
4. 对跨境 ETF 或主题基金，先走 `finance-skills-etf-premium-risk` 做 NAV/溢价检查。

## Output Discipline

- 明确分开事实、估算、推断和情景假设。
- DCF/Comps 必须展示关键假设、敏感性和冲突，而不是只给单点目标价。
- 财报/电话会分析必须指出管理层措辞变化、guidance delta、现金流质量和会计风险。
- 若数据不完整，输出“研究待补项”，不要用模板话术填空。
