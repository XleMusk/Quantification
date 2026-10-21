"""Explicit post-hoc record-close repair on a copied diagnostic replay state."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from types import SimpleNamespace

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..engine import corporate_actions
from ..ledger import Ledger


METHOD_ID = "annual_quality_single_cash_action_record_close_repair_v1"
SHANGHAI = timezone(timedelta(hours=8))


def _json(value):
    # Unlike Python equality, this also distinguishes False from zero.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _daily_json(value):
    # CSV-loaded empty diagnostic cells are existing float NaNs. Compare their
    # unchanged representation without converting or rewriting the daily rows.
    def check(item):
        if isinstance(item, float) and math.isinf(item):
            raise ValueError("Infinite daily values are not supported")
        if isinstance(item, dict):
            for child in item.values():
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=True)


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Exact ISO action and checkpoint dates required")
    return value


def _amount(action, field):
    value = action.get(field)
    if value is None or isinstance(value, bool) or isinstance(value, str) and not value.strip():
        raise ValueError("Complete numeric cash-only terms required")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Complete numeric cash-only terms required") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("Complete numeric cash-only terms required")
    return number


def repair_record_close(checkpoint, config, action, *, reviewed_at):
    """Return a copied checkpoint and audit; never persist or advance a session.

    The caller must have loaded the terminal checkpoint with the existing replay
    loader and separately verified the issuer source. This helper neither grants
    source quality nor changes a replay's data compatibility checks.
    """
    if not isinstance(checkpoint, dict) or not isinstance(config, StrategyConfig):
        raise ValueError("A loaded replay checkpoint and StrategyConfig are required")
    config.validate()
    if checkpoint.get("config") != config.to_dict():
        raise ValueError("Original checkpoint configuration must be preserved")
    state, daily = checkpoint.get("state"), checkpoint.get("daily")
    if not isinstance(state, dict) or not isinstance(daily, list) or not daily:
        raise ValueError("A completed record-close replay checkpoint is required")
    context = checkpoint.get("context", RunContext("standalone_replay", TradingMode.REPLAY))
    if (not isinstance(context, RunContext) or context.mode != TradingMode.REPLAY
            or checkpoint.get("mode", TradingMode.REPLAY.value) != TradingMode.REPLAY.value
            or state.get("broker_binding") is not None or checkpoint.get("broker_binding") is not None):
        raise ValueError("Record-close repair is restricted to an unbound REPLAY account")
    if state.get("rights_replay_policy") is not None or checkpoint.get("rights_replay_policy") is not None:
        raise ValueError("Record-close repair requires the unchanged None rights binding")
    if not isinstance(action, dict) or action.get("action_kind") not in (None, ""):
        raise ValueError("Only an ordinary cash-only action can be repaired")
    record = _day(action.get("dividRegistDate"))
    ex = _day(action.get("dividOperateDate"))
    pay = _day(action.get("dividPayDate"))
    if (ex <= record or pay < ex or state.get("day") != record
            or not isinstance(daily[-1], dict) or daily[-1].get("date") != record):
        raise ValueError("Checkpoint must be the exact action record close before ex-date")
    stamp = datetime.fromisoformat(reviewed_at) if isinstance(reviewed_at, str) else reviewed_at
    if (not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None
            or stamp < datetime.combine(date.fromisoformat(record), time(15), SHANGHAI)):
        raise ValueError("Actual timezone-aware review clock must not predate record close")
    cash = _amount(action, "dividCashPsBeforeTax")
    shares = _amount(action, "dividStocksPs")
    reserve = _amount(action, "dividReserveToStockPs")
    if cash <= 0 or shares != 0 or reserve != 0 or action.get("dividStockMarketDate", "") not in (None, ""):
        raise ValueError("Only positive cash with explicit zero bonus and reserve is supported")
    symbol = action.get("symbol")
    if not isinstance(symbol, str) or not isinstance(state.get("entitlements"), dict):
        raise ValueError("Canonical action symbol and saved entitlements required")
    key = f"{symbol}:{ex}"
    if key in state["entitlements"]:
        raise ValueError("Existing entitlement must not be overwritten")
    for container in (state.get("orders", {}), state.get("fills", {}), state.get("events", [])):
        rows = container.values() if isinstance(container, dict) else container
        for row in rows:
            if not isinstance(row, dict) or _day(row.get("day")) > record:
                raise ValueError("A later consumed account event prevents record-close repair")
    lots = state.get("positions", {}).get(symbol, [])
    if (not isinstance(lots, list)
            or any(not isinstance(lot, dict) or type(lot.get("qty")) is not int or lot["qty"] < 0
                   for lot in lots)):
        raise ValueError("Exact nonnegative integer record-close lots are required")
    before_state, before_daily = _json(state), _daily_json(daily)
    repaired = deepcopy(checkpoint)
    book = Ledger(repaired["state"], config, context)
    if book.rule(symbol, record).is_fund:
        raise ValueError("Only ordinary equity cash actions are supported")
    quantity, equity = book.quantity(symbol), book.equity()
    if type(quantity) is not int or quantity < 0:
        raise ValueError("Exact nonnegative integer record-close quantity required")
    expected = deepcopy(state)
    expected["entitlements"][key] = quantity
    registration_feed = SimpleNamespace(actions=[deepcopy(action)],
        manifest={"historical_price_adjustments": []}, rights_replay_policy=None)
    corporate_actions(book, registration_feed, record, at_close=True)
    if (_json(repaired["state"]) != _json(expected) or _daily_json(repaired["daily"]) != before_daily
            or book.equity() != equity or _json(state) != before_state or _daily_json(daily) != before_daily):
        raise ValueError("Record-close repair changed state beyond its one new entitlement")
    audit = {"schema": "annual_quality_entitlement_repair_audit_v1", "method_id": METHOD_ID,
        "knowledge_mode": "posthoc_component_diagnostic", "symbol": symbol,
        "registration_date": record, "ex_date": ex, "pay_date": pay,
        "entitlement_key": key, "registered_quantity": quantity, "reviewed_at": stamp.isoformat(),
        "context": context.to_dict(), "source_checkpoint": checkpoint.get("source"),
        "only_added_entitlement": True, "daily_unchanged": True,
        "cash_equity_positions_orders_fills_pending_events_unchanged": True,
        "session_advanced": False, "historically_timely_signal": False,
        "historical_available_at_backfilled": False, "source_review_verified_by_helper": False,
        "strategy_validated": False, "selection_enabled": False, "execution_admitted": False}
    return repaired, audit
