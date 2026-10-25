# D:\Quantification active handoff

Updated: 2026-10-25 01:42 +08:00

## Objective

Continue the V3/F2 A-share quantification rebuild with the shared `Data -> Signal -> Target Portfolio -> Orders -> BrokerAdapter -> Fill -> Cash/Position` pipeline, reliable StockDB/recent data, historical replay, daily paper, target holdings, read-only THS evidence and a replaceable candidate strategy library. Keep `execution_admitted=false`; prohibit real broker/THS login, orders, cancellations, transfers and paid services.

## Completed in the latest continuation

- F1 local vertical slice: `outputs/pipeline_receipts/20261024_f01_local_vertical_slice_v1.json`, status `PARTIAL_LOCAL_TIMESTAMP_VERTICAL_SLICE`, 3 orders and 6 simulated fills through all seven shared stages.
- F1 source retrieval: `outputs/pipeline_receipts/20261024_f01_source_retrieval_receipt_v1.json`, 3/3 HTTP successes, 3/3 full-response SHA-256 matches, offset-aware current retrieval timestamps, HTTP `Last-Modified`/ETag metadata, and a 3/3 StockDB market join on `2024-05-06`.
- F1 network publication sidecar: `outputs/pipeline_receipts/20261024_f01_publication_sidecar_network_v2.json`, 3 rows with explicit current retrieval/as-of/revision fields. Status remains `REAL_SOURCE_MAPPED_PIT_BLOCKED` because current retrieval is after the historical `2024-05-01` cutoff.
- F2/F1 contract correction: `trading/research/f01_publication_sidecar.py` now passes mapped retrieval/as-of/revision fields into `evaluate_publication_evidence`; the regenerated `outputs/pipeline_receipts/20261025_f01_publication_sidecar_contract_v3.json` remains `REAL_SOURCE_MAPPED_PIT_BLOCKED`, but correctly reports `revision_identity_proven=3/3` and `retrieval_or_asof_after_decision` rather than false missing metadata. Focused sidecar/coverage/contract tests: `11 passed`.
- F2 coverage funnel was regenerated from that corrected sidecar: `outputs/financials/f01_coverage_funnel_contract_v3_20261025.json` remains `BLOCKED_DATA` with 3/3 market joins, 0/3 retrieval-as-of eligible rows and 0/3 F01 rows; no zero fill or task creation occurred.
- F2 sidecar-to-fact bridge is now executable through `run_committee.py f2-fact-bridge`. `trading/research/f2_fact_bridge.py` materializes only rows with proven historical as-of, explicit issuer scope/currency/unit and a valid quarterly identity into typed `FinancialFact` records; all other rows retain machine-readable reasons. The real 3-row receipt `outputs/pipeline_receipts/20261025_f2_sidecar_fact_bridge_receipt_v1.json` is `BLOCKED_DATA` (3 retrieval/as-of blocks and 3 scope blocks, 0 facts), with `execution_admitted=false`, `factor_computed=false` and `account_replay=false`. Synthetic PIT-safe materialization is covered by `trading/research/tests/test_f2_fact_bridge.py` and does not enter the real candidate queue.
- Focused F1 suite: `15 passed`; py_compile passed. Route, binding, focus, current status and `goal_continuity.json` were updated.

## Current blockers and next work

- Historical as-of/revision evidence for one disclosure event is still required. Do not backdate current retrieval or promote F01.
- Weekend/no-new-session rule: do not run StockDB capture or paper day-cut before the next closed session (`2026-10-26`).
- At the next close, run D0/D1 from the existing `fixed_daily` checkpoint and native StockDB reference. Produce fixed plus registered candidate target/reconciliation receipts through the shared Engine/Ledger; preserve missing rows and hashes; keep THS/live gates closed.
- Candidate queue: `stockdb_low_vol_half_momentum_20` and `factor_candidate_family_balanced_ridge` are research-only follow-ups; fixed low-volatility and Alpha158/LightGBM retain stop evidence; 12-1 momentum remains coverage-blocked.

