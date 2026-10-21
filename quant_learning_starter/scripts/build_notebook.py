"""Build a teaching notebook with nbformat; optionally execute in the local kernel."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import nbformat as nbf
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "notebooks" / "01_factor_lab.ipynb"
OUTPUT = ROOT / "outputs" / "synthetic"


def md(text):
    return nbf.v4.new_markdown_cell(dedent(text).strip())


def code(text):
    return nbf.v4.new_code_cell(dedent(text).strip())


def build():
    nb = nbf.read(SOURCE, as_version=4)
    nb.cells = [
        md("""
        # 教学合成数据，不能用于盈利判断

        ## 目标与顺序
        面向已有 Python / 机器学习经验、初学日频因子研究的读者。
        运行后应能读懂 date × symbol 面板，手算因子及成交时刻收益，解释横截面 Rank IC。
        顺序：生成面板 → 三个因子 → 进入/退出日期 → 横截面评价 → 手算 → 未来信息检查。
        本 Notebook 是本项目的原创教学练习，不是 MIT 或书籍作者的官方作业。

        环境在子项目 `.venv`，内核为 `Quant Learning (Python 3.11)`；全部数据由固定种子生成。
        """),
        code("""
        import sys
        from pathlib import Path
        import numpy as np
        import pandas as pd
        from IPython.display import display, Image

        ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / 'factor_lab.py').exists())
        sys.path.insert(0, str(ROOT))
        from factor_lab import FACTORS, LABEL, synthetic_panel, compute_factors_and_labels
        from factor_lab import evaluate_cross_sections, summarize, manual_example
        from run_demo import plot_diagnostics

        OUT = ROOT / 'outputs' / 'synthetic'
        OUT.mkdir(parents=True, exist_ok=True)
        SEED = 20260909
        print('Python:', sys.version.split()[0], '| Seed:', SEED, '| Source: synthetic')
        """),
        md("""
        ## 1. 看一张日频表
        SYN 开头的股票全部虚构。价格由随机扰动生成，未植入预测因子。
        日历是工作日扣除两个人为休市日，不代表中国或美国交易所。
        故意缺失少量个股日，后面检查这些空格是否改变交易日期。
        """),
        code("""
        panel, calendar = synthetic_panel(seed=SEED)
        print('Sessions:', len(calendar), '| Stocks:', panel.symbol.nunique(), '| Rows:', len(panel))
        display(panel.head(6))
        """),
        md("""
        ## 2. 因子与未来标签
        `reversal_5 = -(C[t]/C[t-5]-1)`；`momentum_20 = C[t]/C[t-20]-1`。
        `relative_volume_20 = V[t]/mean(V[t-20],...,V[t-1])`，分母不含今日量。

        t 收盘后形成信号；t+1 开盘进入；持有五个交易日，t+6 开盘退出。
        标签 `O[t+6]/O[t+1]-1` 要等到退出后才能观察。计算先补全日历，再偏移；没有前向填充。
        """),
        code("""
        factors = compute_factors_and_labels(panel, calendar)
        display(factors.loc[factors.symbol == 'SYN000'].iloc[20:24])
        assert factors.loc[factors.date.isin(calendar[-6:]), LABEL].isna().all()
        print('Last six signal dates: future labels are missing, not zero.')
        """),
        md("""
        ## 3. 横截面评价
        每个日期分别计算各股票因子与标签的 Spearman 相关；少于五对、常量因子或常量标签均留空。
        分组先只看当日因子，未来标签缺失只影响覆盖数。每组先算当日均值，再按日期等权汇总。
        以下是所有教学日期上的描述统计，没有训练/验证/测试集，也没有选参。
        """),
        code("""
        daily, groups = evaluate_cross_sections(factors)
        summary = summarize(daily, groups)
        display(summary.round(6))
        display(daily.groupby(['factor', 'ic_reason']).size().rename('days').to_frame())
        plot_diagnostics(daily, summary, OUT / 'diagnostics.png')
        display(Image(filename=str(OUT / 'diagnostics.png')))
        """),
        md("""
        ## 4. 手算小例子
        从第 0 格开始，开盘价是 100+i，收盘价是 102+i，成交量是 1000+10i。
        第 20 格：过去五日收盘价 117→122，反转值 -5/117；20 日动量 20/102；
        过去二十格成交量平均 1095，因此相对量 1200/1095。
        第 21 格开盘 121 进入，第 26 格开盘 126 退出，标签 5/121。

        **练习：** 先手算四个值，再执行下面的单元格；指出哪一个值在信号日还不可知。
        """),
        code("""
        checks, timing = manual_example()
        display(timing.to_frame('date'))
        display(checks)
        assert checks.absolute_error.max() < 1e-12
        """),
        md("""
        ## 5. 修改未来输入
        **先预测：** 将第 70 个会话之后的开盘、收盘与成交量全部乘七，哪些旧结果应该变化？
        截至第 70 个会话的因子不应改变；跨越修改边界的未来收益标签可以改变。
        """),
        code("""
        changed = panel.copy()
        changed.loc[changed.date > calendar[70], ['open', 'close', 'volume']] *= 7
        altered = compute_factors_and_labels(changed, calendar)
        past = factors.date <= calendar[70]
        pd.testing.assert_frame_equal(factors.loc[past, list(FACTORS)], altered.loc[past, list(FACTORS)])
        print('PASS: perturbing future inputs leaves past factors unchanged.')
        """),
        md("""
        ## 6. 三个理解问题
        1. 为什么标签不是 `close[t+5]/close[t]-1`？哪一个时刻的价格在决定交易时还无法成交？
        2. 若 t+1 的开盘价缺失，能否直接用个股的下一条非空记录？这会怎样改变持有期限？
        3. 为什么不能将每天得到的五日收益连续相乘，称为可交易净值？

        **答题要点：** 对齐真实可进入时点；统一日历且缺失不顺延；相邻标签持有区间重叠，
        还缺现金、仓位、成交约束与成本核算。相关性不是可实现净收益。

        ## 阅读对应与下一项实验
        面板与时间窗口：Wes McKinney 第 10–11 章；相关性：MIT 18.05 Class 7b；
        研究问题：Chan 第 2 章及 MIT 15.401 Equities。具体入口见 `START_TODAY.md`。
        ISLP 第 5 章用于随后理解样本外评价，MIT 18.642 第 3 讲用于连接股票量化研究。
        P2 下一步：确认一个真实数据源的授权、日历、复权和历史股票池，先用小样本核对字段与日期。
        本 Notebook 不能证明因子有效；P2–P8 均尚未执行。
        """),
    ]
    nb.metadata.kernelspec = {"display_name": "Quant Learning (Python 3.11)", "language": "python", "name": "quant-learning"}
    nb.metadata.language_info = {"name": "python", "version": "3.11.15"}
    nbf.validate(nb)
    nbf.write(nb, SOURCE)
    return nb


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    notebook = build()
    print(f"Built: {SOURCE}")
    if args.execute:
        NotebookClient(notebook, timeout=60, kernel_name="quant-learning",
                       resources={"metadata": {"path": str(ROOT)}}).execute()
        nbf.validate(notebook)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        destination = OUTPUT / "01_factor_lab.executed.ipynb"
        nbf.write(notebook, destination)
        record = {"status": "executed", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                  "command": [sys.executable, str(Path(__file__).resolve()), "--execute"],
                  "code_cells": sum(c.cell_type == "code" for c in notebook.cells),
                  "executed_cells": sum(c.cell_type == "code" and c.execution_count is not None for c in notebook.cells),
                  "error_outputs": sum(o.output_type == "error" for c in notebook.cells if c.cell_type == "code" for o in c.outputs),
                  "output": str(destination)}
        (OUTPUT / "notebook_execution.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(json.dumps(record, indent=2))
