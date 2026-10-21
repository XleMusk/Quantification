# 首轮量价因子研究

复用本地 Qlib 0.9.7、LightGBM 4.6.0、BaoStock 历史快照和 `trading.engine/ledger`。不下载新框架，不启动交易，不改变同花顺适配。

```powershell
& D:\Quantification\env\python.exe -X utf8 D:\Quantification\run_committee.py factor-mine
& D:\Quantification\env\python.exe -X utf8 -m unittest trading.research.tests.test_factor_mining -v
```

默认配置 `pilot.json`：2024训练、2025验证、2026测试；约200模板，验证增量筛选最多5个因子，三个模型种子。配置在结果计算前已写定。B0为158个原生特征，B1为B0加筛出的候选；保持同一数据、标签、预处理、训练预算和交易规则。B2遗传搜索本轮不运行。

研究依赖测试使用现有主环境，不要求网页服务的独立环境也安装Qlib。报告在 `D:\Quantification\outputs\factor_mining`，模型、数据与账本在 `D:\QuantificationData\trading\research\factor_mining`。仅写新的研究目录，不覆盖旧模型、`latest`信号或活动账户。

首轮已完成：`D:\Quantification\outputs\factor_mining\20260926_154108_b5bf3062\REPORT.md`。
**实际结论：两个候选通过筛选用种子的验证增量标准，但测试期平均净收益下降，未证明能改善原策略。**详见同目录 `POST_RUN_REVIEW.md`。

固定模型诊断，不重训不重选：

```powershell
& D:\Quantification\env\python.exe -X utf8 -m trading.research.diagnostics --run D:\Quantification\outputs\factor_mining\20260926_154108_b5bf3062
```

完整Alpha158公式见 `alpha158_catalog.csv`，易解释的35个特征见 `interpretable_35.csv`。例如BETA30是价格对时间的归一化斜率，不是市场Beta；RETVOL是额外计算的收益波动，与Alpha158的STD价格离散度不同。

## 固定名单的基准比较

250只历史主板名单全部齐备并通过 `market-snapshot --fixed-hs300` 后，可以直接比较原生Alpha158-LightGBM与既有规则，不再次生成或筛选200个候选：

```powershell
.\env\python.exe -X utf8 run_committee.py factor-mine --baseline-only --dataset <完整固定名单快照>
```

沿用 `pilot.json` 的模型参数、2024训练/2025验证/2026评价划分、17/29/43三个种子和同一组合账本。该模式只训练B0，不运行B1、B2或新因子搜索；不接受部分下载快照。`paired_deltas.csv` 此时明确表示各B0种子相对RULE的差值。配置和实际快照记录在新结果目录的 `run.json`，不覆盖十股试点或活动策略。2026区间已被查看，仍不称为新的未触碰测试集。

对全部已完整取得的多板块历史，另有明确标注的数据接入诊断模式：

```powershell
.\env\python.exe -X utf8 run_committee.py factor-mine --baseline-only --ingestion-only --config trading/research/multiboard_baseline_20260930.json
```

该配置固定为395只不可变快照、假设14万初始资金，其他模型参数、三个种子和时间分段沿用既有基准。
只计算原生158个特征，不搜索新因子、不调优，模型和规则都进入现有账本。
未显式指定 `--ingestion-only` 时，原有完整固定名单要求仍然生效。
诊断样本是按取数队列获得的部分股票，不是PIT或具有代表性的全市场验证；
不读取模拟客户端资金，不发布当前信号，不准许交易，也不将已查看的2026年称为独立未见测试。

后续已完成747只股票的历史接入快照，包含主板525、创业板122、科创板100只：
`D:\QuantificationData\trading\datasets\ashare_2023_20260924\snapshots\20260930_151813_bc518ac8`。
该目录通过实际 `HistoricalFeed` 检查，有601824行行情和2234条公司行动。
三处回放期“无原股东权益”的重整只校验除权参考价，不给账户增加送股或现金；
688101的缺失现金分红另有原公告补充。回放期配股、未知股东分配和未核实事件仍不支持。
此前395只模型比较的配置和结果未换样本；747只也不是5367只全市场或PIT股票池。
本轮核验见 `outputs/architecture_v2/ACTION_RECONCILIATION_20260930.md`。

