"""Read-only chart inputs from saved research snapshots and the shared ledger.

No acquisition, strategy execution, account mutation or inferred market fields
belong here.  The browser may aggregate daily observations, but these endpoints
never manufacture intraday candles or volume-at-price observations.
"""

from datetime import date
import math

from fastapi import HTTPException
import pandas as pd
import pyarrow.parquet as pq

from . import data, settings, store
from trading import store as trading_store
from trading.feed import HistoricalFeed
from trading.report import quote_rmb


FACTOR_IDS = ("reversal_5", "momentum_20", "relative_volume_20")
BAR_FIELDS = ("open", "high", "low", "close", "volume")


def cutoff(value):
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("as_of 必须为 YYYY-MM-DD 日期") from error
    if parsed.isoformat() != value:
        raise ValueError("as_of 必须为 YYYY-MM-DD 日期")
    return value


def _symbol(requested, symbols):
    selected = requested or (symbols[0] if symbols else None)
    if selected is None or selected not in symbols:
        raise ValueError("证券代码不在此数据集中")
    return selected


def dataset_chart(identifier, symbol=None, as_of=None):
    item = store.dataset(identifier)
    if item is None:
        raise HTTPException(404, "数据集不存在")
    selected, end = _symbol(symbol, item["symbols"]), cutoff(as_of)
    frame = data.read_bars(identifier, symbol=selected, end=end).sort_values("date")
    calendar = pd.read_parquet(settings.STORAGE / "datasets" / identifier / "calendar.parquet").date
    if end:
        frame = frame.loc[frame.date.le(pd.Timestamp(end))]
        calendar = calendar.loc[calendar.le(pd.Timestamp(end))]
    # Cash amount is compatible with raw RMB prices only. A dataset containing
    # adjusted prices and raw amount must not enable a raw-price VWAP overlay.
    declared = set(item.get("fields", frame.columns))
    fields = [field for field in (*BAR_FIELDS, *data.OPTIONAL_FIELDS) if field in frame and field in declared]
    capable = item["adjustment"] == "raw" and "amount" in fields
    complete = False
    if capable:
        valid_amount = (frame.amount.notna() & frame.amount.ge(0)
                        & ((frame.volume.gt(0) & frame.amount.gt(0))
                           | (frame.volume.eq(0) & frame.amount.eq(0))))
        # Preserve valid prefixes: a later amount gap must not suppress an
        # earlier replay's indicator. The client checks its visible prefix.
        frame["turnover"] = frame.amount.where(valid_amount)
        complete = bool(len(frame) and valid_amount.all())
        fields.append("turnover")
    return {"dataset": item, "symbol": selected, "prices": data.records(frame),
            "calendar": pd.to_datetime(calendar).dt.strftime("%Y-%m-%d").tolist(),
            "source_period": "D", "price_basis": item["adjustment"],
            "source_kind": "research_snapshot", "realtime": False,
            "fields": fields, "available_fields": fields, "as_of": end,
            "volume_unit": "share", "turnover_unit": "CNY" if capable else None,
            "field_units": item.get("field_units", {}), "turnover_available": capable,
            "turnover_complete": complete,
            "turnover_reason": None if complete else "累计均价需要可见区间完整成交额（元）、成交量（股）和未复权价格"}


def job_signals(identifier, symbol=None, as_of=None):
    job = store.job(identifier)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job["status"] != "completed":
        raise HTTPException(409, "任务尚未完成，没有可用因子结果")
    dataset = store.dataset(job["dataset_id"])
    if dataset is None:
        raise HTTPException(404, "研究数据集不存在")
    selected, end = _symbol(symbol, dataset["symbols"]), cutoff(as_of)
    path = settings.STORAGE / "results" / identifier / "factors.parquet"
    if not path.is_file():
        raise HTTPException(404, "保存的因子明细不存在")
    required = {"date", "symbol", *FACTOR_IDS}
    if not required.issubset(pq.read_schema(path).names):
        raise HTTPException(409, "保存结果缺少可用因子列")
    filters = [("symbol", "=", selected), ("date", ">=", pd.Timestamp(job["config"]["start"])),
               ("date", "<=", pd.Timestamp(min(job["config"]["end"], end or job["config"]["end"])))]
    frame = pd.read_parquet(path, columns=["date", "symbol", *FACTOR_IDS], filters=filters)
    frame["date"] = pd.to_datetime(frame.date)
    frame = frame.loc[frame.date.between(pd.Timestamp(job["config"]["start"]),
                                       pd.Timestamp(job["config"]["end"]))]
    if end:
        frame = frame.loc[frame.date.le(pd.Timestamp(end))]
    # Export the causal values only; labels, future entry/exit dates and IC are
    # evaluation artifacts and must never masquerade as chart-time signals.
    frame = frame.sort_values("date").replace([float("inf"), -float("inf")], float("nan"))
    return {"job_id": identifier, "dataset_id": dataset["id"], "dataset": dataset,
            "symbol": selected, "price_basis": dataset["adjustment"], "config": job["config"],
            "source_kind": "saved_factor_job", "available_at": "session_close",
            "factors": list(FACTOR_IDS), "values": data.records(frame),
            "as_of": end, "realtime": False,
            "method": {"reversal_5": "-(close[t] / close[t-5 calendar sessions] - 1)",
                       "momentum_20": "close[t] / close[t-20 calendar sessions] - 1",
                       "relative_volume_20": "volume[t] / mean(volume[t-20:t-1 calendar sessions])"}}


