# 基础数据处理练习

这是本项目原创练习，数据为合成数据，不是书籍官方作业或市场行情。

学习顺序：CSV 读取和日期解析（pandas 第 6 章）→ many-to-one 合并（第 8 章）→ 按日分组（第 10 章）→ 按股票滚动均值（第 11 章）→ Parquet 保存和读回核对。

运行：

```powershell
D:\Quantification\quant_learning_starter\.venv\Scripts\python.exe D:\Quantification\quant_learning_starter\learning_resources\practice\data_handling.py
```

已验证：16 条记录没有被合并扩增，分类无缺失，三日均值为 11，首日总成交量为 200，每只股票预热两日保持空值，Parquet 读回数据与保存前相同。
运行记录见 `output/result.json`；所有结果写入 `output/`，再次运行会重建相同教学样本。

自己先回答：

1. 为什么合并时指定 many_to_one？右表标识重复会怎样影响记录数？
2. 为什么滚动均值先按 symbol 分组？哪两个价格不能混在同一个窗口？
3. 前两天的三日均值为什么留空？填零会改变什么含义？

备用上游 Notebook 在 `../code/pydata-book/`。它们是原文件，部分包含故意触发错误的教学代码、在线读取或额外示例数据；本次没有把它们标为本地执行通过。
