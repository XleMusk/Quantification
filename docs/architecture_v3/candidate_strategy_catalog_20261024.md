# 候选策略库索引（2026-10-24）

候选先经过独立诊断，再进入共享 Engine/Ledger；不同输入快照和窗口不做直接收益排序。当前全部保持 `execution_admitted=false`。

| 候选 | 路线 | 状态 | 主要边界 |
|---|---|---|---|
| stockdb_low_volatility_252_20 | 固定低波动 | PAPER_READY_RESEARCH_ONLY | 34 日纸面诊断，非 PIT 验证 |
| stockdb_momentum_12_1_20 | 趋势/动量 | RESEARCH_DIAGNOSTIC_COMPLETE | 与固定控制同快照，未采用 |
| stockdb_low_vol_half_momentum_20 | 趋势/动量 | RESEARCH_DIAGNOSTIC_COMPLETE | 与固定控制同快照，未采用 |
| alpha158_lgbm_candidate_panel_v1 | Alpha158/LightGBM | FULL_NATIVE_AND_UNTOUCHED_FORWARD_COMPLETE_RESEARCH_ONLY | native cap500 回放和 2026-10-14..23 完整未触碰标记已完成；PIT/策略验证未完成 |
| factor_candidate_family_balanced_ridge | 成熟因子 | ACTION_CONTEXT_BOUND_UNTOUCHED_FORWARD_COMPLETE_RESEARCH_ONLY | 行动上下文与 8 日未触碰标记完成；PIT/公司行动未应用 |
| stockdb_value_size_activity_2023 | 成熟因子 | EXISTING_SHARED_LEDGER_DIAGNOSTIC_WITH_PAPER_PROOF | 2023 回溯开发样本，历史共享纸面投影，非当前信号 |
| shortline_native_lr_lgbm | 短线连板 | RESEARCH_ACCOUNT_PARTIAL_WITH_STOP_POINTS | 完整收益未知，停止点保留 |
| public_seat_trend_hypotheses | 公开席位/游资风格 | PARTIAL_NO_TRADE | 9 个假设中 5 个完成、4 个因持仓/执行价参考停止；公告时点未验证 |
| leader_eight_type_replay_engine | 龙头风格 | PARTIAL_CROSS_YEAR_RESOURCE_STOP_WITH_PAPER_PROOF | 跨年资源停止，已有历史共享纸面投影，仍为独立闸门 |

详细指标、证据路径和限制见同名 JSON 文件。

### 2026-10-24 long-window evidence update

`stockdb_low_volatility_252_20` now also has a calendar-bound 678-session replay (`2024-01-02..2026-10-23`) with `-4.09%` net return, `-12.92%` maximum drawdown, 160 orders and 229 fills. This is research-only StockDB evidence and remains separate from PIT/untouched-holdout validation.

The same calendar-bound 678-session replay now covers two trend candidates from the identical StockDB snapshot, costs and shared Engine/Ledger: `stockdb_momentum_12_1_20` returned `-13.51%` with `-18.38%` maximum drawdown, 146 orders and 201 fills; `stockdb_low_vol_half_momentum_20` returned `+10.08%` with `-16.58%` maximum drawdown, 391 orders and 656 fills. The run is recorded in `outputs/stockdb_rolling_low_volatility/20261024_121737_d2456fd6/run.json` and the two-variant replay audit in `outputs/pipeline_receipts/20261024_stockdb_momentum_long_replay_audit.json`. These are comparable research diagnostics only, not PIT, untouched-holdout or strategy-validation evidence.

## 2026-10-24 full native Alpha158/LightGBM increment

