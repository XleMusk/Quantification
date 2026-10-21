"""Conservative minute-OHLC diagnostics through the shared in-memory Ledger.

This module has no file, network or account-store writer. Minute close labels
are an explicit simulation assumption; order queue and tick paths are unknown.
"""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import math
from zoneinfo import ZoneInfo

from .config import StrategyConfig
from .contracts import ExecutionEvent, OrderIntent, Price, PriceBasis, RunContext, SecurityId, ShareQuantity, TradingMode
from .ledger import ACTIVE, Ledger, new_state
from .rules import listing_limit_reason, notional_cents
from .simulated_broker import SimulatedBrokerAdapter


TZ = ZoneInfo("Asia/Shanghai")
MODEL = "marketable_open_adverse_minute_range_v1"


def instant(value):
    if not isinstance(value, str):
        raise ValueError("分钟模拟时间须为带时区的 ISO 日期时间")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("分钟模拟时间无效") from error
    if moment.utcoffset() is None:
        raise ValueError("分钟模拟时间须明确时区，如 +08:00")
    return moment.astimezone(TZ)


def unavailable(reason, **metadata):
    return {"status": "unavailable", "reason": reason, "reasons": [reason], "fills": [],
            "filled_qty": 0, "unfilled_qty": metadata.pop("qty", None), "total_fees_rmb": None,
            "execution_admitted": False, "persistent_account_modified": False, **metadata}


def _continuous(end):
    stamp = (end.hour, end.minute, end.second, end.microsecond)
    return ((9, 31, 0, 0) <= stamp <= (11, 30, 0, 0)
            or (13, 1, 0, 0) <= stamp < (14, 58, 0, 0))


