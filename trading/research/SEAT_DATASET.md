# A股公开席位披露研究库

## 范围

目标是保存当前供应商可返回的全市场历史龙虎榜，不预先按名人、胜率或存续股票筛选。原端点混有B股、可转债/可交换债；原始层保留，`security_registry` 按市场和代码分类，A股分析使用 `a_share_seat_events`，不会把债券混成股票。
首次全范围查询从1990-01-01开始，实际最早返回2004-06-25，截止2026-09-24。
这是供应商披露记录的边界，不是“2004年前没有游资”，也不是全市场所有交易的边界。

**公开席位数据不能还原任何个人的完整交割单、全部账户、真实资金规模、买入成本或未披露持仓。**
所有人物关联都是独立的有来源假说；媒体称呼不等于交易所确认。没有再次上榜不代表清仓或仍持有。

目录：`D:\QuantificationData\trading\research\seat_trend\public_history_2004_20260924_v2`。
旧目录保留原件，但买卖方分页仅按日期、股票排序，实测存在跨页重复，不能用于完整性结论。v2增加榜单标识、营业部代码和买卖额排序；股票汇总复用已完成唯一性检查的原始页，席位明细重新取得。

## 数据结构

| 文件/表 | 内容 | 注意事项 |
|---|---|---|
| `plan.json` | 全范围查询、源端行数、分页数、最早返回日期 | 计划量不是已下载量 |
| `status.json` | 已入库量、剩余量、错误、覆盖状态 | `partial_backfill` 不能写成全量完成 |
| `summary_integrity.json` / `seat_integrity.json` | 分页、行数和跨页重复检查 | 仅验证本供应商快照，不证明交易所历史披露完整 |
| `overlap_reviews/{buy,sell}/*.json.gz` | 跨页重复所涉整个榜单的独立定向查询原件 | 仅当完整事实及重复次数一致时，才能解释匿名机构的重复列示；不删除原始行 |
| `raw/{summary,buy,sell}/*.json.gz` | 原请求、原始响应、采集时间 | 含供应商事后统计字段，禁止直接当历史特征 |
| `disclosures.sqlite3` | 可查询本地索引 | 无账号、密码、交易下单路径 |
| `stock_disclosures` | 股票、名称、理由、单日/多日类别、金额 | 一只股票同日可有多种榜单 |
| `seat_observations` | 每一条买方/卖方列示、全称、原代码、买卖额、原文件 | 保留方向与重复列示，不把原始行数当成交笔数 |
| `seat_events` | 合并同一可识别席位的相同金额列示，不相加 | 泛称席位保留独立观测；异常冲突另查 |
| `a_share_seat_events` | 明确筛为沪深北A股的披露事件 | 默认按席位/股票导出使用此视图 |
| `security_registry` | 全部出现过的证券及代码类别 | 未识别类型待核对，不自动视为A股 |
| `seat_registry` | 营业部原名与供应商代码、观测起止、股票数 | 起止只表示本库观测范围，不是设立/注销时间 |
| `seat_name_variants` | 同一供应商代码下出现的不同名称 | 仅候选变更线索，不自动认定同一席位/客户 |
| `event_amount_conflicts` | 同一单日席位出现不一致金额的记录组 | 研究前排除或逐条核对，不任取最大值 |
| `person_seat_links` | 人物称呼、报道席位名、来源和时间、有效期与验证状态 | 缺失有效期留空，不用于历史回测身份筛选 |
| `private_trade_documents` | 将来用户主动提供且有合法研究权限的材料登记 | 当前为空，不寻找或推断私人账户 |
| `catalogs/*.csv` | 席位目录、股票清单、人物关联和冲突清单 | CSV带UTF-8 BOM，可用Excel查看 |
| `seat_disclosure_profiles.csv` | 各席位观测股票数、披露股票日、已知买入/卖出出现日 | 不按重叠期间加总金额；不是胜率、持有期或收益 |
| `date_coverage.csv` | 各接口逐日已入库行数和证券数 | 未出现日期不能自动解释成无榜单 |
| `parquet/{stock_disclosures,seat_observations}/year=YYYY/*.parquet` | 仅普通A股的逐年研究文件 | 全部分页到齐且检查通过才发布，保留缺失值和原始列示语义 |
| `stock_inventory.csv` | 股票级汇总覆盖的全部A股，以及已入库席位明细行数 | 行数为0表示尚未取得明细；全部分页到齐后仍为0则须核对源端缺口，不代表历史无交易 |
| `a_share_stock_days_without_details.csv` | 有股票级榜单但本库无对应股票日席位的清单 | 尚未回补和源端缺失都必须显式保留 |
| `stocks_with_seat_details.csv` | 当前确实有席位明细的A股 | 不要把全历史股票目录规模说成席位明细覆盖规模 |

