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

依赖安装以各子项目已有说明及锁文件为准；`webapp/setup.ps1` 用于重建网页环境。
根目录 `env/python.exe` 尚无统一的可移植依赖锁，不能把“代码已克隆”视为整个
研究运行环境已恢复。研究工作台可按其 README 使用 `-NoWorkers` 启动，保持交易
worker 关闭。第三方 `vendor/` 工具按各模块已有说明单独配置。

`.gitattributes` 保留文件原始换行字节，避免迁移时改变研究登记引用的源码摘要。
