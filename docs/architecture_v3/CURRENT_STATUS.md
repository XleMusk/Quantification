# 当前实施状态（2026-10-24）

这页是《A股量化系统_GitHub核查与完整落地规划_20261022.docx》的当前绑定页。它记录当前本地检出和可复核证据，不把源文档在 2026-10-22 的 GitHub、PR、测试条数或同花顺观察直接当作今天的事实。

后续工作按 [`implementation_route_20261024.md`](implementation_route_20261024.md) 和 [`implementation_route_20261024.json`](implementation_route_20261024.json) 执行：先数据刷新和纸面日切，再同样本候选比较，最后推进 F01 与 THS 只读证据；防御性工程只有在直接验收失败时才增加。

机器可读绑定见 [`github_plan_20261022_binding.json`](github_plan_20261022_binding.json)。V3 与 F2 的原有追溯分别见 [`TRACEABILITY.md`](TRACEABILITY.md) 和 [`F2_TRACEABILITY.md`](F2_TRACEABILITY.md)。

## 当前边界

- 当前检出：`main`，HEAD `a9f9f3dff12401c885ac8d272af402d1d2db7fb0`。
- 当前执行闸门：`execution_admitted=false`。
- 禁止动作：真实券商或同花顺登录、真实预填提交、报单、撤单、转账、付费数据购买。
- 现有未提交改动保留在工作区；本轮没有合并、重置、推送或创建 PR。
- 源文档中的历史分支和 PR 状态标记为 `HISTORICAL_UNVERIFIED_CURRENT`；外部 PR 状态没有被本页伪造为已知。

## 已验证的本轮增量

### P0 撤单竞态

`trading/ledger.py` 在迟到成交时保留原有 `cancel_pending`，仍发出独立的 `partial` 成交事件并带上 `order_status`；剩余部分确认撤单后才进入 `cancelled`。全部成交优先进入 `filled`，不会被后续撤单覆盖。

新增的竞态例位于 [`trading/tests/test_trading.py`](../../trading/tests/test_trading.py)：

- `test_partial_fill_during_cancel_pending_preserves_pending_state`
- `test_full_fill_during_cancel_pending_wins_over_cancel`

本轮局部回归：`56 passed`（`trading/tests/test_trading.py`、`trading/tests/test_broker.py`）。

成交事件的 `kind` 继续表示实际成交事件（`partial` 或 `filled`）；撤单竞态下的订单状态通过同一事件的 `order_status` 字段保留为 `cancel_pending`。读取旧事件的消费者应按订单状态字段判断待撤，而不是把成交事件当作撤单确认。

### 架构边界

核心仍然只通过明确的 `CORE_SUPPORT`、`CORE_PACKAGE_BRIDGES` 和 `WORKBENCH_COMPOSITE` 允许研究包桥接；没有把整个 `trading.research` 加入白名单。本轮架构审计结果为 `531 modules / 0 violations`，下一次代码改动后须重新运行同一审计。

新增 [`tools/run_offline_ci.ps1`](../../tools/run_offline_ci.ps1) 作为本地候选检查入口。本轮回执为 [`offline_ci_receipt.json`](../../outputs/offline_ci/20261024_090611_645_c85210ed/offline_ci_receipt.json)：核心、F2、本地 Web API、Alpha158 split、隔离联合场景和组合目标比较聚焦测试 `210 passed`，架构审计 `536 modules / 0 violations`；回执记录的是命令范围内的自声明无网络、无账户访问，并带有工作树状态和 diff 指纹。该数字只属于本地选择的测试集合，不是源文档所述 PR 作者测试条数。

F01 研究账户包新增 `restore_bundle()`，只复制源 manifest 列出的文件，并在空目录中重新校验哈希；对应测试已通过。

新增 [`recovery_bundle.py`](../../trading/research/recovery_bundle.py) 将配置、策略、输入快照、纸面账本和日结游标作为带版本和哈希的同一恢复包，并使用 SQLite Backup API 生成一致备份；空目录恢复、数据库完整性和篡改拒绝测试已通过。`trading.store.recover_interrupted_runs()` 现在会在 worker 启动恢复时保留游标和状态哈希、只记录一次中断回执，并要求显式 paper/replay resume；隔离子进程和 bounded `worker.main` 已验证日切检查点不重复、恢复后从下一游标继续。该证据使用合成输入，未升级为真实市场或账户恢复。

新增 [`production_receipt_audit.py`](../../trading/research/production_receipt_audit.py) 和 `run_committee.py production-receipt-audit`，对已有 Alpha158 日更实际回执及路线兼容回执做只读结构审计。当前审计结果为 `VERIFIED_LOCAL_READONLY_RECEIPT`，明确标注 `HISTORICAL_RECEIPT`、无账户推进、无客户端交互；它复核已有证据，不重跑 producer/split。

新增 [`f01_worker.py`](../../trading/research/f01_worker.py) 作为 F01 的持久任务边界：每个 `run_key:attempt` 只写一份请求和终态回执，完成态必须通过共享账户包 manifest 校验；只有请求没有终态时返回 `UNKNOWN_INTERRUPTED_NO_RETRY`，不自动重跑。它可选绑定现有 [`trading/store.py`](../../trading/store.py) 的 research_attempts 表，已验证请求和终态会进入同一 SQLite store；worker 仍使用合成输入测试，尚未接入真实财报任务 API。

新增 [`f01_task_binding.py`](../../trading/research/f01_task_binding.py) 作为财报任务入口闸门：`BLOCKED_DATA` 的输入不会创建任务；只有 `READY_FOR_F01_FACT_ASSEMBLY` 且 `execution_admitted=false` 才能向现有 store 注册 paper 任务。当前 readiness 仍被单季度输入和 PIT 未验证阻断，因此本地只验证了闸门与 store 绑定语义，没有伪造真实财报事件。

已用当前财务 readiness 回执做了一次只读绑定：[`f01_task_binding_blocked_20261024.json`](../../outputs/financials/f01_task_binding_blocked_20261024.json) 返回 `BLOCKED_DATA`，原因是 `single_quarter_inputs_missing` 和 `pit_not_verified`，`task_created=false`。

离线候选检查现在纳入现有 Alpha158 split runner 的 3 个生命周期/幂等/只读状态例；它与撤单竞态、证券转换和共享账本检查使用同一 checkout，但双分支整合候选仍未宣称完成。

新增 [`integration_harness.py`](../../trading/research/integration_harness.py)、[`tools/run_integration_candidate.ps1`](../../tools/run_integration_candidate.ps1) 和 [`test_combined_integration_candidate.py`](../../trading/tests/test_combined_integration_candidate.py)，并生成 [`integration_candidate_receipt.json`](../../outputs/integration_candidate/20261024_085859_088_5f2cfcf8/integration_candidate_receipt.json)：隔离合成副本场景联合验证撤单竞态、证券转换和 Alpha158 split，共 `21 passed`，架构审计 `535/0`。回执标为 `ISOLATED_COMBINED_SCENARIO_PASS`；由于 Alpha158 持久 checkpoint 有不可变 journal/hash 链，这仍不是共享生产状态或双分支已整合证明。

新增 [`portfolio_allocation_comparison.py`](../../trading/portfolio_allocation_comparison.py)，把经济目标权重与当前持仓、可卖数量、冻结委托、费用和现金拆开比较；它只生成可执行目标解释，不创建订单或修改账本。

`trading.engine` 现在在生成 FULL_TARGET 后记录 `economic_executable_target_comparison`，并把比较结果保存在账户状态中；后续订单仍由原有 target → order → broker adapter → fill 路径生成。组合比较与账本分离的回归例已通过。

### 数据与 F2

- 66 页、6 组已有原始缓存的只读回审为 complete；没有网络访问，也不等于 PIT 已就绪。
- 财务采集仍为 partial：`53,520 / 80,505` tasks，缺失 `26,985`，完整证券 `3,568 / 5,367`。
- F01 readiness 仍为 `BLOCKED_DATA`，阻塞项是单季度输入和 PIT 未验证。
- F01、fetch receipt、时间区间、标签成熟度和共享账户回放均继续沿用同一 Data → Signal → Target Portfolio → Orders → BrokerAdapter → Fill → Cash/Position 路径。

### 直接回放产物

复核并固定了一份已有 StockDB 低波动回放的完整账户产物：[`20261024_stockdb_lowvol_historical_pipeline.json`](../../outputs/pipeline_receipts/20261024_stockdb_lowvol_historical_pipeline.json)。对应历史回放覆盖 `2026-08-31..2026-10-16`、29 个交易日，生成 20 个目标、25 个订单和 44 个成交，净收益 `1.19%`、最大回撤 `-2.03%`，并保留现金、持仓、订单、成交和事件流水。该结果是研究诊断，不是未触碰样本或策略验证。

按当前代码重新冷启动同一窗口时，回放在 `2026-09-01` 因 `000001.SZSE` 的 `Mixed StockDB execution reference basis` 被阻断；这是现行“持仓跨日必须有独立 native raw 执行引用”的数据门槛，已保留失败产物 [`20261024_091210_21577e92`](../../outputs/stockdb_rolling_low_volatility/20261024_091210_21577e92)。没有用成交量推断替代原始状态，也没有把这次失败写成策略结果。

随后已把补充 raw_daily 的显式 `tradestatus/adjustflag` 标注到 StockDB 重叠行，同时保留 StockDB 的 OHLCV；专项及组合回归测试 `26 passed`。本地组合视图将 `stock_daily_bulk_extended` 的 `2026-10-13` 前原始状态与 `lowvol_supplements` 的 `2026-10-14..16` 状态拼接后，固定覆盖窗口 `2026-08-31..2026-10-16` 已成功完成共享 Engine/Ledger 回放：29 个交易日、三个候选全部完成。低波动 `+1.19% / -2.03%`、动量 `-2.93% / -3.41%`、低波动半动量 `-6.70% / -7.21%`。随后从本机 StockDB 捕获 `2026-10-19..23` 日线，合并视图推进低波动账户到 `2026-10-23`：34 个交易日、25 个订单、44 个成交，净收益 `-0.26%`、最大回撤 `-2.03%`，目标仍为 20 个等权标的。证据汇总见 [`20261024_stockdb_replay_paper_progress.json`](../../outputs/pipeline_receipts/20261024_stockdb_replay_paper_progress.json)。

最新低波动结果已注册为外部 `QUANT_TRADING_HOME` 下的持续纸面研究账户 [`D:\QuantificationData\trading\fixed_daily\20261024_lowvol_current`](../../../../QuantificationData/trading/fixed_daily/20261024_lowvol_current)，当前种子日为 `2026-10-23`。近期 StockDB 捕获覆盖率为 `24,881 / 26,005`，缺失 `124` 行；当前 20 个持仓标的在这些日期均无缺失。纸面计划回执 [`20261024_lowvol_current_paper_plan_receipt.json`](../../outputs/paper_plans/20261024_lowvol_current_paper_plan_receipt.json) 保持研究-only；没有生成当前日 THS 提交内容。

已基于该纸面目标生成 [THS 只读参考请求](../../outputs/paper_plans/20261024_lowvol_current_ths_readonly_request/receipt.json)：20 个标的、权重合计 `0.8`，并绑定当前纸面持仓 reconciliation（19 个标的、20,400 股）；价格、目标股数和差额仍留空，等待未来同日行情/账户只读捕获；没有伪造 action gate，也没有接触 THS 客户端。

本周复盘回执见 [`20261024_weekly_review_receipt.json`](../../outputs/weekly_reviews/20261024_weekly_review_receipt.json)：最大阻碍是 THS 外部只读捕获、近期 124 行缺失数据和 F01 单季度/PIT 输入；下一闭市日优先刷新 StockDB 与纸面账户。

今日（2026-10-24）为周末闭市，日切状态见 [`20261024_daily_refresh_status.json`](../../outputs/paper_plans/20261024_daily_refresh_status.json)；未重复回放，下一预期交易日为 2026-10-26。

S2 已执行一次 Alpha158/LightGBM 有界候选面板回放：25 个行情日、18 个候选标的、15 个订单、30 个成交，净收益 `-1.98%`；与低波动控制的共同窗口指标和边界见 [`20261024_alpha158_route_receipt.json`](../../outputs/pipeline_receipts/20261024_alpha158_route_receipt.json)。随后在同一冻结 StockDB OHLCV 快照、同一 `2026-08-31..2026-10-09` 窗口、同一 14 万初始资金和成本配置下重跑两者，24 个共同会话的 Alpha158/LightGBM 为 `-2.00%`、低波动为 `+0.57%`；完整回执见 [`20261024_same_snapshot_strategy_comparison_receipt.json`](../../outputs/pipeline_receipts/20261024_same_snapshot_strategy_comparison_receipt.json)。低波动额外使用本地 `raw_daily` 的显式状态标注，Alpha158 未消费该状态流，因此这仍是可复现诊断，不是全市场、PIT 或策略采用结论。

当前候选库索引见 [`candidate_strategy_catalog_20261024.md`](candidate_strategy_catalog_20261024.md)：固定低波动、趋势/动量、Alpha158/LightGBM、成熟因子、短线连板、公开席位和龙头风格均保留各自证据路径与不可比边界，没有把不同快照的收益混排。

短线候选本轮额外保留了 [`20261024_panel_contract_blocked.json`](../../outputs/shortline_native_selection_replay/20261024_panel_contract_blocked.json)：冻结评分文件 25,150 行全部为 `paper_candidate_only=false`，因此 `shortline-panel-replay` 在输入契约阶段正确阻断；没有手改标志或伪造候选。
新增 `trading/research/shortline_score_gate_audit.py` 与 `shortline-score-gate-audit` 命令，对该冻结评分文件做可重复的只读审计：按 `event_id+model_kind+generation_id` 识别跨模型同事件，不把合法的 LR/LightGBM 同事件行误判为重复；25,150 行中 `paper_candidate_only=true` 为 0，唯一阻断为 `paper_candidate_only.none_true`。回执 [`20261024_shortline_score_gate_audit_v1.json`](../../outputs/pipeline_receipts/20261024_shortline_score_gate_audit_v1.json) 与报告 [`20261024_shortline_score_gate_audit_v1.md`](../../outputs/research_reports/20261024_shortline_score_gate_audit_v1.md) 保留该事实，未修改输入或生成候选目标。

已有的短线保存选择回放已补充机器审计 [`20261024_shortline_saved_replay_audit_v1.json`](../../outputs/pipeline_receipts/20261024_shortline_saved_replay_audit_v1.json)：LR 运行到 103/187 个会话、LightGBM 运行到 95/187 个会话，分别生成 223/190 个研究委托和 446/380 个成交回调；两套完整窗口收益均为 `null`，停止原因和未解决参考条款保留。该回执证明共享 Engine/Ledger 的研究路径和停止点，不把截断结果写成策略收益。

已有公开席位/趋势批量回放已接入 `trading/research/public_seat_replay_audit.py` 与 `public-seat-replay-audit` 命令：9 个假设中 5 个完成 241 会话诊断，合计 1,205 个完成会话、3,836 个研究委托和 5,926 个成交回调；4 个假设在 145 或 167 个会话处因未解析的持仓/执行价参考停止，完整窗口收益保持未知。机器回执 [`20261024_public_seat_replay_audit_v1.json`](../../outputs/pipeline_receipts/20261024_public_seat_replay_audit_v1.json) 与报告 [`20261024_public_seat_replay_audit_v1.md`](../../outputs/research_reports/20261024_public_seat_replay_audit_v1.md) 已绑定候选目录；公告时点、PIT 成员资格、因果席位 Alpha 均保持未验证，`execution_admitted=false`。

证据文件：

- [`receipt_audit.json`](../../outputs/bulk_annual_financials/20261004_220836_94303b12/receipt_audit.json)
- [`collection_audit.json`](../../outputs/financials/20260930_162448_8be1e603/collection_audit.json)
- [`f01_readiness_20261024.json`](../../outputs/financials/f01_readiness_20261024.json)