A股金额字段使用人民币元；B股的供应商金额币种未核实，金额保留在原始响应，标准化库的CNY列隔离为空，不假设自动换汇。仅披露一侧时保留已知侧，另一侧和净额保持空值，不补零；这些记录仍属于事件库，但不能进入依赖净额的首版策略。`published_at` 在来源不提供时保持空值。采集时间不能冒充历史发布时间。
`a_share_stock_disclosures` 和 `a_share_seat_observations` 同时排除北交所2021-11-15开市前被源端追溯标记为.BJ的精选层记录；原始表不删除，汇总清单为 `pre_bse_disclosures.csv`。`security_registry` 是源端证券代码类别，不是完整的时点上市状态主表。各股票数量按市场代码计算，不冒充已消除转板/历史代码差异的公司数。
三日“内”的异常波动榜不一定正好覆盖三个交易日，无法确认的区间起点保持空值。
数据库事件层保留多日榜；第一版单日研究只使用可识别、金额有效且无冲突的单日记录。

## 运行

```powershell
# 默认只显示计划，不联网
.\env\python.exe -X utf8 run_committee.py seat-dataset

# 分批回补，默认3个请求线程，全局最多每秒2个请求
.\env\python.exe -X utf8 run_committee.py seat-dataset --max-requests 900 --execute

# 可以单独完成股票级汇总，或单独回补买/卖方明细
.\env\python.exe -X utf8 run_committee.py seat-dataset --kinds summary --max-requests 600 --workers 2 --execute

# 再执行同一命令会跳过已入库页，恢复已下载但尚未入库的页
.\env\python.exe -X utf8 run_committee.py seat-dataset --status

# 离线检查买卖方明细跨页重复，保留全部原始列示
.\env\python.exe -X utf8 -m trading.research.seat_dataset_audit

# 全部分页核对通过后导出按年份分区的研究数据
.\env\python.exe -X utf8 -m trading.research.seat_dataset_parquet

# 更新覆盖报告及全部CSV目录
.\env\python.exe -X utf8 -m trading.research.seat_dataset_report

# 全称精确查询，导出的是披露事件，不是个人交割单
.\env\python.exe -X utf8 run_committee.py seat-dataset --export-stock 000063.SZSE --out D:\QuantificationData\trading\research\seat_trend\exports\000063.csv
```

每次请求都检查源端数量、页数、日期区间和页长。网络等待较长时可用 `--workers 6`（硬上限8），但全局请求频率仍不超过每秒2次。权限、频率限制、内容截断或源端数量变化会停止本次采集，保留已取得的数据，不绕过限制。传输中断或502/503/504服务端临时错误最多冷却5秒后用新连接重试一次，原因写入原始响应元数据；重复失败停止，403/429不重试。
数据仅在本地用于获准研究，不假定拥有重新分发供应商数据库的许可。

## 当前缺口

- 数据回补完成前，不做完整历史人物画像、席位胜率排行或“所有游资”结论。
- 单一供应商完整分页不等于交易所历史披露完整，仍需跨源抽查和逐交易日覆盖核对。
- 人物与全部席位的穷尽关系、私下账户和未上榜交易无法由公开龙虎榜得到。
- 目前只有原10股试点日线经统一账本检查，全市场配套行情、证券历史状态、行业和公司行为尚未补齐。
- 供应商历史行中的简称可能已改成当前名称；名称原样保留，但不标记为已验证的历史时点名称。
- 媒体关联仅为已读取原报道的种子，含群体/风格标签，不等于自然人人数。营业部简称尚未自动映射全称；不是完整人物名录。有效期未知的关系不能用于历史身份筛选。

## 可授权补充的数据源

截至2026-09-26核对的官方文档，Tushare `hm_list` 提供游资称呼及关联机构，要求5000积分；`hm_detail` 提供逐日供应商归类明细，要求10000积分，历史始于2022年8月。当前未调用这两个接口，没有购买权限或寻找密钥。
这类接口只能补充供应商标签和交叉核对，不能证明私人身份、完整持仓或未上榜交易；当前名录不能直接作为过去各年的时点身份表，文档示例也不能冒充实际下载结果。
- [hm_list 官方说明](https://tushare.pro/document/2?doc_id=311)
- [hm_detail 官方说明](https://tushare.pro/document/2?doc_id=312)

## 第一轮策略结果

已完成的10股历史对照位于 `outputs/seat_trend/20260926_155706_ad94365e`。
2025-09-26至2026-09-24，纯Donchian净收益约-22.17%；任意单日龙虎榜过滤约-6.02%（另加一日延迟约-6.82%）。
按前段出现频次选出的席位买入过滤没有交易，净收益0不是盈利证据。卖方过滤未改变纯趋势结果。
保持负结果，不根据这个已看过的测试区间反复更换席位、门槛或名字。

来源：
- [东方财富龙虎榜](https://data.eastmoney.com/stock/lhb.html)
- [AKShare原接口说明](https://akshare.akfamily.xyz/data/stock/stock.html)
- [Tushare席位字段与重复示例](https://tushare.pro/document/2?doc_id=107)
- [财联社2025-05-19报道，人物标签来源](https://www.cls.cn/detail/2035101)
- [上交所九号公司存托凭证上市公告](https://www.sse.com.cn/disclosure/announcement/listing/c/c_20201027_5242851.shtml)：689009单列CDR，不混为普通股票。
- [北交所简介](https://www.bse.cn/company/introduce.html)、[北交所新旧代码对照说明](https://www.bse.cn/service/code_mapping.html)：注意开市日期和精选层历史挂牌日期的区别。
