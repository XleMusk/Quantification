---
name: trader-memory
description: A股交易记忆 skill。用于登记股票交易想法、跟踪 IDEA/ENTRY_READY/ACTIVE/CLOSED 状态、附加仓位报告、记录退出原因和复盘教训。触发场景包括“记录这个想法”“加入观察清单”“交易日志”“复盘”“close position”“postmortem”。
---

# 交易记忆

把每个交易想法变成可追踪对象。重点是保留当时的想法，而不是事后改写历史。

## Workflow

1. 从研究模板登记 thesis，初始状态为 `IDEA`。
2. 仓位计算完成后转为 `ENTRY_READY`。
3. 实际买入后可手动转为 `ACTIVE`。
4. 卖出或逻辑失效后转为 `CLOSED` 或 `INVALIDATED`。
5. 记录退出原因、经验教训和后续规则修改。

## Commands

```powershell
.\env\python.exe run_committee.py memory-add committee_journal\sample_thesis.json
.\env\python.exe run_committee.py memory-list
.\env\python.exe run_committee.py memory-close <thesis_id> --exit-price 10.5 --reason "触发时间止损"
```

## Read When Needed

- `references/lifecycle.md`：状态流转规则。

