---
name: factor-research-toolkit
description: 选择和验证本地可用的 A 股因子库、自动特征生成和因子诊断工具。用于因子目录、公式挖掘、时间序列特征、Rank IC 和候选策略研究。
---

# Factor Research Toolkit

工具版本、许可证、解释器和冒烟命令见
`D:\Quantification\vendor\factor-research\CATALOG.md`。先区分因子定义、候选生成、诊断和交易回放：

- Qlib Alpha158/Alpha360、TA-Lib：已有特征定义与算子；Alpha158 已有项目研究路径，Alpha360 只是定义可用。
- OpenFE：表格特征组合；gplearn/DEAP：符号或进化搜索；tsfresh：按股票和决策日构造的过去窗口特征。新工具均先做受限研究，不能将合成测试称为市场有效性。
- Alphalens-reloaded：IC、分组和换手等因子诊断；不能替代订单、费用、价格限制和账户回放。

使用真实 A 股数据前固定快照、股票池、复权和标签时点；训练/验证/测试按时间分离，清除跨界的未来收益标签。OpenFE `fit` 必须传显式时间索引，并为每次调用指定由调用方清理的独立 `tmp_save_path`；gplearn 和 tsfresh 不得在整个样本上先挖掘再回头宣称样本外有效。候选公式冻结后，沿 `trading/research/experiment.py` 的统一 `trading.engine`/ledger 路径核对净收益、回撤、交易约束和恢复能力。

当前只验证了软件可在本机运行，没有新增因子通过市场数据与账本门槛。不得据此解除 `NO_TRADE`，也不连接或操作真实交易账户。