- R04 fixed-horizon follow-up is now complete for the mature-factor Ridge candidate. Using the existing feature run/provider view and frozen StockDB raw snapshot, `stockdb_factor_monthly_ridge_horizon_variant.py` produced the prespecified 60-session fit at `outputs/stockdb_factor_monthly_ridge_horizon60/20261025_same_lineage_v2/run.json` (55,797 training rows; 34 score dates; 180,948 score rows). The shared Engine/Ledger replay at `outputs/stockdb_factor_monthly_ridge_replays/20261025_monthly_same_lineage_h60_cap500_v3/result.json` covered 648 sessions, 152 symbols and 158 selected events with 279 orders, 556 fills, net return 32.1767%, maximum drawdown -19.1039%, Sharpe 0.7395, fees CNY 4,129.55 and 10 blocked order events. Receipts: `outputs/pipeline_receipts/20261025_mature_factor_stockdb_monthly_ridge_h60_receipt.json` and `outputs/pipeline_receipts/20261025_mature_factor_stockdb_monthly_ridge_h60_replay_receipt.json`.
- R04 is `VERIFIED_LOCAL` in `docs/architecture_v3/work_packages_v3_binding.json`. The 60-session horizon was fixed before the run and used no weekend capture or network access. Focused variant/replay tests pass (`6 passed`); `execution_admitted=false`, `strategy_validated=false`, `ledger_mutated=false`, PIT/action gates remain closed.

## D1 candidate paper batch fan-in

Added `trading/research/daily_candidate_paper_batch.py` and `run_committee.py daily-candidate-paper-batch`. A historical two-candidate run produced 20 targets/20 paper orders for half momentum and 5 targets/5 paper orders for Ridge in isolated package directories; receipt: `outputs/paper_plans/20261025_candidate_batch_v1/packages/batch_receipt.json`. Focused tests: `2 passed`. The batch accepts explicit target/quote inputs and does not fetch data, create signals, mutate `fixed_daily`, or touch THS/broker state.

The batch manifest now makes catalog-to-payload mapping explicit: `stockdb_low_vol_half_momentum_20` -> `trend_low_vol_half_momentum_20`, and `factor_candidate_family_balanced_ridge` -> `stockdb_factor_monthly_ridge_v1`. The alias-safe rerun is in `outputs/paper_plans/20261025_candidate_batch_v1/packages_v2/batch_receipt.json`; use this mapping on the next closed session.

The v3 batch also reads `docs/architecture_v3/candidate_strategy_catalog_20261024.json` and requires `research_followup_allowed=true`; its receipt is `outputs/paper_plans/20261025_candidate_batch_v1/packages_v3/batch_receipt.json`. Stop, coverage-blocked and unassessed candidates cannot enter the paper projection.

The v4 batch adds same-day quote validation: quote clocks and rows must match each target decision day, and execution must be later. Receipt: `outputs/paper_plans/20261025_candidate_batch_v1/packages_v4/batch_receipt.json`; a future or misdated quote is rejected before order projection.

The v5 batch binds the receipt to the exact manifest path, SHA-256 and candidate count, and rejects a candidate list that differs from the manifest. Receipt: `outputs/paper_plans/20261025_candidate_batch_v1/packages_v5/batch_receipt.json`; manifest SHA-256 is `34f68b5d2638b635555ffc776003dacd88145d303417f0d9c40d1f9cb9c810a4`.

Focused validation after the v5 change: `26 passed` across the D1 batch and all F01 tests. The next closed-session action remains the 2026-10-26 D0/D1 run using native StockDB inputs; no weekend day-cut was fabricated.

The candidate input fan-in is now automated by `trading/research/candidate_daily_input_bundle.py` and `run_committee.py candidate-daily-input-bundle`. It converts the two saved StockDB replay sources into a manifest, rejects a strategy alias that changes the replay identity, and feeds that manifest into D1. The one-command wrapper `run_committee.py candidate-daily-paper-run` now also accepts an explicit fixed terminal research account, persists fixed target/quote payload hashes, and projects it through the same `build_paper_target_package` path. The latest historical proof is `outputs/paper_plans/20261025_candidate_batch_v1/pipeline_v3/pipeline_receipt.json`: fixed low-vol half-momentum 20/20, trend candidate 20/20, Ridge 5/5, all with `execution_admitted=false`. Focused D1/F01/input-bundle/orchestration validation: `32 passed`.

