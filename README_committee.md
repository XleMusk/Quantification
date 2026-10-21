# A股大师投委会学习系统

这个系统用于训练买卖股票前的研究、风控和复盘流程。它不会预测明天涨跌，也不会替你给出“必买/必卖”的结论；它会用六个流派视角审查你的交易想法，逼你写清楚证据、风险、仓位和卖出条件。

## 六个审查席位

- 好生意与安全边际席：商业模式、护城河、估值、安全边际。
- 成长故事与一线调研席：增长逻辑、催化剂、可验证证据。
- 趋势与止损纪律席：趋势、成交量、关键价位、错误边界。
- 宏观赔率与反身性席：市场共识、差异化判断、政策和行业周期。
- 指数化怀疑席：追问为什么不买指数，主动交易优势在哪里。
- A股执行与风控席：ST、停牌、涨跌停、T+1、100股整数手、流动性和仓位上限。

## 快速开始

### 本地网页工作台

已实现策略自动调仓、历史回放、日终纸面账户，以及原有后台因子任务。运行 `& D:\Quantification\webapp\start.ps1` 后使用脚本输出的本机 URL，详见 [工作台](webapp/README.md) 和 [交易 V1 规格](trading/TRADING_V1.md)。真实券商连接/下单关闭，模型训练尚未接通；历史十股工程回放不能视为策略有效性证明。

### A 股量化研究基础

本地资产盘点、历史因子评估和 Qlib 原生基线入口见 [A 股量化基础](docs/A_SHARE_QUANT_FOUNDATION.md)。运行 `env\python.exe run_committee.py quant-inventory` 检查现有资产，运行 `env\python.exe run_committee.py quant-research` 生成历史因子诊断；加 `--backtest` 请求组合回测。当前旧数据中的 OHLC 异常和缺失证券会阻断回测，结果不会被当成策略有效或实时行情。

日频因子研究入门包已放在 [quant_learning_starter](quant_learning_starter/README.md)：首轮 P0/P1 使用独立 Python 3.11 环境，包含合成数据教学、三个因子、交易日历对齐标签与已执行 Notebook。学习入口见 [今天的练习](quant_learning_starter/START_TODAY.md)。

列出本地 A股 skills：

```powershell
.\env\python.exe run_committee.py skills
```

查看某个 skill：

```powershell
.\env\python.exe run_committee.py skill-info position-sizer
```

创建空白模板：

```powershell
.\env\python.exe run_committee.py init --out committee_journal\my_thesis.json --stock-name "贵州茅台" --code "600519.SSE"
```

创建示例模板：

```powershell
.\env\python.exe run_committee.py sample --out committee_journal\sample_thesis.json
```

生成审查报告：

```powershell
.\env\python.exe run_committee.py review committee_journal\sample_thesis.json --print
```

生成可复制给AI导师的提示词：

```powershell
.\env\python.exe run_committee.py prompt committee_journal\sample_thesis.json --out committee_reviews\sample_prompt.md
```

计算 A股仓位：

```powershell
.\env\python.exe run_committee.py size --account-equity 100000 --entry 10 --stop-pct 8 --risk-pct 0.5 --max-position-pct 5 --print
```

登记和查看交易记忆：

```powershell
.\env\python.exe run_committee.py memory-add committee_journal\sample_thesis.json
.\env\python.exe run_committee.py memory-list
```

## 使用方法

1. 每次想买或卖一只股票，先复制一份JSON模板。
2. 用自己的话填写买入理由、商业模式、估值、趋势、风险和卖出规则。
3. 运行 `review` 生成投委会报告。
4. 红灯没有解决前，只做观察或纸面交易。
5. 真实交易之后，把结果写回 `notes`，定期复盘“判断错”还是“执行错”。

百分数字段建议直接填日常写法：`3` 表示 `3%`，`8` 表示 `8%`，`0.5` 表示 `0.5%`。如果想明确表达小数比例，也可以写成字符串 `"0.5%"`。

## Skill Pack 结构

比照 `tradermonty/claude-trading-skills` 的思想，本项目现在也采用本地 skill pack：

```text
a_stock_committee/skills/
  a-share-committee-orchestrator/
  a-share-data-screener/
  serenity-ict-attack-defense/
  uzi-stock-deep-analyzer/
  tradermonty-us-trading-workflows/
  investskill-us-stock-frameworks/
  finance-skills-etf-premium-risk/
  fincept-terminal-workbench/
  buffett-quality-value/
  lynch-growth-story/
  canslim-oniel-growth/
  minervini-vcp-trend/
  livermore-trend-discipline/
  druckenmiller-macro-conviction/
  position-sizer/
  trader-memory/
  a-share-execution-risk/
```

每个 skill 都有 `SKILL.md`；复杂方法放在 `references/`；确定性计算放在 `scripts/` 或 `a_stock_committee` 内部模块里。

查看 Serenity + ICT 攻防总控：

```powershell
.\env\python.exe run_committee.py skill-info serenity-ict-attack-defense
```

## 数据与筛选

第一版数据层使用 BaoStock 免费日线数据，并缓存到 `market_data/a_share/daily/`。

生成单股决策报告：

```powershell
.\env\python.exe run_committee.py stock-report 600000.SSE --start 2024-01-01 --print
```

筛选一组候选股票：

```powershell
.\env\python.exe run_committee.py screen --symbols 600000.SSE,000001.SZSE,600519.SSE,000333.SZSE,300750.SZSE --start 2024-01-01 --top 10
```

你的默认配置按 1 万启动资金生成，偏保护本金：

```text
最多持仓：2只
单票上限：40%
总仓位上限：70%
现金保留：30%
单笔风险：0.5%
价格范围：3-60元
20日均成交额：至少1亿元
过滤：ST、停牌、北交所、未开权限的科创板
```

重新生成配置：

```powershell
.\env\python.exe run_committee.py radar-config --account-equity 10000 --print
```

查看本地数据缓存：

```powershell
.\env\python.exe run_committee.py data-cache --print
```

## 风险边界

这是学习工具，不是投资建议。A股实盘还需要额外检查最新公告、财报、停复牌、涨跌停、流动性、账户权限、可用资金和可卖持仓。
