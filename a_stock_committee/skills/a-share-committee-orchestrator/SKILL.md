---
name: a-share-committee-orchestrator
description: A股大师投委会总控 skill。用于把股票研究模板交给多个交易/投资流派审查，整合买前检查、仓位计算、A股执行风控、交易记忆和复盘。触发场景包括“A股投委会”“多位大师审查”“生成审查报告”“买前审查”“交易学习系统”。
---

# A股大师投委会总控

这是总控 skill。它不直接替用户买卖股票，而是调用各个子 skill 形成买前审查、仓位建议和复盘闭环。

## Workflow

1. 收集或读取股票研究模板。
2. 用 `buffett-quality-value` 审查长期质量和估值。
3. 用 `lynch-growth-story` 审查成长故事和证据。
4. 用 `canslim-oniel-growth` 或 `minervini-vcp-trend` 审查强势成长/技术买点。
5. 当用户问“进攻什么/防守什么/指数破位/抄底/主线”时，先用 `serenity-ict-attack-defense` 把产业链卡点、技术结构和仓位边界合成攻防计划。
6. 用 `druckenmiller-macro-conviction` 限制总仓位环境。
7. 用 `position-sizer` 计算风险预算内的股数。
8. 用 `a-share-execution-risk` 做实盘前红灯检查。
9. 用 `trader-memory` 登记、跟踪、关闭和复盘 thesis。

## Commands

```powershell
.\env\python.exe run_committee.py review committee_journal\my_thesis.json --print
.\env\python.exe run_committee.py skill-info serenity-ict-attack-defense
.\env\python.exe run_committee.py size --account-equity 100000 --entry 10 --stop-pct 8 --risk-pct 0.5 --print
.\env\python.exe run_committee.py memory-add committee_journal\my_thesis.json
```
