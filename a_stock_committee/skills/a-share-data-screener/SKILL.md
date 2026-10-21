---
name: a-share-data-screener
description: A股数据与筛选器 skill。用于抓取A股日线数据、写入本地Parquet缓存、生成个股决策支持报告、对候选股票做CANSLIM-lite、VCP-lite、value-lite、trend-watch筛选。触发场景包括“自动拿数据”“筛选股票”“生成个股报告”“决策支持”“A股数据缓存”。
---

# A股数据与筛选器

用于把投委会从“手写审查”升级成“数据先行”。默认免费源为 BaoStock；Tushare token、AkShare和券商数据可以作为后续增强。

## Workflow

1. 用 `fetch-stock` 或 `stock-report` 抓取候选股票日线数据。
2. 本地保存到 `market_data/a_share/daily/*.parquet`。
3. 自动计算均线、涨跌幅、120日高点、15日波动、成交额、PE、PB、ST和交易状态。
4. 用 `screen` 对候选股票排序，命中 `canslim-lite`、`vcp-lite`、`value-lite` 或 `trend-watch`。
5. 对高分股票进入 `a-share-committee-orchestrator` 深审，不把筛选分数当买入指令。

## Commands

```powershell
.\env\python.exe run_committee.py stock-report 600000.SSE --start 2024-01-01 --print
.\env\python.exe run_committee.py screen --symbols 600000.SSE,000001.SZSE,600519.SSE --start 2024-01-01
.\env\python.exe run_committee.py data-cache --print
```

## Read When Needed

- `references/data-policy.md`：数据源分层和实盘边界。