The public-seat candidate route also advanced: completed case `lag2_TREND_ANY_LHB` now has a shared local shadow package with 5 targets, 5 paper orders and 4 available routes. Receipt: `outputs/pipeline_receipts/20261025_public_seat_candidate_shared_paper_receipt_v1.json`. The catalog remains `UNASSESSED_SEPARATE_GATE`; timing, PIT membership and causal attribution are still required before it can enter the D1 follow-up queue.

The public-seat shared-paper receipt is now bound into R05/O04/U03 in docs/architecture_v3/work_packages_v3_binding.json, so the candidate catalog, operational batch and provenance package all point to the same receipt while preserving the separate timing/PIT/causal gate. JSON binding validation and git diff --check passed.

The separate-gate path is now executable through `run_committee.py candidate-daily-paper-run --gate-mode separate_gate`: `outputs/paper_plans/20261025_public_seat_lag2_pipeline_v1/run/pipeline_receipt.json` records 1 candidate, 5 targets, 5 paper orders and 4 available routes. The catalog decision must be `UNASSESSED_SEPARATE_GATE`; it remains outside the regular D1 follow-up queue. Focused candidate/public-seat tests: `24 passed`; py_compile and JSON validation passed.

The next closed-session entry is now idempotent: `candidate-daily-paper-run` computes an input fingerprint from the exact source manifest, catalog, optional fixed account, dates, cash and gate mode. Repeating the same output directory with the same fingerprint reuses the existing receipt; changed inputs are rejected instead of overwriting a prior paper projection. Cash is normalized before hashing, so CLI `1000000.0` and API `1000000` have the same identity. The live rerun at `outputs/paper_plans/20261025_public_seat_lag2_pipeline_v2/pipeline_receipt.json` was then read back with `reused_existing_run=true`; focused orchestration tests: `4 passed`; `execution_admitted=false` remains enforced.

The shared paper target package now binds an explicit cost model (`cn_a_share_standard_ledger_costs_v1`) and an explicit benchmark status (`NOT_APPLICABLE_TO_PAPER_TARGET_PROJECTION`) into each result. The separate-gate public-seat run at `outputs/paper_plans/20261025_public_seat_lag2_pipeline_v3/paper_batch/public_seat_trend_hypotheses/result.json` records the full `StrategyConfig`, raw-RMB quote basis and shared Ledger fee basis; it remains research-only. Candidate paper and cost-binding tests: `9 passed`.

The fixed-plus-candidate historical fan-in was regenerated after that cost binding at `outputs/paper_plans/20261025_candidate_batch_v1/pipeline_v5/pipeline_receipt.json`: fixed low-vol half momentum `20/20`, trend candidate `20/20`, and Ridge `5/5` all use the same cost model and benchmark status. The orchestration receipt now records the shared cost/benchmark IDs and rejects mixed versions before publishing the paper result. This is a historical reproducibility rerun, not a weekend market refresh; all three remain research-only and `execution_admitted=false`.

F2 `FP19` is now `VERIFIED_LOCAL`: its binding points to the fixed and candidate v5 result files, and the next closed-session batch must reuse the same cost model and explicit benchmark status or reject the mixed-version run.

The candidate catalog now points to the v5 fixed-plus-candidate result files for both continuing candidates, making the latest cost-bound paper batch the authoritative catalog evidence.

FP19 comparison binding is now present in `trading/research/f01_account_replay.py`: a replay receipt carries the benchmark configuration id, explicit cost model parameters/version and observation window. The local proof `outputs/pipeline_receipts/20261025_f01_comparison_contract_receipt_v1.json` covers 2 synthetic sessions, 2 orders and 4 fills with `execution_admitted=false`; it proves receipt binding only and does not claim PIT market readiness. The focused F01 signal/account tests passed `6`.


