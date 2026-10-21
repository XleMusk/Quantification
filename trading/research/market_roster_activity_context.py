"""Research-only gross quantity context over caller-validated price observations.

No provider calls, counter repair, cross-stock VWAP, free-float denominator or
price admission is performed. Old raw packets are reinterpreted only after the
saved semantic assessment was actually available.
"""
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path

from ..settings import DATA_ROOT
from .baostock_daily_slice import digest
from .index_observation import CN
from .index_observation_change import _time
from .leader_environment import ARCHETYPES
from .prior_dated_market_quote_snapshot import FLAGS as PRICE_FLAGS, SOURCE_SCOPE

SCHEMA = "market_roster_research_activity_context_v1"
METHOD_ID = "sina_ordinary_SH_SZ_A_shares_CNY_research_activity_v1"
CONTRACT_FILE = Path(__file__).resolve().parents[2] / "outputs/leader_environment_research/20261015_volume_amount_contract_v1/source_contract_assessment.json"
FLAGS = {**PRICE_FLAGS, "production_volume_or_orderbook_admitted": False, "turnover_rate_admitted": False,
         "net_capital_flow_identified": False, "cross_stock_VWAP_computed": False,
         "field_level_atomic_update_verified": False, "independent_trade_event_time_available": False}
TOLERANCE_RULE = "half_the_coarsest_decimal_quantum_of_original_quote_low_high_no_fitted_tolerance"


def _number(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_quantity")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid_quantity") from error
    if not number.is_finite():
        raise ValueError("nonfinite_quantity")
    if number < 0:
        raise ValueError("negative_quantity")
    return number


def _sanity(volume, amount, quote):
    if (volume == 0) != (amount == 0):
        raise ValueError("one_zero_one_positive_quantity")
    if volume == 0:
        return None
    low, high = [Decimal(quote[key]) for key in ("low_price_rmb", "high_price_rmb")]
    if not low.is_finite() or not high.is_finite() or min(low, high) <= 0 or low > high:
        raise ValueError("invalid_price_range")
    tolerance = max(Decimal(1).scaleb(low.as_tuple().exponent), Decimal(1).scaleb(high.as_tuple().exponent)) / 2
    if amount < (low - tolerance) * volume or amount > (high + tolerance) * volume:
        raise ValueError("quantity_VWAP_outside_latest_day_range")
    return amount / volume


def parse_quantity(quote):
    """Parse exact shares/CNY; caller price validity still governs usability."""
    try:
        with localcontext() as context:
            context.prec = 50
            volume, amount = [_number(quote.get(key)) for key in ("source_raw_volume", "source_raw_amount")]
            if volume != volume.to_integral_value():
                raise ValueError("fractional_share_quantity")
            vwap = _sanity(volume, amount, quote)
            return {"known": True, "reason": None, "volume": volume, "amount": amount, "vwap": vwap}
    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
        return {"known": False, "reason": str(error), "volume": None, "amount": None, "vwap": None}


def parse_delta(first, latest):
    """Use the price-selected window only; resets/corrections stay unknown."""
    a, b = parse_quantity(first), parse_quantity(latest)
    if not a["known"] or not b["known"]:
        return {"known": False, "reason": "baseline_" + a["reason"] if not a["known"] else "latest_" + b["reason"],
                "volume": None, "amount": None, "vwap": None}
    try:
        with localcontext() as context:
            context.prec = 50
            volume, amount = b["volume"] - a["volume"], b["amount"] - a["amount"]
            if volume < 0 or amount < 0:
                raise ValueError("negative_cumulative_delta_reset_or_revision_unknown")
            vwap = _sanity(volume, amount, latest)
            return {"known": True, "reason": None, "volume": volume, "amount": amount, "vwap": vwap}
    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
        return {"known": False, "reason": str(error), "volume": None, "amount": None, "vwap": None}


def _contract(read_at, derived_now):
    value = json.loads(CONTRACT_FILE.read_text(encoding="utf8"))
    if (value.get("schema") != "sina_ordinary_SH_SZ_A_volume_amount_research_contract_assessment_v1"
            or value.get("research_only") is not True
            or any(value.get(key) is not False for key in ("production_volume_or_orderbook_admitted", "is_BrokerQuote",
                        "all_A_breadth", "turnover_rate_admitted", "net_capital_flow_identified", "participant_intent_inferred",
                        "current_signal_admitted", "selection_enabled", "execution_admitted", "orders_generated"))):
        raise ValueError("Research quantity contract boundary changed")
    available = _time(value["actual_derived_at"])
    if available > read_at or available > derived_now:
        raise ValueError("Quantity contract was not yet actually available at this read")
    a = value["assessment"]
    if (set(a["ordinary_scope"]) != {"SH60/688 ordinary A shares", "SZ00/30 ordinary A shares"}
            or a["field_8"].get("proposed_research_unit") != "shares"
            or a["field_8"].get("raw_to_research_scale") != "1"
            or a["field_9"].get("proposed_research_unit") != "CNY yuan"
            or a["field_9"].get("raw_to_research_scale") != "1; no multiplication by 100 or 10000"
            or any(a[key].get("research_unit_hypothesis_supported") is not True for key in ("field_8", "field_9"))):
        raise ValueError("Ordinary-A shares/CNY unit hypothesis changed")
    return {"file": str(CONTRACT_FILE), "sha256": digest(CONTRACT_FILE), "available_at": available.isoformat()}