def run_chart(identifier, symbol=None, as_of=None):
    run = trading_store.get(identifier, full=True)
    if run is None:
        raise HTTPException(404, "运行不存在")
    requested_end = cutoff(as_of)
    state = run["state"]
    if run["mode"] == "paper":
        raise HTTPException(409, "日终纸面账户尚未保存逐日行情版本，请选择历史回放账户或研究快照")
    if state.get("broker_binding"):
        # A broker observation does not prove that an arbitrary dataset is the
        # same raw-price feed as the observed account. Keep that route separate.
        raise HTTPException(409, "券商观察账户没有已核实的同源冻结行情，暂不叠加成交")
    if not run.get("dataset"):
        raise HTTPException(409, "运行没有保存行情快照")
    try:
        feed = HistoricalFeed(run["dataset"])
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(409, "运行保存的行情快照不可读取或未通过校验") from error
    selected = _symbol(symbol, feed.symbols)
    # A running/paused account cannot expose candles beyond the day actually
    # reached by that ledger. Earlier rows remain available for indicator warmup.
    observed_day = state.get("day") or run["start"]
    end = min(run["end"], observed_day, requested_end or run["end"])
    raw = feed.raw.loc[feed.raw.symbol.eq(selected) & feed.raw.date.le(end)].copy()
    trading = raw.tradestatus.eq(1)
    positive_prices = raw[["open", "high", "low", "close"]].gt(0).all(axis=1)
    valid = raw.loc[trading & positive_prices].sort_values("date")
    columns = ["date", "symbol", *BAR_FIELDS]
    if "amount" in valid:
        valid["turnover"] = valid["amount"]
        columns.append("turnover")
    fields = list(BAR_FIELDS) + (["turnover"] if "turnover" in columns else [])
    turnover_usable = bool(len(valid) and "turnover" in valid
                           and valid.turnover.map(math.isfinite).all() and valid.turnover.ge(0).all()
                           and valid.loc[valid.volume.gt(0), "turnover"].gt(0).all())
    fills = []
    for execution_id, fill in state["fills"].items():
        if fill["symbol"] != selected or fill["day"] > end:
            continue
        unit = fill.get("price_unit", "cent")
        fills.append({**fill, "execution_id": execution_id, "date": fill["day"],
                      "price_unit": unit, "price_rmb": quote_rmb(fill["price"], unit),
                      "fees_rmb": fill.get("fees") / 100 if fill.get("fees") is not None else None})
    fills.sort(key=lambda row: (row["day"], row["execution_id"]))
    signals = []
    for event in state["events"]:
        if event["kind"] != "targets_generated" or event["day"] > end:
            continue
        targets = event.get("targets") or {}
        # An explicit zero is a saved full-target decision, not a future return.
        signals.append({"date": event["day"], "symbol": selected,
                        "target_weight": targets.get(selected, 0),
                        "signal_date": event.get("signal_date", event["day"]),
                        "execution_not_before": event.get("execution_not_before"),
                        "signal_source": event.get("signal_source"),
                        "decision_reason": event.get("decision_reason"),
                        "available_at": "session_close"})
    snapshot = {key: feed.manifest[key] for key in ("status", "kind", "source", "version", "start", "end")
                if key in feed.manifest}
    return {"run": {key: run[key] for key in ("id", "name", "mode", "status", "start", "end", "day")},
            "symbol": selected, "symbols": feed.symbols, "prices": data.records(valid[columns]),
            "calendar": [day for day in feed.calendar if day <= end],
            "fills": fills, "signals": signals, "source_kind": "run_snapshot",
            "source_period": "D", "price_basis": "raw", "realtime": False,
            "snapshot": snapshot, "fields": fields, "available_fields": fields,
            "snapshot_binding": "saved_historical_replay",
            "as_of": end, "volume_unit": "share", "turnover_unit": "CNY",
            "turnover_available": turnover_usable,
            "excluded_nontrading_rows": int((~(trading & positive_prices)).sum()),
            "execution_source": "simulator", "live_trading": False}