The full native feature route is now evidence-bound on the frozen StockDB snapshot through 2026-10-13: 5,322 symbols, 669 sessions, 158 native features, and 3,560,418 calendar-grid rows. The dated score batch covers 184 decision dates and 979,248 score rows. Its cap500 shared Engine/Ledger replay covers all 184 score dates, 165 symbols, 436 orders, 816 fills, net return -7.3249%, maximum drawdown -28.6817%, fees 4438.42, and 83 blocked order events. The earlier cap20 replay ending 2026-06-22 is retained as a range-sensitivity experiment. The corrected full-market selector rerun completed 158/158 definitions with 23 selection dates, 68 later-check sessions and 15 proxy replays; selected orientations are RESI10 low, RSQR10 high, VWAP0 high, RESI5 low and VMA10 high. The prior 20261024_122933 output is retained only as a split-boundary failure experiment.

This is a research-only diagnostic. It has not been promoted to the paper account or broker path, and it is not PIT, untouched holdout, or strategy validation. The score, native feature, factor panel, replay, and audit receipts are recorded in the machine-readable catalog entry `alpha158_lgbm_candidate_panel_v1`.



## Mature-factor current snapshot bridge

The frozen mature-factor Ridge score has been reconstructed on the 2026-10-13 StockDB cutoff for all 5,322 symbols and 190 factors, with 5,322 finite scores. This is a cross-lineage diagnostic only: the Ridge model was trained on the legacy bulk-adjusted prices/action/calendar lineage, so no ranking, target holdings, paper orders or shared-ledger replay was generated. The bridge receipt records the mismatch and promotion blocker.

The mature-factor route also has a same-lineage monthly input panel: 34 month-end sessions, 5,322 symbols, 180,948 rows and 190 factors through 2026-10-13. It remains an input artifact until PIT/as-of publication, dated eligibility and action views are bound; no model fit or paper replay is claimed.

## Mature-factor same-lineage Ridge diagnostic

The monthly StockDB panel is now fitted with the fixed 190-factor Ridge rule using 20-session forward open labels from the same provider: 11 training months and 55,909 training rows produce 34 score dates, 180,948 finite scores and 3,319,047 finite forward-label cells. The run is `outputs/stockdb_factor_monthly_ridge_diagnostics/20261024_monthly_same_lineage_v1/run.json`, with receipt `outputs/pipeline_receipts/20261024_mature_factor_stockdb_monthly_ridge_receipt.json`. It remains a research diagnostic: no ranking, target holdings, paper orders, Ledger or broker/THS artifact was generated. PIT/as-of, dated eligibility, actions and liquidity qualification remain required before replay.

### 2026-10-24 Mature factor shared replay

The same-lineage monthly Ridge score stream now runs through the shared Engine/Ledger on the pinned StockDB snapshot: 648 sessions, 152 symbols, 157 selected events, 281 orders and 562 fills. The research-only result is net return 36.9764%, maximum drawdown -18.1384%, Sharpe 0.7614, fees 4,584.21 and turnover 8,227,019.86. Thirteen events were dropped for incomplete raw paths and seven order events were blocked. This remains a bounded candidate panel: `pit_verified=false`, `company_actions_applied=false`, `eligibility_applied=false`, `strategy_validated=false`, and `execution_admitted=false`.

### 2026-10-25 prespecified 60-session Ridge variant

R04 now has an independent fixed-horizon result. Using the same feature/provider inputs and frozen StockDB raw snapshot, the 60-session forward-open-label Ridge fit produced 55,797 training rows and 180,948 scores over 34 dates. Its shared Engine/Ledger replay covered 648 sessions, 152 symbols and 158 selected events, with 279 orders, 556 fills, net return 32.1767%, maximum drawdown -19.1039%, Sharpe 0.7395, fees 4,129.55, turnover 7,416,800.94 and 10 blocked order events. The horizon was fixed before the run; no horizon search, weekend capture, PIT claim or promotion occurred. Receipts: `outputs/pipeline_receipts/20261025_mature_factor_stockdb_monthly_ridge_h60_receipt.json` and `outputs/pipeline_receipts/20261025_mature_factor_stockdb_monthly_ridge_h60_replay_receipt.json`. Safety flags remain `execution_admitted=false`, `strategy_validated=false`, and `ledger_mutated=false`.

### 2026-10-24 dated eligibility replay

