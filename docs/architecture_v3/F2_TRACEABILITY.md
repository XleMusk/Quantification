# F2 财报事实到完整账户实施绑定

本文件把 `C:\Users\13944\Downloads\financial_factor_implementation_F2_20261023.docx`
纳入 V3 的实施控制记录。附件明确说明它是实施规格和离线参考，不是对当前
仓库、财报覆盖或账户回放的验收；因此这里保留 `UNBOUND`、`PARTIAL` 和
`BLOCKED_DATA`，不把文档中的接口示意或历史进度线索当成现状。

F2 的纵向目标是：一份真实披露原件经过完整性回执、事实身份、信息时点和
固定 F01，进入现有 Portfolio/Engine/Ledger，产生可复算的订单、成交、现金、
持仓、费用、公司行动、净值和基准。`account_replay` 是实际隔离研究账户回放
的结果字段，`execution_admitted` 是权限门；前者完成也不会打开后者。

## 当前已绑定的证据

- 现有财务采集和时点视图：`trading/research/financials.py`、
  `trading/research/annual_financial_inputs.py`；它们保留失败、空结果和
  未验证 PIT 的状态，但还没有完成 F2 要求的全量披露原件和修订历史。
- F2 采集回执合同：`trading/research/fetch_receipts.py` 已接入
  `financials.collect` 的单期间 BaoStock 任务，能够区分 `COMPLETE`、
  `COMPLETE_EMPTY`、部分、未知、模式变化、拒绝和可重试错误；当前单期间
  适配尚未保留供应商原始响应字节。分页年度适配器
  `trading/research/bulk_annual_financials.py` 已为每页保留原始响应文件和
  对应回执；两条路径都不能据此升级为真实原件覆盖验收。独立边界记录在
  `docs/architecture_v3/FP05_FETCH_RECEIPT.json`。对现有
  `20261004_220836_94303b12` 缓存的只读审计已核对 66 页、6 个分组，6 个分组
  均完整，产物为 `outputs/bulk_annual_financials/20261004_220836_94303b12/receipt_audit.json`；
  这仍不证明 PIT 或收益可用性。
- 采集完整性：`trading/research/financials.py --audit` 已对现有财报目录做只读
  文件/任务审计，结果为 `PARTIAL_COLLECTION`：80,505 个预期任务中已登记
  53,520 个，缺 26,985 个，3,568/5,367 个证券期间完整，已有文件哈希没有
  不匹配；产物为 `outputs/financials/20260930_162448_8be1e603/collection_audit.json`。
  这条缺口会阻塞真实 F01 市场结论，不能被空候选或空账户掩盖。
- 现有固定因子/候选回放：`trading/research/factor_candidate_replay.py`、
  `trading/research/factor_execution_projection.py`；它们使用共享 Engine/Ledger，
  但不能据此声称财报 F01 已贯通或市场策略已验证。
- F2 时点合同：`trading/research/financial_factor_contracts.py` 已用独立合成
  预期覆盖日期公告区间、严格事件前参考、交易日锚点和标签成熟；证据记录在
  `docs/architecture_v3/f2_time_contracts_traceability.json`，状态为
  `VERIFIED_LOCAL`，尚未绑定真实财报原件或 F01 账户回放。
- F01 事实计算：`trading/research/f01_financial_factor.py` 使用显式版本化事实、
  `Decimal` 和事件前证明，区分缺失、冲突、不可比、非正分母与
  `PRE_EVENT_UNPROVEN`；独立测试在 `trading/research/tests/test_f01_financial_factor.py`。
  这只证明纯函数语义，尚未证明真实披露覆盖或市场收益。
- 因子登记：`trading/research/financial_factor_registry.py` 将 F01 登记为唯一
  `ACTIVE` 因子，把现金支持和毛利率保留为 `DIAGNOSTIC`，并按消费者白名单拒绝
  任意字符串表达式或未授权用途；独立登记测试已通过。
- 单位合同：`trading/research/financial_units.py` 以 `Decimal` 保存原始值、原始
  单位、标准化值和转换记录，未知单位直接失败；它只验证转换语义，尚未替代
  真实来源字段字典或发行人原件核对。
- 期间计算：`trading/research/f01_financial_factor.py` 保留累计到单季的显式减法，
  `trading/research/financial_periods.py` 增加可比 YTD/FY/YTD 的 TTM 纯函数；
  scope、币种、单位或期间不一致时失败，不跨版本静默相减。
- F01 输入就绪审计：`trading/research/f01_readiness.py` 已检查现有 2023–2025
  年度输入，15,731 行、5,247 个证券，其中 15,721 行有资产/归母利润数值；但
  由于输入只有年度期间且 `pit_verified=false`，结果明确为 `BLOCKED_DATA`，产物为
  `outputs/financials/f01_readiness_20261024.json`。这条阻塞必须先补齐事件时点
  单季事实，不能把年度数据代作 F01。