def _base(mode, read_at, clocks):
    return {"schema": SCHEMA, "method_id": METHOD_ID, "mode": mode, "status": "unavailable_activity_context",
            "read_at": str(read_at), "derived_now": datetime.now(CN).isoformat(), "contract_available_at": None,
            "contract": None, "source_clocks": clocks, "current_summary": None, "observation_summary": None,
            "read_summary": None, "groups": [], "archetype_groups": [], "tolerance_rule": TOLERANCE_RULE,
            "price_admission_changed": False, "source_scope": SOURCE_SCOPE, "provider_contacted": False,
            "source_clock_basis": "shared_packet_clock_not_independent_trade_event_time",
            "amount_semantics": "gross_traded_activity_not_net_money_flow", **FLAGS}


def _check(targets, *rows):
    expected = [target["symbol"] for target in targets]
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("Full unique target inventory is required")
    if any(len(items) != len(targets) or [row["symbol"] for row in items] != expected for items in rows):
        raise ValueError("Activity context cannot drop or reorder price targets")


def _activity(quality, quantities, *, window=False, current=False):
    result = []
    for price, quantity in zip(quality, quantities):
        known_price = bool(price["paired"] and (not current or price["current_at_read"])) if window else price["current_at_read"]
        result.append({"price_known": known_price, **quantity} if known_price else
                      {"price_known": False, "known": False, "reason": "price_pair_unavailable" if window else "price_observation_unavailable",
                       "volume": None, "amount": None})
    return result


def _summary(rows, window):
    known = [row for row in rows if row["known"]]
    n, k = len(rows), len(known)
    with localcontext() as context:
        context.prec = 50
        amount = sum((row["amount"] for row in known), Decimal(0)) if known else None
        volume = sum((row["volume"] for row in known), Decimal(0)) if known else None
    amount_text = ("0" if amount == 0 else format(amount, "f")) if amount is not None else None
    volume_text = str(int(volume)) if volume is not None else None
    return {"member_count": n, "price_known_count": sum(row["price_known"] for row in rows),
            "price_pair_count": sum(row["price_known"] for row in rows) if window else None,
            "known_activity_count": k, "unknown_activity_count": n - k,
            "zero_activity_count": sum(row["amount"] == 0 for row in known),
            "known_amount_cny": amount_text, "known_volume_shares": volume_text,
            "full_group_amount_cny": amount_text if n and n == k else None,
            "full_group_volume_shares": volume_text if n and n == k else None,
            "unknown_reason_counts": dict(Counter(row["reason"] for row in rows if not row["known"]))}


def _finish(result, targets, read_rows, saved_rows, contract, window):
    current, observed = _summary(read_rows, window), _summary(saved_rows, window)
    has_current_price = current["price_known_count"] > 0
    def group(id_, name, indices):
        return {"id": id_, "name": name, "member_count": len(indices),
                "current_summary": _summary([read_rows[i] for i in indices], window) if has_current_price else None,
                "observation_summary": _summary([saved_rows[i] for i in indices], window)}
    predicates = [("all", "完整前交易日名册", lambda target: True),
                  ("fixed_590", "原固定590集合成员", lambda target: target["in_fixed_590"]),
                  ("rest", "名册其余成员", lambda target: not target["in_fixed_590"]),
                  ("prior_day_nonST", "前交易日非ST背景", lambda target: target["background_isST"] == "0"),
                  ("prior_day_ST", "前交易日ST背景", lambda target: target["background_isST"] == "1"),
                  ("prior_day_active_nonST", "前交易日正常交易非ST背景", lambda target: target["background_isST"] == "0" and target["background_trade_status"] == "1")]
    return {**result, "status": "observed_research_quantity_activity", "contract": contract,
            "contract_available_at": contract["available_at"], "target_count": len(targets),
            "current_summary": current if has_current_price else None, "read_summary": current,
            "observation_summary": observed,
            "groups": [group(id_, name, [i for i, target in enumerate(targets) if predicate(target)]) for id_, name, predicate in predicates],
            "archetype_groups": [group(id_, name, [i for i, target in enumerate(targets) if id_ in target["fixed_590_labels"]]) for id_, name in ARCHETYPES]}


def cumulative_activity_context(*, read_rows, saved_rows, quotes, targets, source_clocks, read_at, data_root=DATA_ROOT):
    result = _base("cumulative", read_at, source_clocks)
    try:
        contract = _contract(_time(read_at), _time(result["derived_now"]))
        _check(targets, read_rows, saved_rows, quotes)
        quantities = [parse_quantity(quote) for quote in quotes]
        return _finish(result, targets, _activity(read_rows, quantities), _activity(saved_rows, quantities), contract, False)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        return {**result, "error": f"{type(error).__name__}: {error}"}


def window_activity_context(*, pairs, baseline_quotes, latest_quotes, targets, source_clocks, read_at, data_root=DATA_ROOT):
    result = _base("same_watch_window_delta", read_at, source_clocks)
    try:
        contract = _contract(_time(read_at), _time(result["derived_now"]))
        _check(targets, pairs, baseline_quotes, latest_quotes)
        quantities = [parse_delta(first, latest) for first, latest in zip(baseline_quotes, latest_quotes)]
        return _finish(result, targets, _activity(pairs, quantities, window=True, current=True),
                       _activity(pairs, quantities, window=True), contract, True)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        return {**result, "error": f"{type(error).__name__}: {error}"}
