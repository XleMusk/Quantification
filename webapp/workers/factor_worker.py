"""Single local worker; calculations do not run inside an HTTP request."""

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time
import traceback

import numpy as np
import pandas as pd

from quant_learning_starter.factor_lab import FACTORS, LABEL, compute_factors_and_labels, evaluate_cross_sections, summarize
from webapp.backend.app import data, store
from webapp.backend.app.settings import MAX_RESEARCH_GRID_CELLS, STORAGE


@contextmanager
def exclusive_worker():
    path = STORAGE / "worker.lock"
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def checkpoint(identifier, percent, message):
    store.progress(identifier, percent, message)
    return store.job(identifier)["status"] != "cancelled"


def research_calendar_window(calendar, start, end):
    """Keep the 20 prior exchange sessions needed by the fixed factor set.

    Bounds operate on the saved calendar, never on a stock's observed rows.
    Reading no sessions after ``end`` also keeps future prices out of the task;
    labels which would cross that date are naturally unavailable.
    """
    calendar = pd.DatetimeIndex(calendar, name="date")
    if (calendar.hasnans or not calendar.is_unique
            or not calendar.is_monotonic_increasing):
        raise ValueError("Calendar must contain unique, sorted, non-missing sessions")
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError("研究日期无效")
    left = calendar.searchsorted(start, side="left")
    right = calendar.searchsorted(end, side="right")
    if left >= right:
        raise ValueError("选择的日期范围没有交易日")
    return calendar[max(0, left - 20):right]


def execute(item):
    identifier = item["id"]
    try:
        if not checkpoint(identifier, 10, "读取数据与交易日历"):
            return
        dataset = store.dataset(item["dataset_id"])
        config = item["config"]
        calendar = research_calendar_window(
            pd.read_parquet(STORAGE / "datasets" / dataset["id"] / "calendar.parquet").date,
            config["start"], config["end"])
        input_start, input_end = str(calendar[0].date()), str(calendar[-1].date())
        if len(calendar) * len(dataset["symbols"]) > MAX_RESEARCH_GRID_CELLS:
            raise ValueError("研究窗口加预热超过 500 万日期 × 证券格点，请缩短日期范围")
        panel = data.read_bars(dataset["id"], start=input_start, end=input_end,
                               columns=data.COLUMNS)
        if dataset["quality"]["invalid_rows"]:
            raise ValueError("数据包含 OHLC 异常，拒绝计算")
        panel.loc[panel.volume.le(0), ["open", "close", "volume"]] = np.nan
        if not checkpoint(identifier, 25, f"计算 3 个因子；输入 {len(panel)} 行"):
            return
        frame = compute_factors_and_labels(panel, calendar)
        selected = frame.loc[frame.date.between(config["start"], config["end"])].copy()
        selected.loc[selected.exit_date.gt(pd.Timestamp(config["end"])), LABEL] = np.nan
        if selected.empty:
            raise ValueError("选择的日期范围没有交易记录")
        if not checkpoint(identifier, 55, "计算日截面 Rank IC 和五组收益"):
            return
        daily, groups = evaluate_cross_sections(selected, min_pairs=config["min_pairs"])
        summary = summarize(daily, groups)
        if not summary.valid_ic_days.gt(0).any():
            raise ValueError("没有有效 IC 样本：需更长日期范围、至少 5 个有效证券以及因子预热数据")
        ranks = selected.groupby("date")[list(FACTORS)].rank(pct=True)
        correlation = ranks.corr().reset_index().rename(columns={"index": "factor"})
        if not checkpoint(identifier, 85, "保存结果、配置与研究报告"):
            return
        folder = STORAGE / "results" / identifier
        folder.mkdir(exist_ok=True)
        summary.to_csv(folder / "summary.csv", index=False)
        daily.to_csv(folder / "daily_ic.csv", index=False)
        groups.to_csv(folder / "groups.csv", index=False)
        selected.to_parquet(folder / "factors.parquet", index=False)
        result = {"job_id": identifier, "dataset": dataset, "config": config,
                  "input_window": {"start": input_start, "end": input_end,
                                   "sessions": len(calendar), "rows": len(panel),
                                   "warmup_sessions_requested": 20,
                                   "calendar_basis": "saved_dataset_calendar",
                                   "after_end_prices_read": False},
                  "completed_at": store.now(), "summary": data.records(summary),
                  "daily_ic": data.records(daily), "correlation": data.records(correlation),
                  "warnings": dataset["warnings"] + [
                      "t 日收盘信号；标签为 t+1 开盘至 t+6 开盘收益，跨研究结束日标签剔除",
                      "五日标签重叠；分组平均收益不是可复利组合净值，未扣交易成本",
                      "没有运行组合回测、模型训练或实盘交易"],
                  "science_evidence": False, "live_trading": False}
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        report = ["# 因子研究结果", "", f"任务：{identifier}", f"数据：{dataset['name']} ({dataset['kind']})",
                  f"来源：{dataset['source']}", "", "```json", json.dumps(config, ensure_ascii=False, indent=2), "```",
                  "", "```text", summary.to_string(index=False), "```", "", "## 边界", ""]
        report += [f"- {value}" for value in result["warnings"]]
        (folder / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        store.progress(identifier, 100, "因子研究完成，结果已保存", status="completed")
    except Exception as error:
        traceback.print_exc()
        store.progress(identifier, 0, "任务失败，未生成有效完成状态", status="failed", error=f"{type(error).__name__}: {error}")


def main():
    store.initialize()
    with exclusive_worker():
        store.recover_interrupted()
        stopped = threading.Event()
        def keep_alive():
            while not stopped.is_set():
                store.heartbeat(os.getpid())
                stopped.wait(3)
        thread = threading.Thread(target=keep_alive, daemon=True)
        thread.start()
        try:
            while True:
                item = store.claim_job()
                if item:
                    execute(item)
                else:
                    time.sleep(.5)
        finally:
            stopped.set()
            thread.join(4)


if __name__ == "__main__":
    main()
