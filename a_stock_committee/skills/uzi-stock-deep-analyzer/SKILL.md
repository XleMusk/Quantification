---
name: uzi-stock-deep-analyzer
description: UZI/游资个股深度分析 skill 的项目内入口。用于调用 vendor/UZI-Skill 对 A股、港股、美股个股做 22 维数据、65 位评委、机构估值、游资 F 派、Serenity I 派、杀猪盘检测和 HTML 报告生成，并与本项目 A股攻防/仓位/风控流程合并使用。
---

# UZI Stock Deep Analyzer

本 skill 是 `wbh604/UZI-Skill` 的项目内 wrapper。源码安装在：

```text
D:\Quantification\vendor\UZI-Skill
```

运行环境安装在：

```text
D:\Quantification\vendor\UZI-Skill\.venv
```

## When To Use

- 用户要求对单只 A股、港股、美股做深度分析、快速扫一眼、DCF、估值、IC memo、杀猪盘排查。
- 用户明确想看游资视角、短线情绪、龙虎榜、题材热度、打板/卡位判断。
- 用户想把 UZI 65 评委结果并入本项目的 `serenity-ict-attack-defense` 攻防决策。
- 用户讨论 ETF/主题基金时，先按项目规则核验 ETF 溢价、底层资产和成分股；UZI 本身主要分析个股，不应直接把 ETF 当个股跑。

## Core Commands

所有命令都从 UZI 仓库根目录运行：

```powershell
Set-Location D:\Quantification\vendor\UZI-Skill
```

快速扫：

```powershell
.\.venv\Scripts\python.exe run.py 600519.SH --depth lite --no-browser
```

默认完整度：

```powershell
.\.venv\Scripts\python.exe run.py 600519.SH --depth medium --no-browser
```

深度分析：

```powershell
.\.venv\Scripts\python.exe run.py 600519.SH --depth deep --no-browser
```

只看游资 F 派：

```powershell
.\.venv\Scripts\python.exe run.py 600519.SH --school F --depth medium --no-browser
```

只看 Serenity I 派：

```powershell
.\.venv\Scripts\python.exe run.py 600519.SH --school I --depth medium --no-browser
```

## Project Integration

把 UZI 输出当作个股研究层，不直接当交易指令。

1. 先用 UZI 生成个股报告，重点读取综合评分、Top bull/bear、估值冲突、F 派游资判断、I 派 Serenity 判断、杀猪盘等级和报告路径。
2. 若问题是“进攻/防守/抄底/主题能不能做”，继续调用 `serenity-ict-attack-defense`，把 UZI 作为候选个股证据输入。
3. 交易落地必须再过本项目市场、宽度、风险和仓位命令：

```powershell
D:\Quantification\env\python.exe D:\Quantification\run_committee.py market-regime --print
D:\Quantification\env\python.exe D:\Quantification\run_committee.py intraday-breadth --print
D:\Quantification\env\python.exe D:\Quantification\run_committee.py risk-radar --symbols 600519 --print
D:\Quantification\env\python.exe D:\Quantification\run_committee.py size --account-equity 10000 --entry 10 --stop-pct 8 --risk-pct 0.5 --max-position-pct 10 --print
```

## Output Discipline

- 不把 UZI 分数解释成买卖建议；只说研究结论、触发条件、失效条件、仓位边界。
- 如果数据源缺失、联网失败、中文名未解析、ETF/LOF/可转债被拒绝，要明确说明并让用户换代码或选成分股。
- 对 A股要补充 T+1、涨跌停、ST/退市、停牌、流动性、100 股整数手和账户现金约束。
- 对跨境 ETF 或港美股映射，不核验溢价/折价和底层资产前，不说“便宜”。

## Related Local Skills

- `serenity-ict-attack-defense`：主题进攻/防守总控。
- `a-share-data-screener`：本项目免费日线筛选。
- `position-sizer`：A股仓位和 100 股整数手计算。
- `a-share-execution-risk`：A股执行约束和风险红灯。
