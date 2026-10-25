# 目标完成审计（2026-10-24）

总体状态：`INCOMPLETE_ACTIVE`。当前证据证明了研究和纸面路径的部分落地，未证明真实执行前置条件或完整目标完成。

纸面账户恢复：`VERIFIED_LOCAL_RESEARCH`。当前 `fixed_daily` 检查点已复制到版本化恢复包并恢复到空目录，现金和持仓一致；外部 raw 输入只做哈希固定，未接触券商或个人账户。

|项目|状态|主要缺口|
|---|---|---|
|StockDB/可靠近年数据与覆盖回执|PARTIAL|124 recent rows missing; PIT membership/corporate actions not verified|
|Data→Signal→Target→Orders→Fill→Cash/Position shared path|VERIFIED_LOCAL_RESEARCH|BrokerAdapter/THS live channel remains closed|
|Reliable historical backtest|PARTIAL_RESEARCH_ONLY|diagnostic sample, not PIT or untouched validation|
|Daily paper run|CURRENT_TO_2026_10_23|2026-10-26 session not yet available|
|Target holdings and reconciliation|REFERENCE_READY_QUOTE_PENDING|same-session price/estimated target quantity unavailable|
|THS read-only prefill/readback evidence|BLOCKED_EXTERNAL_EVIDENCE|no external account/quote/readback capture|
|Trend/momentum candidate library|DIAGNOSTIC_COMPLETE|not adopted or PIT validated|
|Alpha158+LightGBM candidate|FULL_NATIVE_AND_UNTOUCHED_FORWARD_COMPLETE_RESEARCH_ONLY|完整 8 日未触碰标记已完成；PIT、成员资格和策略验证未完成|
|Mature factors|ACTION_CONTEXT_BOUND_UNTOUCHED_FORWARD_COMPLETE_RESEARCH_ONLY|行动上下文与 8 日未触碰标记完成；PIT/公司行动未应用|
|Shortline board-chain|PARTIAL_STOP_POINTS|full strategy return unknown|
|Public seat/游资 style|PARTIAL_NO_TRADE|9 个假设中 5 个完整、4 个因持仓/执行价参考停止；公告时点、PIT 成员资格和因果归因仍未验证|
|F01 financial factor|BLOCKED_DATA|single-quarter/PIT evidence missing|
|Quantifiable replaceable candidate library|INDEXED_WITH_BOUNDARIES|several candidates remain partial|
|No real execution until prerequisites|VERIFIED_CLOSED|none for current policy|

### 目标持仓纸面收盘价 reconciliation（2026-10-24）

`trading/research/paper_target_reconciliation.py` 已通过 `paper-target-reconciliation` 命令运行在当前 `fixed_daily` 纸面种子上。回执 `outputs/paper_plans/20261024_lowvol_current_paper_reconciliation/receipt.json` 绑定 paper plan、daily head、source run、account 和 `raw_daily.parquet` 的 SHA-256；20/20 目标行可用，20/20 账户价格标记与 raw close 相符，输出目标股数、当前股数、可卖数量、差额及估算费用。该回执保持 `execution_admitted=false`、`orders_created=false`、`ledger_mutated=false`，并明确 close reference 不等于 THS 当前报价或账户回读。原 `ths_readonly_request/target_reconciliation.csv` 保持空白，等待同一 reference generation 的授权只读观察。

### 候选策略库证据审计（2026-10-24）

新增 `candidate-catalog-audit` 只读命令，对 9 个候选策略的证据路径、有限指标、可比组、限制字段进行机器校验。回执 `outputs/pipeline_receipts/20261024_candidate_catalog_audit.json` 为 `VERIFIED_CANDIDATE_CATALOG_READONLY`，缺失证据为 0，执行闸门保持关闭。该结果只证明候选库可复现索引，不证明各策略已通过 PIT、未触碰样本或策略采用门槛。

本轮短线候选的共享流水线停止点已固化为 `outputs/pipeline_receipts/20261024_shortline_saved_replay_audit_v1.json`：LR/LightGBM 分别提交 223/190 个研究委托、生成 446/380 个成交回调，但只完成 103/95 个计划会话，完整窗口收益保持 `null`。另外保留了一次因布尔指标类型不符合 catalog audit 约定而失败的回执 `outputs/pipeline_receipts/20261024_candidate_catalog_audit_metric_schema_failure_v1.json`；修正字段位置后最终只读审计通过，没有改变策略结果。

### Public-seat replay audit slice (2026-10-24)