## 阶段结论

|阶段|状态|当前结论|
|---|---|---|
|S0 可整合基线|`PARTIAL`|撤单竞态已有本地修复，离线候选检查已可重复；历史双分支整合候选尚未形成。|
|S1 可信输入与日程|`PARTIAL`|receipt 和时点合同已在代码中；真实财务输入仍有覆盖与 PIT 阻塞。|
|S2 研究净增量|`PARTIAL`|Alpha158/LightGBM 已完成有界候选面板共享账本诊断；与低波动控制的共同窗口比较已留证，但尚无采用结论。|
|S3 组合与风险|`PARTIAL`|共享账本和基础整手分配可复用；成本、冻结、暴露和多策略归因仍需实证。|
|S4 内部稳定版本|`PARTIAL`|目标连续性和失败交接已落地；浏览器写链与空目录恢复演练未完成。|
|S5 可选真实渠道|`BLOCKED_POLICY`|继续保持只读 THS 观察与纸面计划，执行闸门不开放。|

## 下一步顺序

1. 在下一个闭市日继续捕获本机 StockDB，并保留 124 个缺失行及覆盖变化；不把缺失当作零。
2. 在同一完整窗口上继续重跑成熟因子/Ridge 与 Alpha158/LightGBM 候选，保留相同成本、样本和失败原因。
3. 将通过回放的目标持仓接入每日纸面计划和只读 THS 证据链；不提交、不登录、不下单。
4. 数据满足 F01 的单季度与 PIT 闸门后，选择一个新信息机制进行同条件净增量比较。
5. 隔离整合和空目录恢复只保留为支撑性验收，不再扩展与直接回放无关的防御性协议。

每一步都要更新机器可读绑定并保留失败、缺失和阻塞证据。策略历史结果、纸面运行与真实渠道资格仍然分开。

完整目标审计见 [`completion_audit_20261024.md`](completion_audit_20261024.md)：当前状态为 `INCOMPLETE_ACTIVE`，研究/纸面路径已有证据，THS 外部回读、F01 PIT、候选完整验证和真实执行前置仍未完成。

F01季度缓存审计（2026-10-24）：本地季度缓存已有 52,034 条 acquired 记录，但提供字段只有 netProfit、MBRevenue 等，缺少 F01 所需 parent_net_profit 与 total_assets；netProfit 不被静默改名，PIT 和单位仍未验证。详见 outputs/financials/f01_quarterly_cache_audit_20261024.json。

D1 纸面账户连续性核对：2026-10-23 为当前最后日，34 个日线记录无重复日期，25 个订单、44 个成交，generation=0、无 pending target；详见 outputs/paper_plans/20261024_lowvol_current_continuity_receipt.json。
D1 测试回归修复：合成日切夹具补上显式 native raw 状态后，calendar month-end 与 fixed-daily 聚焦测试 16/16 通过；生产执行引用门槛未放宽。
THS 只读参考包已绑定 D1 连续性回执哈希；仍为 blank readback、无客户端交互、不可预填，等待同一 reference generation 的授权只读捕获。
### U03 provenance slice (2026-10-24)

`trading/research/pipeline_provenance.py` 已对同快照 Alpha158/LightGBM 纸面回放做只读来源链校验：Data、Signal、Target Portfolio、Orders、BrokerAdapter、Fill、Cash/Position 七阶段均有实际事件，源快照 manifest、score batch、score parquet、回放结果和比较回执均记录哈希。结果见 `outputs/pipeline_receipts/20261024_u03_pipeline_provenance_receipt.json`，状态 `VERIFIED_LOCAL_PIPELINE_PROVENANCE`；`execution_admitted=false`、未访问券商/同花顺。该证据是本地回放可追溯性，不等同于 PIT 或真实渠道验证。

### O03 paper-account recovery rehearsal (2026-10-24)

针对当前 `fixed_daily` 纸面研究账户完成了一次真实输入复制恢复演练：[`paper_account_recovery.py`](../../trading/research/paper_account_recovery.py) 读取并校验 `head.json`/`registration.json` 指向的 run、protocol、account、replay、daily、snapshot manifest 与交易日历，使用带 manifest 哈希的恢复包复制核心状态，恢复到空目录后核对现金和持仓。回执见 [`20261024_lowvol_current/receipt.json`](../../outputs/recovery_rehearsals/20261024_lowvol_current/receipt.json)：`RECOVERY_REHEARSAL_VERIFIED`、generation `0`、最后日 `2026-10-23`、现金 `4,341,307` cents、20 个持仓标的，`cash_equal=true`、`positions_equal=true`、`execution_admitted=false`。244 MB 的原始补充输入未复制，仅按原 SHA-256 记录为外部依赖，避免把一个大文件的外部引用伪装成完整迁移。核心模块与恢复包测试 `5 passed`；O03 已在 `work_packages_v3_binding.json` 绑定为 `VERIFIED_LOCAL`，下一次纸面日切后复演并保留 worker 中断回执。

### Paper target reconciliation slice (2026-10-24)

已将当前纸面账户与固定的 2026-10-23 StockDB raw close 接入同一整手分配器，生成目标股数、当前股数、可卖数量、差额和估算买入费用。代码入口为 `trading/research/paper_target_reconciliation.py`，命令为 `python run_committee.py paper-target-reconciliation`；实际回执为 `outputs/paper_plans/20261024_lowvol_current_paper_reconciliation/receipt.json`，明细为 `target_reconciliation.csv`。

本次结果：20/20 目标收盘行齐全，20/20 账户价格标记与 raw close 一致，账户权益 `139,638.07` 元，估算股票预算 `111,710.45` 元；回执明确 `execution_admitted=false`、`orders_created=false`、`ledger_mutated=false`。目标股数差额已计算，但仍是纸面 close reference，不能替代同花顺当前报价、账户余额或回读，也没有覆盖原有 THS 空白只读 reconciliation。

验收：`test_paper_target_reconciliation.py` 与组合比较测试 `4 passed`；`architecture-check` 为 `539 modules / 0 violations`。

### Paper-plan reconciliation audit slice (2026-10-24)

已将纸面计划、StockDB close reconciliation、fixed_daily 连续性回执和 THS 空白只读模板绑定为同一只读审计。入口为 `trading/research/paper_plan_reconciliation_audit.py`，命令为 `python run_committee.py paper-plan-reconciliation-audit`；回执为 `outputs/paper_plans/20261024_lowvol_current_plan_reconciliation_audit/receipt.json`。

回执状态 `VERIFIED_LOCAL_PAPER_PLAN_RECONCILIATION`，20 个目标、来源日期 `2026-10-23`、`issues=[]`；计划权重、目标数量、账户连续性日期、源文件哈希和 THS 空白字段均一致。仍明确 `execution_admitted=false`、`orders_created=false`、`readback_observed=false`，历史 StockDB close 不被当作当前 THS 行情。

验收：审计聚焦测试 `2 passed`；架构审计更新为 `540 modules / 0 violations`。

### Candidate catalog audit slice (2026-10-24)

新增 `trading/research/candidate_catalog_audit.py` 与 `candidate-catalog-audit` 命令，对固定低波动、趋势/动量、Alpha158/LightGBM、成熟因子、短线连板、公开席位和龙头风格共 9 个候选逐项校验：证据路径真实存在、指标为有限数值、可比组和限制字段齐全。回执 `outputs/pipeline_receipts/20261024_candidate_catalog_audit.json` 状态为 `VERIFIED_CANDIDATE_CATALOG_READONLY`，缺失证据为 0；该审计不混排不同快照收益，也不把诊断结果升级为策略采用。

本轮曾因把布尔完成标记放入数值 `metrics` 触发一次 catalog audit schema 阻断，失败回执 [`20261024_candidate_catalog_audit_metric_schema_failure_v1.json`](../../outputs/pipeline_receipts/20261024_candidate_catalog_audit_metric_schema_failure_v1.json) 已保留；移出该字段后复跑通过，没有重跑任何策略。

验收：`test_candidate_catalog_audit.py` 2 passed；架构审计 `541 modules / 0 violations`。

### Fixed strategy paper provenance slice (2026-10-24)

新增 `trading/research/paper_account_provenance.py` 与 `paper-account-provenance` 命令，对当前固定低波动纸面账户的 StockDB 数据、信号/目标、订单、模拟 BrokerAdapter 事件、成交和现金/持仓进行七阶段核对。回执 `outputs/pipeline_receipts/20261024_lowvol_paper_account_provenance.json` 为 `VERIFIED_LOCAL_PAPER_ACCOUNT_PROVENANCE`：2026-10-23 截止，34 个日线、148 个事件、25 个订单、44 个成交，七阶段全部为 `true`，`issues=[]`。

该回执只证明本地研究纸面链路可追溯，模拟 BrokerAdapter 不是实盘成交；PIT、THS 和真实账户资格仍保持独立边界。

### Calendar-bound trend comparison slice (2026-10-24)

在与低波动控制相同的 StockDB 快照、补充 raw 状态、交易日历、成本和共享 Engine/Ledger 上，完成了 `momentum_12_1_20` 与 `low_vol_half_momentum_20` 的 678 个会话回放（2024-01-02 至 2026-10-23）。动量策略净收益 `-13.51%`、最大回撤 `-18.38%`、146 个订单和 201 个成交；低波动半动量净收益 `+10.08%`、最大回撤 `-16.58%`、391 个订单和 656 个成交。双变体回执为 [`20261024_121737_d2456fd6/run.json`](../../outputs/stockdb_rolling_low_volatility/20261024_121737_d2456fd6/run.json)，共享流水线回放审计为 [`20261024_stockdb_momentum_long_replay_audit.json`](../../outputs/pipeline_receipts/20261024_stockdb_momentum_long_replay_audit.json)，状态 `VERIFIED_STOCKDB_HISTORICAL_REPLAY`、问题为空。

这两个结果已写入候选库 JSON，并保留原 29 日诊断字段；长窗字段标注为同一 `stockdb_2024_0102_1023_calendar_bound` 可比组。它们仍是研究诊断，不是 PIT、未触碰留出或策略采用结论，`execution_admitted=false` 保持不变。

### Full-market Alpha158 current-snapshot run (2026-10-24, research complete)

只读全市场 Alpha158 诊断已完成，输入为冻结 StockDB 快照 `stockdb_2024_0101_1013_current_cutoff_v1`，实际数据截止 `2026-10-13`，覆盖 5,322 个 SH/SZ 标的、669 个交易日和 158 个因子；源数据 3,435,917 行，其中 15 行因非正成交量/质量条件被明确排除，没有零填。运行协议 [`protocol.json`](../../outputs/full_market_alpha158/20261024_122933_6bf1a087_sh_sz_all_equities/protocol.json) 为 `completed_research_only`，并已固化完成回执 [`20261024_full_market_alpha158_completion_receipt.json`](../../outputs/pipeline_receipts/20261024_full_market_alpha158_completion_receipt.json)。该过程只写研究诊断目录，不访问账户、券商或同花顺；15 个 candidate proxy replay 仍是 provider pct_chg 研究代理，不是共享 Engine/Ledger 账户收益，也不是新鲜留出或策略采用证据。

### F01 scoped financial field evidence (2026-10-24)

对现有发行人报告原文做了一个受限字段核对，回执为 [`f01_scoped_field_coverage_20261024.json`](../../outputs/financials/f01_scoped_field_coverage_20261024.json)，状态 `PARTIAL_FIELD_EVIDENCE_PIT_PENDING`。`600004.SSE`、`688008.SSE`、`000002.SZSE` 的 2024Q1 均有 PDF/TXT、`pubDate`/`statDate`、人民币元单位、合并口径；`parent_net_profit`、`total_assets` 和联合覆盖均为 3/3，并保存行号、摘录和 PDF/TXT SHA-256。

该回执只证明三家公司一个季度的字段事实，不等于全市场季度覆盖。修订安全 PIT、公告事件入库/延迟合同和 provider 字段映射仍未通过，因此没有修改原 readiness、没有创建 F01 任务、没有进入因子或账户回放，`execution_admitted=false` 保持不变。




F01 scoped receipt 已固化为 `trading/research/f01_scoped_field_coverage.py` 只读验证入口，并接入 `run_committee.py f01-scoped-field-coverage --json`。它校验 PDF/TXT 哈希、证据行号/摘录、字段覆盖和 `pit_verified=false` 边界；聚焦测试 `2 passed`，架构审计 `544 modules / 0 violations`。该入口不会写回 receipt、联网或改变 readiness。

F01 来源 sidecar 已进一步固化为 [`f01_publication_sidecar.py`](../../trading/research/f01_publication_sidecar.py)，入口为 `run_committee.py f01-publication-sidecar`，回执为 [`20261024_f01_publication_sidecar_v1.json`](../../outputs/pipeline_receipts/20261024_f01_publication_sidecar_v1.json)。三条 2024Q1 发行人报告的 PDF/TXT 哈希和披露日均核验通过；财务运行回执中的 `retrieved_at` 被明确保留为 provider CSV 证据，没有冒充发行人 PDF 的检索时间。发行人 `retrieved_at`、`asof_at`、`revision_id` 均为 0/3，`pit_safe_rows=0`；provider `netProfit` 与 PDF parent-attributable profit 三条均存在口径或数值差异。该 sidecar 只做来源映射和阻断分类，不创建 F01 任务、不生成因子、不进入账户回放，`execution_admitted=false` 保持不变。

F01 coverage funnel 现在可通过 `--publication-sidecar` 消费上述来源合同，形成 [`f01_coverage_funnel_with_sidecar_20261024.json`](../../outputs/financials/f01_coverage_funnel_with_sidecar_20261024.json)。本次三行市场连接已显式达到 `3/3`，但 issuer retrieval/as-of/revision 仍为 `0/3`，因此 `f01_eligible_rows=0`、状态仍为 `BLOCKED_DATA`；相关测试 `6 passed`。补齐真实时点证据后，同一闸门可直接重新评估，不需要另写一套放行逻辑。

Action publication sidecar 在 v3/v4 中保留了事件聚合前每个来源 artifact 的时点字段和合同结果，并接入显式 `source_retrieved_at` 回退。最新回执 [`20261024_action_publication_sidecar_v4.json`](../../outputs/pipeline_receipts/20261024_action_publication_sidecar_v4.json) 映射 138 个事件、177 个来源 artifact、138 个哈希核验文件；事件级 `retrieved_at_rows=31`、artifact 级为 62，`asof_at_rows=0`、PIT-safe 仍为 0。该增量只恢复真实保存的来源证据，不推断 as-of，不应用公司行动。

v4 已实际接入成熟因子 180,948 行 action context：同日行动行 `961`、PIT-safe 行 `0`、公司行动应用行 `0`，集成回执为 [`20261024_stockdb_dated_action_context_sidecar_v4_receipt.json`](../../outputs/pipeline_receipts/20261024_stockdb_dated_action_context_sidecar_v4_receipt.json)。

v5 在不改变 PIT 判定的前提下，把 action root 的 `input_input_receipts.json` 按标的和年份映射到 138 个事件、177 个来源 artifact 的 `upstream_input_*` 字段：事件级 upstream receipt/retrieved/hash 为 `138/138/138`，artifact receipt 为 `177`；issuer `retrieved_at/asof_at/revision_id` 仍为 `31/0/32`，`pit_safe_rows=0`。这些字段只证明上游年度输入 lineage，不被写入 issuer retrieval，也不触发公司行动应用。回执 [`20261024_action_publication_sidecar_v5.json`](../../outputs/pipeline_receipts/20261024_action_publication_sidecar_v5.json) 与报告 [`20261024_action_publication_sidecar_v5.md`](../../outputs/research_reports/20261024_action_publication_sidecar_v5.md) 已保留。





## 2026-10-24 full native Alpha158 and 190-factor increment

The canonical StockDB Qlib provider is complete for the frozen snapshot through 2026-10-13: 5,322 symbols and 669 sessions. The native Alpha158 build completed 158/158 features on the full calendar grid (3,560,418 rows; 37,254 binary files; finite feature cells 544,111,899; qlib 0.9.7). The one-date frozen 190-factor panel then completed for all 5,322 symbols with 988,487 finite cells.