- F01 到共享账户桥接：`trading/research/f01_signal.py` 和
  `trading/research/f01_account_replay.py` 通过 typed `SignalFrame` 的 Engine
  hook 进入现有目标、订单和 Ledger，并记录 Data→Signal→Target Portfolio→
  Orders→BrokerAdapter→Fill→Cash/Position 阶段。合成端到端证据在
  `docs/architecture_v3/FP17_FP18_REFERENCE.json`。该证据将
  `account_replay=true` 限定为本地合成回放，仍保持 `execution_admitted=false`，
  不能替代真实财报事件和市场账户结果。
- 追查与发布：F01 回放每日记录保留 signal provenance、目标、订单、模拟适配器、
  成交和现金/持仓；`publish_bundle`/`load_bundle` 对新目录执行原子文件写入和
  SHA-256 manifest 校验。`docs/architecture_v3/FP20_FP23_REFERENCE.json`
  只证明本地合成发布与篡改拒绝。
- 运行身份：`trading/research/run_identity.py` 规范化 study、code、snapshot、
  calendar、rules、benchmark、account 起点及研究配置，生成稳定 `run_key`，并
  把 `attempt` 单独编号；它已被 F01 账户适配器作为可选运行绑定使用，尚未接入
  财报专用持久任务/API。
- 查询依赖：`trading/research/query_dependencies.py` 保存 `FOUND`、`MISS` 和
  `UNKNOWN` 查询身份，并能判断后来落入原范围且在 cutoff 前可得的事实是否重开
  事件；同一模块把解释量变化和会影响选股/目标/现金的变化分别标为
  `RECOMPUTE_EXPLANATION` 与 `REPLAY_ACCOUNT`。证据为 5 个独立本地测试，持久化
  和真实修订回放仍待绑定。
- 共享账本本地参考：`docs/architecture_v3/L01_L03_REFERENCE.json` 和
  `trading/tests/test_v3_ledger_reference.py`，状态为 `VERIFIED_LOCAL`，仅覆盖
  合成账本不变量。
- 当前架构审计：`run_committee.py architecture-check` 已通过；它是静态边界
  证据，不是数据、市场或渠道证据。
- F2 的首个时间语义切片已落在
  `trading/research/financial_factor_contracts.py`，并由
  `trading/research/tests/test_financial_factor_contracts.py` 的 11 个独立期望值测试
  验证。机器可读的输入、代码/测试哈希、反例和限制见
  `docs/architecture_v3/f2_time_contracts_traceability.json`；该证据只到
  `VERIFIED_LOCAL`，没有把真实财报、StockDB、账户或市场结果写成已完成。

## F2 强制语义

采集器必须同时返回数据和可验证的 FetchReceipt。`COMPLETE_EMPTY` 只有在请求、
结构和分页全部完成时成立；非空 DataFrame 不能单独证明完整。身份、报告期间、
合并口径、币种、公告版本、解析/字段映射版本和原件引用必须保留。日期公告的
信息时点是区间；交易使用保守的可用上界，而严格事件前参考值必须证明早于事件
可能发生的下界，否则使用更早已知值或返回 `PRE_EVENT_UNPROVEN`。交易日延迟按
版本化 `session_id` 移动，不能按时间戳列表位置移动。训练标签同时检查
`feature_available_at`、`label_actual_end`、`label_available_at` 和完成状态。

修订或补入此前未查到的资料必须通过查询依赖重新评价事件；如果影响排序、目标、
资金或费用，必须从最早受影响检查点重放共享账户。重复运行按规范化输入生成稳定
`run_key`，`attempt` 单独记录，不能把 `account_replay=true` 或
`execution_admitted=true` 当成客户端可提交的参数。

## 24 项保留编号

`docs/architecture_v3/f2_work_packages_binding.json` 保留 FP01–FP24，并为每项
记录实际路径、准备依赖、验收依赖、语义影响、独立预期、完成证据和剩余阻塞。
当前 V3 仓库没有可核验的 F1 原始 24 项任务目录，因此所有未绑定的编号保持
`UNBOUND`；这避免用新命名替换历史任务或用规划字段伪造完成。任何升级都必须
附真实输入、完整命令、产物哈希、状态和适用范围。

外部文档的来源链接、参考 SQL、合成算例和 Agent 协同建议不产生新的账户权限。
本阶段仍禁止真实登录、下单、撤单、转账、付费数据购买，且所有证据保持
`execution_admitted=false`。