已有公开席位/趋势批量回放已通过 `public-seat-replay-audit` 接入可重复的只读审计：9 个假设中 5 个完成 241 会话诊断，合计 1,205 个完成会话、3,836 个研究委托和 5,926 个成交回调；4 个假设在 145 或 167 个会话处因未解析的持仓/执行价参考停止，完整窗口收益保持未知。审计回执 [`20261024_public_seat_replay_audit_v1.json`](../../outputs/pipeline_receipts/20261024_public_seat_replay_audit_v1.json) 和报告 [`20261024_public_seat_replay_audit_v1.md`](../../outputs/research_reports/20261024_public_seat_replay_audit_v1.md) 明确 `publication_timing_verified=false`、`pit_membership_verified=false`、`causal_seat_alpha_verified=false`、`execution_admitted=false`。

### 固定策略七阶段纸面链路回执（2026-10-24）

新增固定低波动纸面账户的七阶段 provenance 回执，绑定 raw StockDB、source run、account、replay、daily 文件哈希；Data、Signal、Target Portfolio、Orders、BrokerAdapter、Fill、Cash/Position 全部通过，截止日为 2026-10-23。此回执仍属于研究-only 纸面证据，不等于 PIT 或真实渠道验证。

### 日历绑定趋势候选长窗回放（2026-10-24）

在同一 StockDB 快照、补充 raw 状态、交易日历、成本和共享 Engine/Ledger 上，`momentum_12_1_20` 与 `low_vol_half_momentum_20` 均完成 678 个会话的历史回放。前者净收益 `-13.51%`、最大回撤 `-18.38%`、146 个订单和 201 个成交；后者净收益 `+10.08%`、最大回撤 `-16.58%`、391 个订单和 656 个成交。运行回执为 `outputs/stockdb_rolling_low_volatility/20261024_121737_d2456fd6/run.json`，回放审计为 `outputs/pipeline_receipts/20261024_stockdb_momentum_long_replay_audit.json`，状态 `VERIFIED_STOCKDB_HISTORICAL_REPLAY`、问题为空。结果已绑定候选库，但仍属于研究诊断，未宣称 PIT、未触碰留出或策略验证。

### F01 scoped financial field evidence (2026-10-24)

受限的三发行人 `2024Q1` 原始报告核对已形成 `outputs/financials/f01_scoped_field_coverage_20261024.json`。三家公司均有 PDF/TXT、报告期和发布日期、人民币元单位及合并口径，`parent_net_profit`、`total_assets` 和联合覆盖均为 `3/3`，字段行号、摘录及 PDF/TXT SHA-256 已保存。回执状态为 `PARTIAL_FIELD_EVIDENCE_PIT_PENDING`：这不是全市场季度覆盖，修订安全 PIT、公告事件入库/延迟合同和 provider 字段映射仍未完成，因此没有把它升级为 F01 readiness 或因子回放资格。

F01 scoped receipt 的只读复核入口已固化为 `trading/research/f01_scoped_field_coverage.py`，并通过 `run_committee.py f01-scoped-field-coverage --json` 暴露。它验证现有 PDF/TXT 哈希、行号摘录、3/3 字段覆盖及 `pit_verified=false`、`factor_ready=false` 边界；测试 `2 passed`，架构审计 `544/0`。该入口只验证局部证据，不把样本升级为 F01 因子资格。

F01 来源 sidecar 已固化为 `trading/research/f01_publication_sidecar.py`，并通过 `run_committee.py f01-publication-sidecar` 运行。回执 `outputs/pipeline_receipts/20261024_f01_publication_sidecar_v1.json` 映射了三条 PDF/TXT 来源，6 个文件哈希全部核验通过，披露日为 2024-04-26 至 2024-04-30。provider CSV 的三条检索时间被单独保留，未作为发行人 PDF 的 retrieval/as-of；发行人检索时间、as-of 和 revision identity 均为 `0/3`，`pit_safe_rows=0`。provider `netProfit` 与 PDF 的 parent-attributable profit 三条均出现口径或数值差异，未静默覆盖。该工作包只形成可复用的真实来源阻断证据，F01 readiness、任务创建和因子回放仍保持阻断。

coverage funnel 已接入 publication sidecar：`run_committee.py f01-coverage-funnel --publication-sidecar ...` 会读取每条 sidecar 的合同评估，并把市场连接与 retrieval/as-of/revision 分开计数。本次真实三行市场连接为 `3/3`，issuer retrieval/as-of/revision 为 `0/3`，`f01_eligible_rows=0`，仍为 `BLOCKED_DATA`；相关测试 `6 passed`。这一步消除了 sidecar 只是旁证、闸门不消费的断点，但没有放宽 F01 资格。