最初的10只历史选定股票试点是小样本；上述395只诊断也不是全市场或PIT股票池。供应商历史修订、股票池范围、日频撮合和多重搜索限制均保留。测试期此前有规则策略汇总被查看，不能称为从未接触的外部样本。后续不得一边看它的结果一边改公式，却继续声称测试未见。

## MASTER市场输入

取沪深300、中证500、中证800三指数历史，使用已归档的作者README中第一份完整公式列表，
由本地Qlib计算63个市场特征；指数点位不进入股票现金/份额账本。

```powershell
.\env\python.exe -X utf8 run_committee.py market-indices --stock-features D:\QuantificationData\trading\research\factor_mining\20260930_113402_ad490a81\features_and_candidates.parquet
```

输出在新的 `D:\QuantificationData\trading\research\market_indices` 子目录。
已有目录可用 `--folder <目录>` 消费；已完成的指数取数和63维计算不会重复。
`--stock-features` 只接受现有原生Alpha158列，并保留输入的所有股票行，不按未来标签筛选。
实际首批输出为905日×63列市场特征，以及215490行×221列、352只有效股票的原始拼接输入。
这仍来自395只部分取数样本；并非667只、CSI800成分或全市场验证。
没有训练、拟合归一化、八日序列采样、当前信号或交易授权，不能称为MASTER策略复现完成。

## MASTER 本地适配与同账本诊断

```powershell
& D:\Quantification\env\python.exe -X utf8 run_committee.py master-replay
```

默认配置为 `master_replay_20260930.json`。只加载核验过的官方架构定义，
不执行作者训练入口或读取旧外部 pickle。个股与指数分别在2024训练期拟合
Qlib RobustZScoreNorm，截断至正负3后填零。序列包含连续八个交易日，
使用固定提供者的2023热身历史；不能取“最后八条可用观测”或向后填充。
训练标签按分段末日净化，尾部过滤只用于训练；验证与预测保留全部当天可选股票。

固定版本沿用现有五日开盘收益标签、14万元和五股/五交易日调仓账本，
不等同于论文的CSI股票池、收盘标签、30股组合或收益复现。
默认单种子17，最多40轮或900秒，仅完整轮训练损失不高于0.95时停止。
没有达到条件时保存诊断权重及未达标状态，不另选验证集最佳模型。

实际运行 `20260930_140541_18385d3c` 在4090 Laptop GPU完成16轮，
训练日236、最后MSE 0.948425。2025验证RankIC 0.02313，2026诊断RankIC
0.06100；预测仍经 `ScoreFeed/replay` 进入唯一账本。178日净收益-5.75%，
最大回撤9.63%，夏普-0.60，557笔成交，手续费3213.40元。
数据、权重、归一化参数、逐日预测及账户流水在外部
`D:\QuantificationData\trading\research\master\20260930_140541_18385d3c`。
首轮归一化兼容错误保存在 `20260930_140220_19b06565`，未开始训练。
395只非PIT部分取数样本、已经观察过的2026及单种子均不能充当独立验证。
这次完成的是端到端工程适配，不是可交易策略，仍为NO_TRADE。

## 全本地市场覆盖检查

从本地 StockDB 服务只读采集股票、北交所和基金代码段日线，固定原始响应及哈希；**不直接提升为 `HistoricalFeed`**：

```powershell
& D:\Quantification\env\python.exe D:\Quantification\run_committee.py full-market-collect --start 2024-01-01 --end 2026-09-24
& D:\Quantification\env\python.exe D:\Quantification\run_committee.py full-market-audit --snapshot <不可变采集目录>
& D:\Quantification\env\python.exe D:\Quantification\run_committee.py full-market-screen --snapshot <不可变采集目录>
```

