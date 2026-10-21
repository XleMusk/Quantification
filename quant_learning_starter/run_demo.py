"""Run P1 and persist small synthetic teaching artifacts."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from factor_lab import FACTORS, LABEL, compute_factors_and_labels, evaluate_cross_sections, manual_example, summarize, synthetic_panel

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "outputs" / "synthetic"


def plot_diagnostics(daily, summary, destination):
    fig, axes = plt.subplots(3, 2, figsize=(12, 9), layout="constrained")
    colors = ["#176b74", "#ba3b4b", "#76539a"]
    for row, factor in enumerate(FACTORS):
        values = daily.loc[daily["factor"] == factor]
        axes[row, 0].plot(values["date"], values["rank_ic"], lw=0.8, color=colors[row], alpha=0.8)
        axes[row, 0].axhline(0, color="#555555", lw=0.7)
        axes[row, 0].set(title=f"{factor}: daily cross-sectional Rank IC", ylabel="Spearman correlation", ylim=(-1, 1))
        axes[row, 0].tick_params(axis="x", rotation=20)
        values = summary.loc[summary["factor"] == factor].iloc[0]
        axes[row, 1].bar(range(1, 6), [values[f"q{i}_mean_label"] * 100 for i in range(1, 6)], color=colors[row])
        axes[row, 1].axhline(0, color="#555555", lw=0.7)
        axes[row, 1].set(title=f"{factor}: mean forward labels by group", xlabel="Q1 (low) to Q5 (high)", ylabel="5-session label (%)", xticks=range(1, 6))
    fig.suptitle("SYNTHETIC EDUCATION ONLY | Group labels are not portfolio returns", fontsize=13)
    fig.savefig(destination, dpi=150)
    plt.close(fig)


def run(output=DEFAULT_OUTPUT, seed=20260909):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    panel, calendar = synthetic_panel(seed=seed)
    factors = compute_factors_and_labels(panel, calendar)
    daily, groups = evaluate_cross_sections(factors)
    summary = summarize(daily, groups)
    checks, timing = manual_example()
    if checks["absolute_error"].max() > 1e-12:
        raise AssertionError("Hand-calculation comparison failed")

    panel.to_parquet(output / "panel.parquet", index=False)
    calendar.to_series(index=range(len(calendar))).to_csv(output / "calendar.csv", index=False)
    factors.to_parquet(output / "factors_labels.parquet", index=False)
    daily.to_csv(output / "daily_ic.csv", index=False)
    groups.to_csv(output / "daily_groups.csv", index=False)
    summary.to_csv(output / "summary.csv", index=False)
    checks.to_csv(output / "manual_check.csv", index_label="calculation")
    plot_diagnostics(daily, summary, output / "diagnostics.png")

    metadata = {
        "run_id": f"synthetic_p1_seed_{seed}", "status": "executed",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "command": [sys.executable, *sys.argv], "working_directory": str(Path.cwd()),
        "reproduce_command": [sys.executable, str(ROOT / "run_demo.py"), "--seed", str(seed), "--output", str(output)],
        "dataset": "synthetic_education_only", "dataset_version": "1",
        "seed": seed, "sessions": len(calendar), "stocks": int(panel["symbol"].nunique()),
        "observed_rows": len(panel), "calendar_panel_rows": len(factors),
        "missing_stock_sessions": len(factors) - len(panel),
        "start": str(calendar[0].date()), "end": str(calendar[-1].date()),
        "calendar": "Weekdays minus invented closures 2025-01-22 and 2025-02-12; NOT an exchange calendar",
        "signal": "after close t", "entry": "open t+1", "exit": "open t+6", "holding_sessions": 5,
        "volume_denominator": "mean of sessions t-20 through t-1, excluding current volume",
        "grouping": "qcut of signal-time factors before filtering unavailable labels; date-equal means",
        "attempted_factor_candidates": list(FACTORS), "parameter_searches": 0,
        "train_validation_test": None, "model_fitted": False, "market_evidence": False,
        "pnl": None, "transaction_costs": None, "drawdown": None, "turnover": None,
        "observed_label_count": int(factors[LABEL].count()),
        "ic_reasons": daily.groupby(["factor", "ic_reason"]).size().to_dict(),
        "group_reasons": daily.groupby(["factor", "group_reason"]).size().to_dict(),
        "python": sys.version, "executable": sys.executable, "platform": platform.platform(),
        "packages": {name: importlib.metadata.version(name) for name in (
            "numpy", "pandas", "scipy", "matplotlib", "pyarrow", "ipykernel", "nbformat", "nbclient", "pytest")},
        "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ("factor_lab.py", "run_demo.py", "uv.lock")},
        "manual_max_absolute_error": float(checks["absolute_error"].max()),
        "output_directory": str(output),
    }
    for key in ("ic_reasons", "group_reasons"):
        metadata[key] = {"/".join(pair): int(value) for pair, value in metadata[key].items()}

    rows = "\n".join(f"| {r.factor} | {r.valid_ic_days} | {r.mean_rank_ic:.6f} | {r.std_rank_ic:.6f} |"
                     for r in summary.itertuples())
    report = f"""# 教学合成数据，不能用于盈利判断