The dated Alpha158/LightGBM score batch covers 184 dates and 979,248 rows. The cap500 shared Engine/Ledger replay covers 2026-01-05..2026-10-13, 165 symbols, 485 selected event rows, 436 orders, 816 fills, net return -7.3249%, maximum drawdown -28.6817%, fees 4438.42, Sharpe (zero risk-free) -0.3460, and 83 blocked order events. It generated target and paper-order diagnostics while keeping `execution_admitted=false`, `strategy_validated=false`, `ledger_mutated=false` and no THS payload. The earlier cap20 replay ending 2026-06-22 is retained as a range-sensitivity failure experiment.

These outputs are research diagnostics, not strategy adoption, PIT validation or untouched holdout evidence. Provider positive-volume proxy semantics, dated eligibility, PIT membership, corporate actions, and independent forward evidence remain open.

### Selector boundary repair and corrected rerun

A real boundary defect was found in the first full-market Alpha158 batch: a five-session label from 2026-06-29 crossed the 2026-07-01 selection split. `decision_dates()` now requires every selection label to mature before the split. The corrected run is `outputs/full_market_alpha158/20261024_133722_8be29ed5_sh_sz_all_equities/protocol.json` (158/158 definitions, 23 selection dates, 68 later-check sessions, 15 proxy replays). The prior run and its exact case are retained in `outputs/pipeline_receipts/20261024_full_market_alpha158_boundary_failure.json` with status `RETAINED_BOUNDARY_FAILURE`; it is not used for candidate selection or promotion.

## Mature-factor same-snapshot bridge

The completed StockDB provider/native/190-factor snapshot now reconstructs the frozen mature-factor Ridge score for the 2026-10-13 cutoff: 5,322 finite scores from 190 factors. The bridge receipt is `outputs/pipeline_receipts/20261024_mature_factor_stockdb_snapshot_bridge_receipt.json` with status `COMPLETED_CROSS_LINEAGE_DIAGNOSTIC`. It intentionally generated no ranking, target holdings, paper orders or ledger replay. The saved Ridge model still trains on the legacy `bulk_adjusted_prices`/action/calendar lineage, so a historical same-lineage StockDB training panel with dated eligibility and PIT evidence is the next acceptance.

## Mature-factor monthly StockDB panel

A historical monthly input panel is now materialized from the same provider/native lineage: 34 month-end sessions from 2024-01-31 through 2026-10-13, 5,322 symbols, 180,948 rows, 190 factors, and 32,438,260 finite cells. Receipt: `outputs/pipeline_receipts/20261024_mature_factor_stockdb_monthly_panel_receipt.json`. It is an input artifact only; no Ridge fit, ranking, target holdings, paper orders or shared-ledger replay was generated. PIT/as-of publication events, dated eligibility and action views remain the acceptance gate.

## Mature-factor same-lineage Ridge diagnostic

The monthly StockDB panel is now fitted with the fixed 190-factor Ridge rule using 20-session forward open labels from the same provider: 11 training months and 55,909 training rows produce 34 score dates, 180,948 finite scores and 3,319,047 finite forward-label cells. The run is `outputs/stockdb_factor_monthly_ridge_diagnostics/20261024_monthly_same_lineage_v1/run.json`, with receipt `outputs/pipeline_receipts/20261024_mature_factor_stockdb_monthly_ridge_receipt.json`. It remains a research diagnostic: no ranking, target holdings, paper orders, Ledger or broker/THS artifact was generated. PIT/as-of, dated eligibility, actions and liquidity qualification remain required before replay.

## 2026-10-24 mature factor replay

Completed a research-only monthly Ridge replay on the same StockDB lineage through the shared Engine/Ledger: 648 sessions, 281 orders, 562 fills, net return 36.9764%, maximum drawdown -18.1384%, Sharpe 0.7614. The output generates historical target holdings and paper-order diagnostics but keeps `ledger_mutated=false` and `execution_admitted=false`; PIT/as-of, eligibility, actions and untouched holdout remain blockers.

## 2026-10-24 dated eligibility binding

A StockDB dated eligibility view now binds raw-bar observation, positive OHLCV, `is_st=false` and a fixed 10,000,000 CNY amount floor to the 190-factor monthly score rows. The eligibility-filtered mature-factor replay completed through the shared Engine/Ledger: 648 sessions, 285 orders, 570 fills, net return 38.6272%, maximum drawdown -18.8216%, Sharpe 0.7886. PIT publication/revision, corporate actions and untouched holdout remain explicit blockers; `execution_admitted=false`.

## 2026-10-24 action context and untouched forward

The monthly score panel now binds a complete canonical action identity view by hash, including operate dates, plan-announcement dates and action terms. It intentionally reports `pit_asof_safe_rows=0` and `company_action_applied_rows=0`. The mature-factor terminal portfolio was marked through the complete 8-session 2026-10-14..23 forward segment without future scores, re-ranking, orders or fills: net return -0.7811%, maximum drawdown -4.7289%, Sharpe -0.3050. The Alpha158 forward attempt remains a retained coverage failure until a complete full-market capture is available.


### Paper target native StockDB close cross-check (2026-10-24)

针对当前纸面目标 20 个标的，已从本机 native StockDB 捕获 2026-10-14..23 八个交易日（160/160 行），并与 `lowvol_supplements` 的 2026-10-23 raw close reference 做独立交叉核对：20/20 行都有数据，收盘价 20/20 精确一致，最大绝对差 0。回执为 `outputs/pipeline_receipts/20261024_paper_target_native_stockdb_reference.json`，明细为 `outputs/paper_plans/20261024_lowvol_current_native_stockdb_reference/comparison.csv`。这只证明本地研究源之间的 close 一致性，不能替代 THS 当前报价、账户余额或回读；没有登录、预填、提交、撤单或账本变更。


### 2026-10-24 publication lower-bound inventory

对 canonical action parquet 的 16,818 个事件做字段盘点：`dividPlanAnnounceDate` 有 16,815 行且均不晚于 operate date，只能作为事件下界；`source_date`、`source_url`、`source_file` 只有 2 行，`retrieved_at`、`asof_date`、`revision_id`、`publication_time` 均不在 schema。未补任何日期，PIT/as-of/revision-safe/company-action 应用继续为 false。详见 `outputs/pipeline_receipts/20261024_action_publication_field_inventory_v1.json`。


### 2026-10-24 candidate-level action coverage

同截止四组终端持仓的行动事件覆盖已单独记录：固定低波动 109/2，低波动/半动量 95/0，成熟因子 22/0，Alpha158 0/0（事件数/完整来源凭据数）。PIT-safe 行为 0；未从公告日推导来源发布日期或检索时点。


### 2026-10-24 reproducible native paper reference

纸面目标的 native-vs-raw close 核对已封装为 `run_committee.py paper-target-native-reference`，使用同一 20 个目标标的和 2026-10-23 截止日重跑得到 20/20 匹配、0 差异。回执为 `outputs/paper_plans/20261024_lowvol_current_native_stockdb_reference_v2/receipt.json`；仍不代表 THS 当前账户、报价或可预填状态。


### 2026-10-24 candidate shadow paper packages

Alpha158 与成熟因子各生成一份历史影子纸面包，复用本地 Ledger 计算目标股数、纸面委托、成交、现金和持仓。两个包均 `execution_admitted=false`、未观察账户、未提交、未持久化账本；报价是 2026-10-13 回放状态，不是 THS 当前行情。回执：`outputs/pipeline_receipts/20261024_candidate_shadow_paper_packages_v1.json`。


### 2026-10-24 reusable candidate shadow route

已将候选终端回放到历史影子纸面包的过程封装为 `run_committee.py candidate-shadow-paper`，可从任意合规研究回放生成目标、参考价格和本地目标/委托/成交/现金/持仓投影。回执 `outputs/pipeline_receipts/20261024_candidate_shadow_paper_packages_v2.json`；不进入日常纸面账户，也不生成可提交的 THS 订单。

### 2026-10-24 trend/half-momentum shadow package

已把 `stockdb_low_vol_half_momentum_20` 的终端回放账户通过显式 legacy 适配接入 `candidate-shadow-paper`：20 个目标、20 个纸面委托、20 个可用路由。14 万研究现金下的零手失败已保留，成功包使用明确标注的 140 万研究现金假设。该包只证明候选可以进入统一本地纸面投影，不进入日常纸面账户，不代表 PIT、策略验证或同花顺资格。回执为 `outputs/pipeline_receipts/20261024_candidate_shadow_paper_packages_v3.json`。

### 2026-10-24 publication-event contract

已新增逐事件发布时点合同并接入 action context：只有 publication date/time、retrieved_at、asof_at、revision_id、source_file 与 SHA-256 齐全且在决策时点前，匹配事件才可标记 `pit_asof_safe`。现有 canonical action 数据仍没有完整 sidecar，因此生产回执仍保持 PIT false；当前只验证了合成契约，不把合成结果当作市场证据。

### 2026-10-24 real action sidecar attempt

已从现有 canonical action cache 找到 000538.SZSE 的两份真实来源文件，SHA-256 均核验通过。两条记录的来源日期只作为下界，原始检索时间、as-of 和完整修订身份缺失，接入 action context 后 2 行仍为 `pit_asof_safe=0`、公司行动应用 `0`。缓存读取时间单独保留，没有冒充检索时间。

### 2026-10-24 reusable action publication sidecar

已把真实行动来源盘点封装为 `run_committee.py action-publication-sidecar`：本次得到 138 个事件级 sidecar、177 个来源文件记录，138 个文件哈希核验通过；20 行有 retrieved_at，但 asof_at 为 0，PIT-safe 仍为 0。重复来源已按事件聚合并保留全部文件与哈希，重复事件失败回执继续保留。



## 2026-10-24 long-window mainline result

已完成固定低波动的 2024-01-02..2026-10-23、678 会话 StockDB 重放，并以统一回执确认 Data→Signal→Target Portfolio→Orders→BrokerAdapter→Fill→Cash/Position 八段均有产物；净收益 -4.0859%、最大回撤 -12.9214%、160 订单、229 成交、期末权益 134,279.81。随后生成五策略统一长窗口比较，回执 `outputs/pipeline_receipts/20261024_long_window_strategy_comparison_v1.json`。所有结果保持研究状态，`execution_admitted=false`，未访问券商或同花顺。


## 2026-10-24 leader cross-year resource-bounded result

龙头风格新增跨年连续适配，但全量 32 case 在约 12GB 工作集处主动停止；首个 case 已完成 424 个 source sessions并保留完整 equity/target/订单/成交文件，首个 case 生成 38 个订单、73 个成交回调和 424 行 equity；另一个 bounded case 在 commit 前停止。两类中断证据均已保存，未重复重跑；此路线仍为部分研究证据，`execution_admitted=false`。

### 2026-10-24 current-cutoff candidate paper reconciliation

已把已有 2026-10-23 长窗口 `low_vol_half_momentum_20` 终端账户接入当前截止点的纸面 target reconciliation：20 个目标、19 个已有持仓、现金 55,447.37 元、终端标记权益 154,106.37 元，14 条 BUY_REFERENCE、0 条 SELL、6 条 HOLD，目标价格全覆盖且卖出 T+1 可卖性通过。该产物只复用本地 StockDB 回放终端标记，没有周末日切、没有 THS/券商交互、没有创建订单或修改 Ledger。回执为 `outputs/paper_plans/20261024_low_vol_half_momentum_current_reconciliation_v1/receipt.json`。

### 2026-10-24 leader bounded-memory retry result

对 leader 跨年适配增加了按日懒加载 panel、共享只读 raw feed、受限风险特征视图和 bounded day-indexed feed；`py_compile`、既有 3 项 leader 测试及 `combine_inputs()` 语义回归通过（424 panels，2,097,867 raw rows，2025-01-02 至 2026-09-30）。受控单 case `liquidity_capacity_proxy__two_close_envguard__causal_first_seen_cap20` 的 v2/v3 尝试仍在输出 case progress 前达到 7,386,066,944 / 8,083,701,760 bytes 工作集而中止；两个版本只写入 abort receipt 和 ABORT.md，没有虚构 target/order/fill/equity。后续不重复该资源路径，除非先实现有实质差异的流式/持久状态设计。

### R01 candidate specification registry (2026-10-24)

已把候选策略目录的 9 个策略绑定到固定的 data snapshot/comparability group、研究窗口、组合限制、成本配置、回放/纸面闸门和关闭的 promotion rule。入口为 `trading/research/candidate_spec_registry.py`，配置为 `docs/architecture_v3/candidate_strategy_specs_20261024.json`，命令为 `python run_committee.py candidate-spec-registry`。

实际回执 `outputs/pipeline_receipts/20261024_candidate_spec_registry.json`：9/9 候选、8 个 comparability groups、`issues=[]`；6 个允许研究回放、5 个允许纸面投影，所有候选仍 `promotion_allowed=false`、`execution_admitted=false`。短线、席位和龙头资源停止候选保留停止原因，不能进入新的回放。测试 `2 passed`，架构审计 `559 modules / 0 violations`。本包未读取新行情、未接触 THS。

### R03 fixed account verification (2026-10-24)

现有 678-session 固定低波动 StockDB 回放的 R03 状态已按已有证据更新为 `VERIFIED_LOCAL`：678 sessions、160 orders、229 fills、费用 CNY 893.19、期末权益 CNY 134,279.81、stale valuation 0、issues 0；保存的 provenance receipt 显示 Data、Signal、Target Portfolio、Orders、BrokerAdapter、Fill、Cash/Position 八段全部存在。没有重新回放，也没有修改账户或执行通道。

### P04 virtual strategy sleeves slice (2026-10-24)

新增 `trading/research/virtual_strategy_sleeves.py` 与 `virtual-strategy-sleeves` 命令，将五个已保存长窗口 terminal `account.json` 以等权 20% sleeve 归因到一个研究现金池。回执 `outputs/pipeline_receipts/20261024_virtual_strategy_sleeves_v1.json` 为 `VERIFIED_RESEARCH_ONLY_VIRTUAL_SLEEVES`：虚拟终端权益 CNY 146,196.56，现金 CNY 70,715.80，持仓市值 CNY 75,480.76；现金与权益归因守恒，合并目标 gross 70.4%、最大单名 3.2%，均满足 80%/20% 限制。五个账户终端日为 2026-10-13 和 2026-10-23，已明确标记 `MIXED_TERMINAL_DATES_RESEARCH_ONLY`，不得由此派生当前可执行目标。

聚焦测试 `trading/research/tests/test_virtual_strategy_sleeves.py` 为 `3 passed`，并覆盖默认单名暴露超限拒绝；未创建 orders/fills、未修改 Ledger、未接触 Broker/THS，`execution_admitted=false`。

### 2026-10-24 P03 exposure and blocked-action stress view

已将保存的 2026-10-23 纸面 reconciliation、native StockDB close reference 接入 `run_committee.py paper-exposure-stress`。回执 `outputs/pipeline_receipts/20261024_p03_paper_exposure_stress_v1.json` 生成 baseline、T+1 卖出阻断和 50% cash haircut 三个固定场景，记录动作计数、阻断数量、残余暴露和现金守恒。基线无阻断；T+1 阻断 1 个动作、100 股、残余暴露 CNY 515.00；现金压力场景仍无阻断。测试 `trading/research/tests/test_paper_exposure_stress.py` 为 2 passed，输入日期或跨源 close 不一致会拒绝。该视图只做研究侧压力投影，不创建订单/成交、不修改 Ledger、不访问券商或 THS，`execution_admitted=false`。P03 已更新为 `VERIFIED_LOCAL`，下一包转向 P05 容量/成本利用率与未成交暴露。

### 2026-10-24 P05 capacity and cost comparison

