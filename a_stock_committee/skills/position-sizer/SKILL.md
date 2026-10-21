---
name: position-sizer
description: A股仓位计算 skill。用于计算股票买入股数、单笔风险、止损距离、100股整数手、单票仓位上限和风险预算。触发场景包括“买多少股”“仓位怎么算”“止损8%能买多少”“风险0.5%”“position sizing”。
---

# A股仓位计算

用确定性脚本计算仓位。默认做多股票，买入按100股整数手向下取整。

## Workflow

1. 收集账户资产、入场价、止损价或止损百分比。
2. 收集单笔风险预算和单票仓位上限，学习阶段默认保守。
3. 运行脚本或主 CLI 的 `size` 命令。
4. 输出股数、持仓金额、亏损金额、绑定约束和A股注意事项。

## Script

```powershell
.\env\python.exe run_committee.py size --account-equity 100000 --entry 10 --stop-pct 8 --risk-pct 0.5 --max-position-pct 5 --print
```

也可以直接运行：

```powershell
.\env\python.exe a_stock_committee\skills\position-sizer\scripts\position_sizer.py --account-equity 100000 --entry 10 --stop-pct 8 --risk-pct 0.5 --max-position-pct 5
```

## Read When Needed

- `references/sizing-method.md`：仓位方法说明。