## 2026-10-25 high-efficiency dispatch wave

已把两份方案落成机器可读/可执行的波次清单：`docs/architecture_v3/execution_wave_plan_20261025.json` 与同名 Markdown。当前波次只保留两个生产写入者：`next_close_d0_d1.py` 负责下一闭市 D0/D1，`f2_fact_bridge.py` 负责 F2 sidecar→typed fact 的 fail-closed 桥接；根线程负责集成。冗余只放在 PIT 时点、单位/期间、现金/股数守恒和未知外部结果四类错误。

F2 bridge 真实回执 `outputs/pipeline_receipts/20261025_f2_sidecar_fact_bridge_receipt_v1.json` 为 `BLOCKED_DATA`：3 行均因历史 retrieval/as-of 晚于决策日且 issuer scope 缺失而阻断，0 facts，未计算因子、未创建账户回放；合成 PIT-safe 仅用于契约测试。两包聚焦测试分别为 F2 `15 passed`、D0/D1 `3 passed`，未抓周末行情。

## Continuity note

The shared `D:\enforce\capability_lab\handoffs\current_handoff.md` is concurrently used by an unrelated workspace and may be overwritten. This project-local handoff is authoritative for resuming D:\Quantification; `docs/architecture_v3/goal_continuity.json` points to the active evidence files and this handoff.

## Safety

No broker/THS action occurred. `execution_admitted=false` remains unchanged.














## 2026-10-25 D0/D1 next-close contract

- Added `trading/research/next_close_d0_d1.py` and `run_committee.py next-close-d0-d1`. The command binds one explicit close date to a read-only local StockDB snapshot, strict same-day fixed/candidate target sources, same-day snapshot quotes, shared `Ledger` paper packages, and one cost/benchmark receipt. It never fetches a signal, writes a persistent account, contacts THS/broker, or sets `execution_admitted=true`.
- The contract requires each fixed/candidate source target `decision_day == close_date`; stale targets fail before capture. Missing StockDB rows are retained in `contract_receipt.json` with `BLOCKED_MISSING_SAME_DAY_STOCKDB_ROWS`; no prior close is carried forward.
- Focused fixture acceptance: `D:\Quantification\env\python.exe -m pytest trading/research/tests/test_next_close_d0_d1.py -q` -> `3 passed`. No live StockDB capture was attempted on the weekend; the next permitted action is after the 2026-10-26 close with same-day strategy source files.
- Example next-close invocation (fill exact same-day source paths and explicit account/cash): `D:\Quantification\env\python.exe D:\Quantification\run_committee.py next-close-d0-d1 --source-manifest <same-day-candidate-source-manifest.json> --fixed-terminal-account <same-day-fixed-account.json> --fixed-strategy-id low_volatility_252_20 --close-date 2026-10-26 --execution-day 2026-10-27 --settlement-day 2026-10-28 --initial-cash <research-cash> --output-dir D:\QuantificationData\trading\research\next_close_d0_d1\20261026`.

## 2026-10-25 W3 candidate evidence

R04 的预先登记 60-session mature-factor Ridge 变体已完成：fit 使用现有 feature/provider 冻结输入，648-session 共享 Engine/Ledger replay 产生 279 orders、556 fills，净收益 32.1767%、最大回撤 -19.1039%、Sharpe 0.7395，安全闸门均为 false。目录绑定校验 `outputs/pipeline_receipts/20261025_candidate_catalog_audit_v3.json` 返回 `VERIFIED_CANDIDATE_CATALOG_READONLY`，9 个候选无缺失证据；该校验只验证绑定，不改变研究结论或晋级状态。
## 2026-10-25 additive Ridge h60 comparison

为避免重复重放，直接复用了 R04 已完成的 60-session Ridge Engine/Ledger 结果，把它作为同一冻结 StockDB 快照下的独立变体行接入长窗口比较。新增 `trading/research/long_window_strategy_comparison.py --include-h60` 与测试覆盖，基线五策略回执保持不变。

