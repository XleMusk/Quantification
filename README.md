# Quantification

个人 A 股量化研究与纸面交易项目。规则策略、模型评分和公开席位研究共用
`Data → Signal → Target Portfolio → Orders → Engine/Ledger → Fill → Cash/Position`。

当前是研究和纸面运行软件。策略仍需验证，真实账户登录、下单和撤单保持关闭。

## 从这里开始

- [投委会与命令行说明](README_committee.md)
- [研究工作台安装与启动](webapp/README.md)
- [架构与已有能力](docs/architecture_v2/BASELINE.md)
- [实现队列](docs/architecture_v2/IMPLEMENTATION_QUEUE.md)
- [同花顺适配边界](trading/ths/README.md)

主要入口为 `run_committee.py`；交易核心在 `trading/`，网页在 `webapp/`。
历史文档中的状态和绝对路径属于当时的本机运行记录，迁移后需按实际资料核对。

## 仓库范围与迁移

Git 保存源码、测试、可复用配置、文档、少量架构基线回执和各子项目依赖锁文件。
生成的研究结果、行情缓存、模型文件、账户记录、虚拟环境、第三方工具安装目录、
项目压缩包及本目录中的无关论文材料不提交；原文件继续保存在本机。

持久数据默认位于 `D:\QuantificationData\trading`，可通过 `QUANT_TRADING_HOME`
指定位置。这些数据不在 Git 中，克隆仓库不会自动恢复历史研究或纸面账户。
需要完整迁移时，应另外迁移相应数据、模型和研究结果。

已安装 Python 3.11 和 `uv` 时，可在项目根目录执行：

```powershell
./webapp/setup.ps1 -BackendOnly
& ./webapp/.venv/Scripts/python.exe -B ./run_committee.py --help
```

这会按现有锁文件恢复 CLI 离线入口及 Engine/Ledger 纸面核心，安装脚本不启动服务。
完整网页安装方式见 [工作台说明](webapp/README.md)。根目录旧 `env/python.exe`、
Qlib、行情供应商和第三方 `vendor/` 工具仍需按实际使用的模块单独恢复，不能把
核心可运行视为全部历史研究环境已恢复。工作台启动时使用 `-NoWorkers`。

`.gitattributes` 保留文件原始换行字节，避免迁移时改变研究登记引用的源码摘要。