Action sidecar v4 进一步保留聚合前每个来源 artifact 的时点合同，并接入明确的 `source_retrieved_at` 字段。回执 `outputs/pipeline_receipts/20261024_action_publication_sidecar_v4.json` 映射 138 个事件、177 个 artifact、138 个核验文件；事件级检索字段为 `31` 行、artifact 级为 `62` 行，as-of 仍为 `0`、PIT-safe 仍为 `0`。没有用缓存读取时间补齐，也没有应用公司行动或运行调整后回放。

该 v4 sidecar 已接入成熟因子 180,948 行 action context；同日行动行 `961`，PIT-safe 行 `0`，公司行动应用行 `0`。集成回执为 `outputs/pipeline_receipts/20261024_stockdb_dated_action_context_sidecar_v4_receipt.json`，因此只证明证据链已接线，不能支持调整后收益或策略晋级。

## 2026-10-24 full native Alpha158/LightGBM evidence

此前启动的全市场 Alpha158 批次已到终态：5,322 个标的、669 个交易日、158/158 个因子，3,435,917 个源行中 15 行按质量条件排除；15 个 candidate proxy replay 均保留为研究代理。完成回执为 `outputs/pipeline_receipts/20261024_full_market_alpha158_completion_receipt.json`，明确 `account_backtest=false`、`fresh_holdout_claimed=false`、`trading_authorized=false`，因此后续不再重复启动该终态批次。

A canonical StockDB Qlib provider was completed on the frozen source snapshot through 2026-10-13 (5,322 symbols, 669 sessions). Native Alpha158 features completed at 158/158 with 3,560,418 calendar-grid rows. A one-date 190-factor panel was generated for all 5,322 symbols. The dated score batch covers 184 dates and 979,248 rows.

The cap500 candidate replay used the shared Engine/Ledger path across 184 sessions and 165 selected symbols: 436 orders, 816 fills, net return -7.3249%, maximum drawdown -28.6817%, fees 4438.42, Sharpe -0.3460, and 83 blocked order events. The audit is `passed_research_only_gates`. Target and paper-order diagnostics exist, but `execution_admitted=false`, `strategy_validated=false`, `ledger_mutated=false`, and no THS payload was generated. The earlier cap20 replay ending 2026-06-22 is preserved as a range-sensitivity experiment.

The evidence remains research-only: positive-volume proxy semantics, PIT membership, corporate actions, untouched holdout, and independent strategy validation are unresolved.

## Selector boundary repair

The first full-market Alpha158 batch had a concrete selection-boundary defect: a five-session label from 2026-06-29 crossed the 2026-07-01 split. The selector now requires every selection label to mature before the split. The corrected receipt is `outputs/full_market_alpha158/20261024_133722_8be29ed5_sh_sz_all_equities/protocol.json` with 158/158 definitions, 23 selection dates, 68 later-check sessions and 15 proxy replays. The old run is preserved by `outputs/pipeline_receipts/20261024_full_market_alpha158_boundary_failure.json` as `RETAINED_BOUNDARY_FAILURE` and is excluded from catalog selection.

Regression coverage for the corrected route, provider, native/batch/panel adapters, factor pack, catalog audit and architecture check passed (30 focused tests; architecture 544 modules and 0 violations).

## Mature-factor same-snapshot bridge

The completed StockDB provider/native/190-factor snapshot reconstructs the frozen mature-factor Ridge score for 2026-10-13: 5,322 finite scores from 190 factors. The bridge receipt is `outputs/pipeline_receipts/20261024_mature_factor_stockdb_snapshot_bridge_receipt.json` with status `COMPLETED_CROSS_LINEAGE_DIAGNOSTIC`. No ranking, target holdings, paper orders or ledger replay were generated. The saved Ridge model still trains on the legacy `bulk_adjusted_prices`/action/calendar lineage; a historical same-lineage StockDB training panel with dated eligibility and PIT evidence remains the next acceptance.

## Mature-factor monthly StockDB panel

A historical monthly input panel is now materialized from the same provider/native lineage: 34 month-end sessions from 2024-01-31 through 2026-10-13, 5,322 symbols, 180,948 rows, 190 factors, and 32,438,260 finite cells. Receipt: `outputs/pipeline_receipts/20261024_mature_factor_stockdb_monthly_panel_receipt.json`. It is an input artifact only; no Ridge fit, ranking, target holdings, paper orders or shared-ledger replay was generated. PIT/as-of publication events, dated eligibility and action views remain the acceptance gate.

