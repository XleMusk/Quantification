---
name: fincept-terminal-workbench
description: FinceptTerminal 彭博终端平替工作台 wrapper。用于把 Fincept Terminal v4 桌面端接入本项目，作为多资产行情、宏观、组合、估值、AI agent、数据连接器和终端式研究界面；所有结论仍需回到 A股攻防、ETF 溢价、风险雷达和仓位纪律中验证。
---

# FinceptTerminal Workbench

FinceptTerminal 是原生 C++20/Qt6 桌面金融终端，定位为 Bloomberg/Refinitiv/FactSet 的开源替代方向。它适合做“信息与分析工作台”，不替代本项目的交易纪律。

## Local Install State

已安装桌面端：

```text
C:\Users\13944\AppData\Local\Programs\FinceptTerminal\FinceptTerminal.exe
```

维护/卸载工具：

```text
C:\Users\13944\AppData\Local\Programs\FinceptTerminal\FinceptMaintenanceTool.exe
```

下载的官方 Windows 安装器：

```text
D:\Quantification\vendor\FinceptTerminal-release\FinceptTerminal-4.3.0-windows-x64-setup.exe
```

校验记录：

```text
D:\Quantification\vendor\FinceptTerminal-release\INSTALLER_VERIFICATION.txt
```

本次接入版本：

```text
FinceptTerminal v4.3.0
SHA256 382A26FF654B7B828B4B5AABEB5810ABDA4B820A43CC8F316607A842E4DEA007
```

注意：安装器 SHA256 已与 GitHub release API digest 匹配，但 Windows Authenticode 显示未签名。仅从官方 release 运行，并保留校验记录。

## When To Use

- 用户想用“彭博终端平替”查看多资产行情、宏观、经济数据、组合、估值、新闻和图表。
- 用户需要把 FinceptTerminal 里的行情/宏观/估值/AI agent 结果转成 A股主题研究输入。
- 用户想用 Fincept 的数据连接器补充 Yahoo Finance、FRED、World Bank、IMF、AkShare、DBnomics 等来源。
- 用户需要终端式研究 UI，而不是只要命令行脚本。

## Project Integration

1. FinceptTerminal 只作为数据和研究工作台，不直接输出买卖指令。
2. A股主题进攻/防守仍先调用 `serenity-ict-attack-defense`。
3. ETF、QDII、跨境 ETF 仍先调用 `finance-skills-etf-premium-risk` 核验 NAV、溢价、底层资产、汇率、时差和申赎风险。
4. 个股深度报告优先结合 `uzi-stock-deep-analyzer`；海外个股框架结合 `investskill-us-stock-frameworks`。
5. 执行前必须回到本项目命令：

```powershell
D:\Quantification\env\python.exe D:\Quantification\run_committee.py market-regime --print
D:\Quantification\env\python.exe D:\Quantification\run_committee.py intraday-breadth --print
D:\Quantification\env\python.exe D:\Quantification\run_committee.py risk-radar --symbols 002465,002446 --print
D:\Quantification\env\python.exe D:\Quantification\run_committee.py size --account-equity 10000 --entry 10 --stop-pct 8 --risk-pct 0.5 --max-position-pct 10 --print
```

## Data And License Discipline

- 对“最新/今天/实时/宏观/Fed/ETF premium/新闻”结论，必须用 live source 或 Fincept 中的具体数据源与时间戳核验。
- 不把 Fincept AI agent 输出当作事实；事实、模型估算、agent 推断要分开写。
- 不把 API keys、broker keys、Fincept credentials 写入本项目文件。
- 上游 README 声明个人/学术/开源贡献可走 AGPL-3.0；商业、公司内部、机构、咨询或再分发使用需要商业许可。使用前按自己的场景核对许可。

## Useful Pairings

- `serenity-ict-attack-defense`：A股主题攻防落地。
- `finance-skills-etf-premium-risk`：ETF 和跨境基金风控。
- `uzi-stock-deep-analyzer`：A股/港股/美股个股深度分析。
- `tradermonty-us-trading-workflows`：美股技术、市场宽度和交易流程。