- 回执：`outputs/pipeline_receipts/20261025_long_window_strategy_comparison_h60_v1.json`
- 报告：`outputs/research_reports/20261025_long_window_strategy_comparison_h60_v1.md`
- h60：648 sessions、279 orders、556 fills、净收益 32.1767%、最大回撤 -19.1039%、Sharpe 0.7395、费用 4,129.55、10 个 blocked order events。
- 该行复用成熟因子候选 spec 仅作为 replay permission；仍是 research-only，`execution_admitted=false`、`strategy_validated=false`、`ledger_mutated=false`。
- 测试：`D:\Quantification\env\python.exe -m pytest -q trading/tests/test_long_window_strategy_comparison.py` -> `3 passed`。

下一步仍是 2026-10-26 收市后的真实 D0/D1；本包没有抓周末行情、没有访问 THS/券商。
## 2026-10-25 staffing plan

两份设计文档已经落成机器可读的 W0-W5 staffing plan：`docs/architecture_v3/staffing_plan_20261025.json` 和 `.md`。每包一名主写入者、一名独立验收者、根线程集成，最多两个实质包并行；冗余只覆盖 PIT 时点、单位/期间、现金/股数/T+1 守恒和未知外部结果恢复。当前 W1 等待 2026-10-26 收市，W2 等原始 PIT 证据，W3 已完成 h60 同快照比较，W4/W5 依赖前置结果。


## 2026-10-25 F2 vertical slice delivery

Added `trading/research/f2_vertical_slice.py` and `run_committee.py f2-vertical-slice`. An explicit PIT-safe fact/quote spec now runs the existing F01 calculation, typed SignalFrame, equal-weight target construction, and shared paper target package through the seven named stages. Late or unavailable facts return `BLOCKED_DATA` before target/order/account creation; no latest-file inference, network, broker or THS action is reachable.

Synthetic acceptance receipt: `outputs/f2_vertical_slice/20261025_synthetic/vertical_slice_receipt.json` (`COMPLETED_RESEARCH_ONLY_F2_VERTICAL_SLICE`, two paper orders, projected equity CNY 99,987.85, `execution_admitted=false`). Focused tests: `D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_f2_vertical_slice.py trading/research/tests/test_f2_fact_bridge.py` -> `5 passed`; py_compile passed. This is a contract/vertical-slice proof only; the real F2 sidecar remains `BLOCKED_DATA` until historical as-of/revision and issuer-scope evidence exists.

## 2026-10-25 root integration verification

Integrated the F2 vertical-slice evidence into `docs/architecture_v3/goal_continuity.json` and W2 in `docs/architecture_v3/execution_wave_plan_20261025.json`. Root verification reran the changed package only: `trading/research/tests/test_f2_vertical_slice.py` plus `trading/research/tests/test_f2_fact_bridge.py` -> 5 passed; changed Python files compile and the four machine-readable JSON files parse. The synthetic slice remains research-only and does not upgrade the real F2 sidecar, which is still `BLOCKED_DATA`.

Safety unchanged: `execution_admitted=false`; no login, broker/THS action, order, cancel, transfer, paid data, or irreversible cleanup.

## 2026-10-25 F2 bridge-to-paper adapter delivery

Added `trading/research/f2_bridge_vertical_slice.py` and `run_committee.py f2-bridge-vertical-slice`. A `FACTS_READY_NO_FACTOR` bridge receipt now joins only an explicit prior-period fact and same-day quote input before delegating to the F2 vertical slice; a blocked bridge, missing prior fact, or missing quote stops before Signal/Target/Orders. Synthetic acceptance receipt: `outputs/f2_vertical_slice/20261025_bridge_ready_synthetic/bridge_vertical_slice_receipt.json` with 1 paper order and projected equity CNY 99,993.80. Focused F2 tests: 8 passed including bridge, adapter and vertical-slice cases. The real sidecar remains `BLOCKED_DATA`; no current provider, broker, THS or live account was accessed.