## Mature-factor same-lineage Ridge diagnostic

The monthly StockDB panel is now fitted with the fixed 190-factor Ridge rule using 20-session forward open labels from the same provider: 11 training months and 55,909 training rows produce 34 score dates, 180,948 finite scores and 3,319,047 finite forward-label cells. The run is `outputs/stockdb_factor_monthly_ridge_diagnostics/20261024_monthly_same_lineage_v1/run.json`, with receipt `outputs/pipeline_receipts/20261024_mature_factor_stockdb_monthly_ridge_receipt.json`. It remains a research diagnostic: no ranking, target holdings, paper orders, Ledger or broker/THS artifact was generated. PIT/as-of, dated eligibility, actions and liquidity qualification remain required before replay.

### 2026-10-24 Mature factor replay acceptance

The monthly same-lineage Ridge diagnostic has been replayed through the shared Engine/Ledger. The receipt records 648 sessions, 281 orders, 562 fills, 36.9764% net return, -18.1384% maximum drawdown and 0.7614 zero-risk-free Sharpe. This satisfies the research replay acceptance only. PIT/as-of publication, dated eligibility, ST/liquidity, corporate actions and untouched holdout remain open, so no strategy promotion or paper-account admission is claimed.

### 2026-10-24 dated eligibility acceptance

The mature-factor route now has a reusable dated raw-bar eligibility view and a filtered replay through the shared Engine/Ledger. The view covers 180,948 score rows and retains 168,532 eligible rows under the fixed amount floor. The replay records 648 sessions, 285 orders, 570 fills, 38.6272% net return, -18.8216% maximum drawdown and 0.7886 Sharpe. This closes the dated raw-bar eligibility diagnostic only; PIT publication/revision, corporate actions and untouched holdout remain open.

### 2026-10-24 action context and untouched forward evidence

Canonical corporate-action identity is now joined to the monthly StockDB score rows with operation dates, issuer plan-announcement dates and source hashes. The resulting context keeps all PIT/as-of-safe and applied-action counts at zero. A separate shared Engine/Ledger holdout utility marked the terminal mature-factor holdings over eight later sessions without consuming future scores or generating orders; it returned -0.7811% with -4.7289% drawdown. The earlier Alpha158 holdout failure is retained. A fresh native StockDB capture now covers the terminal 301686.SZSE holding across all eight sessions; the untouched diagnostic returns -0.5164% with -1.2478% drawdown and no future scores, orders or fills. This remains research-only and does not establish PIT or strategy validation.


### 2026-10-24 same-cutoff four-strategy comparison

固定低波动、低波动/半动量、成熟因子和 Alpha158/LightGBM 已在同一份 46 标的、8 个交易日 native StockDB 快照上完成终端持仓未触碰标记。回放截止 2026-10-13，前向窗口为 2026-10-14..23；四组均未生成未来分数、目标、订单或成交，执行闸门保持关闭。结果回执为 `outputs/pipeline_receipts/20261024_common_untouched_forward_comparison_v2.json`。首轮遗漏 000582.SZSE 的失败回执仍保留，PIT、修订安全、公司行动和策略验证仍是未完成项。


### 2026-10-24 paper target native StockDB cross-check

当前纸面目标的 20 个标的已在 native StockDB 中捕获 8 个交易日（160/160 行）。2026-10-23 的 20 个收盘价与现有 raw close reference 逐行一致，最大绝对差为 0；证据为 `outputs/pipeline_receipts/20261024_paper_target_native_stockdb_reference.json` 和 `outputs/paper_plans/20261024_lowvol_current_native_stockdb_reference/comparison.csv`。这仍是本地研究交叉核对，THS 当前报价/账户/回读证据仍缺失。


### 2026-10-24 publication lower-bound inventory

对 canonical action parquet 的 16,818 个事件做字段盘点：`dividPlanAnnounceDate` 有 16,815 行且均不晚于 operate date，只能作为事件下界；`source_date`、`source_url`、`source_file` 只有 2 行，`retrieved_at`、`asof_date`、`revision_id`、`publication_time` 均不在 schema。未补任何日期，PIT/as-of/revision-safe/company-action 应用继续为 false。详见 `outputs/pipeline_receipts/20261024_action_publication_field_inventory_v1.json`。


### 2026-10-24 candidate-level action coverage

同一四策略终端组合已绑定候选级行动覆盖：成熟因子持仓 22 条行动事件但 0 条完整来源凭据，Alpha158 持仓没有行动事件；固定控制也未获得完整 PIT 凭据。该结果只收窄数据缺口，不代表行动调整或策略验证。


