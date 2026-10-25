# V3/F2 高效落地执行波次（2026-10-25）

这份波次把 `quant_rebuild_V3_detailed_20261023.docx` 与 `financial_factor_implementation_F2_20261023.docx` 转成能改变代码、回执、纸面结果或策略证据的工作包。文档是设计输入；当前状态以代码、回执、纸面结果和保留的失败证据为准。

## 分工原则

每个工作包只有一个生产写入者、一个独立复核者和一个集成者。冗余只放在时点泄漏、单位/期间、现金与股数守恒、未知外部结果重复动作四类高价值错误。根线程负责集成，不把智能体数量当作交付量。

## 波次

| 波次 | 主责 | 并行复核 | 直接交付 | 验收 |
|---|---|---|---|---|
| W0 基线冻结 | root | 目录/回执复核 | 当前候选目录、v5 固定+候选纸面包、F1/F2 绑定 | 快照、成本、基准、执行闸门一致 |
| W1 下一闭市 D0/D1 | next_close_delivery_wave | StockDB 日期/缺失行复核 | 2026-10-26 闭市后的固定策略+两个注册候选同日纸面回执 | 一条幂等命令走统一 Engine/Ledger，`execution_admitted=false` |
| W2 F2 单事件 | f2_factor_delivery_wave | 单季 U 独立 oracle | 标准事实、期间、单位、available_at 与因子账户桥接，或明确 `BLOCKED_DATA` | 只有 PIT/修订/单位完整才进入回放 |
| W3 候选比较 | candidate_strategy_delivery_wave | 冻结前向窗口复核 | 趋势/动量、成熟因子、Alpha158/LightGBM 的同快照结果 | 输出继续、停止或阻塞，不调参救结果 |
| W4 纸面与只读渠道 | paper_runtime_owner | 纸面/渠道边界复核 | 日切、恢复、目标持仓证据、授权后的 THS 只读回读 | 不产生真实动作 |
| W5 发布决定 | root | 输入语义与研究结果两路验收 | 版本化结果包与下一交接 | 六类状态分开，不把研究证据写成真实执行资格 |

## 当前关键节点

- 周末不伪造行情；W1 等 2026-10-26 实际闭市后执行。
- F1 当前真实来源重取时间晚于历史决策日，继续保持 `BLOCKED_DATA`，不回填 PIT。
- 公开席位候选留在 `UNASSESSED_SEPARATE_GATE`，不得挤入普通候选队列。
- 已终态的资源停止路径不重复启动，除非代码、输入快照或资源设计发生实质变化。

机器可读版本见 [`execution_wave_plan_20261025.json`](execution_wave_plan_20261025.json)。

- W3 已完成：mature-factor Ridge 固定 60-session same-lineage variant 已在冻结输入上完成 fit 与共享 Engine/Ledger replay；R04=VERIFIED_LOCAL，但 PIT/action gate 仍关闭。
