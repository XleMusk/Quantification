# 近年沪深A股行情库

在既有 BaoStock 接口上增加可续传采集，不改动十股试点、模拟账户、模型或同花顺。

数据根目录：`D:\QuantificationData\trading\datasets\ashare_2023_20260924`。

## 范围与内容

查询区间是2023-01-01至2026-09-24。按供应商保留的上市、退市日期选择期间存在过的沪深普通A股，保留后来退市的股票，也纳入期间新上市股票；不按最新在市名单、收益或知名席位筛选。

首轮实际发现5367个代码：主板3314、创业板1433、科创板620，其中146个在采集时已经退市。这是供应商名录覆盖，不是已经全部下载完成。

- `provider_instruments.csv`：原始名录；名称和当前状态不能冒充历史时点属性。
- `universe.csv` / `excluded_instruments.csv`：目标范围及排除理由。
- `calendar.csv`：该区间交易日历。
- `raw/*.parquet`：不复权OHLC、股数、人民币成交额、换手率、ST、停牌及供应商估值字段。
- `adjusted/*.parquet`：单独获取的前复权数据，不能拿来下人民币委托。
- `actions/<symbol>/<year>.csv`：按实施年份查询的分红、送转等公司行动；成功返回空记录时保留字段，不虚构分红。
- `receipts/*.json`：逐文件采集时间、行数、范围和质量问题；续传不会重新拉取已完成的任务。
- `coverage.csv` / `status.json`：实际完成量、缺口和错误，不把目标规模冒充已入库规模。

## 运行

```powershell
# 离线查看上次覆盖快照
.\env\python.exe -X utf8 run_committee.py market-dataset

# 分批采集；重复执行会继续尚未完成的股票
.\env\python.exe -X utf8 run_committee.py market-dataset --execute --max-symbols 32

# 有明确需求时，可以分开行情和公司行动
.\env\python.exe -X utf8 run_committee.py market-dataset --execute --stage bars --max-symbols 32
.\env\python.exe -X utf8 run_committee.py market-dataset --execute --stage actions --max-symbols 32

.\env\python.exe -X utf8 -m unittest trading.tests.test_market_dataset -q
```

单进程复用连接，不将 BaoStock 的全局 socket 用于多线程。请求开始间隔至少0.5秒，单次socket读取超时30秒；接口错误停止当前批次并保留成果，先诊断再续传。更改日期必须另建快照目录，不覆盖旧查询。

## 不等于交易就绪

源缓存保持 `execution_admitted=false`。没有接入或改变活动账户；经检查的数据可以另存为 `HistoricalFeed` 快照，但不因此获得真实交易资格。

缺失交易日、非正或不一致价格、状态字段异常均保留具体日期，不填零、不前填，也不因质量问题静默删除整只股票。后续仍须验证原始价与复权价逐行对应、公司行动与价格跳变、退市未平仓头寸以及历史交易规则，才可在原有组合账本内扩大回测。

北交所、CDR、完整历史行业与市值、财报实际披露时点、退市结算资料尚未补齐。取得当前名录不能证明历史名称和估值字段完全PIT。不能将复权价格变化直接当作真实分红后账户收益。