A reusable dated eligibility view now covers 180,948 monthly Ridge score rows from the pinned StockDB raw snapshot. It records raw-bar observation, positive OHLCV, `is_st=false`, and amount >= 10,000,000 CNY; 168,532 rows pass. The filtered shared Engine/Ledger replay covers 648 sessions, 153 symbols, 158 selected events, 285 orders and 570 fills, with 38.6272% net return, -18.8216% maximum drawdown and 0.7886 Sharpe. This is still research-only: PIT publication/revision and company actions are not established, `strategy_validated=false`, and `execution_admitted=false`.

### 2026-10-24 action context and forward holdout

The mature-factor monthly score panel now has a hash-checked canonical action context: 180,948 score rows, 961 same-date action rows, 101 complete action-term rows and 180,088 context-ready rows. `pit_asof_safe_rows=0` and `company_action_applied_rows=0`; the context is not treated as PIT or an adjustment feed. The terminal mature-factor holdings were then marked through 8 untouched sessions from 2026-10-14 to 2026-10-23 with no new scores, orders or fills: net return -0.7811%, maximum drawdown -4.7289%, Sharpe -0.3050. The earlier Alpha158 forward attempt is retained as a coverage failure. A fresh native StockDB capture then covered 6 symbols and 8 sessions (48/48 rows); the terminal 301686.SZSE holding completed the untouched mark-only diagnostic with -0.5164% net return, -1.2478% maximum drawdown and no future scores, orders or fills. Receipt: outputs/pipeline_receipts/20261024_alpha158_forward_capture_complete.json. This remains research-only and does not establish PIT or strategy validation.

The v5 action sidecar increment maps annual bulk-action input receipts into explicit upstream lineage for all 138 events and 177 source artifacts. It records upstream receipt, retrieval and hash coverage while leaving issuer as-of/revision requirements unmet; issuer `retrieved_at/asof_at/revision_id` remains `31/0/32` and PIT-safe rows remain `0`. Evidence: `outputs/pipeline_receipts/20261024_action_publication_sidecar_v5.json`, `outputs/research_reports/20261024_action_publication_sidecar_v5.md`.


### 2026-10-24 common untouched forward comparison

四个终端持仓在同一份 46 标的、8 个交易日的 native StockDB 快照上完成未触碰标记（回放截止 2026-10-13，前向窗口 2026-10-14..23）。固定低波动净收益 -1.4813%、最大回撤 -2.1753%；低波动/半动量净收益 -4.2710%、最大回撤 -4.6049%；成熟因子净收益 -0.7811%、最大回撤 -4.7289%；Alpha158/LightGBM 净收益 -0.5164%、最大回撤 -1.2478%。四组均未生成未来分数、目标、订单或成交，`execution_admitted=false`。这只是同口径终端持仓诊断，不是 PIT、策略验证或纸面晋级。

机器可读回执：`outputs/pipeline_receipts/20261024_common_untouched_forward_comparison_v2.json`；首轮遗漏 000582.SZSE 的覆盖失败保留于 `outputs/pipeline_receipts/20261024_common_forward_coverage_failure_v1.json`。


### 2026-10-24 publication lower-bound inventory

对 canonical action parquet 的 16,818 个事件做字段盘点：`dividPlanAnnounceDate` 有 16,815 行且均不晚于 operate date，只能作为事件下界；`source_date`、`source_url`、`source_file` 只有 2 行，`retrieved_at`、`asof_date`、`revision_id`、`publication_time` 均不在 schema。未补任何日期，PIT/as-of/revision-safe/company-action 应用继续为 false。详见 `outputs/pipeline_receipts/20261024_action_publication_field_inventory_v1.json`。


### 2026-10-24 candidate-level action coverage

四组同截止终端组合的行动覆盖已单独盘点：固定低波动 109 条事件/2 条完整来源凭据，低波动/半动量 95 条/0 条，成熟因子 22 条/0 条，Alpha158 0 条/0 条。计划公告日仍只是事件下界，四组 PIT-safe 行均为 0。详见 `outputs/pipeline_receipts/20261024_candidate_action_publication_coverage_v1.json`。