已将五个保存的共享 Engine/Ledger 终端账户接入 `run_committee.py strategy-capacity-cost`。回执 `outputs/pipeline_receipts/20261024_p05_capacity_cost_comparison_v1.json` 记录 requested/filled quantity、未成交数量与名义暴露、容量利用率、基础费用，并固定输出 0/25/50 bps 成本压力。Alpha158/LightGBM 容量利用率 98.4714%、未成交 14,100 股、未成交名义暴露 CNY 22,849.00；半动量 99.5980%、500 股、CNY 4,930.00；12-1 动量 99.6042%、100 股、CNY 1,324.00；固定低波动和成熟 Ridge 为 100%。终端日 2026-10-13/23 混合，结果保持研究专用。测试 `trading/research/tests/test_strategy_capacity_cost_comparison.py` 为 2 passed，未重放市场、未合并候选订单、未创建订单/成交、未修改 Ledger、未访问券商或 THS，`execution_admitted=false`。P05 已更新为 `VERIFIED_LOCAL`。

下一波切换为 L02/L04 与 D05/D06/O02 两条独立工程切片并行，分别处理未知外部结果/迟到成交恢复，以及快照发布/增量影响/断点续跑。


### 2026-10-24 L02/L04 ledger recovery slice

已完成共享 Ledger 的迟到成交/撤单竞态与未知外部结果纵向切片。`run_committee.py ledger-recovery-scenarios` 生成 `outputs/pipeline_receipts/20261024_ledger_recovery_scenarios_v1.json`：撤单请求后 100 股迟到部分成交仍保留为 `cancel_pending`，确认撤单后订单为 `cancelled`、成交数量/现金/持仓保留；同一幂等键重复提交不新增订单或成交。`submission_unknown`、`timeout`、`connection_lost` 均为 `UNKNOWN_RECONCILIATION_REQUIRED`，`retry_allowed=false`、`duplicate_action_allowed=false`，且没有外部提交调用。focused tests `3 passed`，`py_compile` 通过。所有场景为内存纸面 Ledger，未修改持久账户、未访问 Broker/THS，`execution_admitted=false`。L02/L04 已绑定 `VERIFIED_LOCAL`。

下一包切换为 D05/D06/O02：只做原子快照发布、增量影响与断点恢复的可执行切片，拒绝不完整快照并证明重启不重复发布。


### 2026-10-24 D05/D06/O02 snapshot and recovery slice

已完成原子快照发布、修订影响和断点恢复纵向切片。`run_committee.py snapshot-publication-recovery` 生成 `outputs/pipeline_receipts/20261024_snapshot_publication_recovery_v1.json`：无 manifest 的部分快照被拒绝；完整 data+manifest 以目录替换作为发布点并通过 SHA/行数消费校验；一条修订记录保留 `received_at`、`revision_id`、`supersedes`，影响字段为 `value` 与 `revision_id`；在“发布后、checkpoint 提交前”受控中断后，重启把已发布单元识别为 `IDEMPOTENT_EXISTING`，下一单元只创建一次。focused tests `4 passed`，`py_compile` 通过。全部使用临时研究目录，未修改纸面账户、未访问 Broker/THS，`execution_admitted=false`。D05/D06/O02 已绑定 `VERIFIED_LOCAL`。

下一节点为 F1 单真实披露事件纵向切片，但仅在取得可靠 issuer publication/retrieval/as-of/revision 与市场 join 证据后启动；在此之前不伪造财报 PIT 数据，继续独立策略与纸面主线。


### 2026-10-24 P06 aligned joint portfolio

已完成 P06 组合验收切片：`run_committee.py joint-portfolio-acceptance` 只纳入终端日同为 2026-10-13 的成熟因子 Ridge 与 Alpha158/LightGBM 账户，各占 50% 虚拟 sleeve，生成 7 个研究目标行。合并理论 gross 56%、实际手数取整后 gross 53.2407%、最大单名 10.4629%，现金余量 CNY 65,463.00；301686.SZSE 因 8% 目标不足一手，按固定“若一手仍低于单名上限则向上取一手，否则阻断”规则取 100 股，结果仍通过 20% 单名上限。回执 `outputs/pipeline_receipts/20261024_joint_portfolio_acceptance_v1.json` 为 `VERIFIED_RESEARCH_ONLY_JOINT_PORTFOLIO`。没有把 2026-10-23 的其它策略混入，未创建订单/成交、未修改 Ledger、未访问 Broker/THS，`execution_admitted=false`。P06 已绑定 `VERIFIED_LOCAL`。

下一节点仍为 F1 单真实披露事件纵向切片；必须先取得 issuer publication/retrieval/as-of/revision 证据，不能用现有缺时点的 3 行季度事实冒充 PIT。


### 2026-10-24 R02 research decision matrix

已把公开席位回放的“审计结果”转成固定决策矩阵：`run_committee.py research-decision-matrix` 读取 `public_seat_replay_audit_v2`，要求完整 241-session 窗口且订单/成交均存在才计算 post-event opportunity；完整非正收益案例判为 `STOP_RESEARCH_ONLY`，完整正收益案例判为 `CONTINUE_RESEARCH_ONLY`，未完成案例保留原始错误并判为 `BLOCKED_INCOMPLETE_WINDOW`。本次 9 个案例为 2 个继续研究、3 个停止研究、4 个数据阻断；publication timing、PIT membership、causal seat alpha 均为 false，promotion 仍关闭。回执 `outputs/pipeline_receipts/20261024_research_decision_matrix_v1.json`，focused tests `3 passed`，未生成目标/订单/成交，`execution_admitted=false`。R02 已绑定 `VERIFIED_LOCAL`。

下一节点仍为 F1 单真实披露事件纵向切片；继续研究不等于采用，不把公开席位正收益接入纸面账户。

### 2026-10-24 F1 issuer retrieval and market join slice

F1 已完成一段可复用的真实来源纵向链路：三行 Q1 2024 发行人报告重新从原 URL 抓取，3/3 HTTP 成功、3/3 全文件 SHA-256 与保存 PDF 一致，并保留当前检索时间、HTTP `Last-Modified`、ETag 与 `IsLatest`。同一回执把三只股票连接到固定 StockDB 快照的首个交易日 `2024-05-06`，3/3 行通过，快照 manifest 哈希已记录。回执为 `outputs/pipeline_receipts/20261024_f01_source_retrieval_receipt_v1.json`。

出版 sidecar 已接入该检索回执：`outputs/pipeline_receipts/20261024_f01_publication_sidecar_network_v2.json` 中 3 行均有显式当前 `retrieved_at`、`asof_at` 和内容修订标识，契约仍以 `REAL_SOURCE_MAPPED_PIT_BLOCKED` 拒绝，因为当前重抓时间晚于历史决策日 `2024-05-01`。F1 本地纵向切片仍以 `PARTIAL_LOCAL_TIMESTAMP_VERTICAL_SLICE` 贯通 `Data→Signal→Target Portfolio→Orders→BrokerAdapter→Fill→Cash/Position`，3 个订单、6 个模拟成交，但没有将本地时间戳冒充历史 PIT。测试为 F1 focused `15 passed`；`execution_admitted=false`，没有券商或同花顺操作。


### 2026-10-24 D1 candidate paper batch fan-in

新增 `trading/research/daily_candidate_paper_batch.py` 和 `run_committee.py daily-candidate-paper-batch`，把显式的研究-only target/quote payload 按候选隔离目录接入现有共享纸面 target/order/fill/cash/position 包。历史验证批次包含 `trend_low_vol_half_momentum_20`（20 targets、20 paper orders）和 `stockdb_factor_monthly_ridge_v1`（5 targets、5 paper orders），回执为 `outputs/paper_plans/20261025_candidate_batch_v1/packages/batch_receipt.json`，报告为 `outputs/research_reports/20261025_daily_candidate_paper_batch_v1.md`。该批次不生成信号、不抓取行情、不修改 `fixed_daily`，不访问券商或同花顺；`execution_admitted=false`。测试 `2 passed`。

D1 汇入口已补上候选目录 ID 与回放 payload strategy ID 的显式映射：`stockdb_low_vol_half_momentum_20` → `trend_low_vol_half_momentum_20`，`factor_candidate_family_balanced_ridge` → `stockdb_factor_monthly_ridge_v1`。v2 manifest 与批次回执位于 `outputs/paper_plans/20261025_candidate_batch_v1/manifest_v2.json` 和 `outputs/paper_plans/20261025_candidate_batch_v1/packages_v2/batch_receipt.json`，避免下一交易日因名称差异误拒绝或误标候选。

D1 候选批次现已读取 `candidate_strategy_catalog_20261024.json` 的 `research_selection` 闸门，仅允许 `research_followup_allowed=true` 的候选进入纸面投影。v3 回执 `outputs/paper_plans/20261025_candidate_batch_v1/packages_v3/batch_receipt.json` 记录了目录选择结果；停止、覆盖阻断和未评估候选在生成订单前即被拒绝。

D1 批处理又增加了时点约束：quote 的 `observed_at` 和每行 `observed_date` 必须等于 target 的 `decision_day`，`execution_day` 必须严格晚于决策日。v4 回执 `outputs/paper_plans/20261025_candidate_batch_v1/packages_v4/batch_receipt.json` 已通过该约束；未来报价或错日行情会在订单生成前被拒绝。

D1 v5 又将批次回执绑定到 manifest 的绝对路径、SHA-256 和候选数，并拒绝与 manifest 候选集合不一致的调用。重跑回执为 `outputs/paper_plans/20261025_candidate_batch_v1/packages_v5/batch_receipt.json`，manifest SHA-256 为 `34f68b5d2638b635555ffc776003dacd88145d303417f0d9c40d1f9cb9c810a4`；D1/F01 focused tests `26 passed`。

D1 输入汇入已自动化：`candidate-daily-input-bundle` 从两个已保存的 StockDB 回放源生成 catalog 约束的 target/quote manifest，并拒绝改变 replay identity 的 strategy alias，再交给 D1 批处理。最新输入回执为 `outputs/paper_plans/20261025_candidate_batch_v1/generated_inputs_v2/bundle_receipt.json`，下游回执为 `outputs/paper_plans/20261025_candidate_batch_v1/packages_v7/batch_receipt.json`；两个候选仍为 20/20 与 5/5，focused D1/F01/input-bundle tests `30 passed`。

`candidate-daily-paper-run` 已把上述两步合为一个闭市后研究入口，保存输入 manifest、bundle 回执、D1 batch 回执和总回执；本次总回执为 `outputs/paper_plans/20261025_candidate_batch_v1/pipeline_v1/pipeline_receipt.json`，`execution_admitted=false`，两个候选仍为 20/20 与 5/5，combined focused tests `31 passed`。

该入口又增加了可选 fixed leg：显式传入固定策略终态研究账户后，固定低波半动量 `20/20`、趋势候选 `20/20`、Ridge `5/5` 均通过同一个 `build_paper_target_package` 进入本地 Ledger 投影，固定 leg 的 target/quote 文件也保留 SHA-256。证明回执为 `outputs/paper_plans/20261025_candidate_batch_v1/pipeline_v3/pipeline_receipt.json`；`execution_admitted=false`，未修改固定纸面账户，未访问券商或同花顺。

公开席位候选 `lag2_TREND_ANY_LHB` 的一个完整 241-session 案例已进入同一共享本地纸面投影，5 个目标、5 个纸面订单、4 个可用路由。回执 `outputs/pipeline_receipts/20261025_public_seat_candidate_shared_paper_receipt_v1.json` 明确保留 `UNASSESSED_SEPARATE_GATE`，没有把公开席位结果接入可选策略队列；`validation` 和 `boundary_assertions` 均显示外部账户、客户端、提交、撤单和持久账本状态为 false。


## 2026-10-26 native shortline candidate handoff

新增 `trading/research/shortline_candidate_handoff.py` 与 `shortline-native-candidate-handoff` 命令：它读取已发布且通过 IPO qualified 资格投影的 native preopen plan，仅复制明确 `candidate_selected=true` 的事件到新 handoff，不修改 raw score 或原 plan。2026-10-15→2026-10-16 的 LR/LGBM 各生成 3 行候选，两个独立 score gate 均返回 `VERIFIED_SHORTLINE_SCORE_GATE_READY`；LR 另生成 SignalFrame/target preview。该结果仍是研究候选输入，不是完整回放、PIT 验证或纸面/实盘授权。

证据：`outputs/shortline_native_candidate_handoff/20261026_source1015_ipoqualified_v1/receipt.json`、`outputs/research_reports/20261026_shortline_candidate_handoff_v1.md`。


## 2026-10-26 F01 period semantics

按 CTO 计划修正 F01 计算语义：`total_assets` 必须是 `point_in_time`，利润事实只能是 `single_quarter` 或 `cumulative`；不符合时在因子计算前返回 `INCOMPARABLE`，不把流量期间资产当分母。F01/F2 vertical focused suite `22 passed`。真实 000002.SZSE/2023Q1 仍因历史 retrieval/as-of/revision 缺失保持 `BLOCKED_DATA`。


## 2026-10-26 shortline bounded BaoStock replay

新增 `shortline-baostock-raw-snapshot` 可复用只读采集命令，用公开 BaoStock 补齐候选 2026-10-08..16 原始路径。10 月 15 日冻结 native LR/LGBM handoff 经过共享 Engine/Ledger：LR 1 笔订单/2 笔成交，LightGBM 3 笔订单/6 笔成交。该结果是独立 BaoStock bounded research replay，不是 StockDB 等价、PIT、完整窗口或采用证据。

## 2026-10-26 shortline adoption gate

	rading/research/shortline_adoption_gate.py now joins the immutable native LR/LightGBM handoff with the bounded BaoStock replay through an explicit model-level adoption gate. Both models are CONTINUE_RESEARCH_ONLY: LR 1 order/2 fills and LightGBM 3 orders/6 fills. The receipt keeps xecution_admitted=false, paper_allowed=false and promotion_allowed=false; StockDB-equivalent full-window, PIT/company-action/publication timing and untouched-forward evidence remain required.

Evidence: outputs/pipeline_receipts/20261026_shortline_adoption_gate_v1.json and outputs/research_reports/20261026_shortline_adoption_gate_v1.md. Focused tests: 	est_shortline_adoption_gate.py, 	est_shortline_candidate_handoff.py, 	est_shortline_baostock_raw_snapshot.py -> 8 passed.


## 2026-10-26 mainline factor and workbench delivery

成熟因子 Ridge 20-session 与预声明 60-session 变体已在相同 648 个交易日、同一 Engine/Ledger 路径上完成可复现比较：60-session 净收益低 4.7997 个百分点、最大回撤多 0.9656 个百分点；结果保持 NO_ADOPTION_RESEARCH_ONLY。证据为 outputs/pipeline_receipts/20261026_mature_factor_horizon_comparison_v1.json。

工作台新增手动恢复重提交：失败/中断/取消任务通过 POST /api/jobs/{id}/resubmit 生成带 parent_job_id、recovery_attempt 的不可变子任务，重复幂等键不会重复创建；自动重试、券商/同花顺交互和实盘仍关闭。证据为 outputs/pipeline_receipts/20261026_workbench_manual_resubmit_v1.json。

## 2026-10-26 release v20

The internal research-only release now binds the shortline adoption gate, mature-factor horizon comparison and workbench manual resubmit. Manifest: D:/QuantificationData/trading/research/releases/20261026_v20/release_manifest.json. execution_admitted=false, live_execution_ready=false, and missing_required_code=[].

## 2026-10-26 shortline long-path replay

The same frozen LR/LightGBM candidates now replay through an independent BaoStock path beginning at each symbol's first available session and ending 2026-10-16. LR covers 2,634 sessions and LightGBM 4,074 sessions; both have zero dropped incomplete path rows. The shared Engine/Ledger result remains LR 1 order/2 fills and LightGBM 3 orders/6 fills. The new adoption gate is research-only and does not replace StockDB/PIT/forward evidence.

## 2026-10-26 unified research adoption rollup

