# 书籍、公开课程与项目
核对日期：2026-09-09。以下“必读”是选读指定范围，不是要求16周读完所有书和课程。

## 三条使用原则
优先研究主线，而非学校名气。概率课与机器学习课都有替代选项，不重复全修；旧课用于基础理论，当前市场规则与数据接口另查官方文档；一本书的公开代码不等于书本全文、行情数据与全部使用权免费。

## 先读与按需参考
### B01｜Python for Data Analysis, 3rd Edition
作者：Wes McKinney。版本：2022 / open web edition。优先级：P0-reference。
**范围：** 5–8、10–11章；Python基础熟悉则跳过2–3章。
**访问：** 作者提供免费HTML；按需浏览，不批量镜像。
官方入口：https://wesmckinney.com/book/
配套入口：https://github.com/wesm/pydata-book

### B02｜Quantitative Trading: How to Build Your Own Algorithmic Trading Business
作者：Ernest P. Chan。版本：2nd Edition；出版社另有Revised Edition条目。优先级：P0-core。
**范围：** 第2、3、5、6、7章；先读想法、回测、成本和风险。
**访问：** 付费书或图书馆借阅；已有第二版可用。出版社全文页面可能限制自动访问，不绕过。
官方入口：https://www.wiley.com/en-au/quantitative-trading-how-to-build-your-own-algorithmic-trading-business-2nd-edition-p-9781119800064
配套入口：https://www.wiley.com/en-us/quantitative-trading-how-to-build-your-own-algorithmic-trading-business-revised-edition-2nd-edition-p-9781394378043

### B03｜An Introduction to Statistical Learning, with Applications in Python
作者：Gareth James, Daniela Witten, Trevor Hastie, Robert Tibshirani, Jonathan Taylor。版本：2023 Python edition。优先级：P0-core。
**范围：** 2、3、5、6、8、13章；尤其正则化、重采样、树模型、多重检验。
**访问：** 作者站提供免费PDF和Python实验；教学材料使用范围与软件许可分别核实。
官方入口：https://www.statlearning.com/
配套入口：https://islp.readthedocs.io/en/latest/labs.html

### B04｜Systematic Trading
作者：Robert Carver。版本：2015。优先级：P1-core。
**范围：** 风险调整、信号组合、仓位、交易成本、系统设计；按主题读。
**访问：** 付费书或图书馆借阅；个人小资金研究不照搬书中的杠杆。
官方入口：https://www.harriman-house.com/authors/robert-carver/systematic-trading/9780857194459
配套入口：https://qoppac.blogspot.com/p/systematic-trading-start-here.html

### B05｜Machine Learning for Algorithmic Trading, 2nd Edition
作者：Stefan Jansen。版本：2nd Edition / second-edition branch。优先级：P1-core。
**范围：** 第4、5、6、7、8、12章；第2、9、11章按需。
**访问：** 书和代码分开；主仓库已转第三版，第二版必须使用second-edition分支。旧环境按需迁移，不整仓安装。
官方入口：https://ml4trading.io/second-edition/
配套入口：https://github.com/stefan-jansen/machine-learning-for-trading/tree/second-edition

### B06｜Advances in Financial Machine Learning
作者：Marcos López de Prado。版本：2018。优先级：P2-selective。
**范围：** 金融交叉验证、样本重叠、特征重要性、回测偏差与统计；非开局逐页读。
**访问：** 付费书；本轮出版社有部分403，检索可确认目录信息。不要自动实施书中全部技术。
官方入口：https://www.wiley.com/en-us/Advances%2Bin%2BFinancial%2BMachine%2BLearning-p-9781119482086
配套入口：https://www.wiley.com/en-fr/Edition-p-9781119482109

