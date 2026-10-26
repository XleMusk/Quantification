# 2026-10-26 fixed-only D0/D1 paper package

本包以已登记的 `fixed_daily/20261024_lowvol_current` 为固定策略账户输入，使用冻结的 StockDB 2026-10-23 收市快照生成一次可复现的 D0/D1 研究纸面结果。

- close date: `2026-10-23`
- execution / settlement: `2026-10-26` / `2026-10-27`
- StockDB coverage: 20/20 fixed holdings present, 0 missing
- target holdings / paper orders: 20 / 20
- available routes: 20
- modeled fees: CNY 101.20
- projected final cash / equity: CNY 28,012.80 / CNY 139,898.80
- joint paper: 20 target rows and 20 paper orders, budgeted capacity policy
- `execution_admitted=false`; no broker, THS, login, submission, cancellation, transfer or persistent Ledger mutation

The package is a `TARGET_PREVIEW` projection. It proves that the registered fixed strategy can traverse Data snapshot → target → order/fill projection → cash/position receipt on a complete historical close snapshot. It does not claim current broker state, THS read-only observation, PIT validation, or online paper rollover. Candidate count is zero because no same-day candidate target was available for 2026-10-23; the next changed close should supply a same-day candidate manifest to the same contract.
