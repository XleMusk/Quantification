# 同花顺适配：只读与人工提交前预填

本轮只增加现有 `trading` 下的适配层，不更换交易软件，不新增研究界面。
**目前完成代码与测试，未完成真实账户读取、实际预填和成交回读的联调。**

2026-10-15 已通过官方 Computer Use 重新只读观察模拟账户 `UI**54`，并从实际
accessibility 文本解析资金余额、冻结/可用资金、市值和总资产。`ths-simulation-funds-preview`
只消费已保存文本，不访问界面；它输出 `observed_funds_only_not_reconciliation_ready`，
不把视觉持仓、当日空表或未知费用提升为四表完整证据，也不写入观察日志或 Ledger。
本机记录位于 `D:\QuantificationData\trading\research\ths_readonly\20261015_0852_ui54`。
原生表格导出尚未完成；未预填或提交委托。示例：

```powershell
.\env\python.exe run_committee.py ths-simulation-funds-preview --tree <funds_accessibility.txt> --expected-account '模拟炒股-UI**54' --out <new_funds_only.json>
```

## 离线预填 payload 与回读核对

在有真实客户端前，可对已保存的人工复核计划生成完全相同的三字段
payload，不打开窗口、不写数据库、不访问账户：

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths prefill-payload `
  --plan <reviewed_plan.json> --profile <ths_profile.json> `
  --out <prefill_payload_preview.json>
```

若已有窗口回读导出的三字段 JSON，可追加 `--readback-json`；结果会明确
标记 `readback_verified` 或 `readback_mismatch`，两种情况都保持
`submitted=false`、`execution_admitted=false`。该入口复用
`trading/ths/adapter.py` 的计划校验和字段格式化逻辑，避免离线预览与真实
人工预填出现第二套价格/数量规则。

离线导出包可先用 `preview-observation-bundle` 做一次不落库校验。它读取账户
JSON、委托、成交和累计现金流水四个文件，复用现有列映射与标准化规则，检查
订单/成交/现金流水关联及成交数量，输出 `templates_blank`、`validation_error`
或 `bundle_ready_for_reconciliation`；该入口不初始化观察日志、不修改统一账本、
不访问客户端，也不打开交易准入：

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths preview-observation-bundle `
  --account-json <account_observation.json> `
  --orders-csv <orders.csv> `
  --trades-csv <trades.csv> `
  --cashflows-csv <cashflows.csv> `
  --out <observation_bundle_preview.json>
```

四张表放在标准观察目录时，也可以只传目录，避免手工错配文件：

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths preview-observation-bundle `
  --bundle-dir D:\QuantificationData\trading\ths\observation_bundle `
  --out <observation_bundle_preview.json>
```

对已经形成的结构化账户包，可在同一账户快照上先做订单级风控预览。订单价格、数量、方向必须来自人工复核的明确计划；该命令不读取客户端、不写观察日志、不改 Ledger，也不把缺失费用当成零：

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths preview-account-order-risk `
  --package <structured_observation_package_v1.json> `
  --orders-json <proposed_orders.json> `
  --out <account_order_risk_preview.json>
```

它把当前现金、持仓标记、可卖数量、T+1、单票集中度、总敞口和最低现金缓冲与订单批次合并计算；只有 `order_risk_ready_readonly` 才表示该批次通过这道纸面风控预览，仍不代表提交许可。

`import-observation-bundle` 和 `reconcile-observation-bundle` 同样支持
`--bundle-dir`；导入仍要求显式 `--confirm-import`，不会自动写统一 Ledger。

若券商导出使用非示例表头，追加 `--profile <实际映射.json>`；需要四张表完整
映射（包括账户持仓的原始人民币标记价和累计资金流水）时，可复制
`trading/ths/observation_bundle_profile.example.json` 后只替换实际表头值；开盘 runner
也接受同名的 `-THSProfile` 参数并把 profile 路径写入会话状态。Windows 同花顺
常见的中文 CSV 可在该 profile 中显式声明编码，例如
`{"encoding":"gb18030"}`，也可用 `{"encodings":{"orders":"gb18030","trades":"gb18030","cashflows":"gb18030"}}`
按表分别声明；观察包入口不会根据字节自动猜编码，默认仍为 `utf-8-sig`。
通过工作台初始化观察包时，系统会同时创建该 profile 的副本；已有任一观察包文件
时不会覆盖，也不会替换用户已经填写的映射。

预检通过后，如需把四类人工导出写入本地 `ths_observations`，必须显式确认；
该命令使用单一事务并按账户、视图、观察时间和交易日去重，冲突则整体停止：

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths import-observation-bundle `
  --account-json <account_observation.json> --orders-csv <orders.csv> `
  --trades-csv <trades.csv> --cashflows-csv <cashflows.csv> `
  --confirm-import --out <bundle_import_receipt.json>
```

通过导入校验后，命令会自动在 `D:\QuantificationData\trading\ths\observation_bundle\structured\`
下生成完整 `ths_structured_observation_package_v1`。如需指定目录，可追加
`--structured-out-dir <目录>`。该包只记录结构化观察、订单/成交关联和对账就绪状态，
不会自动提交、不会推断“无订单/无成交”，也不会写入统一 Ledger。

若这四张观察要立即刷新一个已保存计划，可改用
`reconcile-observation-bundle --id <计划ID> --confirm-import`；它先执行同一
事务导入，再调用现有 `journal.refresh`，把成交、现金和持仓不一致保留为
`reconciliation_required`，不会替用户修正或写入统一回放账本。对已绑定的暂停
模拟运行，可再显式提供 `--run-id <运行ID> --confirm-ledger-import`，在观察核对
成功后把同一券商回执写入该运行的统一 Ledger；账户绑定、暂停状态、前缀一致性
和费用证据仍由现有 `import_plan` 再次检查，任一冲突都不改账本。

如果同花顺导出器能够生成原生结构化 JSON 收据，也可以直接走原生收据适配器。
收据必须逐行携带交易所、账户别名、交易日、人民币分、委托号/成交号和累计现金
变动；截图、OCR 候选、缺少资金流水或仅有 `row_count` 的元数据会被拒绝：

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths preview-native-observation-bundle `
  --index <ths_readonly_evidence_index_v1.json> `
  --out-dir <native_structured_package_dir> `
  --out <native_preview.json>
