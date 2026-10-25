# V3 F2 高效落地路线

这份文件把 `quant_rebuild_V3_detailed_20261023.docx` 和 `financial_factor_implementation_F2_20261023.docx` 转成当前仓库可执行的交付顺序。两份 DOCX 是设计输入，代码、回执和实际研究结果才是当前状态依据。

多智能体的角色、并行边界和复核冗余见 [`agent_delivery_topology_20261024.md`](agent_delivery_topology_20261024.md)。

## 交付原则

1. 以一条纵向业务切片推进：真实输入或已冻结快照 → Signal → Target Portfolio → Orders → BrokerAdapter → Fill → Cash/Position → 研究结论。
2. 每个工作包必须改变代码、回执、纸面结果或策略证据；只做状态盘点、泛化审计、重复哈希检查或空接口不入队。
3. 已完成的能力直接复用，不从文档的“第一周”重新开始。工作包只有在其验收产物存在后才改变状态。
4. 研究回放、纸面账户、THS 只读证据和真实执行资格分开。`execution_admitted=false` 持续有效。
5. 数据受阻只暂停依赖它的市场结论；独立的组合、账本、恢复和运行能力继续交付。

## 依赖排序

| 顺序 | 工作包 | 直接结果 | 开始条件 | 完成条件 |
| --- | --- | --- | --- | --- |
| 1 | P03 + P05 | 当前纸面目标的残余暴露、阻断动作、容量和成本压力结果 | 已有 2026-10-23 reconciliation 与 native close reference | 生成可复核的 risk/capacity receipt；不生成可提交订单 |
| 2 | L02 + L04 | 迟到成交、取消竞态、未知外部结果的可恢复状态 | 现有 Engine/Ledger 与只读通道边界 | 合成故障场景能证明不重复动作，保存 recovery receipt |
| 3 | D05 + D06 + O02 | 固定快照原子发布、增量影响和任务续跑 | 已有 snapshot/run receipt | 中断后可从检查点恢复；半包不可被消费 |
| 4 | F1 纵向切片 D01-D03 | 一个真实披露事件的事实、公开时点、单季 U、进入窗口 | 可获得原件及 retrieval/as-of/revision 证据 | 一个事件追到因子和进入窗口；缺口明确，不伪造全市场 |
| 5 | F1 批处理与账户桥接 | scoped 事件母表、覆盖漏斗、20 日账户回放 | 第 4 步通过且固定快照发布 | 事件、因子、目标、订单、成交、现金、持仓、费用和净值同一 run_id |
| 6 | R02/R04/R05/R06 | 20 日主规格、条件 60 日变体、成本/稳健性、真实冻结前向 | 第 5 步真实输入就绪 | 固定样本和变体给出继续、停止、阻塞或无机会决定 |
| 7 | O/U/E | 日常恢复、工作台结果追查、THS 只读证据 | 后端结果语义稳定且有明确外部证据 | 只读观察可追溯；不登录、不预填提交、不下单、不撤单 |

## 本轮执行队列

- 当前主线：P03、P05、L02/L04、D05/D06/O02、P06 和 R02 已完成；下一闸门是 F1 单真实披露事件纵向切片，取决于可靠的 issuer publication/retrieval/as-of/revision 与市场 join 证据。
- 不启动：leader 同一资源路径重跑、F01 无 PIT 证据的全量回测、THS 登录或任何真实执行。
- 下一交易日：只做 StockDB 新闭市数据捕获、纸面日切和已登记候选重跑；周末不伪造刷新。
- 每个包的交付格式：一个实现入口、一个 focused test、一个 receipt/report、一次 handoff 更新。没有四者不宣布完成。

## 停止和切换规则

- 同一终态失败不重跑；只有代码、输入快照或执行路径实质变化才重试。
- F01 的原件、公开时点、修订或单季字段缺失时，保留 `BLOCKED_DATA` 并继续 P03/P05/L/O 路线。
- 任何真实渠道未知结果进入核对状态，不按普通失败自动重发。
- 负收益、覆盖不足或不可执行是研究结果，不通过改成本、窗口或筛选规则消除。

- R05 robustness matrix completed from saved long-window, untouched-forward and capacity/cost receipts; two candidates stop research-only, two remain mixed-evidence research-only, and momentum 12-1 is coverage-blocked; promotion and execution remain closed.

- L05 copied-account replay proof completed: the fixed 2026-10-23 paper checkpoint restores 148 events, 25 orders, 44 fills and 20 positions with identical cash, positions, orders, fills, event digest and terminal day; seven local stages remain provenance-verified.