## 本次完成
固定种子 {seed}，{len(calendar)} 个模拟交易日、{panel['symbol'].nunique()} 只虚构股票。
生成 {len(panel)} 条 OHLCV 记录，完整日历面板有 {len(factors)} 格，其中 {len(factors) - len(panel)} 格故意缺失。
代码生成、三个因子、收益标签、横截面评价及手算核对已执行。

## 日期与计算口径
日历为普通工作日剔除两个虚构休市日，不代表任何真实交易所。信号在 t 日收盘后产生，
t+1 日开盘进入，t+6 日开盘退出，持有五个交易日。标签为 O[t+6]/O[t+1]-1。
缺失价格不前向填充；缺失进入/退出价格及尾部未到期标签留空。
三个因子分别为 -(C[t]/C[t-5]-1)、C[t]/C[t-20]-1、V[t]/mean(V[t-20:t-1])。

## 观察到的教学统计
| 因子 | 有效 IC 日期数 | 平均 Rank IC | 日期间标准差 |
|---|---:|---:|---:|
{rows}

IC 是同一天各股票因子与未来标签的 Spearman 相关；样本不足或常量时留空并记录原因。
分组在信号形成时依据因子值确定，随后才检查未来标签是否可用；缺失覆盖数见 daily_groups.csv。
分组均值先按日计算、再按日期等权汇总。没有模型训练或选参，所有教学日期均已查看。
五日标签相互重叠，本次没有独立样本显著性检验、年化收益或净值曲线。

![教学图表](diagnostics.png)

## 手算对照
例子信号日期 {timing['date'].date()}，进入 {timing['entry_date'].date()}，退出 {timing['exit_date'].date()}。
收盘价从 102 每日增加 1，开盘价从 100 每日增加 1，成交量从 1000 每日增加 10。
第 20 格的反转值是 -5/117；动量是 20/102；相对量是 1200/1095；收益标签是 5/121。
四项最大绝对误差为 {checks['absolute_error'].max():.3g}，明细见 manual_check.csv。

## 能说什么
- Safe：指定合成数据上的计算已运行，可按固定种子复跑。
- Narrow：图表仅描述这个随机数据集；IC 正负不构成市场有效性证据。
- Pending：真实数据、训练期拟合、时间切分、模型比较、交易成本和逐日资金核算均属于后续 P2–P4。
- Blocked：盈利与实盘可用性结论没有所需市场证据。

## 复查与下一步
关键检查覆盖交易日历偏移、缺失价格、未来信息扰动、常量 IC 和未来缺失不改变分组。
实际测试输出见同目录 tests.txt；Notebook 执行记录见 notebook_execution.json。
本轮没有训练预处理器或组合回测，训练期拟合检查与成本/PnL 手算在 P3/P4 实现时执行。
下一项实验：P2 先核验一个已有数据源的授权与字段，只接入小样本并比较与合成数据的缺失模式。
不直接沿用本教学日历处理真实 A 股交易日、停牌或退市。

## 运行
在项目目录执行 `.\\.venv\\Scripts\\python.exe run_demo.py`。
本次精确命令、环境版本、参数与源文件指纹见 run.json；环境恢复使用 `uv sync --frozen`。
今天的最少阅读与课程对应关系见 ../../START_TODAY.md，全部 23 个入口见 ../resource_index.md。
"""
    (output / "REPORT.md").write_text(report, encoding="utf-8")
    metadata["artifacts"] = [str(output / name) for name in (
        "panel.parquet", "calendar.csv", "factors_labels.parquet", "daily_ic.csv",
        "daily_groups.csv", "summary.csv", "manual_check.csv", "diagnostics.png", "REPORT.md")]
    (output / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("SYNTHETIC EDUCATION ONLY - no evidence of market profitability")
    print(summary[["factor", "valid_ic_days", "mean_rank_ic"]].to_string(index=False))
    print(f"Report: {output / 'REPORT.md'}")
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    run(args.output, args.seed)