### 2026-10-24 candidate shadow paper packages

Alpha158 与成熟因子的终端目标已进入历史影子纸面包，目标、委托、成交、现金和持仓均由现有本地 Ledger 投影；该证据与每日纸面账户分离，未生成可提交的 THS 订单。

### 2026-10-24 public-seat replay audit

已有公开席位/趋势批量回放现已接入可重复的只读审计：9 个假设中 5 个完成 241 会话诊断，合计 1,205 个完成会话、3,836 个研究委托和 5,926 个成交回调；4 个假设在 145 或 167 个会话处因未解析的持仓/执行价参考停止，完整窗口收益保持未知。审计明确公告时点未知、无 PIT 席位成员资格、无因果席位 Alpha、无未触碰留出，且 `execution_admitted=false`。

机器回执：`outputs/pipeline_receipts/20261024_public_seat_replay_audit_v1.json`；报告：`outputs/research_reports/20261024_public_seat_replay_audit_v1.md`；命令：`run_committee.py public-seat-replay-audit`。

## 2026-10-24 trend/half-momentum shadow package

The terminal `stockdb_low_vol_half_momentum_20` account is now accepted by an explicit legacy terminal-account adapter and routed through the same local target/order/fill/cash/position projection: 20 targets, 20 paper orders, 20 available routes, modeled fees RMB 342.98 and projected equity RMB 1,399,657.02 under a separate RMB 1,400,000 research cash assumption. The earlier RMB 140,000 zero-lot failure is retained. This is historical shadow evidence only; it is not daily paper, THS evidence, PIT validation or strategy adoption.

Evidence: `outputs/pipeline_receipts/20261024_candidate_shadow_paper_packages_v3.json`, `outputs/pipeline_receipts/20261024_trend_low_vol_half_momentum_shadow_failure_v1.json`.

## 2026-10-24 shortline saved-replay audit

The saved LR and LightGBM selection accounts are now machine-audited as shared Engine/Ledger research runs: LR committed 103/187 planned sessions with 223 research orders and 446 fill callbacks; LightGBM committed 95/187 with 190 orders and 380 callbacks. Both complete-window returns remain `null` because the runs stop on unresolved corporate-action/reference inputs. The audit preserves those stop points and keeps `selection_enabled=false`, `strategy_validated=false`, and `execution_admitted=false`.

Evidence: `outputs/pipeline_receipts/20261024_shortline_saved_replay_audit_v1.json`, `outputs/research_reports/20261024_shortline_saved_replay_audit_v1.md`.

The frozen score input is now covered by a reusable score-gate audit. It contains 25,150 rows across the LR and LightGBM model outputs, with zero `paper_candidate_only=true` rows; cross-model reuse of an event identity is allowed only when `model_kind` and `generation_id` distinguish the row. The gate remains blocked on `paper_candidate_only.none_true` and does not rewrite flags or emit a panel, target, order or fill.

Evidence: `outputs/pipeline_receipts/20261024_shortline_score_gate_audit_v1.json`, `outputs/research_reports/20261024_shortline_score_gate_audit_v1.md`.



### 2026-10-24 long-window unified comparison

在同一冻结 StockDB 快照和共享 Engine/Ledger 上，新增统一长窗口比较回执 `outputs/pipeline_receipts/20261024_long_window_strategy_comparison_v1.json` 与报告 `outputs/research_reports/20261024_long_window_strategy_comparison_v1.md`。固定低波动、低波/半动量、12-1 动量、成熟因子 Ridge、Alpha158/LightGBM 的保存结果分别覆盖 678、678、678、648、184 个会话；统一保留订单、成交、净收益、最大回撤、Sharpe、费用和 blocked 指标。比较未强行对齐不同原生窗口，也未进行策略筛选或推广，`execution_admitted=false`。