`trading/research/research_adoption_rollup.py` now joins the existing candidate adoption matrix, the mature-factor 20/60-session comparison and the shortline LR/LightGBM adoption gate into one machine-readable research-only receipt. It preserves each route's separate decision and next gate, while keeping `execution_admitted=false`, `paper_allowed=false` and `promotion_allowed=false`. Receipt/report: `outputs/pipeline_receipts/20261026_research_adoption_rollup_v1.json` and `outputs/research_reports/20261026_research_adoption_rollup_v1.md`; focused tests: `3 passed`. This is a decision integration artifact, not strategy adoption or live-readiness evidence.

The internal research-only release v22 binds this rollup at commit `7b75705`: `D:/QuantificationData/trading/research/releases/20261026_v22/release_manifest.json`, with `missing_required_code=[]`, `execution_admitted=false` and `live_execution_ready=false`. Binding evidence is `outputs/pipeline_receipts/20261026_release_v22_binding_v1.json`.

## 2026-10-26 release v21

The internal research-only release binds the long shortline raw-path replay and v2 adoption gate. Manifest: D:/QuantificationData/trading/research/releases/20261026_v21/release_manifest.json. execution_admitted=false, live_execution_ready=false, missing_required_code=[].

## 2026-10-26 shortline StockDB long-path replay

已把冻结的 native LR/LightGBM 候选接入本机 StockDB 日线快照，并沿用共享 Engine/Ledger 完成历史长路径回放。StockDB 捕获请求 20,370 行、可用 13,167 行、缺失 7,203 行、分钟数据 0 行；LR 覆盖 2,634 sessions，1 笔订单/2 笔成交，净收益 -0.6032%；LightGBM 覆盖 3,863 sessions，3 笔订单/6 笔成交，净收益 -1.2715%。新增回执 `outputs/pipeline_receipts/20261026_shortline_stockdb_long_path_replay_v1.json` 和报告 `outputs/research_reports/20261026_shortline_stockdb_long_path_replay_v1.md`，并由 adoption gate v3 汇总。StockDB `daily.csv` 的日期已适配，缺失 `pct_chg` 按每个标的前一条可用 raw close 重建，首条回退源 `pre_close`；ST 状态、PIT、公司行动和完整覆盖仍未解决，因此结果仍是研究证据，`execution_admitted=false`、`paper_allowed=false`、`promotion_allowed=false`。

验收：`trading/tests/test_shortline_panel_replay.py` 与 `trading/research/tests/test_shortline_adoption_gate.py` 覆盖 StockDB CSV 日期/回放和 v3 状态，focused tests 通过。下一节点是补齐 StockDB 等价覆盖或绑定完整等价源，再做冻结候选的 untouched-forward、PIT/publication/company-action 证据。

## 2026-10-26 internal release v23

StockDB 短线长路径回放和 adoption gate v3 已绑定到内部研究发布 v23：`D:/QuantificationData/trading/research/releases/20261026_v23/release_manifest.json`，manifest SHA-256 为 `7d3b79230a9dcbbcd009b6c9ca9271b5d9cb3b438b94d7ea5a831b4ad15c46be`，提交 `cbefc75`，`missing_required_code=[]`、`execution_admitted=false`、`live_execution_ready=false`。绑定回执为 `outputs/pipeline_receipts/20261026_release_v23_binding_v1.json`。

## 2026-10-26 Engineering Handbook R2 alignment

已阅读并按《Quantification Engineering Handbook R2》绑定当前实施方式：按 B0-B6 业务结果分批；W0 总控、W1 财报/PIT、W2 模型、W3 组合与连续账户、W4 产品、W5 环境/渠道、W6 独立验收、W7 只清理已证明冗余。手册明确两条主线：资金生产路径（环境/基线→日历与模式→冻结计划→连续账户→真实日程与恢复→发布）和财报研究路径（原件/覆盖→时间期间修复→F01→合格输入→原账户→固定比较/研究决定）；外部数据或 THS 授权只能暂停依赖路线。已提交 `a7a248a`，修复两个 PowerShell 工作台探测脚本的 `Invoke-WebRequest` 兼容参数，解析检查通过。

## 2026-10-26 post-close D0/D1 attempt

收市后实际调用现有 `post-close-d0-d1`，输入为当前 Alpha158 candidate shadow manifest、固定纸面账户、2026-10-26 close 和固定交易日历。回执 `outputs/pipeline_receipts/20261026_post_close_d0_d1_v1/post_close_receipt.json` 返回 `WAITING_FIXED_DAILY_SOURCES`，缺少精确的 2026-10-26 complete close package；因此没有生成目标、订单、成交或账本变更，也没有访问外部渠道，`execution_admitted=false`。下一步是补齐真实 StockDB/可靠来源的完整日线 close package，再用同一 coordinator 重跑。

## 2026-10-26 native StockDB close coverage attempt

已实际启动本地 StockDB 并按 2026-10-13 的完整普通 A 股代码清单请求 2026-10-26 日线快照：5,211 个请求标的仅返回 226 行，4,985 行缺失，分钟数据 0 行。机器回执 `outputs/pipeline_receipts/20261026_stockdb_close_coverage_v1.json`，外部快照 `D:\QuantificationData\trading\research\stockdb_close_packages\20261026_native_full_universe_v1\snapshot.json`。由于 `fixed_daily/post_close` 要求完整日期包，本次没有伪造或消费不完整输入，D0/D1 仍保持未生成，`execution_admitted=false`。该失败改变了下一动作：优先恢复完整可靠收市源或使用已完整的替代来源，再重跑同一固定入口。

## 2026-10-26 internal release v24

最新的收市覆盖失败证据和 post-close waiting 回执已绑定到内部研究发布 v24：`D:/QuantificationData/trading/research/releases/20261026_v24/release_manifest.json`，manifest SHA-256 为 `a1c20f4d74a37d717fb20557cb993c7676ff51fb263ab1ba71bf039b0525c1ad`，提交 `a81c9af`，`missing_required_code=[]`、`execution_admitted=false`、`live_execution_ready=false`。绑定回执为 `outputs/pipeline_receipts/20261026_release_v24_binding_v1.json`。

## 2026-10-26 source coverage throughput

`stockdb_source_coverage.py` 已改为一次读取每个 parquet chunk、按请求标的建立索引，再批量输出缺失覆盖；原实现会对每个标的重复扫描整套 StockDB snapshot。新增 `trading/tests/test_stockdb_source_coverage.py`，覆盖索引读取和缺失行保留，测试 2 passed。该改动只改善可靠来源恢复诊断吞吐，不填充价格、不修改快照、不放开执行权限。

## 2026-10-26 internal release v25

覆盖诊断批处理优化已绑定内部研究发布 v25：`D:/QuantificationData/trading/research/releases/20261026_v25/release_manifest.json`，manifest SHA-256 `1442ffd25912d8410c81aa1e05b2eb1f722dc78adbaef42a783971b201e35d11`，提交 `af90dd1`，`missing_required_code=[]`、`execution_admitted=false`、`live_execution_ready=false`。绑定回执 `outputs/pipeline_receipts/20261026_release_v25_binding_v1.json`。

## 2026-10-26 batched held-position coverage result

优化后的 `stockdb-source-coverage` 已对固定纸面账户 20 个持仓标的完成一次批量诊断，回执目录 `outputs/stockdb_source_coverage/20261026_201700_39ada9fb`，状态 `coverage_gap_confirmed`，20/20 行在当前 StockDB snapshot 和 2026-10-23 raw supplement 中均缺少 2026-10-26 数据。诊断没有填充价格或改写快照；该结果直接解释了 post-close 为什么必须等待完整收市源。

## 2026-10-26 P31 coverage diagnostics

P31 已绑定：StockDB 覆盖诊断现在按批次一次读取 parquet 并建立标的索引；固定纸面账户 20 个标的的 2026-10-26 覆盖结果为 20/20 缺失，未填充价格、未改写快照、未生成 D0/D1。验收测试 2 passed。

## 2026-10-26 internal release v26

P31 覆盖诊断工作包已绑定内部研究发布 v26：`D:/QuantificationData/trading/research/releases/20261026_v26/release_manifest.json`，提交 `520f606`，`missing_required_code=[]`、`execution_admitted=false`、`live_execution_ready=false`。绑定回执 `outputs/pipeline_receipts/20261026_release_v26_binding_v1.json`。

## 2026-10-26 P32 research adoption rollup v2

已把最新候选采用矩阵、成熟因子 20/60-session 比较和 StockDB 短线 gate v3 汇总为 v2 研究回执：3 个继续研究、2 个停止原规格、1 个覆盖阻断、4 个独立 gate；`strategy_validated=0`、`execution_admitted=false`。回执 `outputs/pipeline_receipts/20261026_research_adoption_rollup_v2.json`，报告 `outputs/research_reports/20261026_research_adoption_rollup_v2.md`，测试 3 passed。

## 2026-10-26 internal release v27

P32 策略采用汇总 v2 已绑定内部研究发布 v27：`D:/QuantificationData/trading/research/releases/20261026_v27/release_manifest.json`，提交 `75aafb7`，`missing_required_code=[]`、`execution_admitted=false`、`live_execution_ready=false`。绑定回执 `outputs/pipeline_receipts/20261026_release_v27_binding_v1.json`。

## 2026-10-26 THS readback duplicate-row gate

THS 只读回读比较器现在显式拒绝重复参考证券，并将重复导出证券判为 `readback_mismatch_stop`，不再用字典覆盖前一行。新增重复参考/重复导出测试；THS focused suite `8 passed`。该改动仍不连接客户端、不提交、不改变 Ledger。

## 2026-10-26 internal release v28

P18 THS 重复回读行门禁已绑定内部研究发布 v28：`D:/QuantificationData/trading/research/releases/20261026_v28/release_manifest.json`，提交 `7c88e9b`，`missing_required_code=[]`、`execution_admitted=false`、`live_execution_ready=false`。绑定回执 `outputs/pipeline_receipts/20261026_release_v28_binding_v1.json`。

## 2026-10-26 weekly research review v2

周复盘回执 `outputs/weekly_reviews/20261026_weekly_review_receipt_v2.json` 已更新：候选采用状态为 3 个继续研究、2 个停止原规格、1 个覆盖阻断、4 个独立 gate；成熟因子保持 NO_ADOPTION_RESEARCH_ONLY；短线 LR/LGBM 保持研究-only。2026-10-26 StockDB 仅 226/5,211 行，当前 20 个固定持仓全部缺失；THS 重复回读门禁已测试但没有真实客户端观察。最大阻碍仍是完整收市源、F1 PIT/as-of/revision、授权 THS 只读证据。

## 2026-10-26 internal release v29

周复盘 v2 已绑定内部研究发布 v29：`D:/QuantificationData/trading/research/releases/20261026_v29/release_manifest.json`，提交 `8239aa2`，`missing_required_code=[]`、`execution_admitted=false`、`live_execution_ready=false`。绑定回执 `outputs/pipeline_receipts/20261026_release_v29_binding_v1.json`。

## 2026-10-26 P33 W4 actual HTTP chain

已按工程手册实际运行 W4 工作台链：通过 `POST /api/jobs` 提交合成研究任务，同一 `Idempotency-Key` 重复提交返回同一 job；启动本地 factor worker 后任务完成，生命周期返回 result/download 可用，result JSON 可读，`summary.csv`、`REPORT.md`、`result.json` 均实际下载成功。回执 `outputs/pipeline_receipts/20261026_workbench_w4_actual_http_receipt_v2.json`，`execution_admitted=false`，无 Broker/THS 交互。

## 2026-10-26 internal release v30

P33 W4 实际 HTTP 工作台链已绑定内部研究发布 v30：`D:/QuantificationData/trading/research/releases/20261026_v30/release_manifest.json`，提交 `a36fcff`，`missing_required_code=[]`、`execution_admitted=false`、`live_execution_ready=false`。绑定回执 `outputs/pipeline_receipts/20261026_release_v30_binding_v1.json`。


## 2026-10-26 Engineering Handbook R2 implementation wave

R2 B1/B5 work packages completed: F01 separates disclosure window from decision cutoff while keeping real PIT external evidence blocked; F05 persists invalid-request, idempotency-conflict, and concurrency-limit decisions with read-only receipts; U04 exposes a recursive local recovery-chain query with monotonic attempt numbers. Commits: bc5141e, 6895bb0, 07dbd98.

Validation: F01 10 passed; persistent-task additions 3 passed plus 1 existing test; workbench backend 28 passed. All receipts keep execution_admitted=false; no broker/THS connection and no real-account mutation.


## 2026-10-26 internal release v31

The handbook R2 implementation wave is bound to internal research release v31: D:/QuantificationData/trading/research/releases/20261026_v31/release_manifest.json; manifest SHA-256 e1f6926dfe0ee7025c929e228af7d18a1d6374adbbcd1238b9daacb764f226bd; binding receipt outputs/pipeline_receipts/20261026_release_v31_binding_v1.json. execution_admitted=false and live_execution_ready=false remain closed.


F05 bounded receipt v2 executed through the persistent run store: 4 paper runs admitted to the isolated store, 3 rejected requests persisted (concurrency limit, idempotency conflict, invalid request), no fifth run created, ledger_mutated=false, execution_admitted=false. Receipt: outputs/pipeline_receipts/20261026_persistent_task_decision_v2.json.


## 2026-10-26 internal release v32

F05 accepted bounded paper-task receipt is bound to internal release v32: D:/QuantificationData/trading/research/releases/20261026_v32/release_manifest.json; manifest SHA-256 bfb813ed30f5df6c42625f0c9279b63b3c7894fb2df195fe349aed87cb2aecf2; receipt outputs/pipeline_receipts/20261026_release_v32_binding_v1.json. execution_admitted=false and live_execution_ready=false remain closed.


F04 CLI dispatch cleanup v2 removed the duplicate stockdb-baostock-extension guard. The launcher now has 391 unique static routes; compatibility/uniqueness tests passed 12. Receipt: outputs/pipeline_receipts/20261026_cli_dispatch_cleanup_v2.json; commit 43b5b2e.


## 2026-10-26 internal release v33

F04 CLI dispatch cleanup v2 and D03 provenance evidence are bound to internal release v33: D:/QuantificationData/trading/research/releases/20261026_v33/release_manifest.json; manifest SHA-256 f03d98c1b04140d6481086702ccd0fea1ce27f18936cfcb0acb54b7545e4ab3b; receipt outputs/pipeline_receipts/20261026_release_v33_binding_v1.json. execution_admitted=false and live_execution_ready=false remain closed.


F02 runtime recipe v2 captured a clean main worktree, project interpreter D:/Quantification/env/python.exe, trading import path, runtime directories and Git identity. Test_runtime_recipe: 3 passed. Receipt: outputs/pipeline_receipts/20261026_runtime_recipe_v2.json.

O05 fault injection now rejects tampered producer_repeated/system_head_mutated/ledger_mutated recovery flags without rerunning the producer or mutating head/registration/plan. system_daily_command_recovery plus system_daily_refresh: 14 passed. Receipt: outputs/pipeline_receipts/20261026_system_daily_fault_injection_v1.json; commit ee2d7a3.


## 2026-10-26 internal release v34

F02 runtime recipe v2 and O05 fault-injection evidence are bound to internal release v34: D:/QuantificationData/trading/research/releases/20261026_v34/release_manifest.json; manifest SHA-256 44907bb93d84e0f308136f23f814c51e69ceb46e6949c160e7fd1dbc7c3b0e69; receipt outputs/pipeline_receipts/20261026_release_v34_binding_v1.json. execution_admitted=false and live_execution_ready=false remain closed.

## 2026-10-26 P32 retained joint gate v3