### B07｜Introduction to Probability, 2nd Edition
作者：Joseph K. Blitzstein and Jessica Hwang。版本：Harvard Stat 110 companion。优先级：P0-alternative。
**范围：** 条件概率、期望方差、协方差、条件期望、大数定律与中心极限定理。
**访问：** 官方站提供免费在线版入口与练习；与MIT18.05基础部分择一主学。
官方入口：https://stat110.hsites.harvard.edu/
配套入口：https://stat110.hsites.harvard.edu/strategic-practice-problems

### B08｜Convex Optimization
作者：Stephen Boyd and Lieven Vandenberghe。版本：2004 / official online resources。优先级：P2-selective。
**范围：** 凸集、凸函数、二次规划、约束优化；组合权重项目时使用。
**访问：** 作者站经出版社许可提供免费正文；按需阅读，不作为第一个因子的前置条件。
官方入口：https://web.stanford.edu/~boyd/cvxbook/
配套入口：https://see.stanford.edu/Course/EE364A

## 高校课程：先选主线，再选补充

### C01｜MIT 18.05 — Introduction to Probability and Statistics
学期/版本：Spring 2022。优先级：P0-core。
**选学范围：** 概率、置信区间、假设检验、bootstrap、回归；按基础补课。
**公开情况：** 免费讲义、习题和交互材料；不要声称有全套课堂视频。部分实验使用R，项目练习可另写Python。
官网：https://ocw.mit.edu/courses/18-05-introduction-to-probability-and-statistics-spring-2022/
章节/材料：https://ocw.mit.edu/courses/18-05-introduction-to-probability-and-statistics-spring-2022/pages/classes-reading-and-in-class-materials/

### C02｜MIT 15.401 — Finance Theory I
学期/版本：Fall 2008。优先级：P0-selective。
**选学范围：** Equities；Risk and Return；Portfolio Theory；CAPM and APT；Efficient Markets。
**公开情况：** 公开视频、讲义及习题；理论有用，历史市场案例不当作当前规则。
官网：https://ocw.mit.edu/courses/15-401-finance-theory-i-fall-2008/
章节/材料：https://ocw.mit.edu/courses/15-401-finance-theory-i-fall-2008/pages/video-lectures-and-slides/

### C03｜MIT 18.642 — Topics in Mathematics with Applications in Finance
学期/版本：Fall 2024。优先级：P1-core。
**选学范围：** 第3、6、8、9、11、12、13、19、22、23讲；按主题和项目需求选读。
**公开情况：** 2013年18.S096的更新版；官网有视频、讲义、习题与项目。实际逐讲可得性以页面为准。
官网：https://ocw.mit.edu/courses/18-642-topics-in-mathematics-with-applications-in-finance-fall-2024/
章节/材料：https://ocw.mit.edu/courses/18-642-topics-in-mathematics-with-applications-in-finance-fall-2024/pages/calendar/

### C04｜UC Berkeley CS189/289A — Introduction to Machine Learning
学期/版本：Spring 2026。优先级：P1-selective。
**选学范围：** 第3、7–10、14讲：模型机制、线性回归、正则化、偏差方差。
**公开情况：** 选已结束的春季版本；课程表公开讲义与视频链接，部分外部作业平台可能另需权限。
官网：https://eecs189.org/sp26/
章节/材料：https://eecs189.org/sp26/syllabus/

### C05｜Harvard Stat 110 — Probability
学期/版本：official public course archive。优先级：P0-alternative。
**选学范围：** Lecture 4及期望、方差、协方差、条件期望、LLN/CLT相关课；按标题定位。
**公开情况：** 公开视频、免费书入口、练习和解答；概率打底可替代MIT18.05概率部分，但统计推断仍要补。
官网：https://stat110.hsites.harvard.edu/
章节/材料：https://stat110.hsites.harvard.edu/youtube

### C06｜Yale ECON252 — Financial Markets
学期/版本：Spring 2011。优先级：P2-overview。
**选学范围：** 风险、市场机制、行为金融；金融背景薄弱时选读。
**公开情况：** 公开视频与文字稿；这是市场通识而非因子工程课。
官网：https://oyc.yale.edu/economics/econ-252
章节/材料：https://oyc.yale.edu/economics/econ-252-11/lecture-1

