# 方案落地路线（2026-10-24）

这份路线把三份方案中的设计项绑定到当前可执行产物。源 DOCX 只作为设计和追溯依据；历史分支、PR、测试数量和同花顺观察必须重新用当前本地证据验证。

## 交付顺序

| 顺序 | 工作包 | 直接产物 | 验收标准 | 当前状态 |
|---|---|---|---|---|
| D0 | 固定数据与日切 | StockDB 快照、覆盖回执、缺失清单 | 请求、可用、缺失、来源哈希可复核；缺失不填零 | 已完成到 2026-10-23 |
| D1 | 每日纸面推进 | `fixed_daily` 账户、目标、订单、成交、现金/持仓 | 使用同一 Engine/Ledger；日切幂等；执行闸门关闭 | 当前种子已到 2026-10-23 |
| S1 | 固定策略比较 | 低波动、趋势/动量、混合候选同样本回放 | 同快照、同成本、同日历，输出净收益、回撤、成交和费用 | 29 日比较已完成 |
| S2 | 新候选增量 | Alpha158/LightGBM、成熟因子、连板/席位候选 | 先独立信号，再接共享账户回放；不能把诊断结果写成采用结论 | Alpha158/LightGBM 已完成 native cap500 回放和完整 8 日未触碰标记；成熟因子已完成行动上下文与未触碰标记；PIT/修订安全仍开放 |
| F1 | 财报因子 | F01 readiness、事实快照、账户回放 | 单季度、PIT、时点和 FetchReceipt 全部满足才创建任务；否则保留 BLOCKED_DATA | 数据未就绪，保持阻断 |
| T1 | THS 只读证据 | 目标持仓、只读预填参考、回读证据 | 仅在已有授权捕获时读取；不登录、不提交、不撤单 | 等待外部证据 |
| W1 | 周期复盘 | 每周进度与失败记录 | 只改变路线或优先级，不重复已终止工作包 | 持续 |

## 防止重复失误的规则

1. 每个工作包必须先有一个可检查的产物和验收命令；没有直接产物的防御性扩展暂停。
2. 研究回放、纸面账户、THS 证据和真实执行资格分开记录；任何一项不能替代另一项。
3. 数据覆盖分两种：持仓延续只检查持仓标的和账户状态，策略重算必须检查候选 universe 的覆盖；不能用前者证明后者。
4. 同一样本比较固定数据快照、交易日历、成本和账户初始状态；只改变信号时才可比较策略。
5. 失败分类固定为 `BLOCKED_DATA`、`BLOCKED_POLICY`、`RETRYABLE_INFRA`、`RETRYABLE_TOOL` 或 `BUSINESS_FAILURE`。同一终态不重复重跑，不覆盖失败产物。
6. 只有直接验收失败才新增恢复、架构或审计代码；测试数量和哈希检查不作为主交付。
7. `execution_admitted=false` 永久有效，直到用户另行授权并完成独立资格审查。

## 当前下一步

先捕获下一个闭市日的本机 StockDB 日线，保留覆盖变化和缺失清单；随后用同一快照运行低波动、趋势/动量和 Alpha158/LightGBM 候选比较。F01 继续等待 provider-backed parent_net_profit/total_assets 映射、单季度、PIT/单位输入，THS 继续等待授权只读捕获。任何一条路线阻断时，独立路线继续推进。




### 2026-10-24 common untouched forward comparison

S1/S2 已绑定同一份 46 标的 native StockDB 快照和 2026-10-14..23 八日窗口，四组终端持仓均从 2026-10-13 截止回放继承，未生成未来分数、目标、订单或成交。结果和初轮覆盖失败均保留在 `outputs/pipeline_receipts/20261024_common_untouched_forward_comparison_v2.json` 与 `outputs/pipeline_receipts/20261024_common_forward_coverage_failure_v1.json`。PIT、修订安全、公司行动和策略验证仍未完成。


### 2026-10-24 reproducible native paper reference

纸面目标的 native-vs-raw close 核对已封装为 `run_committee.py paper-target-native-reference`，使用同一 20 个目标标的和 2026-10-23 截止日重跑得到 20/20 匹配、0 差异。回执为 `outputs/paper_plans/20261024_lowvol_current_native_stockdb_reference_v2/receipt.json`；仍不代表 THS 当前账户、报价或可预填状态。


### 2026-10-24 reusable candidate shadow route