### 2026-10-24 reproducible native paper reference

纸面目标的 native-vs-raw close 核对已封装为 `run_committee.py paper-target-native-reference`，使用同一 20 个目标标的和 2026-10-23 截止日重跑得到 20/20 匹配、0 差异。回执为 `outputs/paper_plans/20261024_lowvol_current_native_stockdb_reference_v2/receipt.json`；仍不代表 THS 当前账户、报价或可预填状态。


### 2026-10-24 candidate shadow paper packages

Alpha158 与成熟因子已通过共享本地目标/委托/成交/现金/持仓路径生成历史影子包。它们只证明候选可以进入纸面流水线，不代表当前纸面账户、THS 预填或策略验证。


### 2026-10-24 reusable candidate shadow route

已将候选终端回放到历史影子纸面包的过程封装为 `run_committee.py candidate-shadow-paper`，可从任意合规研究回放生成目标、参考价格和本地目标/委托/成交/现金/持仓投影。回执 `outputs/pipeline_receipts/20261024_candidate_shadow_paper_packages_v2.json`；不进入日常纸面账户，也不生成可提交的 THS 订单。

### 2026-10-24 trend/half-momentum shadow package

The existing terminal account for `stockdb_low_vol_half_momentum_20` now enters the shared historical shadow route through an explicit legacy adapter. With the retained 100-share-lot failure at RMB 140,000, the successful research-only package uses RMB 1,400,000, produces 20 targets and 20 paper orders, and has 20 available routes. All account/client/submission/cancel/persistent-ledger flags remain closed. This fills the candidate-to-shared-paper evidence gap only; PIT/action timing, daily paper promotion, THS read-only evidence and strategy validation remain open.

Receipt: `outputs/pipeline_receipts/20261024_candidate_shadow_paper_packages_v3.json`.

### 2026-10-24 publication-event contract

新增可复用的 `publication_event_contract.py`，并接入 `stockdb_action_context_view.py` 的可选 sidecar。只有逐事件 publication date/time、retrieved_at、asof_at、revision_id、source_file 和 SHA-256 均满足时，匹配的 action row 才能被标记为 `pit_asof_safe`；缺少 sidecar、同日仅日期或不完整修订信息仍保持 false。当前回执只是合成契约测试，不代表 canonical action parquet 已获得 PIT 数据，也没有应用公司行动。

本地契约回执：`outputs/pipeline_receipts/20261024_publication_event_contract_local_receipt.json`。

### 2026-10-24 real action sidecar attempt

从现有 canonical action cache 找到 `000538.SZSE` 的 2025-09-24 和 2026-09-24 两条真实来源文件，两个 SHA-256 均核验通过。来源日期被保留为日期下界；原始检索时间、as-of 和完整修订身份仍缺失，因此 action context 的 2 行中 `pit_asof_safe=0`、`company_action_applied=0`。缓存读取时间单独保留，没有冒充 `retrieved_at`。

回执：`outputs/pipeline_receipts/20261024_action_publication_sidecar_attempt_v1.json`。

### 2026-10-24 reusable action publication sidecar

已将真实来源映射封装为 `run_committee.py action-publication-sidecar`。本次扫描保存的行动源得到 138 个事件级 sidecar、177 个来源文件记录，138 个文件哈希核验通过；其中只有 20 行保留了 retrieved_at，asof_at 仍为 0，PIT-safe 仍为 0。重复来源按事件聚合，但每个来源文件和哈希都保留。此前重复事件导致 action context 拒绝的失败回执仍保留。

回执：`outputs/pipeline_receipts/20261024_action_publication_sidecar_v2.json`。

### 2026-10-24 action upstream lineage increment

v5 读取 action root 的 `input_input_receipts.json`，按标的和年份把年度 bulk-action 输入的路径、SHA-256、来源和检索时间映射到事件及每个来源 artifact 的 `upstream_input_*` 字段。138 个事件和 177 个 artifact 均有上游输入回执、检索时间和哈希；issuer `retrieved_at/asof_at/revision_id` 仍为 `31/0/32`，`pit_safe_rows=0`。该增量只补 lineage，不把上游缓存检索时间写成 issuer retrieval，也不应用公司行动。

回执：[`20261024_action_publication_sidecar_v5.json`](../../outputs/pipeline_receipts/20261024_action_publication_sidecar_v5.json)；报告：[`20261024_action_publication_sidecar_v5.md`](../../outputs/research_reports/20261024_action_publication_sidecar_v5.md)。