## 2026-10-25 root verification of bridge adapter

The bridge adapter and CLI were re-run from the saved synthetic READY receipt. Focused tests now pass `8`; `run_committee.py f2-bridge-vertical-slice` produced a research-only receipt with `execution_admitted=false`. Machine-readable F2 binding, goal continuity and wave plan parse successfully. The real bridge receipt remains `BLOCKED_DATA` with zero ready rows and zero account replay.

## 2026-10-25 aligned joint portfolio paper delivery

Added `trading/research/joint_portfolio_paper_package.py` and `run_committee.py joint-portfolio-paper-package`. The existing P06 target-only contract remains unchanged; this supplemental route converts two date-aligned saved strategy accounts (mature-factor Ridge and Alpha158/LightGBM) into one lot-rounded target payload and sends it through the shared paper order, BrokerAdapter route, fill and cash/position projection. Receipt: `outputs/pipeline_receipts/20261025_joint_portfolio_paper_package_v1/joint_paper_receipt.json`; 7 paper orders, modeled fees CNY 35.76, projected final equity CNY 139,964.24, 2 routes available and 5 routes blocked, `execution_admitted=false`.

The first run exposed and fixed a real lot-rounding mismatch: the paper route initially reused economic weights and produced zero shares for the expensive 301686.SZSE row. It now consumes the accepted lot-rounded executable weights, preserving the one-lot policy. Focused joint tests pass `5`; CLI and changed files compile. No broker/THS action or persistent Ledger mutation occurred.

## 2026-10-25 root regression after joint package

Focused regression covering the new joint paper route, existing P06 contract, F2 bridge/vertical slices and next-close contract passed `16 tests`. Changed Python files compile and the V3 binding, goal continuity and execution wave JSON files parse. The preserved lot-rounding failure receipt remains linked to P07; the repaired run is the authoritative paper package.

## 2026-10-25 direct target input adapter

为缩短下一交易日 D0/D1 的接线路径，`trading/research/next_close_d0_d1.py` 现在接受候选输入清单中的 `target` 文件引用。适配器只接受 `decision_day` 对齐且 `research_only=true`、`execution_admitted=false` 的预构建目标，并校验清单中的 `strategy_id`；随后把原始目标复制进冻结运行目录，继续使用同一 StockDB 快照、Engine/Ledger 与纸面订单路径。原有 replay/terminal account 输入契约保持兼容。

新增同日预构建 target fixture 后，`D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_next_close_d0_d1.py` 为 `4 passed`，`py_compile` 通过。W1 已绑定该模块和测试；真实 2026-10-26 收盘前不抓取、不伪造行情，`execution_admitted=false` 保持不变。

## 2026-10-25 current-date three-strategy paper package

将 2026-10-23 同一终止日的固定低波、半动量和 12-1 动量三条账户接入 `joint_portfolio_paper_package`。这次接线暴露并修复了两个主线问题：联合目标的板块最小交易单位此前统一按 100 股，导致科创板 688498.SSE 的 200 股规则在 Orders 阶段被拒；联合目标此前只传可执行权重，浮点向下取整又可能把已接受的最小手数变回非法数量。现在 `joint_portfolio_acceptance.py` 按板块规则生成数量，`joint_portfolio_paper_package.py` 将数量写入目标，`stockdb_paper_target_package.py` 在目标携带显式数量时直接复用并仍由共享 Ledger 校验。

聚焦测试：联合目标、联合纸面包和 D0/D1 共 `9 passed`，改动模块 `py_compile` 通过。当前三策略包在明确的 CNY 10,000,000 容量诊断下生成 `60` 个纸面订单；CNY 2,000,000 运行保留为 `BLOCKED_JOINT_TARGET` 容量回执，未静默放宽约束。两份回执均 `execution_admitted=false`，没有持久 Ledger、券商或同花顺写入。

## 2026-10-25 joint paper rejection receipt