def replay_minutes(bars, *, symbol, side, qty, limit_price, available_at, as_of,
                   market, calendar, config=None, state=None, context_id="minute-preview"):
    """Fill only fully revealed, marketable continuous-session minute intervals.

    A buy needs minute open <= limit and receives min(high, limit); a sell
    needs open >= limit and receives max(low, limit). The fill is recorded at
    minute end, after that entire price/volume interval is observed. This is a
    deliberately adverse OHLC assumption, not evidence of queue execution.
    """
    SecurityId(symbol)
    if side not in {"buy", "sell"} or type(qty) is not int or qty <= 0:
        raise ValueError("方向须为 buy / sell，数量须为正整数股")
    start, cutoff = instant(available_at), instant(as_of)
    if start.date() != cutoff.date() or cutoff < start:
        raise ValueError("分钟模拟须在同一日期内，截止时间不得早于订单可用时间")
    day = start.date().isoformat()
    config = (config or StrategyConfig()).validate()
    if calendar != sorted(set(calendar)) or day not in calendar:
        return unavailable("missing_verified_session_calendar", qty=qty)
    index = calendar.index(day)
    if index + 1 >= len(calendar):
        return unavailable("missing_next_exchange_session_for_T1", qty=qty)
    if not market or market.get("price_basis") != "raw":
        return unavailable("missing_raw_daily_context", qty=qty)
    required = ("pre_close", "is_st", "tradestatus", "listing_date")
    if any(market.get(key) is None for key in required):
        return unavailable("incomplete_daily_eligibility_context", qty=qty)
    if type(market["is_st"]) is not bool or market["tradestatus"] not in {0, 1}:
        return unavailable("unknown_daily_security_status", qty=qty)
    try:
        pre_close = float(market["pre_close"])
        if not math.isfinite(pre_close) or pre_close <= 0:
            raise ValueError
        listing_reason = listing_limit_reason(market["listing_date"], day, calendar)
    except (TypeError, ValueError):
        return unavailable("invalid_daily_eligibility_context", qty=qty)
    if listing_reason:
        return unavailable(listing_reason, qty=qty)
    if market.get("out_date") and day >= market["out_date"]:
        return unavailable("delisted_or_out_of_instrument_span", qty=qty)
    if state is None and side == "sell":
        return unavailable("sell_requires_saved_settled_holdings", qty=qty)
    original = state if state is not None else new_state(config.initial_cash)
    trial = deepcopy(original)
    if trial.get("broker_binding"):
        return unavailable("broker_observation_cannot_receive_simulated_fills", qty=qty)
    if trial.get("day") is not None and (index == 0 or trial["day"] != calendar[index - 1]):
        return unavailable("saved_account_is_not_the_previous_session", qty=qty,
                           account_day=trial.get("day"), required_day=calendar[index - 1] if index else None)
    if state is not None and trial.get("day") is None and (trial.get("positions") or trial.get("fills") or trial.get("orders")):
        return unavailable("undated_account_state", qty=qty)
    context = RunContext(context_id, TradingMode.REPLAY)
    book = Ledger(trial, config, context, instrument_profiles=market.get("instrument_profiles"))
    broker = SimulatedBrokerAdapter(book)
    rule = book.rule(symbol, day)
    limit = Price.from_rmb(limit_price, PriceBasis.RAW, rule.price_unit)
    lower, upper = rule.limits(pre_close, day, market["is_st"])
    blocked = ("suspended" if market["tradestatus"] == 0 else
               "risk_warning_buy_blocked" if side == "buy" and market["is_st"] else
               "limit_price_outside_daily_range" if not lower <= limit.value <= upper else None)
    if blocked:
        return unavailable(blocked, qty=qty)
    if rule.same_day_resale:
        return unavailable("minute_stock_diagnostic_requires_T1_equity", qty=qty)
    marks_missing = [value for value in trial.get("positions", {})
                     if book.quantity(value) > 0 and not trial.get("prices", {}).get(value)]
    if marks_missing:
        return unavailable("held_security_valuation_missing", qty=qty, symbols=marks_missing)
    equity = book.equity()
    if equity <= 0:
        return unavailable("account_equity_unavailable", qty=qty)
    held_notional = book.position_value(symbol)
    buy_notional = limit.value * qty if rule.price_unit == "cent" else int(Decimal(limit.value * qty) / 10)
    pending_buys = [(order["symbol"], notional_cents(order["limit_price"], order["qty"] - order["filled"], order.get("price_unit", "cent")))
                    for order in trial["orders"].values() if order["side"] == "buy" and order["status"] in ACTIVE]
    pending_single = sum(value for security, value in pending_buys if security == symbol)
    if side == "buy" and (held_notional + pending_single + buy_notional) / equity > config.single_limit + 1e-12:
        return unavailable("single_security_risk_limit", qty=qty)
    gross = sum(book.position_value(value) for value in trial.get("positions", {}))
    if side == "buy" and (gross + sum(value for _, value in pending_buys) + buy_notional) / equity > config.gross_limit + 1e-12:
        return unavailable("gross_position_risk_limit", qty=qty)
    signature = json.dumps([symbol, side, qty, str(limit_price), start.isoformat(), cutoff.isoformat()], separators=(",", ":"))
    order_id = "minute-preview:" + sha256(signature.encode()).hexdigest()[:24]
    intent = OrderIntent(order_id, context, day, SecurityId(symbol), side, ShareQuantity(qty), limit, "isolated_minute_limit_diagnostic")
    order = broker.submit(intent)
    fills, considered, skipped = [], 0, {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    visible = []
    for bar in bars:
        end = instant(bar["date"])
        if end.date().isoformat() == day and end <= cutoff:
            visible.append((end, bar))
    if len(visible) > 241 or len({end for end, _ in visible}) != len(visible):
        raise ValueError("单日分钟数据须唯一，最多 241 条含开盘集合竞价记录")
    for end, bar in sorted(visible, key=lambda item: item[0]):
        interval_start = end - timedelta(minutes=1)
        if interval_start < start:
            skip("interval_started_before_order_available")
            continue
        if end.second or end.microsecond:
            raise ValueError("分钟结束标签须位于整分钟")
        if not _continuous(end) or bar.get("auction"):
            skip("auction_or_outside_continuous_session")
            continue
        if bar.get("symbol", symbol) != symbol or bar.get("is_partial") is True:
            skip("wrong_symbol_or_incomplete_interval")
            continue
        considered += 1
        try:
            quotes = {name: Price.from_rmb(bar[name], PriceBasis.RAW, rule.price_unit).value
                      for name in ("open", "high", "low", "close")}
        except (ValueError, KeyError):
            return unavailable("minute_quote_off_declared_tick", qty=qty, timestamp=end.isoformat(), model=MODEL)
        if quotes["high"] < max(quotes.values()) or quotes["low"] > min(quotes.values()):
            raise ValueError("分钟 OHLC 不一致")
        volume = bar.get("volume")
        if type(volume) not in (int, float) or not math.isfinite(volume) or volume < 0:
            raise ValueError("分钟成交量须为非负有限股数")
        if volume == 0:
            skip("zero_minute_volume")
            continue
        if not all(lower <= value <= upper for value in quotes.values()):
            skip("minute_outside_verified_daily_limit")
            continue
        if side == "buy" and quotes["open"] >= upper or side == "sell" and quotes["open"] <= lower:
            skip("price_limit_queue_unknown")
            continue
        if side == "buy" and quotes["open"] > limit.value or side == "sell" and quotes["open"] < limit.value:
            skip("limit_not_marketable_at_interval_open")
            continue
        if order["status"] not in {"submitted", "partial"}:
            continue
        capacity = int(Decimal(str(volume)) * Decimal(str(config.participation)))
        remaining = order["qty"] - order["filled"]
        part = rule.quantity.rounded(min(remaining, capacity), side, book.quantity(symbol))
        if part <= 0:
            skip("minute_participation_below_one_lot")
            continue
        price = min(quotes["high"], limit.value) if side == "buy" else max(quotes["low"], limit.value)
        execution_price = Price(price, PriceBasis.RAW) if rule.price_unit == "cent" else Price.from_mills(price, PriceBasis.RAW)
        event = ExecutionEvent(f"{order_id}:{end.isoformat()}", order_id, context, day, intent.security,
                               side, ShareQuantity(part), execution_price, calendar[index + 1])
        before_fees = order["fees"]
        broker.apply_execution(event)
        fills.append({"timestamp": end.isoformat(), "interval_start": interval_start.isoformat(), "qty": part,
                      "price_rmb": float(execution_price.rmb), "fees_rmb": (order["fees"] - before_fees) / 100,
                      "minute_volume": volume, "participation": part / volume, "available_day": calendar[index + 1]})
    # Working remainder is cancelled only on the disposable account copy.
    if order["status"] in {"submitted", "partial"}:
        broker.cancel(order_id, day=day)
    return {"status": "available", "reason": order.get("reason"),
            "reasons": [order["reason"]] if order.get("reason") else sorted(skipped),
            "model": MODEL, "symbol": symbol, "side": side, "qty": qty, "filled_qty": order["filled"],
            "unfilled_qty": qty - order["filled"], "total_fees_rmb": order["fees"] / 100,
            "fills": fills, "order": deepcopy(order), "considered_minutes": considered, "skipped_minutes": skipped,
            "available_at": start.isoformat(), "as_of": cutoff.isoformat(),
            "cash_before_rmb": original["cash"] / 100, "cash_after_rmb": trial["cash"] / 100,
            "sellable_after": book.available(symbol, day), "quantity_after": book.quantity(symbol),
            "execution_admitted": False, "persistent_account_modified": False,
            "execution_assumptions": {"participation": config.participation,
                                      "slippage_model": "adverse_minute_range_without_additional_fixed_bps"},
            "assumptions": ["供应商时间标签按分钟结束处理；安装版本标签语义待核对",
                            "仅连续竞价完整分钟，跳过开盘及收盘集合竞价和价格限制队列",
                            "只有分钟开盘已满足限价才模拟成交，使用该分钟内对订单不利的限价内价格",
                            "逐分钟实际成交量参与率限制；手续费、整手、资金及 T+1 复用原 Ledger",
                            "日线状态作为日期对应的历史研究假设，未证明盘中信息到达时间",
                            "分钟高低价与成交量在分钟结束后才用于记录模拟成交；不还原逐笔路径"]}