2026-09-28 的实际快照、覆盖核验、10组信号诊断及未通过的策略门槛见 `D:\Quantification\outputs\full_market\20260928_011314_1aadfbef\REPORT.md`。诊断输出只有因子排序与未扣成本的未来五日组间差，不是账户收益率或回撤。基金代码段缺前复权与可靠历史分类；ETF 补采命令 `full-market-etf` 可续跑，但已记录外部代理失败，需先恢复数据源并核验口径，不应直接重复失败请求。

ETF 新浪历史补采可新建独立来源目录，并复用已保存的候选代码清单：

```powershell
.\env\python.exe -X utf8 .\run_committee.py full-market-etf --provider sina-ohlcv --master-folder D:\QuantificationData\trading\research\etf_adjusted\20260929_103137_e9588553 --max-symbols 20 --workers 1 --request-interval 2 --transport-failure-limit 3
```

后续只用输出目录的 `--folder` 续采；来源、期间和候选清单在创建时固定。
该来源保存 2023 年至 2026-09-24 的 OHLCV 及原始响应，不执行网络返回的
JavaScript，也不改原来的东财前复权目录。它不提供成交额、历史停牌标记、
可靠复权或证券权限。当前名称筛选也不是历史时点 ETF 范围。取得数据不代表
分红、拆分、成交量单位、品种交易规则或账户回放已通过，仍不能直接加入交易。
`stockdb-audit` 会报告成交额/成交量均价超出 OHLC、缺失和非有限比较字段；
即使某日价格一致，也不能据此自动换算另一日的成交量单位。

已取得的ETF缓存可只读检查报价精度，不重新下载、不产生回测或委托：

```powershell
.\env\python.exe -X utf8 .\run_committee.py etf-quote-audit --folder D:\QuantificationData\trading\research\etf_sina_history\20260930_121238_800642f1
```

实际完成结果在 `D:\QuantificationData\trading\research\etf_quote_precision\20260930_144004_812ab4b3`：
1562个文件、971860行、3887440个OHLC报价，其中3405355个报价有分以下精度，
没有偏离0.001元价格步长的报价。原始文件未改写；失败的589000仍保留。
现有唯一账本已区分整数分现金与整数厘报价，但仅实现明确指定的510300场内T+1模拟规则。
完整ETF复权、成交量单位、历史证券状态与分类尚未准入，不能把此次精度检查称为ETF回测。
规则来源、费用假设、兼容结果见 `outputs/architecture_v2/QUOTE_PRECISION_RESULT.md`。

510300 可使用两份已保存来源做只读核对，不重复下载其他 ETF：

```powershell
.\env\python.exe -X utf8 run_committee.py etf-reconcile --sina-folder D:\QuantificationData\trading\research\etf_sina_history\20260930_121238_800642f1 --tencent-folder D:\QuantificationData\trading\research\etf_source_reconciliation\20260930_152929_8c99a4ee --calendar D:\QuantificationData\trading\datasets\ashare_2023_20260924\calendar.csv
```

实际输出 `D:\QuantificationData\trading\research\etf_reconciled\20260930_154707_441d1ca6`：
905日两源OHLC全一致，成交量与金额的候选单位关系全期吻合；只适用于该文件及510300，
不是独立交易所单位证明。发行人2023—2025年报的利润分配表已逐年对上现金事件，
2026全期分红完整性仍缺证据。输出有逐日原值、单位残差和参考价候选，故意不生成
历史交易状态或可执行快照。来源及限制见 `outputs/architecture_v2/ETF_SOURCE_RECONCILIATION_20260930.md`。

## 全市场 Alpha158 与本地模板

下面两步只使用上述不可变股票快照和本地 Qlib 0.9.7；输出是 StockDB `pct_chg` 研究代理，不进入 `trading.engine/ledger` 或同花顺委托。

```powershell
.\env\python.exe -X utf8 run_committee.py full-market-alpha158 --snapshot D:\QuantificationData\trading\research\full_market\20260928_011314_1aadfbef --group sh_sz_all_equities
.\env\python.exe -X utf8 run_committee.py full-market-alpha158 --snapshot D:\QuantificationData\trading\research\full_market\20260928_011314_1aadfbef --group bse_equities
.\env\python.exe -X utf8 run_committee.py full-market-templates --alpha-run <已完成的对应市场 Alpha158 输出目录>
```