The research adoption rollup now accepts the retained half-momentum/Ridge joint comparison as a separate gate. It binds the terminal target and paper projection while preserving the explicit `NOT_COMPARABLE_PROJECTION_ONLY` return boundary; no joint return uplift is inferred from a target projection. Receipt/report: `outputs/pipeline_receipts/20261026_research_adoption_rollup_v3.json` and `outputs/research_reports/20261026_research_adoption_rollup_v3.md`. Focused tests: `trading/research/tests/test_research_adoption_rollup.py` (3 passed) and `trading/research/tests/test_retained_joint_candidate_comparison.py` (1 passed). `execution_admitted=false`, `paper_allowed=false`, and `promotion_allowed=false` remain closed.


## 2026-10-26 fixed-only historical D0/D1 delivery

在已登记的 `fixed_daily/20261024_lowvol_current` 上，使用冻结 StockDB 2026-10-23 收市快照完成一次完整的固定策略 D0/D1 研究纸面包：20/20 标的覆盖，20 个目标、20 个纸面订单、20 条可用路由，模型费用 CNY 101.20，期末现金/权益投影 CNY 28,012.80 / CNY 139,898.80。联合纸面同样完成 20 个目标和 20 个订单，回执为 `outputs/pipeline_receipts/20261026_fixed_only_d0_d1_v1/contract_receipt.json`，报告为 `outputs/research_reports/20261026_fixed_only_d0_d1_v1.md`。本包保持 `TARGET_PREVIEW`、`execution_admitted=false`，没有券商/同花顺交互、持久 Ledger 写入或真实账户变更；候选数为 0，下一次变更收市必须传入同日候选清单才能完成完整联合比较。

### F2 PIT capture selection update (2026-10-26)
`f2_wayback_probe.py` now selects the latest Wayback capture on or before the decision cutoff, independent of CDX row order, and records the selection policy in the receipt. The real `000002.SZSE/2023Q1` probe found one capture after `2024-05-01`, so the new v3 receipt remains `BLOCKED_DATA`; no PIT fact or factor was admitted. Focused F2 tests passed (`20 passed` across the Wayback, historical gate, source snapshot and fact bridge suites).

## 2026-10-26 fixed + candidate D0/D1 continuation

- Completed a same-day research-only D0/D1 package for close `2026-10-23`, execution `2026-10-26`, settlement `2026-10-27`.
- Fixed `low_volatility_252_20` and candidate `stockdb_low_vol_half_momentum_20` used the shared `next-close-d0-d1` route; StockDB coverage was `40/40`, candidate batch produced `20` targets and `20` paper orders, and the budgeted joint projection produced `22` orders.
- The candidate terminal account was extended through `2026-10-23` from the pinned StockDB replay plus the retained local supplement. This remains research-only and is not a promotion decision.
- Fixed a timing defect in `trading/research/next_close_d0_d1.py`: a next-morning snapshot capture no longer makes a close-day quote appear to be observed on D1. Quote `observed_at` is bound to the close day at `15:00 +08:00`; the actual snapshot capture clock is retained as `source_available_at`. Focused tests: `19 passed`.
- Failure evidence is retained in `outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v1/contract_receipt.json`; successful receipt is `outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v2/contract_receipt.json`.
- Boundaries remain unchanged: `execution_admitted=false`; no broker/THS interaction; TARGET_PREVIEW only; persistent fixed_daily rollover still waits for a complete changed close.

## 2026-10-26 F05/F06 local task measurement

在已验收的固定腿 + 候选腿 D0/D1 纸面包上，`next_close_d0_d1.py` 现在为成功、缺数阻断和失败回执统一记录 `local_task_measurement_v1`：本地单调时钟墙钟耗时、完成时间和未量化的外部 provider 成本。测量不进入业务输入指纹，不改变 Engine/Ledger、费用规则或执行权限。新回执 `outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v3/contract_receipt.json` 记录 40/40 覆盖、22 笔联合纸面订单和 `wall_seconds=0.132335`；报告为 `outputs/research_reports/20261026_fixed_candidate_d0_d1_v3.md`。`test_next_close_d0_d1.py`：19 passed。F06 已从 UNBOUND 推进为 PARTIAL；provider expense、真实渠道耗时和持久任务吞吐仍未宣称已测量。

## 2026-10-26 工作台生命周期绑定

工作台 `submitted → compute → result → download` 生命周期的每个阶段现在都返回同一个 `job_id`，后端与前端 `JobLifecycle` 类型同步。排队态和完成态的四阶段绑定测试已通过 28 项；回执 `outputs/pipeline_receipts/20261026_workbench_lifecycle_job_id_v2.json`，报告 `outputs/research_reports/20261026_workbench_lifecycle_job_id_v2.md`。该改动只改善本地 research-only 工作台的刷新/重连可追踪性，`execution_admitted=false`，没有券商、同花顺或真实账户交互。

## 2026-10-26 internal release v35 refresh

研究发布 v35 已纳入 fixed+candidate D0/D1 v3 本地任务测量和工作台生命周期 job_id 绑定证据：`D:/QuantificationData/trading/research/releases/20261026_v35/release_manifest.json`。发布范围仍是 research-only；`execution_admitted=false`、`live_execution_ready=false`，没有真实登录、预填提交或账户变更。

## 2026-10-26 F2 publication boundary contract

F2 的 publication contract 现在要求：日期型披露按 `[D 00:00, D+1 00:00)` 解释，`retrieved_at` 与 `asof_at` 必须晚于保守结束边界，同时不晚于 decision cutoff，并满足 `asof_at >= retrieved_at`。新增聚焦测试 21 passed；现有 000002.SZSE 真实来源快照重跑为 `BLOCKED_DATA`，哈希验证通过但仍缺 `asof_at` 与 `revision_id`，因此没有财报事实、因子或账户回放准入。

证据：`outputs/pipeline_receipts/20261026_f2_real_source_snapshot_v2/receipt.json`、`outputs/research_reports/20261026_f2_publication_boundary_contract_v1.md`。

## 2026-10-26 strategy adoption rollup D0/D1 binding

研究采用汇总现在可选地绑定最新完成的 D0/D1 回执，而不是继续把已完成收市任务列为待办。`research-adoption-rollup` 新增 `--d0-d1-receipt`，严格校验完成状态、40/40 目标覆盖、执行边界、固定腿、候选批次和联合投影字段；回执 `outputs/pipeline_receipts/20261026_research_adoption_rollup_v4.json` 已绑定 2026-10-26 D0/D1，报告为 `outputs/research_reports/20261026_research_adoption_rollup_v4.md`。

该改动只更新研究证据汇总和下一步路由，不改变任何策略采用决定：候选仍是 `CONTINUE_RESEARCH_ONLY`，成熟因子、短线模型和联合组合仍保持各自研究门禁，`paper_allowed=false`、`promotion_allowed=false`、`execution_admitted=false`。聚焦测试 `17 passed`。

## 2026-10-26 Google engineering D0/D1 v4

已把 2026-10-26 收市后的 StockDB 冻结快照、固定 `low_volatility_252_20` 和候选 `stockdb_low_vol_half_momentum_20` 接入同一 `next-close-d0-d1` 合同。目标集合覆盖 40/40；固定腿生成 20 个 TARGET_PREVIEW 目标和 20 个纸面订单；候选批次进入研究 follow-up 门禁；预算型联合投影生成 22 个纸面订单。输入清单为 `outputs/pipeline_receipts/20261026_candidate_source_manifest_v2.json`，回执为 `outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v4/contract_receipt.json`，报告为 `outputs/research_reports/20261026_fixed_candidate_d0_d1_v4.md`。

本包遵循 Google Applied Design 的明确边界：输入快照、账户、策略、费用和时间语义均写入 fingerprint；`persistent_ledger_mutated=false`、`execution_admitted=false`，没有 Broker/THS 交互，结果只属于 TARGET_PREVIEW 和共享 Ledger 的临时纸面投影。由于 `stockdb_fixed_daily` 仍只接受严格完整的 BaoStock close package，当前 v4 使用直接终端账户输入，不能宣称 persistent fixed_daily 已滚动推进；完整同日 close package 到位后再通过注册账户入口发布。

## 2026-10-26 Google engineering refactor: explicit unknown publication boundary

修正了 `publication_event_contract.evaluate_publication_evidence` 在 publication 元数据缺失或格式错误时引用未初始化窗口的错误路径。现在该路径返回 `publication_boundary_unknown`，并保持 `pit_asof_safe=false`；新增缺失 `publication_date` 与错误 `publication_time` 反例。F2 聚焦测试共 23 passed。证据：`outputs/pipeline_receipts/20261026_publication_boundary_unknown_v1.json`、`outputs/research_reports/20261026_publication_boundary_unknown_v1.md`。


## 2026-10-26 F2 Wayback capture precision

`f2_wayback_probe.py` 现在保留 Wayback CDX 14 位 UTC 捕获时间，写入 `retrieved_at`、`asof_at` 和 `capture_selection.selected_capture_at`，不再把捕获时点截成日期午夜。真实 `000002.SZSE/2023Q1` 探针重新运行后仍为 `BLOCKED_DATA`：唯一捕获为 `2025-09-05T23:50:23+00:00`，决策日 `2024-05-01` 前没有捕获，因此没有 PIT、因子或账户准入。17 个 F2 聚焦测试通过。

证据：`outputs/pipeline_receipts/20261026_f2_wayback_2023q1_v4.json`、`outputs/research_reports/20261026_f2_wayback_2023q1_v4.md`。

## 2026-10-26 current-window fixed versus half-momentum comparison

基于 2026-10-26 已完成的两个 StockDB rolling 账户产物，新增显式路径的同窗口比较入口 `run_committee.py current-window-strategy-comparison`，不改变既有长窗口回执，也不重跑策略。共同保存窗口为 2026-08-31..2026-10-26、35 个交易日：固定 `low_volatility_252_20` 净收益 0.8293%、最大回撤 -2.0255%、Sharpe 0.8366、25 笔订单；低波/半动量净收益 -9.4132%、最大回撤 -9.6664%、Sharpe -5.8082、40 笔订单。按固定研究规则，固定腿为 `CONTINUE_RESEARCH_ONLY`，半动量腿为 `STOP_RESEARCH_ONLY`；该结论只用于研究路由，不构成策略验证或采用。

回执为 `outputs/pipeline_receipts/20261026_current_window_fixed_candidate_v1.json`，报告为 `outputs/research_reports/20261026_current_window_fixed_candidate_v1.md`，聚焦测试 `17 passed`。比较严格绑定共享 StockDB snapshot、Engine/Ledger 产物和执行边界，保持 `execution_admitted=false`、`strategy_validated=false`、`promotion_allowed=false`，没有 broker/THS 交互。

## 2026-10-26 adoption rollup binds current-window comparison

`research-adoption-rollup` 新增 `--current-window-comparison`，将显式 2026-08-31..2026-10-26 固定/半动量同窗口比较作为独立研究 gate 绑定到 v5 rollup。回执 `outputs/pipeline_receipts/20261026_research_adoption_rollup_v5.json` 同时保留 D0/D1 close 2026-10-26、成熟因子、短线和联合投影 gate；固定腿继续 `CONTINUE_RESEARCH_ONLY`，半动量在 35 日终端窗口为 `STOP_RESEARCH_ONLY`。该绑定不把短窗口结果提升为策略验证，仍保持 `paper_allowed=false`、`promotion_allowed=false`、`execution_admitted=false`。聚焦测试 `24 passed`。

## 2026-10-26 strict StockDB close package boundary

新增 `trading/research/stockdb_close_package.py`，定义 `stockdb_close_package_v1` 的严格只读 loader：校验 plan/receipt/roster/raw 的哈希、精确收市日与标的清单、源 StockDB snapshot 的 manifest/daily 哈希和逐行 close 一致性，并拒绝不完整原生快照。`stockdb_fixed_daily.load_complete` 现在按 plan schema dispatch：仅 `stockdb_close_package_v1` 走新 loader，BaoStock 包继续走原有 loader；新增 `run_committee.py stockdb-close-package --package` 入口。当前没有可消费的完整 2026-10-26 全宇宙包，因此没有伪造或推进 persistent 账户。

测试：`trading/research/tests/test_stockdb_close_package.py trading/research/tests/test_stockdb_fixed_daily.py trading/research/tests/test_research_adoption_rollup.py trading/research/tests/test_current_window_strategy_comparison.py trading/tests/test_run_committee_dispatch.py` 共 `37 passed`。

## 2026-10-26 StockDB close package producer

新增 `trading/research/stockdb_close_package_builder.py` 与 `run_committee.py stockdb-close-package-build`。该入口只读取已保存 StockDB snapshot、availability、日历和精确日期，完整覆盖时生成 loader 可消费的 `plan.json`、`receipt.json`、`roster.csv`、`raw_daily.csv`；覆盖不足时只写阻断回执，不生成可被 fixed_daily 消费的包。对当前 `stockdb_20261026_recent_full_v1` 实际运行结果为 `BLOCKED_INCOMPLETE_STOCKDB_SNAPSHOT`：5,201 个请求标的中 5,176 个可用，25 个缺失，阻断回执为 `D:/QuantificationData/trading/research/stockdb_close_packages/20261026_native_full_universe_v2_blocked/receipt.json`，没有填充或改写源数据。

producer 完整包/缺数测试与 fixed_daily、rollup、launcher 测试共 `31 passed`。

## 2026-10-26 F2 semantic extraction boundary

`f2_issuer_source_evidence.py` now accepts `pypdf` text only when the required semantic field labels (`总资产` and `归属于上市公司股东的净利润`) are present; otherwise it falls back to Poppler. The regression test uses mojibake-like ticker/year output to prove that digits alone cannot satisfy the scope contract. The re-captured `000002.SZSE/2023Q1` receipt records verified issuer field scope and a content-hash match, while historical retrieval/as-of and revision history remain unproven, so status stays `BLOCKED_DATA`.

Evidence: `outputs/pipeline_receipts/20261026_f2_issuer_source_capture_v2.json`, `outputs/research_reports/20261026_f2_issuer_source_capture_v2.md`; focused F2 suite `24 passed`. No F2 fact, factor, target, order or account replay was admitted; `execution_admitted=false`.

## 2026-10-27 F2 unified entrypoint

`run_committee.py f2-issuer-source-evidence` now exposes the F2 issuer-source capture through the project entrypoint. The route only forwards explicit symbol, period, publication date, decision cutoff, source URL, local artifact and output path; it does not widen the PIT gate or create account state. Dispatch/help regression coverage passes together with the semantic extraction tests (`23 passed`).

## 2026-10-27 workbench recovery unified entrypoint

新增 `run_committee.py workbench-recovery-evidence` 统一入口，消费保存的本地 job observation，生成纸面恢复回执和报告。CLI 实际回执 `outputs/pipeline_receipts/20261027_workbench_recovery_cli_v1.json` 将 interrupted job 分类为 `MANUAL_RESUBMIT_REQUIRED`，自动重试、券商/同花顺交互和真实执行均为 false；5 个聚焦测试通过。该包不修改任务、不创建账户、不连接外部渠道。

## 2026-10-27 StockDB close package consumer integration

完成了严格 `stockdb_close_package_v1` 到 append-only supplement 的真实隔离链路：2026-10-22 与 2026-10-23 两个 20 标的包均为 20/20 完整覆盖，生成的 `20261027_append1023_stockdb_v2` 保留原始前缀并追加 40 行，source proof 与 fixed_daily 使用同一 completion-clock fallback。固定纸面账户的三次尝试全部保留为 `failed_uncommitted`：首次是原始 source lineage 不匹配，修复 proof 后进入明确的 `tradestatus/adjustflag` 执行引用门禁。由于这两个 bounded StockDB 包只有 OHLCV/`pre_close`，没有独立 native 执行状态流，fixed_daily 在 replay 前停止；没有填充状态、没有推进 head、没有生成订单，也没有券商或同花顺交互。

代码与回归测试已绑定到 `trading/research/lowvol_supplement_append.py`、`trading/research/stockdb_fixed_daily.py`；聚焦测试 `16 passed`。证据回执为 [`20261027_stockdb_close_package_fixed_daily_consumer_v1.json`](../../outputs/pipeline_receipts/20261027_stockdb_close_package_fixed_daily_consumer_v1.json)，报告为 [`20261027_stockdb_close_package_fixed_daily_consumer_v1.md`](../../outputs/research_reports/20261027_stockdb_close_package_fixed_daily_consumer_v1.md)。该结果是 bounded research integration，不是全市场、PIT 或 live execution 证据；`execution_admitted=false` 保持不变。