```

预览通过后，才可显式写入同一观察日志；如要把已核对的计划回执写进同一个暂停
paper Ledger，必须同时提供 `--plan-id`、`--run-id` 和 `--confirm-ledger-import`：

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths import-native-observation-bundle `
  --index <ths_readonly_evidence_index_v1.json> `
  --structured-out-dir <native_structured_package_dir> `
  --confirm-import --plan-id <plan-id> --run-id <paused-paper-run-id> `
  --confirm-ledger-import --out <native_import_receipt.json>
```

该入口不访问客户端、不提交委托、不撤单；原生收据的订单、成交、资金流水仍会
经过四表关联、账户级风控和同一 Ledger 的二次校验。

提供 `--run-id/--plan-id/--confirm-ledger-import` 时，命令会先用这四张原生收据
执行同一暂停 paper Ledger 的预览和账户风控；预览不是 `ready_for_import` 时不会
先写观察日志。通过后才写观察日志、刷新计划并调用既有 `import_plan`，仍不会访问
客户端或提交委托。

在正式入账前，可用同一套计算先查看准确的现金、冻结资金、手续费、订单/成交和
持仓变化。预览不初始化观察日志、不更新计划、不写 `runs.state`；如附带观察包，四张
表只在预览事务内暂存并回滚：

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths preview-ledger-import `
  --run-id <paused-paper-run-id> --id <plan-id> `
  --bundle-dir D:\QuantificationData\trading\ths\observation_bundle `
  --out <ledger_import_preview.json>
```

返回 `ready_for_import` 时才表示这次回执计算可以进入现有显式 `import-ledger`；
预览仍是纸面消费者，`native_verified=false`、`submission_enabled=false`，不会
操作客户端或提交订单。重复预览已入账计划返回 `already_imported_no_change`。

2026-09-30：统一入口为 `trading/ths/broker.py` 的 `THSAdapter`，复用原
`PrefillAdapter` 和 `ths_*` 记录；`capture`、`plan-target`、`prefill` 已接入。
它与现有账本模拟通道实现同一 `BrokerAdapter` 接口，但自动提交与撤单始终拒绝。
`run_committee.py ths capabilities` 无桌面操作、不连接账户，报告代码能力，
不是已登录、账户权限、资金或本机映射可用的证据。用户报告已登录后，
首个只读重查被物理 Esc 停止，尚未核验资金/持仓、真实表单和委托成交。

2026-10-01：`reconcile` 回读现在保存逐笔成交号、证券、方向、股数、成交价
及人民币分单位的毛成交额，并在原 `ths_plans.result` 中持久保留已确认成交。
重复读取不会重复计入成交；成交表缺行、同成交号金额变化或委托与计划冲突，
进入 `reconciliation_required`，保留先前证据并阻止下一笔计划。重新读取完整一致
的记录后才恢复对应状态。两个查询视图和前次结果在同一 SQLite 事务中读取。
旧记录只有成交号时不补造价格，先保留成交号/数量，待后续完整回读补齐。
这轮以合成窗口和隔离数据库复现并验证，不是本机成交联调，也未导入模拟或实盘账户。

2026-10-01：原 `import-export/reconcile` 现支持显式券商累计现金流水与前后
账户余额、持仓核对。费用必须来自实际导出，不能套用研究佣金或用余额反推。
资金余额必须明确含冻结资金，并满足“余额－冻结＝可用”；可取资金、总资产
不能替代资金余额。缺字段为 `pending_evidence`；差一分钱、持仓不一致、确认过的
成交或费用变化，进入 `reconciliation_required`，保留前次证明并阻止下一计划。
账户读取时间必须晚于委托、成交和现金流水读取；新计划也不能复用更早的余额。
下一计划建立后，旧计划的余额证明限定在原时间区间，但仍核对最新成交/费用
是否修订。这是只读券商观察核对，`ledger_imported=false`，不写 `runs/daily`。
原数据库只读检查仍为零 THS 计划/观察；本机全流程与实际账户账本导入仍未验收。

2026-10-01：增加显式 `bind-ledger/import-ledger` 导出消费者，写入已有暂停
纸面运行的 `runs.state`，复用同一个 `Ledger` 的委托/成交/现金/持仓计算。
绑定须人工明确确认是模拟账户，运行尚未演进，观察权益等于配置初始本金，
持仓有明确不复权人民币标记价，无未结委托或冻结现金，别名没有其他账本写入者，
数据快照日历含当日和下一交易日。不覆盖旧回测或已演进账户，不另造账户。
首次交付只接入期初当日、沪深主板计划；下述后续交付补上跨交易日导出核对，
预填品种仍仅限沪深主板，日历不够就停止。
已有持仓的未知不可卖期限不会猜成次日解锁。券商账户类别仅靠人工确认，
不是已完成真实客户端身份和字段核验。不要将真实账户导出冒充模拟账户。

入账须先通过原 `reconcile` 的成交、累计实际费用、前后现金与持仓核对，
且计划期初/已入部分成交与当前唯一账本一致。重复/重启/并发导入不会重复
扣费或增加持仓；同成交号变化、缺行、费用修订、现金/持仓/价格证据不足
保持账本不变，保留旧证据和入账标记并阻止下一计划。费用记录在委托累计数，
逐笔 `fees=null`，不能解释为零佣金。入账标记是 `result.ledger_import`，
只读检查器自身的 `account_reconciliation.ledger_imported=false` 不变。
所有写入在同一个数据库事务中完成；不产生伪造的日净值、收益或模拟成交。
绑定后禁止模拟撮合、恢复日终模拟 worker 或由 worker 完成该运行。
界面展示来源、估值观察时间、委托累计费用，逐笔显示“未分摊”。
以上仅以隔离合成导出和数据库、模拟 API 界面验证，**不是本机券商联调**。
原数据库仍为零计划/观察/绑定账户，自动提交、撤单和真实账户接入保持关闭。

