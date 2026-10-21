# 本地 A 股量化工作台

已实现双线并行：规则策略 -> 目标 -> 自动调仓 -> 历史回放/日终纸面账户；CSV 导入 -> 实际因子计算 -> 结果比较。
交易进程与因子进程独立，前端只管理和展示真实计算。真实券商连接与下单关闭；纸面账户不是盘中实时仿真。
完整策略、执行假设、外置数据、已完成和未接通事项见 [交易 V1](../trading/TRADING_V1.md)。

## 启动与停止

依赖已装好，日常运行：

```powershell
& D:\Quantification\webapp\start.ps1
& D:\Quantification\webapp\stop.ps1
```

启动脚本输出实际 URL，默认 [工作台](http://127.0.0.1:5175) 和 [API 文档](http://127.0.0.1:8765/docs)。
仅浏览保存研究和执行计划、不运行计算及交易工作进程：

```powershell
& D:\Quantification\webapp\start.ps1 -NoWorkers -TradingHome D:\QuantificationData\trading
```

`-NoWorkers` 不启动两个后台工作进程，但 API 仍可管理本地任务，并非只读权限模式。
`-TradingHome` 显式指定当前服务的数据目录；未指定时沿用环境变量或项目默认目录。
端口已占用时向后寻找空闲端口，不停止其他服务；实际端口与进程编号写入 `.runtime/processes.json`。
只监听 `127.0.0.1`。不绑定局域网、不开放公网、不配置券商密钥。
启动后服务在后台运行；关闭网页不等于停止服务。重启电脑后需重新运行启动脚本。
启动和停止核对项目、命令行以及新记录的进程创建时点；失效或被复用的进程号不会被停止。
旧进程记录保留到 `.runtime/archive`。`stop.ps1` 不删除交易数据；建议先让任务完成再停止。

首次克隆或重建依赖前，安装 Python 3.11 和 `uv`；完整网页还需要 Node.js 20.19+ 或 22.12+。
在项目根目录执行 `./webapp/setup.ps1`，它通过 `uv` 创建 `webapp/.venv`、同步已有锁定依赖，
并安装、构建前端。无需先创建教学环境，也不会自动下载 Python 或修改原 `env`。
可以用 `-Python 'C:\路径\python.exe'` 指定已经安装的 Python 3.11。

只恢复 CLI 与纸面核心、不安装 Node.js 或构建前端时：

```powershell
./webapp/setup.ps1 -BackendOnly
& ./webapp/.venv/Scripts/python.exe -B ./run_committee.py --help
```

安装脚本不会启动服务或 worker。包安装需要网络，或已准备好的完整 `uv` 缓存；
有缓存时可设置 `$env:UV_OFFLINE='1'` 离线安装。历史行情、冻结模型、持久账户另行迁移。
此环境覆盖 CLI 离线入口及 Engine/Ledger 核心，不代表 Qlib、行情供应商 SDK 和原 worker
所用主环境已全部恢复；独立 worker 仍复用原 `env`，迁移后先使用 `-NoWorkers`。

## 交易页面

新增运行总览、策略与目标、订单与成交、研究比较。可创建历史回放/日终纸面账户、暂停恢复、运行同条件对照、下载全量委托成交和权益。
交易数据独立位于 `D:\QuantificationData\trading`，通过 `QUANT_TRADING_HOME` 配置，不覆盖原缓存。
工作日16:15后，运行中的纸面账户触发行情更新；启动脚本需保持运行，关闭网页无影响，未设置开机自启。

## 原有研究页面

1. **数据中心**：同步已有股票缓存、上传 CSV、查看证券价格、字段质量与日期缺口。
2. **因子研究**：选择数据集、研究日期、最小截面样本数，计算 5 日反转、20 日动量、20 日相对成交量。
3. **任务管理**：查看实际状态、计算阶段和日志，取消任务；失败或中断任务可以重新提交。
4. **研究结果**：查看日度 Rank IC、分组远期收益、截面秩相关性，下载 CSV、Parquet、JSON 和 Markdown 报告。

可先在导入窗口下载“合成示例 CSV”，将类型选为“合成数据”、口径选为“合成价格”，填写名称与来源后导入。
实际历史数据必须保留其供应商、价格口径和日期说明，不能把合成数据改名当作行情。

## 数据格式与边界

行情文件为 UTF-8 CSV，必需字段：

```csv
date,symbol,open,high,low,close,volume
2025-01-06,DEMO001,10,10.2,9.8,10.1,100000
```

上面仅演示字段，不是可用于因子评估的完整样本。单文件上限 12 MB、100000 行、100 个证券；交易日历乘证券最多 150000 格点。
日期必须为日频且不含时区；`date + symbol` 必须唯一。缺失交易日保留缺口，不能用零价格或前值填充。
可上传只有 `date` 一列、有序且唯一的日历 CSV；未提供时用观测日期并集，并明确标注未独立验证。
证券代码统一转大写；不自动把不同代码格式合并。成交量字段按股计，调用方须确认供应商单位。
非正价格、OHLC 关系异常、负成交量阻断任务；零成交量在计算中视为缺失，不假定能成交。

首次启动会登记本机前复权股票缓存以及合成教学数据。缓存同步只读取已有 Parquet，不联网获取最新行情。
2026-09-26 验证时，缓存有 10 只股票、4951 行，最大日期 2026-05-26，各股票截止日不同；另有 ETF 缓存，未混入股票数据集。
导入和同步按内容及元数据去重；相同输入不会重复登记，更新的数据保留为新快照，不覆盖旧任务引用。

## 方法与状态

- 信号时点为 t 日收盘后，标签为 t+1 开盘至 t+6 开盘收益；跨越研究结束日的标签剔除。
- 按完整日历计算因子和标签，缺失观测不压缩持有期。研究开始日前数据可用于预热。
- 五组按当日可得信号分配；未来价格缺失只影响评价，不重排当日信号组。
- Rank IC 按日期计算；相关性表是日内百分位秩汇集后的相关系数，不是收益相关性。
- 重叠五日标签的分组均值不是可复利净值，未扣交易成本，不构成可执行策略或盈利证据。
- SQLite 持久保存任务、配置和阶段日志；单 worker 通过文件锁和事务领取任务，最多 8 个待处理任务。
- 同一提交请求键保持幂等。主动再建同配置任务仍是新研究任务。
- 取消为阶段间协作式取消；进程在计算中异常退出，下一次 worker 启动将其标为“中断”，不伪装完成、不自动重跑。
- 列表显示最近 100 个任务，旧记录和文件仍保留。备份时先停止服务，再复制整个 `storage` 目录。

## 目录

| 路径 | 内容 |
|---|---|
| `backend/app` | FastAPI、输入校验、数据登记、SQLite 队列 |
| `frontend/src` | React + TypeScript + ECharts + Lucide |
| `workers/factor_worker.py` | 独立因子研究进程，复用 `quant_learning_starter/factor_lab.py` |
| `storage/research.sqlite3` | 数据集和任务元数据 |
| `storage/datasets/<id>` | 归一化行情与日历 Parquet |
| `storage/results/<job_id>` | 指标、IC、分组、因子明细、配置及报告 |
| `.runtime` | 当前进程、API / worker / 前端日志 |
| `verification` | 本轮验收说明与页面截图 |

## 架构取舍和后续阶段

参考 [FastAPI 官方全栈模板](https://github.com/fastapi/full-stack-fastapi-template) 的 React/FastAPI 分层，但本轮不是完整克隆该模板。
针对现有 Windows 本地项目，采用最小独立应用：SQLite 替代 PostgreSQL，不引入 Docker、登录系统和 Redis；研究计算放入独立进程，而非 HTTP 请求或页面定时器。
本地写请求有来源和自定义头检查，但这不是多用户鉴权系统，不能直接用于远程部署。

交易回放、日期适用规则、费用、公司行动、日终纸面账户和薄前端已接入。券商适配代码已做替身测试，尚未连接真实账户。
Qlib 模型训练与外部预测导入网页仍待接入；原始数据阻断保留。后续研究信号进入组合模块，不另建账户写入者。
原项目已有 Qlib、风控与 FinceptTerminal 继续保留，不在首版重复安装；旧 Qlib 数据的异常阻断也未绕过。

## 测试

```powershell
Set-Location D:\Quantification
& .\webapp\.venv\Scripts\python.exe -m pytest trading/tests webapp/backend/tests quant_learning_starter/tests -q
uv pip check --python .\webapp\.venv\Scripts\python.exe
Set-Location D:\Quantification\webapp\frontend
npm.cmd run build
npm.cmd audit
```

上述测试使用应用隔离环境，不为测试改动原主环境。验收细节见 [verification/ACCEPTANCE.md](verification/ACCEPTANCE.md)。