联合纸面包现在把共享 Orders/Ledger 的 `ValueError` 转为 `BLOCKED_PAPER_TARGET_ORDER` 回执，并保留已构建的目标、报价和原始拒绝原因；这样容量、板块或现金约束不会以无上下文的 CLI traceback 结束，且不会写入持久账户。新增回执契约测试后，联合纸面包、联合目标与 D0/D1 聚焦测试为 `10 passed`，改动模块编译通过。

## 2026-10-25 budgeted joint target delivery

为解决三策略在 140,000 元研究账户下因多标的整手约束而整包阻断，联合目标新增显式 `capacity_policy=budgeted`。它按板块最小数量、目标权重、单票上限和总现金预算确定性选择最多 22 个可执行目标，并把被容量裁掉的标的及原因留在 `dropped_rows`；默认 `strict` 模式不变。当前固定低波、半动量、12-1 动量三条 2026-10-23 账户的预算模式回执为 22 个纸面订单、38 个容量裁剪行，继续走共享 Orders/Fill/Cash/Position 路径，`execution_admitted=false`。

新增预算模式 fixture 后，联合目标、联合纸面包和 D0/D1 测试为 `11 passed`，改动模块编译通过。回执：`outputs/pipeline_receipts/20261025_joint_portfolio_paper_package_current_v1/budgeted_140k/joint_paper_receipt.json`。这仍是研究纸面目标，不代表当前券商或同花顺订单。

## 2026-10-25 same-day Target+Quote joint route

联合目标接线已从历史账户文件扩展到候选输入清单直接提供的同日 `Target+Quote` payload。`build_joint_portfolio_from_payloads` 校验策略身份、决策日、报价日、原始人民币价格和只读标志，再交给相同的板块单位与预算分配；`joint-portfolio-paper-package --payload-manifest` 随后复用同一个纸面 Orders/Fill/Cash/Position 包。

用现有候选输入清单做历史同日重放，预算模式产生 22 个纸面订单，回执为 `outputs/pipeline_receipts/20261025_joint_payload_paper_integration_v1/joint_paper_receipt.json`。联合目标、联合纸面包和 D0/D1 测试为 `12 passed`，模块编译通过；这为下个收盘后的候选 bundle 直接接入 D0/D1 留下了可执行入口，仍未访问券商或同花顺。

## 2026-10-25 next-close joint paper handoff

`next_close_d0_d1.py` 现在可选开启 `joint_capacity_policy=budgeted`，在同一冻结 StockDB close snapshot 下，把 fixed target 和候选 Target+Quote payload 送进联合容量分配，再复用共享纸面 Orders/Fill/Cash/Position；原有 fixed 单腿和 candidate batch 回执继续保留。输入指纹包含联合模式和最大目标数，重复运行仍按同一输入复用。

新增 D0/D1 fixture 验收后，联合目标、联合纸面包和 D0/D1 测试为 `13 passed`，模块编译通过。下一个真实收盘只需在命令中加 `--joint-capacity-policy budgeted`，不访问券商或同花顺。


## 2026-10-25 shortline LR shared paper route

已将保存的短线 LR 目标接入现有联合纸面流水线：输入 `D:/QuantificationData/trading/research/shortline_native_selection_replay/20261018_v1/lr/account.json`，终止日为 `2026-06-10`，在严格容量策略下生成 5 笔纸面订单，5 条路由均可用，回执为 `outputs/pipeline_receipts/20261025_shortline_lr_joint_paper_v1/joint_paper_receipt.json`。该结果实际复用了 Target Portfolio→Orders→BrokerAdapter→Fill→Cash/Position 路径，`execution_admitted=false`，没有真实登录、券商/同花顺写入或持久账户变更。

这次接线只证明保存目标可以进入共享纸面路径；短线 LR/LGBM 的 `UNASSESSED_SEPARATE_GATE`、`paper_allowed=false`、完整窗口缺失和既有 score-gate 阻断均保持不变，不把 5 笔纸面订单解释为策略收益或晋级证据。P11 已绑定到工作包清单，W3 与候选目录同步了该回执。
