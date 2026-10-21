# 独立量化研究：学习与 Codex 实施包
核对日期：2026-09-09

## 本包是什么
这是书籍、课程和独立量化研究的**学习与实施任务包**。2026-09-09 已完成首轮 P0/P1：本地独立环境、资料索引、合成数据、三个因子、日历对齐标签、横截面评价、测试与已执行 Notebook。它不构成已验证盈利的策略。

沿用每周8–10小时的节奏：前8周完成第一份研究，后8周扩展验证与模拟运行。以股票日频横截面研究为主。首轮在本地运行，独立环境约 419 MiB，教学产物约 1.2 MiB；将来的正式数据和长运行优先放到确认后的云端工作区。本阶段不投入实盘本金。

## 直接开始

备用资料已整理到 [量化学习资料库](learning_resources/RESOURCE_NAVIGATION.html)，含本地讲义、pandas 配套 Notebook、在线书籍/课程/项目入口和逐项准备状态。此次资料准备记录见 [PREPARATION_REPORT](learning_resources/PREPARATION_REPORT.md)。

先读 [今天的练习](START_TODAY.md)，查看 [已执行 Notebook](outputs/synthetic/01_factor_lab.executed.ipynb) 或 [教学报告](outputs/synthetic/REPORT.md)。

```powershell
Set-Location D:\Quantification\quant_learning_starter
.\.venv\Scripts\python.exe run_demo.py
.\.venv\Scripts\python.exe -m pytest -q
```

已执行结果：24 只虚构股票、180 个模拟交易日、4278 条 OHLCV、三个因子；13 项测试通过，Notebook 六个代码单元全部执行且没有错误输出。
缺失未来收益保持空值；五日标签没有被连续累乘成净值。图表已检查可读。

## 环境与复现

使用 Python 3.11.15，依赖完整锁定在 `uv.lock`，另有 `requirements.lock.txt`。
已有 `D:\Quantification\env` 的依赖检查发现预先存在的 mcp / pytest / pluggy 缺失，因此本包使用独立 `.venv`；主环境未改动。
新环境 `uv pip check` 检查 52 个包全部兼容。机器资源和本地/远程状态见 [environment.json](outputs/environment.json)。

在已有 Python 3.11.15 和 uv 的机器恢复环境：

```powershell
uv sync --frozen
uv pip check --python .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m ipykernel install --prefix .\.venv --name quant-learning --display-name "Quant Learning (Python 3.11)"
.\.venv\Scripts\python.exe scripts/build_notebook.py --execute
```

Notebook 源文件是 `notebooks/01_factor_lab.ipynb`，可在已有 Notebook 编辑器里选择本项目 `.venv` 运行。
`scripts/build_notebook.py` 从教学源代码重新生成它，会覆盖该生成文件；自己的练习另存一个名称。
未安装浏览器端 JupyterLab 服务。无 uv 时，可用 Python 3.11 创建 venv 后以 pip 安装 `requirements.lock.txt`。

精确执行与复现命令、随机种子、计算口径、实际包版本见 [run.json](outputs/synthetic/run.json)。
测试原始输出见 [tests.txt](outputs/synthetic/tests.txt)，Notebook 运行记录见 [notebook_execution.json](outputs/synthetic/notebook_execution.json)。
`configs/research.example.yaml` 仍是后续研究意图示例，不是本轮执行配置；本轮参数由 `run_demo.py --help` 和运行记录给出。

## 数据与后续阶段

[数据字典](DATA_DICTIONARY.md) 明确字段、可得时刻、模拟日历和缺失处理。
每次运行覆盖默认 `outputs/synthetic`；保留新试验可指定 `--output outputs/synthetic_seed2 --seed 2`。
输出路径应保持明确的 synthetic 标识，真实数据接入使用单独目录和报告。
保留全部 23 项资源链接，本轮核对 8 个重点入口，其中 7 个可访问，Chan 出版社页面读取失败；其余 15 项未重查，见 [资料索引](outputs/resource_index.md)。

P2–P8 全部保留待办。没有新建云资源、远程上传、真实行情回测、模型训练或交易执行。
发现已有 SSH 主机别名，但尚无确认用于本项目的远程目录及运行授权，不能把别名当作已部署云工作区。
当前父目录不是 Git 仓库，本轮只保存本地文件，未创建或推送远程仓库。

## 怎么交给 Codex
1. 把本目录放到你准备使用的研究工作区，优先已有云端工作区。
2. 让 Codex 先读 `AGENTS.md`，再读 `CODEX_START_HERE.md`。
3. 本轮只执行 P0 和 P1，即环境确认、资料索引、小型教学实验；其余阶段保留为待办。
4. 已有同名文件时应合并，不覆盖已有用户指令和研究代码。单纯上传附件不等于已经把文件写入任何远程仓库。

## 文件
- `READING_LIST.md`：书单、课程范围、配套项目、访问与版本注意事项。
- `LEARNING_PLAN.md`：16周安排、学习和代码之间的对应关系。
- `TASKS.md`：按P0–P8组织的实施清单与完成标准。
- `resources.json`：23个官方资源入口，供程序读取；不包含书籍正文或课程视频。
- `AGENTS.md`：简短项目规则。
- `CODEX_START_HERE.md`：可直接交付的首轮指令。
- `configs/research.example.yaml`：待落实的研究默认设定，不是可直接调用Qlib的完整配置。
- `templates/experiment.md`：一个实验的记录模板。

## 起步成果
先完成“数据 → 少量因子 → 线性/树模型 → 时间上靠后的评价 → 简单持仓 → 成本与回撤”流程。用合成数据跑通只证明程序行为，不证明市场规律；没有可靠授权数据时，真实市场结论保持为空。

## 阅读顺序
Chan《Quantitative Trading》+ MIT15.401选段建立金融认识；pandas按需查；MIT18.05补统计；ISLP训练与评价；MIT18.642和Qlib连接研究；再读Carver与Jansen的相关部分。

所有资源的完整官方链接见 `resources.json` 与 `READING_LIST.md`；原包的核对声明保留在原资料中，本轮实际核对范围另见 `outputs/source_checks.json`。部分出版社页面自动访问受限，部分课程平台可能需要账号。学习进度以实际阅读和练习为准。