在上述门禁基础上新增了显式 opt-in 的 `stockdb_pre_close_derived_research_v1` 纸面模式。它只对带有严格 StockDB package proof、但缺少独立 `tradestatus/adjustflag` 的输入开放，按 StockDB 成交量派生研究状态，并把 `execution_reference_mode=derived_stockdb_research_only` 写入 registration、head、protocol 和 run。新的隔离账户 [`20261027_close_package_derived_consumer_v1`](../../../../QuantificationData/trading/research/fixed_daily/20261027_close_package_derived_consumer_v1) 已从 2026-10-21 推进到 2026-10-23，新增 2 个交易日、25 个订单、44 个成交，期末标记权益 CNY 139,638.07；这是真实共享 Engine/Ledger 研究纸面结果，仍不是 native quote、全市场、PIT 或 live 证据。默认 native 模式仍会在相同输入上停止，失败证据保持不变。回执为 [`20261027_stockdb_fixed_daily_derived_research_v1.json`](../../outputs/pipeline_receipts/20261027_stockdb_fixed_daily_derived_research_v1.json)，聚焦测试 `20 passed`。

随后将该注册账户接入 post-close 协调器，完成了固定策略 + 1 个候选策略的同一 D0/D1 纸面投影。2026-10-23 收市包对应的 40 个目标标的收市覆盖为 40/40；固定腿生成 20 个 TARGET_PREVIEW 目标和 20 个纸面订单，联合预算投影生成 22 个纸面订单。fixed_daily 只复用已提交的 generation-1 head，D0/D1 使用临时共享 Ledger，不改持久账户。摘要回执为 [`20261027_post_close_derived_d0_d1_v1_summary.json`](../../outputs/pipeline_receipts/20261027_post_close_derived_d0_d1_v1_summary.json)，报告为 [`20261027_post_close_derived_d0_d1_v1.md`](../../outputs/research_reports/20261027_post_close_derived_d0_d1_v1.md)；完整协调回执保留在同目录。`execution_admitted=false`、broker/THS 交互均为 false。

基于该 post-close D0/D1，已刷新研究采用汇总 v7：候选矩阵为 3 个继续研究、2 个停止原规格、1 个覆盖阻断、4 个独立门；策略验证数为 0。汇总同时绑定成熟因子、短线 LR/LightGBM、保留联合组合、2026-08-31..2026-10-26 current-window 比较和最新 2026-10-23 D0/D1。固定低波策略在 current-window 为 `CONTINUE_RESEARCH_ONLY`，低波/半动量为 `STOP_RESEARCH_ONLY`；所有结果继续保持 `paper_allowed=false`、`promotion_allowed=false`、`execution_admitted=false`。回执为 [`20261027_research_adoption_rollup_v7.json`](../../outputs/pipeline_receipts/20261027_research_adoption_rollup_v7.json)，报告为 [`20261027_research_adoption_rollup_v7.md`](../../outputs/research_reports/20261027_research_adoption_rollup_v7.md)。

又将 `execution_reference_mode=derived_stockdb_research_only` 从 fixed_daily head 透传到 D0/D1 的 `fixed_account` 绑定，避免后续消费丢失报价语义。重新生成的 post-close v2 仍为 40/40 覆盖、20 固定目标、22 联合纸面订单，摘要回执为 [`20261027_post_close_derived_d0_d1_v2_summary.json`](../../outputs/pipeline_receipts/20261027_post_close_derived_d0_d1_v2_summary.json)。

补充了当前 D0/D1 目标的 THS 只读回读契约夹具：20 行 TARGET_PREVIEW 固定目标被转换成 reference/readback 两份本地 JSON，比较器返回 `readback_verified`。回执明确 `account_observed=false`、`quote_observed=false`、`client_interacted=false`、`submitted=false`、`execution_admitted=false`；这证明了可复用的字段一致性比较，不代表真实同花顺窗口观察。回执为 [`20261027_ths_target_readback_current_synthetic_v1.json`](../../outputs/pipeline_receipts/20261027_ths_target_readback_current_synthetic_v1.json)，报告为 [`20261027_ths_target_readback_current_synthetic_v1.md`](../../outputs/research_reports/20261027_ths_target_readback_current_synthetic_v1.md)。

同时刷新了 F2 财报因子消费者的 synthetic vertical slice：两家合成 issuer 通过 Data、F01、Signal、Target Portfolio、Orders、BrokerAdapter、Fill、Cash/Position 七段，生成 2 个目标和 2 个纸面订单。回执明确所有事实、发布时间、revision_id 和报价均为 synthetic fixture，不能替代真实 PIT 财报证据；真实 F2 仍保持 `BLOCKED_DATA`。摘要回执为 [`20261027_f2_vertical_slice_current_synthetic_v1_summary.json`](../../outputs/pipeline_receipts/20261027_f2_vertical_slice_current_synthetic_v1_summary.json)，报告为 [`20261027_f2_vertical_slice_current_synthetic_v1.md`](../../outputs/research_reports/20261027_f2_vertical_slice_current_synthetic_v1.md)。

## 2026-10-27 explicit target roster close package and fixed-only D0/D1

2026-10-26 的全市场 StockDB 捕获仍有 5,201 个请求标的中的 25 个缺失，严格全市场 close package 继续保留 `BLOCKED_INCOMPLETE_STOCKDB_SNAPSHOT`。为推进独立主线，close-package builder 新增显式 `--roster`：只接受已包含在同一捕获请求中的目标集合，不发现新标的、不填补缺数，并在 plan/receipt 中保留 roster 引用和模式。基于固定策略当前 20 个目标生成了完整的 `D:/QuantificationData/trading/research/stockdb_close_packages/20261026_fixed_target_v1`，并把 20 行追加到 `20261027_append1026_stockdb_v1`，原 supplement 前缀保持不变。

注册的 derived StockDB research-only fixed_daily 账户已从 2026-10-23 推进到 2026-10-26 generation 2，新增 1 个交易日、25 个订单、44 个成交，期末权益 CNY 141,161.07。随后完成固定腿 D0/D1：2026-10-26 close、2026-10-27 execution、2026-10-28 settlement，20/20 目标覆盖、20 个 TARGET_PREVIEW 纸面订单、候选为空，回执为 `outputs/pipeline_receipts/20261027_post_close_fixed_only_20261026_v1/post_close_receipt.json`。所有结果仍为 derived StockDB research-only，`execution_admitted=false`、broker/THS/持久联合账本交互均为 false；完整全市场 package 和 native 执行状态门禁没有被绕过。builder/launcher 聚焦测试 `25 passed`。

## 2026-10-27 public-seat adoption gate binding

研究采用汇总现在把公开席位候选的独立闸门从普通候选矩阵中显式提出：绑定 `20261026_candidate_adoption_decision_v2.json` 中的 9/9 完整回放，记录 `CONTINUE_RESEARCH_ONLY`，并保留发布时间时点、PIT 成员资格和因果席位归因均未验证的事实。该闸门不进入 D1 follow-up，也不允许 paper/promotion；`execution_admitted=false` 保持不变。实现、边界拒绝测试和回执为 `trading/research/research_adoption_rollup.py`、`trading/research/tests/test_research_adoption_rollup.py`、[`20261027_research_adoption_rollup_v8.json`](../../outputs/pipeline_receipts/20261027_research_adoption_rollup_v8.json) 和 [`20261027_research_adoption_rollup_v8.md`](../../outputs/research_reports/20261027_research_adoption_rollup_v8.md)。聚焦 rollup/launcher 测试 `26 passed`，内部发布候选为 `D:/QuantificationData/trading/research/releases/20261027_v46/release_manifest.json`。

## 2026-10-27 F2 historical gate CLI

新增 `run_committee.py f2-historical-gate`，将已保存的来源快照或显式 rows 接入现有 PIT/as-of/revision 合同。实际使用 CNINFO 000002.SZSE 2023Q1 当前来源快照生成 [`20261027_f2_historical_gate_cli_real_source_v1.json`](../../outputs/pipeline_receipts/20261027_f2_historical_gate_cli_real_source_v1.json)：1 行、0 行 ready，`asof_at` 和 `revision_id` 缺失被保留为 `BLOCKED_DATA`，没有补齐、因子计算或账户回放；`execution_admitted=false`。F2 historical gate 与 launcher 聚焦测试 `25 passed`，提交为 `4adab45`。

同花顺只读回读比较器现在区分 `synthetic_fixture`、`offline_reference` 和带来源证明的 `authorized_read_only_export`。任何声称账户、行情或客户端已观察的回读，必须同时提供授权捕获模式、带时区的 `observed_at`、`source_file` 和 SHA-256；缺少这些字段会停止比较。现有 20 行合成回读仍为 `readback_verified`，但 `authorized_readonly_observation=false`；回执和报告为 `outputs/pipeline_receipts/20261027_ths_target_readback_current_synthetic_v1/comparison.json` 与 `report.md`，THS 真实观察仍未声称完成。

## 2026-10-27 workbench recovery release binding

工作台恢复回执现在绑定保存的 job observation 路径与 SHA-256，并记录只读内部 release 的版本和 commit。中断任务仍分类为 `MANUAL_RESUBMIT_REQUIRED`，不会自动重试，也不触碰券商、同花顺或真实账户；最新回执 [`20261027_workbench_recovery_release_bound_v2.json`](../../outputs/pipeline_receipts/20261027_workbench_recovery_release_bound_v2.json) 绑定 `20261027_v50`，`execution_admitted=false`、`live_execution_ready=false`。workbench/recovery 与 WebApp 聚焦测试 `42 passed`。

## 2026-10-27 F2 source artifact integrity binding

F2 historical gate CLI 现在对输入行声明的 `source_file/source_sha256` 做本地字节校验，并把 `verified`、`missing_file`、`hash_mismatch` 明确写入回执；该校验只增强来源可追查性，不补齐 `asof_at` 或 `revision_id`。对现有 CNINFO 000002.SZSE 2023Q1 来源快照重跑后，来源 PDF hash 校验为 `verified`，但历史 retrieval/as-of 与 revision 仍缺失，结果继续为 `BLOCKED_DATA`、0 行 ready。回执为 `outputs/pipeline_receipts/20261027_f2_historical_gate_cli_real_source_v2.json`，F2 测试 `7 passed`。

## 2026-10-27 candidate same-day freshness gate

新增 `candidate-same-day-gate`，在候选进入 D0/D1 target projection 前校验每个 target 的 `decision_day`。对保存的 `stockdb_low_vol_half_momentum_20` 2026-10-23 manifest 请求 2026-10-26 close，回执明确为 `BLOCKED_STALE_CANDIDATE_INPUT`，没有生成 target、订单或账户状态；回执为 `outputs/pipeline_receipts/20261027_candidate_same_day_gate_20261026.json`。该闸门已接入 `run_committee.py`，候选 gate/F2/launcher 测试共 `33 passed`，`execution_admitted=false` 保持不变。

研究采用汇总 v9 已绑定该 freshness gate：2026-10-23 的旧候选输入在 2026-10-26 请求下保持 `BLOCKED_STALE_CANDIDATE_INPUT`，没有进入 D0/D1；回执和报告为 `outputs/pipeline_receipts/20261027_research_adoption_rollup_v9.json` 与 `outputs/research_reports/20261027_research_adoption_rollup_v9.md`，paper/promotion 仍关闭。

## 2026-10-27 F2 Wayback historical probe

对 CNINFO `000002.SZSE/2023Q1` 原始 PDF URL 执行真实 Wayback CDX 探针：CDX 查询成功，但唯一捕获时间为 `2025-09-05T23:50:23+00:00`，晚于决策日 `2024-05-01`，因此没有历史 as-of 证据，结果为 `BLOCKED_DATA`。没有生成 FinancialFact、因子、目标或账户回放。回执为 `outputs/pipeline_receipts/20261027_f2_wayback_000002_2023q1_v2.json`，`execution_admitted=false` 保持不变。

Wayback 回执现已接入 `f2_fact_bridge`：使用公开替代财务 sidecar 与该历史探针回执运行 18 行事实桥，结果为 `BLOCKED_DATA`、0 个事实、0 个可计算因子；其中 2023Q1 行保留 `archive_capture_after_decision_only` 原因，其余行保留历史证据缺失。回执为 `outputs/pipeline_receipts/20261027_f2_wayback_bridge_000002_v1.json`，Fact bridge/Wayback/launcher 测试 `35 passed`。



## Google Applied Design incremental delivery (2026-10-27)

已将日终纸面刷新主循环按“attempt / success / retry / input revision”拆成可恢复的最小边界。`RefreshTracker` 不再在 `prepare_data()` 调用前把当天标记为完成：失败会在 30、60、120、240、300 秒上限内退避重试，成功后同日不重复刷新，成功输入目录会作为 `input_revision` 写入既有 `meta.refresh` 回执，worker 重启时恢复该状态。

验收回执：[`20261027_worker_refresh_retry_v1.json`](../../outputs/pipeline_receipts/20261027_worker_refresh_retry_v1.json)；报告：[`20261027_worker_refresh_retry_v1.md`](../../outputs/research_reports/20261027_worker_refresh_retry_v1.md)。测试命令产生 `4 passed, 55 deselected`。该改动只影响纸面数据刷新调度，不改变 Engine/Ledger、账户余额、候选采用或执行闸门。

下一步沿同一条 Google 波次推进：在下一个可用收市输入上复用该回执，继续 fixed_daily → D0/D1；数据缺口只阻塞依赖的收市包，不阻塞代码和研究比较。


## Google Applied Design mainline increments (2026-10-27)

本轮完成三个可复用主线包。研究工作台把 submit/query/download 的业务校验和结果读取移到 `webapp/backend/app/research_jobs.py`，HTTP 层只保留解析、错误映射和响应；原有 job_id、幂等键、状态码与研究-only 边界保持不变。

F2 fact bridge 现在可以直接消费 `f2_source_snapshot_v1` 原始清单，先复用 source snapshot 哈希检查，再进入 publication/as-of/revision 闸门。真实 current-fetch 回执仍是 `BLOCKED_DATA`：18 行、0 个可用事实、0 个因子，决策日前 as-of/revision 证据缺失被保留。

策略比较新增六策略同口径入口：固定低波动、低波/半动量、12-1 动量、成熟因子 Ridge、Alpha158/LightGBM、Ridge h60。共同 StockDB 快照覆盖 `2026-01-05..2026-10-13` 的 184 个会话，六个结果均保持 `STOP_RESEARCH_ONLY`；回执已绑定到 adoption rollup v10，仍不允许 paper/promotion。

组合回归：研究任务/Web API 32 passed；F2、策略比较、adoption rollup 及相关边界测试在提交前复跑。当前发布继续保持 `execution_admitted=false`、`NO_TRADE`、`candidate_panel_only=true`。


## Mainline delivery increments (2026-10-27, wave 2)

日终主线的 `post_close_d0_d1.run_pipeline` 已按阶段拆分为 fixed_daily、candidate_inputs、D0/D1 和 terminal receipt；回执新增 `stage_trace`，保留输入指纹、幂等复用、fixed_daily 先于 D0/D1 和 `execution_admitted=false`。

F2 新增共享 `latest_on_or_before_cutoff_v1` 来源选择器，Wayback 探针改用同一选择契约，并生成 000002.SZSE 选择回执。现有观察仍只有 2025-09-05 之后捕获，eligible=0，结果继续为 `BLOCKED_DATA`，没有放宽 PIT/revision 闸门。

U02 新增隔离本地 HTTP 观测探针，实际完成 CSV 上传、同幂等键 POST、worker 完成、lifecycle/result 查询和三个结果文件下载；回执为 `COMPLETED_LOCAL_RESEARCH_HTTP_CHAIN`。这是本地合成研究链证据，不是浏览器、THS 或账户证据。

