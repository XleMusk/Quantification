---
name: finance-skills-etf-premium-risk
description: finance-skills ETF/NAV/流动性风险 wrapper。用于调用 himself65/finance-skills 中与 ETF 溢价折价、NAV 偏离、跨境 ETF、yfinance 数据、流动性、相关性和期权 payoff 相关的技能，补强本项目“ETF 未核验底层资产与溢价前不许说便宜”的风控规则。
---

# Finance Skills ETF Premium Risk

上游仓库安装在：

```text
D:\Quantification\vendor\finance-skills
```

本次接入版本：

```text
87f688e
```

## When To Use

- 用户问 ETF/LOF/跨境 ETF 是否便宜、是否溢价、是否能抄底。
- 用户问 QDII、港股/美股主题 ETF、商品/债券/杠杆/反向/crypto ETF 与底层资产的偏离。
- 用户问 ETF 单日大涨是 NAV 驱动、情绪驱动、gamma/dealer hedging，还是申赎/做市受阻。
- 用户需要检查股票或 ETF 的流动性、相关性、组合相关风险或简单期权 payoff。

## Selected Upstream Skills

本项目只接入这些低噪声 market-analysis 技能：

```text
plugins/market-analysis/skills/etf-premium
plugins/market-analysis/skills/yfinance-data
plugins/market-analysis/skills/stock-liquidity
plugins/market-analysis/skills/stock-correlation
plugins/market-analysis/skills/options-payoff
plugins/market-analysis/skills/company-valuation
plugins/market-analysis/skills/earnings-preview
plugins/market-analysis/skills/earnings-recap
```

读取路径：

```text
D:\Quantification\vendor\finance-skills\plugins\market-analysis\skills\<skill-name>\SKILL.md
```

## Local Runtime

若需要用 `yfinance/pandas/numpy` 做简单 ETF/NAV 计算，优先复用 UZI 的隔离 venv，不改项目主 `env`：

```powershell
D:\Quantification\vendor\UZI-Skill\.venv\Scripts\python.exe -c "import yfinance, pandas, numpy; print(yfinance.__version__)"
```

## Project Integration

1. 对 ETF/LOF/主题基金，先确认证券类型、底层资产、溢价/折价、流动性和申赎风险。
2. 若是 A股上市跨境 ETF，还要结合本项目规则检查涨跌停、T+1、成交额、场内溢价、QDII 额度和跟踪误差。
3. 若用户实际想研究主题或产业链，ETF 只做入口，随后拆到成分股和 `serenity-ict-attack-defense`。
4. 若用户给的是个股，不要强行套 ETF premium；改用 UZI 或 InvestSkill。

## Output Discipline

- 没有 NAV/IOPV/底层资产核验，不说“便宜”。
- 对跨境 ETF 必须区分：底层资产涨跌、汇率、交易时差、场内溢价、流动性和申赎限制。
- 溢价/折价数据源不同可能冲突，必须标注来源和时间戳。
- 对 A股场内基金，Yahoo/yfinance 数据可能不完整；必要时改用交易所、基金公司公告、天天基金/东方财富等可核验来源。