2026-10-01 后续：`reconcile-session` 已能把下一交易日导出基线核对到同一个
`runs.state/Ledger`。必须是固定快照日历中的下一交易日；重新读取上一日委托、
成交和实际费用，逐项对照已经入账的回报。上一日剩余委托必须有明确成交、撤单
或拒单终态才释放账本预留；该步骤只消费终态证明，**不会向客户端发出撤单**。
新一天委托/成交必须为空，无冻结资金；账户时间必须晚于所有相关查询视图，
可卖数量、现金和持仓都必须匹配。已知 T+1 买入依固定日历变为可卖；期初
未知锁定期限默认仍保持锁定，不根据日期或账户差额猜测解锁。显式解除导出支持见下。

非交易现金变化通过显式 `movements` 导出入账，支持现金分红、利息、税费、
存入和转出；正负号、币种、单位、流水号必须明确，空流水也须有表头的空导出。
这只记录已经发生的模拟账户资金变化，不执行转账，也不把资金存入计为收益。
重复、重启、并发、跨日重读旧回报不重复入账；旧成交/费用变动、未归属成交、
现金差一分钱或未经证明的股份变化保持账户状态不变，阻止下一计划。
不生成 `daily`、回放游标或收益曲线。未经支持的股份公司行动、没有明确解除证据的期初锁定、
绑定首日期间的非交易流水，以及已建立当天新计划后的非交易流水仍须继续核对，
不能重设余额。已核对次日基线、尚未建立当天新计划的追加流水支持见下。
已完成的验证仍为隔离合成导出、数据库及模拟 API 界面，不是本机券商验收。

2026-10-03：`import-movements` 消费已核对交易日内的累计非交易资金导出，
只把新流水编号追加到同一个 `runs.state/Ledger`。必须先有 `reconcile-session`
建立的当天基线，尚未建立当天新计划、委托/成交为空且没有冻结资金；绑定首日
没有完整的期初流水范围，暂不支持。当天已入账流水必须全部保留、内容一致，
新增金额逐分核对同期含冻结余额、完整持仓和可卖数量。没有从余额差额反推费用，
没有执行转账。旧价不能替代当前持仓的显式不复权标记价。

原交易日基线不覆盖，已有成交和持仓批次不重置；重复、并发、重启和历史回读不
重复加钱。差额或修订保留旧账本并阻止新计划，不能重读旧基线清除该冲突；修复
完整导出后再调用本入口。下一交易日还会核对已入账的前日流水是否修订或缺行。
已建立新计划或订单时调用本入口只会拒绝，不因调用时机不支持而锁死合法账户。
既有账户详情直接显示更新后的现金、费用和资金事件，不生成虚构日净值/收益。
此交付仍只在隔离合成导出和实际 CLI 消费者中验证，未操作同花顺本机。

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths import-movements --run-id <同一已绑定暂停模拟运行>
```

此前需先按已有 `import-export --kind movements` 导入完整累计流水，再导入最新
账户观察；`--observed-at` 仅重读已经入账的精确历史账户时间，不更新当前余额。

2026-10-03：次日基线核对可显式消费 `restriction_releases` 券商导出，只解除
该账户期初记录中“期限未知”的锁定股数，不增加股份、不改现金、不提前解锁
已知 T+1/未来可卖批次。总持仓、完整可卖数量与最新账户必须一致；部分解除
只分割对应股数，余下仍锁定。仅增加账户页可卖数量不是解除证据。

导出映射须声明 `basis: opening_restriction_releases`、`quantity_unit: share`，
并明确映射 `release_id, symbol, market, released_qty, release_day, reason` 六列；
`markets` 显式映射实际交易所，`reasons` 将经核实的实际解除业务名称映射为
`opening_unknown_restriction_release`。编号保留前导零，数量为股而非手，生效日
必须等于核对交易日。不支持用公共解禁公告或策略推算代替这个账户的解除流水。

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths import-export --kind restriction_releases --profile <实际映射.json> --csv <账户解除记录.csv> --trading-date <下一交易日> --observed-at <实际导出时间及+08:00>
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths reconcile-session --run-id <同一已绑定暂停模拟运行> --include-restriction-releases
```

账户观察必须晚于此导出和原有委托/成交/现金视图。首轮必须显式添加该参数；
未核对的新交易日默认不消费解除记录，旧已入账交易日的回读自动保留原证明。
重复、重启、并发回读不会重复解锁；后续交易日还会核对旧编号、股数和证券，
修订、缺行、超额或未入账的新解除记录停止，不能靠匹配现金余额掩盖。
绑定首日和已建立同日新计划后的解除暂不支持；真实列义、实际账户及客户端
仍未验收。已走通隔离合成 CSV、CLI、唯一账本、后续卖出计划和成交费用读取，
没有操作同花顺或提交订单，真实自动提交保持关闭。

2026-10-03：次日基线也可显式消费 `share_credits` 账户送转股到账导出。
仅支持普通股票的 `bonus_shares`（送股）和 `capital_transfer`（资本公积转增），
按实际入账股数追加持仓批次，不按公告比例推算个人权益，不创建买入成交或扣现金。
原持仓、未知期初锁定和已知 T+1 批次保留；新股的明确可卖日期控制该批次可用量。
未来可卖日必须在绑定的固定交易日历内，且该日还有下一交易日；未知期限不猜测。