2026-09-28 的四批结果在 `outputs/full_market_alpha158` 和 `outputs/full_market_templates`：沪深 5,284 个观测代码、北交所 310 个代码，每组均完成 158/158 个 Qlib 原生因子和 200/200 个本地挖掘模板的两方向、分段诊断。各批仅用前段指标选最多五个候选，再做相同的 80% 股票仓位、五股、成本代理回放；20 个候选的全年夏普全为负，零个通过暂定历史夏普/回撤门槛。此处不是 716 个方向都经过账户回测，更不是独立测试集或可交易策略。完整 WorldQuant 101 所需的当期市值、行业分类在此快照中缺失；本地 SDK 虽导出对应接口，2026-09-28 对单股单日各做的一次只读查询均返回 HTTP 500，尚不能认定其历史数据可用。ETF 分红和收益历史也未核准，均未计入“完成”。

## 财务披露日滞后视图

`financials.py` 采集 BaoStock 盈利能力季报，保留原始字段及空返回，不换算未核实的单位。
复用已有财务采集目录续跑，不重新创建股票池或采集已成功的季度：

```powershell
.\env\python.exe -X utf8 run_committee.py financials --folder D:\QuantificationData\trading\research\financials\20260930_162448_8be1e603 --execute --max-symbols 8 --build-view
```

该目录固定5367个供应商保留的沪深历史代码、2022Q4至2026Q2共15个季度，含146个后来退市代码。
截至2026-09-30 19:03，累计100个代码完成全部季度请求：1187条非空季报、313次空返回。
另两条已导入探测记录尚未达到完整季度覆盖，不混入日频视图。
新输出 `views\20260930_190255_6e5a3d8a\publication_lagged.parquet` 有90500行，
74703行有可用报告；此前8及68代码旧视图仍保留，银行等缺失财务字段仍为缺失值。

已有研究股票池的日度上市/退市、ST、停牌与成交额约束，现在有真实消费者：

```powershell
.\env\python.exe -X utf8 run_committee.py financials --folder D:\QuantificationData\trading\research\financials\20260930_162448_8be1e603 --build-view --align-market --market-snapshot D:\QuantificationData\trading\datasets\ashare_2023_20260924\snapshots\20260930_185149_d95d99f8
```

每次生成新的不可变视图和对齐子目录。实际对齐 `190312_84c2996d` 有36922个可用日度行、
72个代码；24228行缺行情，37781行有报告但不在研究股票池，保留记录并屏蔽财务特征。
不补造交易状态，不把研究股票池当成账户可下单权限。

只有 `pubDate` 严格早于决策交易日才可读取，禁止季度末或披露当日提前使用；
迟披露的旧季度不替换新季度，报告超过统计期末400天不再沿用。
供应商可能回填修订值，因此这是**按披露日期滞后的供应商视图，不是严格PIT财务库**。
基础财务视图不提供证券可交易状态；上述对齐使用供应商历史行情，不能直接作为当前信号。
白云机场、澜起科技、万科2024Q1的原始财报已保存并核对相关报表页，15项数值对照吻合。
这些样本的`netProfit`包含少数股东损益，不是归母利润；万科两者符号相反。
`roeAvg`吻合归母利润除以期初/期末归母权益算术均值的样本假设；不可直接改称加权ROE。
万科`totalShare`包括A/H股和库存股，不是A股流通股数；`MBRevenue`空值不补成营业收入。
三例不证明全库单位、版本历史或`epsTTM`分子/股数口径。证据在原采集目录
`field_checks\20260930_q1_three_issuers\result.json`，未修改任何供应商值。
当前没有财务因子筛选、模型训练或收益比较。实际覆盖及原报表核对见
`D:\Quantification\outputs\architecture_v2\FINANCIAL_DATA_RESULT_20260930.md`。

采集会话在供应商关闭连接时停止，写明失败而不是无限接收或伪造空报告；成功季度仍复用。
最新875代码固定规则回放及未完成的零碎股权益问题见
`D:\Quantification\outputs\market_replay\20260930_175747_ec8fe67c\REPORT.md`。
