# 2026-10-23 固定策略 + 候选策略 D0/D1 纸面联合包

状态：`COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1`；`execution_admitted=false`；未接触券商或同花顺，未修改持久账户。

本次把固定低波动策略 `low_volatility_252_20` 与候选 `stockdb_low_vol_half_momentum_20` 接入同一 `next-close-d0-d1` 入口。两条策略都以 2026-10-23 收盘为决策日，执行日为 2026-10-26，结算日为 2026-10-27；StockDB 固定快照覆盖 40/40 个目标证券，候选批次生成 20 个目标和 20 个纸面订单，全部 20 条路由在本地纸面规则下可用。

联合组合使用 `budgeted` 容量策略、50%/50% sleeve，合并目标 22 个证券，纸面订单 22 笔；合并目标权重 0.8，单证券上限 0.02，手数取整和费用模型均通过。联合 TARGET_PREVIEW 的预计费用为 CNY 110.66，预计期末现金 CNY 78,534.34、预计权益 CNY 139,889.34。该结果是共享 Engine/Ledger 的一次性投影，不是持久账户滚动结果，也不是 broker fill 或策略收益验证。

候选终端账户来自已完成至 2026-10-23 的 StockDB 研究回放：`D:\QuantificationData\trading\research\stockdb_rolling_low_volatility\20261026_214427_f57e9c67\low_vol_half_momentum_20\account.json`。它沿用固定成本、交易规则和研究边界；PIT 成员、公司行为和真实渠道证据仍未补齐。

## 代码修复与失败证据

快照通常在次日批量捕获，原实现把 `captured_at` 直接写进候选行情的 `observed_at`，导致合法的 D0 行情在候选批次被误判为 D1 并中断。现改为行情 `observed_at=2026-10-23T15:00:00+08:00`，同时保留 `source_available_at` 记录实际快照捕获时间；新增回归断言并通过 19 个 `next_close_d0_d1` 测试。

首次修复前运行的失败回执保留在 `outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v1/contract_receipt.json`，错误为 `Candidate quote clock is not on its decision day`，未自动重试或覆盖。修复后的成功回执为 `outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v2/contract_receipt.json`，联合回执为 `outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v2/joint_paper/joint_paper_receipt.json`。

边界：当前持久 `fixed_daily` 账户仍停在 2026-10-23；本包的固定腿和候选腿均为 `TARGET_PREVIEW`，不得当作已完成的当日持久日切、PIT 事实、THS 授权只读证据或真实执行。