复权口径参考：[BaoStock官方复权说明](https://www.baostock.com/helpdocs/pdf/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf)。实际请求和字段实现沿用项目已安装的BaoStock客户端。

## 接入回放

`run_committee.py market-snapshot` 把当前已完整采集的主板数据固定为独立的 `snapshots/<id>`，不改变活动账户或默认行情。它核对原始价与前复权价的日期/代码对应、金额/股数单位、上市区间内缺口、重复分红及未解释价格跳变；失败保留原始记录，不通过剔除问题股票制造成功。

这是采集接入验证样本，不是策略选股池：采集队列含当前上市状态分组，且全市场尚未齐备，不能用这批部分样本证明策略有效。`selection_date` 仅兼容现有引擎的2023年预热/2024年开始回放，不代表已取得历史成分名单。

持仓跨越供应商退市日期，或持仓缺少可用收盘估值时，原有账本现在中止回放，要求补充结算/行情资料，不按旧价永久计值，也不自动清零或假定卖出。已明确停牌且有供应商收盘标记的持仓可以继续记录，但会计入 `stale_valuation_days`。研究回放失败时，`replay.json` 记录失败日，`account.json` 与 `daily.csv` 仅保留最后成功交易日的状态。

退市后可能进入其他转让场所，不能从摘牌日期推定自动现金结算；参见[上交所上市规则第9章](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/mainipo/c/c_20260424_10816589.shtml)。这不构成对其他交易所或历史年份结算规则的补全。

## 固定范围优先补齐

已取得供应商按2023-12-29查询的沪深300名单，共300只，标记更新日2023-12-25；其中250只为当前引擎支持的主板代码。它们保存在 `universes/hs300_20231229`，不根据最新在市状态或收益再次筛选，尚未与交易所/指数公司独立核对。

```powershell
# 名单已保存时只读缓存；首次执行会请求供应商
.\env\python.exe -X utf8 run_committee.py market-dataset --execute --prepare-hs300-priority

# 优先补这250只，仍保留其余全市场目标，不重拉已完成文件
.\env\python.exe -X utf8 -u run_committee.py market-dataset --execute --max-symbols 32 --priority-file D:\QuantificationData\trading\datasets\ashare_2023_20260924\universes\hs300_20231229\mainboard_priority.csv

# 指定已固定的接入验证快照，使用原账本回放三个既有信号
.\env\python.exe -X utf8 run_committee.py market-replay --dataset <snapshot-folder>

# 离线列出固定250只名单的完成量和缺失任务
.\env\python.exe -X utf8 run_committee.py market-snapshot --coverage

# 必须全员齐备且通过数据检查，不按下载进度/退市状态删减名单
.\env\python.exe -X utf8 run_committee.py market-snapshot --fixed-hs300
```

固定名单快照使用 `fixed_preperiod_hs300_mainboard_20231229` 范围标记，可交给同一个 `market-replay` 入口比较三个既有信号。仍不等于滚动沪深300组合或全A股；历史成分来源尚未独立核对。2026区间已在其他样本上看过，结果不得冒充新的未触碰测试集；不根据这些结果调整参数。`equal_weight` 是按代码排序的持仓控制组，不是指数基准。

## 已核对的公司行动补充

128只缓存检查发现供应商漏记两次现金分红。原 `actions` CSV 不改；`supplements/corporate_actions.csv` 单独记录核对结果，并随新快照固定公告PDF。当前仅支持现金分红补缺；若供应商后来补齐同一条，金额和日期一致则不重复计入，冲突则停止。源缓存检查仍保留原始缺口。

- 南山铝业：2025-12-11登记，12-12除息/发放，每股实际现金0.2584元；差异化除息参考额0.2559元不是账户现金。见[公司实施公告](https://static.cninfo.com.cn/finalpage/2025-12-05/1224850617.PDF)。
- 贵州茅台：2023-12-19登记，12-20除息/发放，每股现金19.106元。见[发行人实施公告](https://www.moutaichina.com/mtgf/attachDir/2023/12/2023122716424647784.pdf)。

这两处补充不代表全库公司行动已被独立穷尽核对。账本继续沿用既有20%分红税预留假设，不冒充按每笔持有期精确清算的个人账户税费。

2026-09-26采集曾在同时查询另一份名单期间出现 `10001001` 会话失效。错误快照保存在 `session_failure_20260926_183332.json`。现在 `baostock_session()` 使用操作系统文件锁，另一进程会在登录前收到 busy 错误；异常退出会释放锁。不得另开未受保护的 BaoStock 会话并发查询，也不靠重新登录循环掩盖错误。修改前已运行的服务不会自动加载此锁，本轮未重启活动服务。