- F1 local vertical slice completed as a partial implementation: `trading/research/f01_local_vertical_slice.py` routes three scoped Q1 2024 issuer facts through `Data -> Signal -> Target Portfolio -> Orders -> BrokerAdapter -> Fill -> Cash/Position`, producing 3 orders and 6 simulated fills on decision day `2026-10-22`. Receipt: `outputs/pipeline_receipts/20261024_f01_local_vertical_slice_v1.json`; focused test: `trading/research/tests/test_f01_local_vertical_slice.py`.
- The F1 slice deliberately remains `PARTIAL_LOCAL_TIMESTAMP_VERTICAL_SLICE`: the source PDFs are hashed, but their filesystem timestamps are only local retrieval/as-of proxies. Original issuer-network retrieval, revision history and PIT safety are not proven, so `pit_verified=false`, `factor_ready=false`, `promotion_allowed=false`, and `execution_admitted=false` remain unchanged. The next F1 step is to replace the local timestamp with reliable issuer retrieval/as-of/revision evidence and rerun the same slice.
- F1 source retrieval evidence was then refreshed for the same three issuer URLs. `outputs/pipeline_receipts/20261024_f01_source_retrieval_receipt_v1.json` records 3 successful PDF responses, 3/3 full-response hash matches, offset-aware retrieval times, and a 3/3 StockDB market join on the first trading session after the publication cutoff (`2024-05-06`). This closes current network provenance and market joining for the mapped files but does not prove historical as-of availability or revision history; F1 remains PIT-blocked and no promotion or execution gate changed.
- The publication sidecar now accepts that receipt and emits `outputs/pipeline_receipts/20261024_f01_publication_sidecar_network_v2.json`: all three rows carry explicit current retrieval/as-of fields and content-hash revision identities, while the contract still rejects them for the historical `2024-05-01` cutoff. This makes the timing/revision gap explicit without backdating evidence.
- The retrieval receipt also preserves HTTP `Last-Modified`/ETag metadata; all three server modification dates are at or before the observed publication dates. This narrows the provenance gap but is still not historical retrieval or revision-history proof, so F1 remains blocked.
- Because the current date is a closed weekend, the next executable D0/D1 node is the 2026-10-26 close: advance the existing `fixed_daily` checkpoint and run the registered mixed-evidence candidates (`stockdb_low_vol_half_momentum_20`, `factor_candidate_family_balanced_ridge`) through target/reconciliation receipts. No synthetic day-cut is permitted before that close.
- The D1 fan-in is now implemented in `trading/research/daily_candidate_paper_batch.py`: it accepts explicit research-only target/quote payloads, projects each registered candidate through the shared paper target/order/fill/cash/position package in isolated directories, and emits one batch receipt. A historical two-candidate run produced 20 targets/20 paper orders for half momentum and 5 targets/5 paper orders for Ridge; it remains separate from `fixed_daily`, THS and live execution.
- The manifest now binds catalog IDs explicitly to payload strategy IDs (`stockdb_low_vol_half_momentum_20` → `trend_low_vol_half_momentum_20`; `factor_candidate_family_balanced_ridge` → `stockdb_factor_monthly_ridge_v1`) so the next daily run cannot silently reject or mislabel a registered candidate.
- D1 now reads the candidate catalog selection gate and accepts only entries with `research_followup_allowed=true`; the historical v3 receipt proves both queued candidates pass that gate. Stop, coverage-blocked and unassessed candidates are rejected before paper projection.
- D1 also requires quote timestamps and every quote row to match the target decision day, with execution strictly later; the v4 receipt records this timing-safe batch contract.
- D1 v5 now binds the batch receipt to the exact manifest path, SHA-256 and candidate count, and rejects a supplied candidate list whose identifiers differ from the manifest. The regenerated receipt is `outputs/paper_plans/20261025_candidate_batch_v1/packages_v5/batch_receipt.json`; the manifest hash is `34f68b5d2638b635555ffc776003dacd88145d303417f0d9c40d1f9cb9c810a4`.
- The next fan-in step is automated by `trading/research/candidate_daily_input_bundle.py`: saved replay results (including explicit legacy terminal accounts) become target/quote payloads and a catalog-bound manifest, while a requested strategy alias must match the replay identity. The latest historical two-candidate run is recorded in `outputs/paper_plans/20261025_candidate_batch_v1/generated_inputs_v2/bundle_receipt.json` and `outputs/paper_plans/20261025_candidate_batch_v1/packages_v7/batch_receipt.json`; focused D1/F01/input-bundle tests are `30 passed`.
- `run_committee.py candidate-daily-paper-run` now combines the input bundle and D1 projection into one closed-session research command. Its two-candidate receipt is `outputs/paper_plans/20261025_candidate_batch_v1/pipeline_v1/pipeline_receipt.json`, with nested shared-paper results of 20/20 and 5/5; the combined focused suite is `31 passed`.
- The same command now accepts an explicit fixed terminal research account and persists its generated target/quote payloads with SHA-256. `pipeline_v3/pipeline_receipt.json` proves the fixed low-vol strategy (20/20) and both registered candidates (20/20 and 5/5) use the same local `build_paper_target_package` path; no persistent account or broker/THS state is touched.
- The public-seat lane now has one completed-case shared paper projection for `lag2_TREND_ANY_LHB` (5 targets, 5 orders, 4 routes) under a separate `UNASSESSED_SEPARATE_GATE`; evidence is `outputs/pipeline_receipts/20261025_public_seat_candidate_shared_paper_receipt_v1.json`. It remains outside the D1 queue until publication timing, PIT membership and causal attribution are available.