映射须声明 `basis: account_bonus_share_credits`、`quantity_unit: share`，明确映射
`credit_id, symbol, market, credited_qty, credit_day, available_day, kind` 七列。
`markets` 与 `kinds` 必须按经核实的券商列义显式映射，数量为正整数股，可包含零股；
到账日等于核对交易日，可卖日不早于到账日。不得将配股认购、拆分、股份转托管、
公共发行人公告或账面股数差额映射成送转股。没有个人到账/可卖日证据时仍停止。

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths import-export --kind share_credits --profile <实际映射.json> --csv <账户送转股到账.csv> --trading-date <下一交易日> --observed-at <实际导出时间及+08:00>
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths reconcile-session --run-id <同一已绑定暂停模拟运行> --include-share-credits
```

账户观察须晚于全部相关导出，现金、总持仓和可卖数量必须与追加后的同一账本一致。
默认的新交易日核对不消费未获准的送转股导出，已入账历史回读保留原证明；旧编号、
股数、可卖日或业务类型修订/缺行，以及新增未入账记录都会停止而不重设账户。
可与 `--include-restriction-releases` 同用，但不绕过零股卖出必须完整退出的原规则。
仅支持尚未建立当天新计划的次日基线；绑定首日、日内追加、股份扣减或其他证券
转换仍不支持。已通过隔离合成 CSV/CLI、卖出计划及原成交入账流程，非本机验收。

## 本机实测，2026-09-26

- 实际委托程序：`D:\同花顺软件\同花顺\xiadan.exe`，文件版本 `2026.7.2.0`。
- 行情进程：同目录 `hexin.exe`，版本 `2026.7.24.10`。它不是委托页面。
- 15:26 左右探测到两个进程运行；委托进程只暴露一个 238×29 无标题窗口，Win32/UIA 均为 0 个子控件。没有据此猜控件 ID 或写入表单。
- 15:30 再查：行情窗口已最小化，委托进程仍在但没有可见顶层窗口。仍须展开交易主页面，不能将进程存在当作已登录可操作。
- `D:\同花顺远航版\bin\happ.exe` 的旧快捷方式目标不存在，不算已验证安装。
- 券商、已登录账户和正式交易 API 权限尚未确认；因此尚不能判断正式接口是否可用。
- 独立环境：`trading/ths/.venv`；安装 pywinauto 0.6.9、psutil 7.2.2，依赖检查通过，原项目环境未改。
- 实测报告位于 `D:\QuantificationData\trading\ths\`：`probe.json`、`xiadan_controls_win32.json`、`xiadan_controls_uia.json`。句柄只对当时会话有效，不能永久复用。

## 已实现的边界

1. 按实际进程、绝对路径、版本、窗口句柄和控件 ID 定位；发现界面时不读取输入框值。
2. 可读取已验证映射的原生 Win32 ListView 表格与可用资金文本。虚拟表格/UIA 表格尚未适配；明确拒绝，不以 OCR 或猜测金额兜底。
3. 可导入用户从客户端导出的 CSV/TSV，显式配置列、交易所、方向和状态映射。保留证券代码/合同号前导零；未知或重复记录停止。
4. 用真实资金、持仓和可卖数量建立临时校验状态，复用 `trading/ledger.py` 的资金、100 股买入、零股卖出及可卖数量约束。不会把模拟盘持仓用于预填。
5. 当前策略的绝对目标股数可转换为委托差额；不自动接入历史回放或旧 Qlib 信号。只接受显式 `raw_rmb` 价格，账户观察和计划最长 5 分钟有效，账户日期须为本地当日。上一笔已确认成交、撤单或拒单后，新计划必须使用不早于其委托/成交观察时间的新账户快照，不能复用旧余额和可卖数量。
6. 仅填写代码、价格、数量三个 Edit 控件，并逐项和最终回读。账户、方向、限价模式、前台窗口及弹窗检查不通过就停止。首版由用户先选中对应页面，没有自动导航。
7. 提交由用户在客户端完成，再手工绑定实际合同号；通过当日委托与成交记录区分受理、部分成交、全部成交。早于计划的记录不能确认新委托；拒单状态却存在成交时保留为待核对。结果不明只查询，不重填、不重发。
8. 只读记录使用现有 `D:\QuantificationData\trading\trading.sqlite3` 的 `ths_*` 表，不把预填或提示框记作成交。仅在额外显式绑定模拟账户并调用 `import-ledger` 后，把已核对的导出写入同一个 `runs.state`；不写 `daily`，不更新回放游标，不改其他账户。

回读中的 `executions`、`gross_executed_cents` 表示本次查询的逐笔成交及毛额，
`confirmed_executions`、`confirmed_filled_qty` 保留此前确认过的证据，两者冲突时
不可将本次缺行解释成零成交。毛额不含券商费用，不等于资金流水；
实际余额和可卖数量必须从新的账户页读取，现金核对标记默认为 `false`。
仅在实际现金流水、含冻结余额和完整持仓均匹配时，
`account_cash_reconciled/account_positions_reconciled` 可为 `true`，表示对应观察时间
的只读核对通过，不代表当前余额已更新到统一账本、券商字段已本机验证或可自动交易。
`confirmed_account_reconciliation` 保留此前通过的实际费用/净现金/持仓证明。
费用/现金流水与统一账本的真实本机联调仍未完成，不能用成交毛额推算可用资金。

导出账户映射可额外包含 `price` 列，必须声明 `price_basis: "raw_rmb"`，
表示持仓在该观察时间的不复权人民币标记价、分精度，不是成本价或研究复权价。
原四列映射继续用于只读核对；持有证券却缺少标记价时不能建立或导入账本。
待本机核验和账户确认后使用以下入口；本轮没有对原账户执行它们：

```powershell
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths bind-ledger --run-id <已有未演进暂停纸面运行> --account <脱敏模拟账户别名> --confirm-simulated-account
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths import-ledger --run-id <同一运行> --id <已人工提交并绑定合同号的计划>
& 'D:\Quantification\env\python.exe' D:\Quantification\run_committee.py ths reconcile-session --run-id <同一运行>
```

`reconcile-session` 默认消费最新账户观察；重读已核对历史基线可加
`--observed-at <已记录的精确观察时间>`。这不允许用旧基线替换新余额，也不允许
跳过交易日。新计划只在所属交易日基线核对通过、无跨日核对错误后建立。

**没有提交、撤单、登录、验证码识别、自动确认/关闭弹窗、回车提交、后台定时交易入口。**
未验证 ST/退市、停牌、上市初期、当日价格限制、行情真实性和实际券商字段；预填前的费用预算仍采用研究配置，不能替代券商校验，现金核对则只接受实际导出费用。首轮只支持沪深主板股票预填，不支持融资融券、创业板、科创板或 ETF 预填。以上限制未解决前不得将本模块当作无人值守交易系统。

## 使用入口

在 `D:\Quantification` 中运行：

```powershell
$py = 'D:\Quantification\trading\ths\.venv\Scripts\python.exe'
& $py .\run_committee.py ths --help
& $py .\run_committee.py ths probe
& $py .\run_committee.py ths status
```

实际交易页面展开并由用户登录后，先对新探测到的**委托 HWND**做只读检查：

```powershell
& $py .\run_committee.py ths inspect --hwnd <实际句柄> --backend win32 --out D:\QuantificationData\trading\ths\controls.json
```

必要时用 `--backend uia` 检查。`profile.example.json` 是**不可直接执行的空映射**，不是本机已适配配置。实际配置放在外部数据目录，填入经本机确认的 ID、页面/账户标签和导出表头；只用脱敏账户别名，不保存密码或完整账户号码。两个后端都不能可靠定位表单时，需要进一步验证本机视觉定位，当前没有声称已完成视觉适配。

账户、委托、成交三个视图分别读取；页面由用户手动选择，命令默认留 5 秒切到前台：

```powershell
& $py .\run_committee.py ths capture --profile <实际配置.json> --kind account --trading-date <当日日期>
& $py .\run_committee.py ths capture --profile <实际配置.json> --kind orders --trading-date <当日日期>
& $py .\run_committee.py ths capture --profile <实际配置.json> --kind trades --trading-date <当日日期>
```

若三张查询页都已准备好，可用一次只读流程依次回读账户、委托和成交；每一步仍须由用户把对应查询页置前，程序不会导航、确认或提交。可选 `--refresh-plan` 会在三次观察保存后刷新同一计划：

```powershell
& $py .\run_committee.py ths capture-session --profile <实际配置.json> --trading-date <当日日期> --account-out <账户观察.json> --out <回读回执.json> --refresh-plan <计划ID>
```

生成的 `<账户观察.json>` 可直接交给 `prepare-opening-pack --account-observation`，避免人工复制账户/现金字段。

开盘只读脚本默认仍只保存摘要、截图和候选证据；需要一次性生成结构化账户/委托/成交观察时，显式追加
`-StructuredSession`。它要求用户依次把三个已映射查询页置前，只读回读并写入该会话的
`structured_bundle`，不会导航、确认、提交或修改 Ledger；累计现金流水仍须单独提供真实导出：

若由单一开盘协调器调用，可在 `run_opening_window.ps1` 后追加
`-StructuredSession` 透传同一只读路径；如已有真实现金流水导出，可再追加
`-CashflowsCsv <现金流水.csv>`，脚本只把副本放入本次结构化观察包；不追加时保持原摘要/截图模式。

```powershell
& 'D:\Quantification\scripts\capture_ths_opening_readonly.ps1' -Profile <实际配置.json> -TradingDate <当日日期> -StructuredSession
```

账户观察与同日原始行情准备好后，开盘包可显式按当前账户重算目标股数；它会复用
`Engine.choose_targets` 的持仓留存/排名缓冲，再用当前总权益、持仓标记价、整手和先卖后买规则
生成唯一下一笔人工复核计划。默认的 `replay_shares` 仍只展示历史回放股数，不能直接照搬到另一账户：

```powershell
& $py .\run_committee.py ths prepare-opening-pack --account-observation <账户观察.json> --pack <opening_pack.json> --strategy <策略ID> --quotes <当日原始行情.json> --sizing observed_account --execution-scope mainboard_only --out <账户重算预览.json>
```

`observed_account` 只生成 `manual_only` 预览；不访问、控制或提交同花顺。
默认 `execution-scope strict` 保留完整候选范围，遇到当前 THS 不支持的板块会停止；
只有明确指定 `mainboard_only` 才会在账户重算前排除非主板候选，并在结果中记录被排除的代码。
如果主板候选不足原策略持仓数，也会停止，不会静默降低持仓数。

保存研究账户的新方法 `saved_fixed_account_next_session_v1` 使用 `--sizing saved_intent`，
不走上述排名重选，也不复制研究账户股数。先用 `stockdb-saved-intent --workflow <持久日更目录>
--signal <策略ID> --execution-day <下一交易日> --out <新意图.json>` 导出准确保存的目标。
再把它交给现有入口：

```powershell
& $py .\run_committee.py ths prepare-opening-pack --account-observation <同期账户观察.json> --pack <opening_pack.json> --strategy <策略ID> --saved-intent <新意图.json> --sizing saved_intent --quotes <带账户身份的原始行情.json> --out <研究账户差异.json>
```

该分支只作离线研究比较：`NO_CHANGE` 保持观察股数，仅明确的未退出残仓继续以零为目标；
`FULL_TARGET` 按保存权重与观察权益换算，保留现金、整手及 T+1 限制。
不变且没有残仓退出时可省略行情；发生调整必须使用同账户、同执行日的新鲜原始行情 JSON，
不能使用无账户身份的 CSV。CLI 必须显式提供账户观察文件，不初始化日志；
结果 `next_plan=null`、`prefill_allowed=false`，诊断订单不能保存为预填计划。
研究意图与同花顺实际持仓、委托或成交不能相互替代。

八类龙头的32个独立研究账户可用 `leader-eight-type-opening-pack --run-dir <原forward目录>
--execution-day <准确下一交易日> --out <新输出目录>` 接入同一开盘纸面包。
它读取各账户已保存的组合目标与退出队列，保留各自现金、持仓和已封存来源；
未就绪账户单独列原因，不补成空仓或保持不变。各预览自带保存意图，后续用上述
`prepare-opening-pack --strategy <case_id> --sizing saved_intent` 作单账户诊断，
无需再传附件；32个研究账户不会合并为个人账户或自动选择一个版本。

原20组龙头账户和冻结 Alpha158 账户复用同一生产入口：
`published-account-opening-pack --source-family leader20|alpha158 --run-dir <原forward目录>
--execution-day <准确下一交易日> --out <新输出目录>`。各策略标识和独立账户保持原样；
含冒号的标识使用稳定的目录名映射。Alpha 的原日志只嵌在 head 内，输出明确注明这一范围，
不会把它称为另存封存日志或补造历史记录。该命令只读取保存结果和原价，不评分、训练或推进账户。

四条已接通路线的纸面包现在由 `opening-daily` 统一管理，已登记目录为
`D:/QuantificationData/trading/research/opening_daily/20261017_v1`。首次登记已采用原有包，
不要重复 `register`。日常读取用 `opening-daily status --run-dir <上述目录>`；研究账户保存状态
真实更新后用 `opening-daily run --run-dir <上述目录>`，它只生成变化路线的包，同一状态直接复用。
这里不调用固定策略旧 `status`，也不推进策略、创建账户或访问同花顺。
相同来源失败不自动重试；如已有成功产物而指针提交中断，使用
`opening-daily recover --run-dir <上述目录> --route <准确路线ID> --input-id <准确记录ID>`，
只接纳保存结果，不重新生成。各路线的日期、缺项和旧包过期状态分别展示。

不能直接读取的表格使用 `import-export`，指定真实导出时间，不用导入时间冒充新鲜行情。`tables` 映射必须与导出的实际表头一致，不能照抄示例的英文名称；同时在配置中显式声明 `delimiter: comma` 或 `delimiter: tab`，不同表可用 `delimiters.orders/trades/cashflows` 单独覆盖，程序不会根据内容猜分隔符：

```powershell
& $py .\run_committee.py ths import-export --profile <实际配置.json> --kind account --csv <持仓.csv> --observed-at <实际观察时间及+08:00> --trading-date <日期> --cash-rmb <同期可用资金> --encoding gb18030
```

需要现金核对时，在计划生成前的账户导入和成交后的账户导入中，另外传入
`--cash-total-rmb <含冻结的证券资金余额>`、`--cash-frozen-rmb <同期冻结资金>`。
经实机核验的账户控件映射也可同时声明 `views.account.cash_total/cash_frozen`；
两者必须成对，现有仅可用资金映射仍可读取，但不升级为现金核对证明。

现金流水目前只接受真实导出，不支持 `capture --kind cashflows`。在实际配置的
`tables.cashflows` 声明 `basis: cumulative_order_cashflow`，并为以下字段逐一映射
真实列名：`symbol, market, side, order_id, gross_amount, fees, cash_change`。
每个合同号必须恰好一条截至导出时的累计记录，`fees` 是该合同实际总费用，
`cash_change` 是带正负号的净资金发生额；不自动拼分项费用、合并未知业务或猜测列义。
逐笔流水、费用尚未落账或没有上述字段的导出不能冒充这种累计表。

```powershell
& $py .\run_committee.py ths import-export --profile <实际配置.json> --kind cashflows --csv <实际累计现金流水.csv> --observed-at <实际导出时间及+08:00> --trading-date <日期> --encoding gb18030
```

然后读取更新的账户页再 `reconcile`。同日计划核对仍不靠分红、转账或持仓差异
反推费用；这些差异必须有单独证据。历史计划中的
`reconciliation_interval_end` 说明下一计划已经开始；旧证明不是当前账户余额。
已绑定账户进入下一交易日时，核对成功的新基线也会封闭上一日的余额证明区间，
但仍检查当前回读的旧成交/费用是否修订。

下一交易日的非交易流水仅接受用户真实导出，不能用 `capture` 读取。
实际配置的 `tables.movements` 必须声明如下含义；`columns` 的值和 `kinds` 的键
分别替换为真实表头和业务名称，不得猜测。流水号保留前导零且每个交易日唯一：

```json
{
  "basis": "nontrade_cash_movements",
  "currency": "CNY",
  "amount_unit": "rmb",
  "columns": {"movement_id": "流水编号", "kind": "业务名称", "cash_change": "资金发生额"},
  "kinds": {"现金分红": "cash_dividend", "利息": "interest", "税款": "tax", "费用": "fee", "资金存入": "deposit", "资金转出": "withdrawal"}
}
```

`columns` 的键固定为标准字段，值是实际导出表头；`kinds` 的键是实际业务名称，
值固定为上述支持类型。分红/利息/存入为正，税费/转出为负，零值或未知业务停止。

```powershell
& $py .\run_committee.py ths import-export --profile <实际配置.json> --kind movements --csv <实际非交易资金流水.csv> --observed-at <实际导出时间及+08:00> --trading-date <下一交易日> --encoding gb18030
```

随后读取更新的下一交易日账户页再 `reconcile-session`；必须先有新的空委托/成交
视图以及重读的上一日终态/成交/费用视图。不要在原账户上照此执行，直到实际字段、
模拟账户身份和本机权限核验完成。

明确的人工确认计划，或当前策略的目标股数（字段见 `target.example.json`）：

2026-10-02：`plan-portfolio` 接受完整组合，顶层字段为 `mode: current_preview`、
`strategy_id`、真实带时区的 `generated_at`、`account_alias` 和 `targets` 数组。
每项包含 `symbol`、非负整数 `target_qty`；需调整的证券另须
`limit_price_rmb` 和 `price_basis: raw_rmb`。当前所有非零持仓必须明确列出，
缺项不会默认为清仓。未调整的 ETF 等持仓可保留，但不支持其交易计划。

按证券代码确定性排序，先卖后买；只给出下一笔计划，其余明确等待。
首笔受 T+1、零股或资金约束时停止，不自动缩量、跳过或预计卖出回款。
默认仅输出预览；`--save-next` 只写入既有 `ths_plans`，不控制窗口或提交。
下一笔须重新消费已更新的账户观察与原有委托核对结果，不保存整批可执行订单。
这是隔离合成账户及实际 CLI 验证的组合入口，**不是本机联调或策略交易准入**。

```powershell
& $py .\run_committee.py ths plan-portfolio --account <别名> --portfolio <完整当前组合.json> --out <预览.json>
& $py .\run_committee.py ths plan-portfolio --account <别名> --portfolio <完整当前组合.json> --save-next --out <单笔计划回执.json>
```

2026-10-02：`plan-weights` 将完整当前权重组合转换为上述绝对股数入口，
示例结构见 `weights.example.json`，占位符不能直接执行，也不是买入建议。
顶层另须 `weight_unit: equity_fraction`；每项使用 `target_weight`，而非股数或百分数。
所有非零持仓须明确列出，退出使用零权重。现阶段整个权重组合仅限沪深主板；
单证券上限20%、总仓位上限80%，超限拒绝，不静默裁剪。

权益来自账户含冻结资金的实际余额，加上全部持仓在该观察时间的 `marks` 估值。
缺总余额、冻结余额或任何持仓估值均停止，不能用可用资金、成本价或委托限价代替。
已有持仓以账户标记价计算股数，不另传 `quote`；新入选的正权重证券必须提供同代码、
当日、不复权人民币的 `quote` 及实际观察时间。限价仍单独用 `limit_price_rmb`。
账户、策略及新行情最长5分钟有效，旧行情不会因保存计划而续期。
复用引擎原有整手向下取整、既有组合先卖后买及未决计划保护，不预支卖出资金。
隔离合成账户实际 CLI 已运行；没有本机读取、预填、委托或交易准入。

```powershell
& $py .\run_committee.py ths plan-weights --account <别名> --weights <完整当前权重.json> --out <股数及计划预览.json>
& $py .\run_committee.py ths plan-weights --account <别名> --weights <完整当前权重.json> --save-next --out <下一笔计划回执.json>
```

2026-10-02：`plan-signal` 接入现有 `SignalFrame`，不再人工抄写选仓权重。
必须指向已明确绑定模拟券商账户的同一暂停 `paper` 运行；读取该运行的配置、
当前已核对账本及最新账户观察，不创建第二账户，也不调用日终模拟撮合。
评分批次的账户/模式必须匹配，日期只能是当前交易日的前一固定日历交易日。
`historical_replay`、`research:` 来源及历史日终可用性假设不能改名后用于此入口。

输入 JSON 顶层为 `mode: current_preview`、`account_alias`、`strategy_id`、
新鲜 `generated_at`、现有 `SignalFrame.to_dict()` 生成的 `signal_frame`，以及
`prices` 数组。`strategy_id` 必须等于批次 `source`。信号使用明确可用时间的
降序 `ranking_score`；预期收益须另有组合政策，不能直接当排名分数。
`prices` 每项为 `symbol`、`price_basis: raw_rmb`、必要的 `limit_price_rmb`，
新入选证券另含与 `plan-weights` 相同的当日新鲜 `quote`。
这些价格输入须恰好覆盖入选证券及所有已有持仓，不含未用报价或重复项。

2026-10-06：`build-signal-request` 新增 `--score-bundle`。该输入必须是
`ths_current_eod_score_bundle_v1`，同时提供显式 `source` 回执：
`kind=current_eod`、`status=validated_current_eod`、匹配的 `decision_day`、
带时区的 `observed_at`，以及 `execution_admitted=false`。回执会保留在
`current_preview` 并在保存计划前再次校验。这样历史研究导出不能仅靠改名
进入当前预览；该入口仍不读账户、不访问客户端、不提交委托。若没有经过
独立验证的当前源，不应制作或填充 bundle。

复用引擎 `choose_targets` 的持仓数量及排名缓冲，并按运行起始交易日维持
原调仓间隔。非调仓日输出 `NO_CHANGE`；缺失批次停止，不能当作清仓。
调仓日的显式空批次才是全现金目标；落选持仓的零目标来自完整组合语义，
不是手工组合漏填。继续复用权重估值、整手换算、先卖后买及单笔计划入口，
沿用其主板范围；仓位上限及费用预算读取绑定运行的 `StrategyConfig`，
而不是在选股后退回独立权重入口的默认20%/80%与默认佣金。保存时在同一事务内
重核配置、固定日历及账户观察；预览后变化须重新生成，不保存陈旧计划。
默认只预览，`--save-next` 只写一笔计划；不更新账户、模拟成交或控制客户端。
隔离合成账户的实际 CLI 已验证，**不是当前策略获准或真实本机联调**。

2026-10-04：绑定配置已贯穿权重、股数、原Ledger现金预留及计划回读。
计划保留 `execution_config`，与选股配置及绑定运行不一致时停止并要求重生成。
旧独立计划缺该字段时仍按原默认配置检查；非默认绑定信号旧计划不能静默迁移。
已用隔离 CLI 预览、单笔保存和 `show` 回读验证自定义上限与费用，无客户端访问。

现有“策略与目标”页已为绑定模拟运行接入同一个信号预览消费者。
`POST /api/trading/runs/{run_id}/broker/signal-preview` 接受 `signal_request`，
内容与 CLI 的信号/报价 JSON 相同；返回完整调仓清单、约束与下一笔预览。
文件错误、接口拒绝或账户/配置变化会清掉旧结果，过期预览不能继续下载。
网页不保存计划、不预填、不提交，不访问客户端；账户/日历/信号/报价保护仍在。
隔离实际 HTTP/浏览器已验证桌面、手机与下载，唯一合成账本未改，非本机联调。

2026-10-05：现有权重入口新增可选 `observed_stock_lot_tracking_v1`，复用研究
开盘配置器的纯整数计算，直接把股数交给原单笔计划消费者。使用的是观察到账户
标记价及显式当前原价报价，不称实际开盘价。原权重总预算/单股上限、冻结现金、
费用、T+1、报价有效期及先卖后买仍生效；未售持仓不会预先贷记可用资金。
独立 `plan-weights` 的 JSON 可显式给出 `lot_allocation`；省略仍按原向下取整。
绑定信号政策只能在尚未演进的运行首次 `bind-ledger` 时用
`--lot-allocation observed_stock_lot_tracking_v1` 固定，旧绑定不迁移。
`plan-signal` 继承绑定政策；显式不同政策及保存时政策变化均拒绝。分配顺序
沿用原选择器，退出持仓显式为零，不按代码排序更换配置的同分优先级。
原历史 open/close 配置器依然仅限 REPLAY；这不放宽主板范围或自动提交权限。
实际隔离 CLI 完成绑定、预览、单笔保存、show 回读、拒绝重发/改政策，唯一账本
现金和持仓未改。合成14万元情景分配11万元只是输入路由证明，不是已核验账户、
新行情、策略收益或本机联调。实物见研究目录 `ths_observed_lot_20261005`。

2026-10-04 后续：订单页增加“保存的计划与回读”，通过只读
`GET /api/trading/runs/{id}/broker/history?limit=20&offset=0` 分页读取绑定模拟账户
的原 `ths_plans`，不调用客户端刷新、回读入账或委托接口。保留冲突前确认的
成交、最新核对失败、上次通过的实际累计费用及其时间；未知不显示为零。
“已观察全部成交”“现金持仓匹配”和原唯一账本“已入账”分别显示，最后一项
必须有属于该运行的原入账标记。网页可查看、重读和下载保存记录，没有修改按钮。
空记录、22条合成历史分页、冲突详情与桌面/手机下载已实际验证，唯一账本未改；
这仍不是本机字段、券商模拟成交或真实账户验收。

```powershell
& $py .\run_committee.py ths plan-signal --run-id <同一已绑定暂停模拟运行> --signal <当前策略批次及报价.json> --out <组合预览.json>
& $py .\run_committee.py ths plan-signal --run-id <同一已绑定暂停模拟运行> --signal <当前策略批次及报价.json> --save-next --out <下一笔计划回执.json>
```

```powershell
& $py .\run_committee.py ths plan --account <别名> --symbol <六位代码.SSE或SZSE> --side <buy或sell> --price-rmb <未复权人民币限价> --qty <股数> --raw-rmb-confirmed
& $py .\run_committee.py ths plan-target --account <别名> --target <当前目标.json>
& $py .\run_committee.py ths show --id <计划ID>
& $py .\run_committee.py ths prefill --id <计划ID> --confirm-plan <同一计划ID> --profile <实际配置.json>
```

`prefill` 返回 `awaiting_manual_submission` 后不再操作窗口。用户自行检查决定是否提交。只有用户确实提交并获得合同号后，才执行绑定；重新读取委托/成交再核对：

```powershell
& $py .\run_committee.py ths bind --id <计划ID> --contract-id <实际合同号> --trading-date <日期>
& $py .\run_committee.py ths reconcile --id <计划ID>
& $py .\run_committee.py ths pause
```

暂停只阻止后续预填写入，**不撤销券商订单**。`resume` 解除暂停，但不重试旧计划。未尝试的计划可用 `discard` 放弃；已尝试预填但确实从未提交的，用户须先清空表单、暂停，再用 `close-unsubmitted --id ... --confirm-plan ... --form-cleared-and-never-submitted` 明确确认。不能用它消除已绑定委托或仍在执行/进程中断的未知状态。

若预填进程被强制结束，先暂停，再用 `show` 查看该计划保存的
`result.prefill_owner.attempt_id`。`recover-prefill` 核对准确尝试的PID和进程创建时间，
只有确认原进程已终止才将 `prefilling` 转为 `prefill_unknown`：

```powershell
& $py .\run_committee.py ths pause
& $py .\run_committee.py ths show --id <原计划ID>
& $py .\run_committee.py ths recover-prefill --id <原计划ID> --confirm-plan <原计划ID> --confirm-attempt <保存的attempt_id> --out <恢复回执.json>
```

恢复不会操作窗口、重填或清空表单，不会认定从未提交，也不改Ledger。
后续仍需检查实际表单和券商回执：有合同号走原 `bind` 和对账；只有用户已确认
清空表单且从未提交，才走原 `close-unsubmitted`。原进程仍存活、进程身份无法核实、
旧记录没有进程身份时均拒绝恢复；等待时间长不能证明进程已停。
恢复后旧尝试的迟到结果也不能覆盖已保存状态。

保持桌面解锁并串行操作。仅加载本机检查过的配置；不要把未核对的互联网控件配置用于账户。正式启用范围及券商要求仍需由用户与实际券商确认。

## 验证与参考

```powershell
& .\webapp\.venv\Scripts\python.exe -m pytest trading/tests webapp/backend/tests quant_learning_starter/tests -q
uv pip check --python .\trading\ths\.venv\Scripts\python.exe
```

单元测试用假窗口，不会向真实客户端输入数据。真实客户端当前只完成进程与只读控件探测，不能用测试通过代替实际联调。

- [easytrader 源码](https://github.com/shidenggui/easytrader)：参考读取与控件设计，没有直接实例化其交易客户端。其 `buy/sell` 会提交，`connect`/弹窗辅助也可能操作界面，故未调用。
- [pywinauto 接入说明](https://pywinauto.readthedocs.io/en/latest/HowTo.html)
- [桌面会话限制](https://pywinauto.readthedocs.io/en/latest/remote_execution.html)