### C07｜Stanford EE364A — Convex Optimization I
学期/版本：public archive。优先级：P2-selective。
**选学范围：** 凸优化基础、最小二乘、二次规划；组合权重项目时使用。
**公开情况：** 使用确实公开的SEE版本；不假定当年在校生录像可外部访问。
官网：https://see.stanford.edu/Course/EE364A
章节/材料：https://web.stanford.edu/~boyd/cvxbook/

### C08｜Statistical Learning with Python
学期/版本：official Python course。优先级：P1-alternative。
**选学范围：** 与ISLP指定章节相配套；伯克利数学推导负担重时替代。
**公开情况：** 作者站有课程入口；第三方平台注册/证书权益以当前页面为准。
官网：https://www.statlearning.com/online-courses
章节/材料：https://online.stanford.edu/courses/sohs-ystatslearningp-statistical-learning-python

## 课程怎样直接变成研究任务
|课程|代码练习|你需要回答的问题|
|---|---|---|
|MIT18.05或HarvardStat110|噪声中的相关性、置信区间与多次筛选实验|筛选很多次后最好的曲线为什么未必可信？|
|MIT15.401|两个资产的收益、协方差和等权组合|总收益中多少来自共同市场涨跌？|
|MIT18.642|横截面回归、PCA、波动与组合实验|新因子是在增加信息还是重复已有风险暴露？|
|BerkeleyCS189或ISLP配套课|Ridge、正则化、LightGBM比较|改善来自模型，还是数据与评价方法改变？|
|StanfordEE364A（可选）|有权重与换手约束的组合优化|优化后成本与回撤是否真的改善？|
|YaleECON252（可选）|市场机制笔记，不额外建代码框架|某个交易假设在真实市场中为什么可能做不到？|

## 项目与官方技术文档

### P01｜Qlib
用途：Alpha158 + LightGBM；改为自己的数据、时间和交易设定。
说明：官方README仍提示官方数据集暂时停用；社区数据不自动代表完整质量与许可。
官网：https://github.com/microsoft/qlib
配套入口：https://raw.githubusercontent.com/microsoft/qlib/main/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml

### P02｜Alphalens-reloaded
用途：单因子Rank IC、分组、换手报告。
说明：分析工具，不是完整成交仿真；不得把传入价格的时间对齐问题交给工具猜。
官网：https://github.com/stefan-jansen/alphalens-reloaded

### P03｜RD-Agent
用途：fin_factor场景；仅在人工研究流程稳定以后。
说明：当前官方说明Linux，多数场景需Docker；外部模型调用可能计费。首轮不安装。
官网：https://github.com/microsoft/RD-Agent

### P04｜pydata-book
用途：只取当前需要的pandas、分组、时间序列Notebook。
说明：官方示例代码MIT；书本文本与代码许可不同。
官网：https://github.com/wesm/pydata-book
配套入口：https://wesmckinney.com/book/

### P05｜ISLP Labs
用途：回归、验证、正则化和树模型实验。
说明：不要把包含深度学习的全套教学依赖装进主环境；确需原版全套实验时另建环境。
官网：https://islp.readthedocs.io/en/latest/labs.html
配套入口：https://www.statlearning.com/resources-python

### D01｜TimeSeriesSplit
用途：时间序列划分；股票面板按日期整体切分并另外处理标签区间重叠。
说明：普通随机K折不适用于直接评估金融时间序列；该类本身不是完整面板/事件清除实现。
官网：https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html

### D02｜Custom instructions with AGENTS.md
用途：项目根目录AGENTS.md；合并已有指令，不覆盖用户规则。
说明：官方地址当前跳转ChatGPT Learn；不涉及API密钥创建或收费服务开通。
官网：https://developers.openai.com/codex/guides/agents-md
配套入口：https://learn.chatgpt.com/docs/agent-configuration/agents-md