已将候选终端回放到历史影子纸面包的过程封装为 `run_committee.py candidate-shadow-paper`，可从任意合规研究回放生成目标、参考价格和本地目标/委托/成交/现金/持仓投影。回执 `outputs/pipeline_receipts/20261024_candidate_shadow_paper_packages_v2.json`；不进入日常纸面账户，也不生成可提交的 THS 订单。

### 2026-10-24 trend/half-momentum shared shadow route

The terminal `stockdb_low_vol_half_momentum_20` account now uses the explicit `candidate-shadow-paper --terminal-account` route and reaches the same local target/order/fill/cash/position projection as the other candidates. The 140,000 RMB zero-lot failure is preserved; the successful package uses a stated 1,400,000 RMB research cash assumption. The package remains historical shadow evidence and does not enter the daily paper account or THS.

### 2026-10-24 publication-event contract

Action context now accepts an optional per-event publication sidecar. The contract requires a conservative publication window before the decision cutoff, retrieval/as-of timestamps before the cutoff, a revision identity and a hashed source artifact. Without the sidecar, the existing route remains explicitly non-PIT and no company action is applied.

### 2026-10-24 real action sidecar attempt

Two existing 000538.SZSE source files now have hash-verified sidecar mappings. The mapping keeps source dates as lower bounds and preserves cache-read time separately; absent original retrieval/as-of/revision metadata leaves both rows PIT-blocked, so no action adjustment was applied.

### 2026-10-24 reusable action publication sidecar

The action-source inventory is now reproducible through `run_committee.py action-publication-sidecar`. It collapses duplicate artifacts to event-level rows, retains all source files and hashes, and feeds the optional action context. The current cache yields 138 events and 177 retained artifacts, but no as-of timestamps and no PIT-safe rows.

The v5 increment also maps `input_input_receipts.json` by symbol and year into explicit `upstream_input_*` lineage fields for all 138 events and 177 artifacts. The mapping records upstream receipt, retrieval time and SHA-256 without promoting those fields to issuer retrieval/as-of or PIT evidence; issuer `retrieved_at/asof_at/revision_id` remains `31/0/32` and `pit_safe_rows=0`.

### 2026-10-24 public-seat replay audit

The saved public-seat/trend batch is now indexed by `run_committee.py public-seat-replay-audit`. The audit preserves five complete 241-session cases and four saved stop points caused by unresolved held/execution price references, with 1,205 completed sessions, 3,836 research orders and 5,926 fill callbacks. Publication timing, PIT membership, causal seat alpha and execution admission remain false; the result is a candidate evidence artifact, not an adoption decision.



## 2026-10-24 long-window mainline continuation

主线已切到长窗口结果：固定低波动完成 678 会话重放，并生成五策略统一比较。龙头风格跨年适配已完成 1/32 case 后因约 12GB 工作集主动停止，保留 partial/abort 证据；不会重启同一资源路径。F01/PIT 和 THS 仍保留为依赖数据/外部证据路线，当前不占用主线工作时间。

## R01 candidate specification registry

All nine catalog candidates now have one fixed specification under `docs/architecture_v3/candidate_strategy_specs_20261024.json`. The executable registry and receipt bind snapshots, windows, portfolio limits, costs, replay/paper gates and closed promotion rules. `long_window_strategy_comparison.py` consumes this registry before reading the five retained replays, so a blocked or mixed candidate cannot silently enter the comparison. Receipt: `outputs/pipeline_receipts/20261024_candidate_spec_registry.json` (`VERIFIED_CANDIDATE_SPEC_REGISTRY`, 9/9, issues 0).

## 2026-10-24 生成式软件工程落地顺序

两份方案不再作为一张待勾选清单执行，而是拆成可运行的纵向业务切片。每个切片必须同时有实现入口、focused test、receipt/report 和交接记录；只有文档状态、模块数量或审计数量变化不算完成。

当前已完成的切片包括固定长窗口共享 Engine/Ledger、候选规格登记、当前截止纸面目标、P01/P02 目标分配、P03 暴露与阻断压力视图、P04 五策略虚拟资金池、P05 容量/成本、L02/L04 账本恢复、D05/D06/O02 快照/续跑、P06 对齐组合和 R02 研究决策矩阵。下一节点是 F1 的一个真实披露事件纵向贯通；只有原件、issuer publication/retrieval/as-of/revision、单季 U 和市场 join 均有证据才进入。F1 的 PIT、单季或原件字段缺失只阻塞该研究结论，不阻塞已有纸面、候选比较和恢复路线。

这条路线复用已完成阶段，不重新从文档的第一周开始；leader 资源中止路径不重跑，THS 只读证据在后端结果语义稳定且有授权观察后才进入。`execution_admitted=false` 保持不变。