本轮新增工作包测试：post-close focused 57 passed；F2 selection/Wayback/source snapshot/dispatch 33 passed；U02 真实进程链 1 passed。


## Weekly research review v3 (2026-10-27)

本周复盘回执 [`20261027_weekly_review_receipt_v3.json`](../../outputs/weekly_reviews/20261027_weekly_review_receipt_v3.json) 已生成。六策略同快照比较覆盖 184 个会话且全部保持 `STOP_RESEARCH_ONLY`；2026-10-26 全市场收市覆盖仍为 226/5211 行，显式 20 标的固定子集为完整研究包；F2 原始快照桥接仍为 18 行、0 个 ready fact；U02 本地 HTTP 链已完成。

最大阻碍仍是完整全市场 StockDB 收市包、F2 决策日前 retrieval/as-of/revision 和授权 THS 只读导出。下一收市日沿 staged post-close 执行，保持缺失显式、无补值、无真实渠道操作。


## THS readback provenance contract (2026-10-27)

`trading/ths/target_readback.py` 现在可在提供 `source_root` 时重新读取授权只读导出并校验 SHA-256；文件缺失或哈希不一致会停止比较。合成和离线回读仍明确标记为非授权观察，未产生客户端操作、提交或账本变更。回执见 [`20261027_ths_readback_provenance_contract_v1.json`](../../outputs/pipeline_receipts/20261027_ths_readback_provenance_contract_v1.json)，测试 `10 passed`。


## Post-close timing contract (2026-10-27)

post-close 终态回执现在记录 `finished_at` 和 `elapsed_seconds`，绑定现有输入指纹与执行边界。它只衡量本地编排耗时，不冒充供应商费用、成交质量或策略证据。回执 [`20261027_post_close_timing_contract_v1.json`](../../outputs/pipeline_receipts/20261027_post_close_timing_contract_v1.json)，测试 `8 passed`。

## Google Applied Design command-policy refactor (2026-10-27)

日终刷新入口已完成一个保持行为不变的结构重构：`trading/research/daily_refresh_policy.py` 以不可变 `RoutePolicy` 表集中保存允许的研究路由、子命令约束、固定三页采集边界和 native PAPER closing 参数；`system_daily_refresh.py` 只负责委托和编排，不再维护逐路由条件链。原有命令 argv、`execution_admitted=false`、`candidate_panel_only=true` 和禁止 `--live/--submit/--train` 的边界保持不变。新增策略表测试覆盖正常路由、runtime-repair、非法子命令、seat-collect 三页同日约束和危险 flag 拒绝；聚焦刷新/披露/路由适配测试共 `16 passed`。代码提交为 `6097e4a`。

后续按 Google Applied Design 的迁移顺序推进：先补真实 F2 `retrieval/as-of/revision` 事实，再在新收市输入上复用 `fixed_daily → D0/D1`，随后从一个真实纸面旅程继续抽取薄应用用例；最后只在实际压力有证据时扩展发布、恢复和容量。暂不做全仓格式化、Bazel/微服务迁移或重复审计；完整全市场 StockDB、授权 THS 只读导出和真实 PIT 仍是独立未完成项。

## Google Applied Design paper continuity and F2 cutoff (2026-10-27)

纸面账户连续性查询已完成第二个薄应用用例切片：`/paper-account-continuity` 路由只做 HTTP 委托，连续性回执读取和只读投影移到 `trading/research/paper_account_continuity.py`，显式接收根目录，不推进账户或 Ledger。回执为 [`20261027_paper_account_continuity_use_case_v1.json`](../../outputs/pipeline_receipts/20261027_paper_account_continuity_use_case_v1.json)，提交 `e36a4b8`；连续性/API/operator/architecture 相关测试及本轮组合运行共 `89 passed`。

F2 来源选择现在按 `decision_time(decision_day)=15:05:00+08:00` 比较时间戳，而不是只比较日期；同日决策后捕获会留在观察证据中但不会进入 `eligible`。真实 `000002.SZSE/2023Q1` Wayback 仍只有 `2025-09-05T23:50:23+00:00` 捕获，2024-05-01 决策日的选择回执为 `BLOCKED_DATA`、eligible=0；没有 PIT 事实、因子或账户回放准入。回执为 [`20261027_f2_decision_cutoff_selection_v1.json`](../../outputs/pipeline_receipts/20261027_f2_decision_cutoff_selection_v1.json)，提交 `29ee086`，相关测试 `34 passed`。

下一步仍按主线推进：取得真实决策日前的 retrieval/as-of/revision/source bytes 证据；下一完整收市后复用 `fixed_daily → D0/D1`；只在有真实消费者时继续抽取应用用例。

## E05 paper timing state binding (2026-10-27)

已有 post-close 终态回执和日终刷新回执现在正式绑定 E05：非实时纸面编排的 `finished_at` 与 `elapsed_seconds` 可追踪，状态由 `UNBOUND` 更新为 `PARTIAL`。该状态不代表供应商费用、队列位置、成交质量或策略证据；下一完整收市再把耗时绑定到实际接受的纸面任务，费用字段只有在授权的非实时来源提供时才加入。

## Accepted paper-task timing binding and F2 provenance (2026-10-27)

E05/F06 已完成一个可复用的纸面任务绑定器：[`paper_timing_binding.py`](../../trading/research/paper_timing_binding.py) 将 post-close timing contract 与一条已完成的 research-only D0/D1 回执按哈希绑定。实际绑定 2026-10-23 收市任务，覆盖 40/40、固定纸面订单 20、联合纸面订单 22、本地耗时 `0.099731s`；供应商费用保持 `null`。回执为 [`20261027_paper_task_timing_binding_v1.json`](../../outputs/pipeline_receipts/20261027_paper_task_timing_binding_v1.json)，测试 `5 passed`，提交 `0cec03c`。这不是券商或同花顺证据。

F2 fact bridge 现在把 `source_snapshot_id`、`source_sha256`、`snapshot_source_check` 和 `snapshot_source_check_passed` 传入每个事实的 `source_provenance`；哈希不匹配仍然阻断，不放宽 PIT/revision 闸门。18 行真实桥接重跑仍为 `BLOCKED_DATA`、0 个 ready fact、0 个因子。回执为 [`20261027_f2_fact_bridge_provenance_v1.json`](../../outputs/pipeline_receipts/20261027_f2_fact_bridge_provenance_v1.json)，测试 `27 passed`，提交 `da312f5`。

补充探测了已有 F01 样本中的 600004.SSE/2024Q1 与 688008.SSE/2024Q1 两个原始披露 URL。Wayback CDX 均返回 0 个捕获，故两条仍为 `BLOCKED_DATA`、`pit_asof_safe=false`；失败证据保留在 [`20261027_f2_wayback_two_issuer_probe_v1.json`](../../outputs/pipeline_receipts/20261027_f2_wayback_two_issuer_probe_v1.json)。这次探测没有把当前披露冒充历史 PIT，也没有阻塞代码、纸面或策略比较路线。

## Candidate library binding (2026-10-27)

新增 [`candidate_library_binding.py`](../../trading/research/candidate_library_binding.py) 将候选目录、六策略同口径面板和 adoption rollup v10 按 `candidate_spec_id` 绑定成一份机器读候选库。9 个目录项中，Ridge 两个 horizon 作为同一 spec 的显式 variant；shortline、公开席位、value-size 和 leader-style 保留为 `SEPARATE_GATE_BOUND`，没有被冒充成共同窗口比较。实际回执 [`20261027_candidate_library_binding_v1.json`](../../outputs/pipeline_receipts/20261027_candidate_library_binding_v1.json) 的汇总为 3 continue、2 stop、1 coverage blocked、4 separate gate、0 strategy validated；测试 `2 passed`，提交 `6ad3356`。

候选库 v2 又把最新 D0/D1 纸面回执、candidate same-day gate、full-panel comparison 和 current-window comparison 作为顶层 pipeline source bindings 暴露出来，形成从候选规格到纸面计划的可追查入口；这些绑定只引用已保存回执，不重跑策略或改变采用决定。回执为 [`20261027_candidate_library_binding_v2.json`](../../outputs/pipeline_receipts/20261027_candidate_library_binding_v2.json)，测试 `2 passed`。

候选库 v3 增加目标持仓到 THS 只读回读的 channel binding：当前来源明确为 `READBACK_VERIFIED_SYNTHETIC_ONLY`、20 行、`authorized_readonly_observation=false`，并保留来源哈希与比较状态。它没有打开客户端、预填、提交或修改账本；真实授权导出仍需单独提供。回执为 [`20261027_candidate_library_binding_v3.json`](../../outputs/pipeline_receipts/20261027_candidate_library_binding_v3.json)，测试 `3 passed`。

补充生成了 [`20261027_paper_pipeline_lineage_v1.json`](../../outputs/pipeline_receipts/20261027_paper_pipeline_lineage_v1.json)：将同一 2026-10-23 D0/D1 保存产物按哈希串起 Data、Signal、TARGET_PREVIEW、纸面 Orders、`REFERENCE_PAYLOAD_ONLY` BrokerAdapter、建模 Fill 和 Cash/Position。回执明确 22 个联合目标、22 个联合纸面订单、22 个 projected positions；Fill 数量不从订单数推断，BrokerAdapter 不代表真实 THS 交互。

该 lineage 已收敛为可重复 CLI [`paper-pipeline-lineage`](../../run_committee.py)，重新读取并校验同一组保存回执后生成 v2；测试 `2 passed`，`py_compile` 通过。它只重建研究证据，不重跑策略、不访问外部渠道。

候选库 v4 已将该 lineage receipt 作为必需 source binding，候选库现在可从规格和采用决定直接追到 D0/D1、THS synthetic readback 与 Data→Cash/Position stage map。回执为 [`20261027_candidate_library_binding_v4.json`](../../outputs/pipeline_receipts/20261027_candidate_library_binding_v4.json)，测试 `3 passed`。

按同一薄用例边界，`/paper-preview-latest` 已从 `trading/api.py` 抽到 [`paper_preview_view.py`](../../trading/research/paper_preview_view.py)，路由只做委托，保留 missing/invalid/saved 状态和只读执行边界。回执 [`20261027_paper_preview_usecase_v1.json`](../../outputs/pipeline_receipts/20261027_paper_preview_usecase_v1.json)，相关测试 `58 passed`。

## Google applied design A: F01 period contract (2026-10-27)


按 `Quantification_Google_Applied_Design_20261026.pdf` 的设计决定 A，F01 期间契约已完成一个局部、可交付的语义修复：`compute_f01` 现在只接受已经转换为 `single_quarter` 的归母利润事实；累计利润必须先经过 `cumulative_to_single_quarter`，且派生单季从前一累计期间结束日的次日开始。当前利润与上年利润还必须共享同一季度结束月日，否则返回 `INCOMPARABLE/profit_period_not_comparable`。

聚焦验证命令覆盖 F01、F2 vertical slice、F2 bridge 和 Signal，结果为 `35 passed`。证据回执为 [`20261027_f01_period_contract_v1.json`](../../outputs/pipeline_receipts/20261027_f01_period_contract_v1.json)；该结果只证明本地语义和桥接行为，真实 PIT 检索/as-of/revision 证据仍未补齐，F2 仍保持外部数据阻断。`execution_admitted=false`、`NO_TRADE` 未改变。


F2 的合成 vertical slice 与 bridge 测试夹具已同步改为同季度同比期间（2024Q1 对 2023Q1），避免旧夹具用上一季度数据掩盖 F01 的期间契约；2026-10-25 的旧合成回执保留为历史证据，不作为当前契约的重新验证。


## Google design C: strategy comparison application view (2026-10-27)

同样本策略比较已从 HTTP 路由中提取为 [`trading/research/research_comparison_view.py`](../../trading/research/research_comparison_view.py)。`GET /research-comparison` 和 `GET /research-comparison-snapshots` 现在只负责请求解析与错误映射，应用视图负责读取已保存比较结果；不创建账户、不重放来源、不写入保存产物，响应字段和 404/409 语义保持不变。

聚焦命令 `D:/Quantification/env/python.exe -m pytest -q trading/research/tests/test_research_comparison_view.py trading/tests/test_research_comparison.py` 结果为 `20 passed`。既有 API 消费者测试确认比较过程不调用账户存储且不修改源文件。证据回执为 [`20261027_research_comparison_usecase_v1.json`](../../outputs/pipeline_receipts/20261027_research_comparison_usecase_v1.json)。该切片服务策略比较主线，不改变策略采用决定，`execution_admitted=false` 保持不变。


## 2026-10-26 close: fixed D0/D1 paper production (2026-10-27)

新的完整 2026-10-26 目标标的 StockDB close package 已接入已登记的 derived `fixed_daily` 账户，账户 generation 从 `2026-10-23` 推进到 `generation-2:2026-10-26`。固定低波策略的 D0/D1 合同现在明确记录 `close_date=2026-10-26`、`execution_day=2026-10-27`、`settlement_day=2026-10-28`，同日快照覆盖 20/20 标的，生成 20 个目标持仓、20 个 TARGET_PREVIEW 纸面订单，并生成 20 个联合纸面订单。

本次同日候选清单为空，因此没有复用旧候选或把旧日期结果冒充当前候选；`candidate_count=0` 被保留在回执中。代码修复把解析后的执行日和结算日写入 D0/D1 回执顶层，聚焦 `next_close_d0_d1` 与 `post_close_d0_d1` 测试为 `27 passed`。完整回执为 [`20261027_post_close_derived_d0_d1_v5/contract_receipt.json`](../../outputs/pipeline_receipts/20261027_post_close_derived_d0_d1_v5/contract_receipt.json)，摘要为 [`20261027_post_close_derived_d0_d1_v5_summary.json`](../../outputs/pipeline_receipts/20261027_post_close_derived_d0_d1_v5_summary.json)。

本次仍为 `derived_stockdb_research_only` 与 `TARGET_PREVIEW`，没有 broker/THS 交互、没有持久账本变更，`execution_admitted=false` 保持不变。


## Candidate adoption rollup bound to latest fixed D0/D1 (2026-10-27)

研究采用总表已更新到 [`20261027_research_adoption_rollup_v11.json`](../../outputs/pipeline_receipts/20261027_research_adoption_rollup_v11.json)，并由 [`20261027_candidate_library_binding_v5.json`](../../outputs/pipeline_receipts/20261027_candidate_library_binding_v5.json) 绑定到候选库。总表同时引用 2026-10-26 fixed D0/D1、六策略共同窗口、当前窗口诊断和候选同日 freshness gate。最新 fixed D0/D1 为 20/20 覆盖、20 个固定订单、20 个联合订单；候选 freshness gate 仍为 `BLOCKED_STALE_CANDIDATE_INPUT`，因此没有旧候选被重新用于当前日。

候选库当前摘要仍是 `continue_research_only=3`、`stop_research_only=2`、`blocked_coverage=1`、`strategy_validated=0`。`paper_allowed=false`、`promotion_allowed=false`、`execution_admitted=false` 持续有效。


## Paper lineage corrected for actual candidate input (2026-10-27)

最新 D0/D1 纸面结果已生成 [`20261027_paper_pipeline_lineage_v4.json`](../../outputs/pipeline_receipts/20261027_paper_pipeline_lineage_v4.json)，并由候选库绑定 v6 接入。此次修复了 lineage 的实际语义：Signal 阶段从 fixed paper package 与 candidate manifest 派生策略列表，2026-10-26 输入实际只包含 `low_volatility_252_20`，`candidate_count=0`；不会再把旧的半动量候选写入当前 lineage。

lineage 继续贯通 StockDB Data、Signal、Target Portfolio、Orders、offline BrokerAdapter reference、MODELED Fill 和 projected Cash/Position；没有 broker/THS 提交、没有持久账户变更，`execution_admitted=false` 保持不变。相关回归测试 `2 passed`。
