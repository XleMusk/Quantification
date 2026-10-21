# 今天从这里开始

首轮目标：能解释一条因子记录的产生时刻，能手算它对应的未来收益。
打开 `outputs/synthetic/01_factor_lab.executed.ipynb` 阅读已执行结果；用 `notebooks/01_factor_lab.ipynb` 重新练习。

## 今天的最少阅读（约 60–90 分钟）

1. Chan《Quantitative Trading》第 2 章，按原包安排先写出一个可检验想法及失效条件。
   使用已有正版书或图书馆版本；[出版社入口](https://www.wiley.com/en-au/quantitative-trading-how-to-build-your-own-algorithmic-trading-business-2nd-edition-p-9781119800064) 本轮自动访问失败，未读取正文或重新确认目录。
2. [MIT 15.401：Equities](https://ocw.mit.edu/courses/15-401-finance-theory-i-fall-2008/pages/video-lectures-and-slides/equities/) 选看股票收益相关部分，先能区分价格、收益与持有期。
3. 执行 Notebook 第 4 节，先手算 `-5/117` 与 `5/121`，再核对程序结果；回答末尾三个问题。

没有 Chan 正文时，先完成 MIT 公开部分和 Notebook，书籍阅读保持待办。

## 按问题补课

| 本项目的计算 | 资料与最少范围 | 要留下的学习证据 |
|---|---|---|
| date × symbol、滚动窗口 | [Python for Data Analysis](https://wesmckinney.com/book/) 第 10–11 章，按需回查第 5–8 章 | 说明为何先补全日期再 shift |
| 横截面相关与常量情况 | [MIT 18.05](https://ocw.mit.edu/courses/18-05-introduction-to-probability-and-statistics-spring-2022/pages/classes-reading-and-in-class-materials/) Class 7b，欠缺基础再补 Class 4b/5a | 写一例相关系数未定义的输入 |
| 股票量化研究问题 | [MIT 18.642](https://ocw.mit.edu/courses/18-642-topics-in-mathematics-with-applications-in-finance-fall-2024/pages/calendar/) 第 3 讲 | 提出一个因子假设和反例 |
| 后续训练/评价 | [ISLP 作者站](https://www.statlearning.com/) 第 5 章，模型阶段再读第 3/6/8 章 | 画出训练标签结束与验证起点 |
| 后续 Qlib 接入 | [Qlib 官方仓库](https://github.com/microsoft/qlib) 的数据准备与 Alpha158 示例 | 明确数据源与默认示例需改的条件 |
| 第二版配套代码 | [Jansen second-edition](https://github.com/stefan-jansen/machine-learning-for-trading/tree/second-edition) | 按指定章节查看，保留分支信息 |

课程入口与上述 MIT 主题已在线核对；这不等于完成课程或阅读全书。
Qlib 官方 README 当前仍提示官方数据集暂时停用，未来接入时需重新核验数据来源和使用许可。
全部 23 项原始资源保存在 `resources.json`，可扫描索引与本轮核对范围在 `outputs/resource_index.md`。

## 下一项实验

P2：优先检查当前项目已有 `market_data` / `qlib_data` 的来源记录、用途许可和字段。
确认一个源后，先取少量股票与日期，核对交易日历、复权、缺失日、历史股票池与停牌/退市字段。
没有明确来源与权限的本地文件不直接当作已授权研究样本。

已有 SSH 主机别名不等于已有获授权的量化云工作区。下一阶段若在云端运行，先落实明确项目目录、数据授权、可用资源与预算。
本轮 P0/P1 在本机完成；16 周学习计划的后续阶段见 `TASKS.md`，人的学习进度仍由实际阅读和练习确认。
