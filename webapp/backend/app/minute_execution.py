"""Read frozen market context and preview a disposable minute Ledger copy."""

from dataclasses import replace
import math

from trading.config import StrategyConfig
from trading.contracts import SecurityId
from trading.feed import HistoricalFeed
from trading.minute_replay import instant, replay_minutes, unavailable
from trading import store as trading_store
from . import minute_data


def preview(payload):
    if not isinstance(payload, dict):
        raise ValueError("分钟执行预览须为 JSON 对象")
    required = {"minute_dataset_id", "symbol", "side", "qty", "limit_price", "available_at", "as_of"}
    if not required.issubset(payload):
        raise ValueError("缺少分钟快照、证券、方向、股数、限价、可用时间或截止时间")
    SecurityId(payload["symbol"])
    if not isinstance(payload["minute_dataset_id"], str) or not payload["minute_dataset_id"]:
        raise ValueError("分钟快照编号须为非空字符串")
    if not isinstance(payload["side"], str) or payload["side"] not in {"buy", "sell"} or type(payload["qty"]) is not int or not 1 <= payload["qty"] <= 1_000_000:
        raise ValueError("方向须为 buy / sell，数量须为 1–1000000 整数股")
    if type(payload["limit_price"]) not in (int, float) or not math.isfinite(payload["limit_price"]) or payload["limit_price"] <= 0:
        raise ValueError("限价须为正有限人民币价格")
    start, cutoff = instant(payload["available_at"]), instant(payload["as_of"])
    if start.date() != cutoff.date() or cutoff < start:
        raise ValueError("订单可用时间与截止时间须位于同一日期并顺序排列")
    day = start.date().isoformat()
    state_run_id = payload.get("run_id")
    context_run_id = payload.get("context_run_id") or payload.get("daily_feed_run_id") or state_run_id
    if any(value is not None and (not isinstance(value, str) or not value or len(value) > 128)
           for value in (state_run_id, context_run_id)):
        raise ValueError("账户或日线来源编号须为非空字符串")
    if not context_run_id:
        return unavailable("missing_frozen_daily_context_run", qty=payload["qty"])
    run = trading_store.get(context_run_id, full=True)
    if not run:
        return unavailable("daily_context_run_not_found", qty=payload["qty"])
    if run["mode"] != "replay" or not run.get("dataset"):
        return unavailable("daily_context_requires_pinned_historical_replay", qty=payload["qty"])
    try:
        feed = HistoricalFeed(run["dataset"])
    except (OSError, ValueError, KeyError):
        return unavailable("pinned_daily_context_unreadable", qty=payload["qty"])
    symbol = payload["symbol"]
    if symbol not in feed.instruments or day not in feed.calendar:
        return unavailable("symbol_or_day_not_in_daily_context", qty=payload["qty"])
    row = feed.rows.get(day, {}).get(symbol)
    if not row or any(row.get(key) is None for key in ("preclose", "isST", "tradestatus")):
        return unavailable("daily_status_or_reference_price_missing", qty=payload["qty"])
    if (str(row.get("adjustflag")) != "3" or row["isST"] not in {0, 1} or row["tradestatus"] not in {0, 1}):
        return unavailable("daily_context_status_or_raw_basis_unknown", qty=payload["qty"])
    # A copied previous-session account needs its corporate-action processing
    # before this diagnostic can change settled holdings. Keep that route clear
    # rather than mutating entitlements with incomplete intraday information.
    if any(action.get("symbol") == symbol and action.get("dividOperateDate") == day for action in feed.actions):
        return unavailable("corporate_action_session_requires_account_processing", qty=payload["qty"])
    if any(action.get("symbol") == symbol and action.get("date") == day
           for action in feed.manifest.get("historical_price_adjustments", [])):
        return unavailable("price_adjustment_session_requires_account_processing", qty=payload["qty"])
    state = None
    config = StrategyConfig(**run["config"]).validate()
    if state_run_id:
        state_run = run if state_run_id == context_run_id else trading_store.get(state_run_id, full=True)
        if not state_run or state_run.get("dataset") != run["dataset"] or state_run["mode"] != "replay":
            return unavailable("saved_account_and_daily_context_do_not_match", qty=payload["qty"])
        config, state = StrategyConfig(**state_run["config"]).validate(), state_run["state"]
        held = set(state.get("positions", {}))
        if any(action.get("symbol") in held and action.get("dividOperateDate") == day for action in feed.actions):
            return unavailable("held_security_action_requires_account_processing", qty=payload["qty"])
        if any(action.get("symbol") in held and action.get("date") == day
               for action in feed.manifest.get("historical_price_adjustments", [])):
            return unavailable("held_security_adjustment_requires_account_processing", qty=payload["qty"])
    elif payload["side"] == "sell":
        return unavailable("sell_requires_saved_settled_holdings", qty=payload["qty"])
    else:
        cash = payload.get("initial_cash", config.initial_cash)
        if type(cash) not in (int, float) or not math.isfinite(cash):
            raise ValueError("初始资金须为有限人民币金额")
        config = replace(config, initial_cash=cash).validate()
    if "participation" in payload:
        participation = payload["participation"]
        if type(participation) not in (int, float) or not math.isfinite(participation):
            raise ValueError("参与率须为有限小数")
        config = replace(config, participation=participation).validate()
    chart = minute_data.minute_chart(payload["minute_dataset_id"], symbol, "1m", cutoff.isoformat())
    market = {"price_basis": "raw", "pre_close": row["preclose"], "is_st": bool(row["isST"]),
              "tradestatus": row["tradestatus"], "listing_date": feed.instruments[symbol].get("ipoDate"),
              "out_date": feed.instruments[symbol].get("outDate"), "instrument_profiles": feed.instrument_profiles}
    result = replay_minutes(chart["prices"], symbol=symbol, side=payload["side"], qty=payload["qty"],
                            limit_price=payload["limit_price"], available_at=start.isoformat(), as_of=cutoff.isoformat(),
                            market=market, calendar=feed.calendar, config=config, state=state,
                            context_id="minute-preview:" + str(state_run_id or context_run_id))
    result.update(minute_dataset_id=payload["minute_dataset_id"], minute_fingerprint=chart["dataset"].get("fingerprint"),
                  daily_context={"run_id": context_run_id, "dataset": run["dataset"], "day": day,
                                 "source": feed.manifest.get("source"), "version": feed.manifest.get("version"),
                                 "fields_used": ["preclose", "isST", "tradestatus", "ipoDate", "outDate", "calendar"]},
                  account_source="saved_previous_session_copy" if state_run_id else "isolated_cash_example",
                  coverage=chart["coverage"], source_warnings=chart.get("warnings", []))
    return result
