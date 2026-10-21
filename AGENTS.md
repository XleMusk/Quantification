# Quantification Project Instructions

This workspace develops A-share strategy research and automated trading software in parallel. The `trading` core supports historical replay and end-of-day paper simulation through one portfolio/order/account path. Research signals must reuse that path. Real-account connection and real orders remain disabled unless separately authorized and validated; no return guarantees are made.

The V2 long-term priority is an explainable, reproducible, extensible internal research-to-paper platform. Do not optimize the project around quickly pre-filling the first Tonghuashun order. Start follow-on architecture work from `docs/architecture_v2/BASELINE.md` and its pinned baseline manifest; keep each work package's unverified parts explicit.

## Parallel Tracks

- Prioritize usable code, actual strategy runs and the execution path. Do not make routine hash audits, smoke checks, test counts or document production the main work.
- Reuse unchanged evidence and checks. Run only verification justified by the current change, a real failure or an explicit acceptance requirement; retain existing data-integrity and trading-safety protections.
- Resolve data blockers with reliable sources and reusable batch processing. Do not make manual review of every historical notice a prerequisite for all research; preserve unknowns and distinguish diagnostic research from account-ready results.
- Track missing data and account evidence per route. Pause only the dependent route and continue independent research, end-of-day replay and software work. When user input is needed, name the exact fields, format, purpose and affected route; missing live quotes or broker exports do not by themselves justify stopping the whole project.
- Prioritize complete strategy rules, target holdings, order/fill accounting, recovery, and a usable trading UI alongside research comparisons.
- New trading data and persistent accounts live outside the repository under `D:\QuantificationData\trading` (override with `QUANT_TRADING_HOME`). Do not move old data or modify the main environment without a scoped need.
- Historical replay must pin its data snapshot. Do not compare different samples or cost settings as if only the signal changed.
- The first strategy is an unvalidated candidate. Keep historical pilot results, daily paper replay, intraday simulation, broker integration, and live readiness distinct.
- Do not route the older futures-style `bridge/vnpy_strategy.py` to the same account; the new order ledger is the sole application order path.

## Core Workflow

- For current market, sector, ticker, "latest", "today", "now", macro, US market, Fed, ETF premium, or news-driven questions, verify with live sources before giving a conclusion.
- For OpenAI product/API questions, use official OpenAI docs first.
- Prefer the project interpreter: `D:\Quantification\env\python.exe`.
- Prefer project commands through `D:\Quantification\run_committee.py`.

## Tonghuashun Workbench

For Tonghuashun desktop navigation, screenshots, funds/sector/ETF analysis, or requests to operate its tools, load `run_committee.py skill-info tonghuashun-workbench`. It provides the verified classic-PC version baseline, local function index, combined workflows, and data/operation caveats. Configuration discovery is not UI verification. Keep live trading disabled; learning the software does not authorize orders, cancellations, transfers, or activating conditional orders. Read `trading/ths/README.md` before any separately authorized adapter work and preserve its supported-instrument and manual-submission limits.

## Attack/Defense Framework

When the user asks about:

- which A-share sectors to attack or defend;
- whether an index has broken down;
- whether to bottom-fish semiconductors or a theme ETF;
- commercial aerospace, satellite internet, AI semiconductors, CPO, robotics, innovation drugs, or other theme trades;
- whether prior predictions were too short-term;

use the local skill:

```powershell
D:\Quantification\env\python.exe D:\Quantification\run_committee.py skill-info serenity-ict-attack-defense
```

Then combine:

1. `serenity-skill`: supply-chain layer, bottleneck, evidence quality, and what would falsify the thesis.
2. `ict-strategy`: market structure, liquidity sweep, FVG/OB, entry trigger, and invalidation level.
3. A-share committee system: market regime, breadth, risk radar, position sizing, execution constraints, and trade memory.

Do not recommend attack solely because something is up today. First ask what will still be repriced over the next 1-12 weeks, then use today's price action only as confirmation or rejection.

## Useful Commands

```powershell
D:\Quantification\env\python.exe D:\Quantification\run_committee.py skills
D:\Quantification\env\python.exe D:\Quantification\run_committee.py skill-info serenity-ict-attack-defense
D:\Quantification\env\python.exe D:\Quantification\run_committee.py market-regime --print
D:\Quantification\env\python.exe D:\Quantification\run_committee.py intraday-breadth --print
D:\Quantification\env\python.exe D:\Quantification\run_committee.py risk-radar --symbols 002465,002446 --print
D:\Quantification\env\python.exe D:\Quantification\run_committee.py size --account-equity 10000 --entry 10 --stop-pct 8 --risk-pct 0.5 --max-position-pct 10 --print
```

## Risk Discipline

- No industry evidence: research only.
- No technical structure or invalidation level: wait.
- No position boundary: no attack.
- Cross-border ETF without premium/discount and underlying-asset check: do not call it cheap.
- A-share order plan must respect T+1, limit-up/limit-down, ST/delist risk, liquidity, 100-share lots, and account cash/position constraints.

## Context Handoff

If a task becomes long-running or context pressure is high, update `D:\enforce\capability_lab\handoffs\current_handoff.md` with objective, latest request, completed work, current state, next steps, key files, commands, constraints, and blockers.