固定低波动另完成一次 2024-01-02..2026-10-23 的 678 会话重放，回执 `outputs/pipeline_receipts/20261024_stockdb_low_volatility_252_20_long_replay_audit.json`，净收益 -4.0859%、最大回撤 -12.9214%、160 个订单、229 个成交；这是主线账户流水线的研究证据，不是 PIT、未触碰留出或实盘资格。


### 2026-10-24 leader cross-year bounded replay

已把 2025-01-02..2026-09-30 的两段既有输入接入新的跨年连续 Engine/Ledger 适配。首个固定 case `space_height_leader__unguarded__causal_first_seen_cap20` 完成 424 个 source sessions，净收益 -17.4237%、最大回撤 -19.1642%、费用 306.67、期末权益 82,576.33；该 case 生成 38 个订单、73 个成交回调和 424 行 equity，并保留 94 个目标/执行诊断事件；最终目标含 `603928.SSE: 0.16`。全量 32 case 在约 12GB 工作集处主动停止，另一个 bounded liquidity case 也在 commit 前停止，均保留失败/中断回执，未伪造结果。

证据：`trading/research/leader_cross_year_continuous.py`、`D:/QuantificationData/trading/research/leader_eight_type_case_replay/20261024_2025_2026_continuous_v1/partial_result.json`、`D:/QuantificationData/trading/research/leader_eight_type_case_replay/20261024_2025_2026_continuous_v1/abort_receipt.json`。该路线保持 `execution_admitted=false`，不作为策略推广或完整收益结论。

## 2026-10-24 candidate research selection

机器回执：`outputs/pipeline_receipts/20261024_candidate_research_selection_v1.json`；报告：`outputs/research_reports/20261024_candidate_research_selection_v1.md`。

固定队列：`stockdb_low_vol_half_momentum_20` 与 `factor_candidate_family_balanced_ridge` 仅允许继续研究；`stockdb_low_volatility_252_20` 与 `alpha158_lgbm_candidate_panel_v1` 保留停止证据；`stockdb_momentum_12_1_20` 因共同前推覆盖缺失阻塞。短线、公开席位和 leader 候选仍保留独立时点/归因闸门。所有候选保持 `paper_allowed=false`、`promotion_allowed=false`。

## 2026-10-25 public-seat shared paper projection

已把完整的 `lag2_TREND_ANY_LHB` 公开席位案例（241 个会话、757 个历史订单、1,140 个历史成交）接入共享本地纸面包，生成 5 个目标、5 个纸面订单、4 个可用路由，模拟费用 RMB 246.62，终态研究权益 RMB 999,753.38。该包只证明候选能进入 `Data→Signal→Target Portfolio→Orders→Fill→Cash/Position` 的本地投影，`public_seat_trend_hypotheses` 仍为 `UNASSESSED_SEPARATE_GATE`，没有进入 D1 follow-up 队列。

证据：`outputs/pipeline_receipts/20261025_public_seat_candidate_shared_paper_receipt_v1.json`、`outputs/paper_plans/20261025_public_seat_lag2_shadow_v2/shadow_receipt.json`。回执新增明确的 `validation` 实际边界值和 `boundary_assertions`，均证明 `account_observed/client_interacted/submitted/cancelled/ledger_persisted=false`；没有券商或同花顺操作。

## 2026-10-25 additional shared paper evidence

The value_size_activity_2023 account at terminal day 2023-12-29 now reaches the shared paper path with 20 targets, 20 paper orders, 19 available routes and 20 THS read-only reference rows. Receipt: D:/QuantificationData/trading/research/candidate_shadow/value_size_activity_2023_20261025_v1/paper_batch/joint_paper_receipt.json. It remains UNASSESSED_SEPARATE_GATE and retrospective-only.

The saved leader_turnover_eight_type target at terminal day 2026-10-15 now reaches the same path with 5 targets, 5 paper orders, 5 available routes and 5 THS read-only reference rows. Receipt: D:/Quantification/outputs/pipeline_receipts/20261026_leader_eight_type_turnover_joint_paper_v1/joint_paper_receipt.json. Its separate gate and PIT/resource boundaries remain unchanged.
