# 2026-10-26 fixed + candidate D0/D1 v4

本次按 Google Applied Design 的小而完整原则，将 2026-10-26 收市快照、固定低波动终端账户和半动量候选终端账户接入同一 `next-close-d0-d1` 纸面流水线。输入、时间语义、目标、订单投影、联合容量分配和回执均绑定在一个不可变输出目录内。

- 收市日：`2026-10-26`；D1 执行日：`2026-10-27`；结算日：`2026-10-28`。
- StockDB 冻结快照覆盖目标集合 `40/40`，缺失目标标的 `0`；快照仍是本地只读研究观察，不是 PIT 或公司行动准入。
- 固定腿 `low_volatility_252_20` 生成 `20` 个目标和 `20` 个 TARGET_PREVIEW 纸面订单。
- 候选腿 `stockdb_low_vol_half_momentum_20` 通过现有研究 follow-up 门禁，生成 `1` 个候选批次；联合预算投影生成 `22` 个纸面订单。
- 费用模型保持 `cn_a_share_standard_ledger_costs_v1`，比较绑定保持 `paper_target_projection_no_market_benchmark_v1`。
- 本次使用直接终端账户输入，`persistent_ledger_mutated=false`、`execution_admitted=false`、无 Broker/THS 交互；因此它完成的是当日可重放的 D0/D1 目标/订单/联合纸面证据，不等同于 `fixed_daily` 持久账户已推进。
- `fixed_daily` 仍等待严格完整的同日 close package；不能把 StockDB 原生快照伪装成 BaoStock 完整包。

回执：`outputs/pipeline_receipts/20261026_fixed_candidate_d0_d1_v4/contract_receipt.json`。输入清单：`outputs/pipeline_receipts/20261026_candidate_source_manifest_v2.json`。
