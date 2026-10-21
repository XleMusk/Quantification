"""Read saved target/quantity diagnostics without running or changing an account.

Opening-reference values and saved close valuations have different prices and
denominators. Their gross ratios are displayed together, never as a cash/PnL
bridge. A configured gross limit is only a cap.
"""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import mean

from .rules import notional_cents


def _number(value, *, strings=False):
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float, Decimal) + ((str,) if strings else ())):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _integer(value, *, positive=False):
    number = _number(value)
    if number is None or number != number.to_integral_value() or number < (1 if positive else 0):
        return None
    return int(number)


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _close(row, warnings):
    values = {key: _number(row.get(key), strings=True) for key in ("equity", "cash", "receivables")}
    if any(value is None for value in values.values()) or values["equity"] <= 0:
        warnings.append("saved_close_equity_cash_or_receivables_unknown")
        return None
    invested = values["equity"] - values["cash"] - values["receivables"]
    if values["cash"] < 0 or values["receivables"] < 0 or invested < 0:
        warnings.append("saved_close_components_invalid")
        return None
    return float(invested / values["equity"])


def _stale(row, warnings):
    value = row.get("stale_symbols")
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            value = None
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        warnings.append("saved_stale_valuation_flag_unknown")
        return None
    return bool(value)


def _trace(rows, warnings):
    if not rows:
        return None, None, None
    symbols = [row.get("symbol") for row in rows]
    if any(not isinstance(symbol, str) or not symbol for symbol in symbols):
        warnings.append("diagnostic_symbol_unknown")
        return None, None, None
    if len(symbols) != len(set(symbols)):
        warnings.append("duplicate_symbol_diagnostics_no_last_row_selection")
        return None, None, None

    weights = [_number(row.get("target_weight")) for row in rows]
    if any(value is None for value in weights):
        target = None
        warnings.append("target_weight_unknown_or_quantity_retention_not_zero")
    elif any(value < 0 or value > 1 for value in weights):
        target = None
        warnings.append("target_weight_invalid")
    else:
        target = float(sum(weights, Decimal(0)))

    equities = [_integer(row.get("open_equity_cents"), positive=True) for row in rows]
    if any(value is None for value in equities) or len(set(equities)) != 1:
        warnings.append("opening_equity_unknown_or_inconsistent")
        return target, None, None
    target_values, held_values = [], []
    valid_reference = True
    for row in rows:
        symbol = row["symbol"]
        mark = _integer(row.get("engine_mark"), positive=True)
        unit = row.get("price_unit")
        if row.get("has_execution_open") is not True:
            warnings.append("fresh_execution_open_not_saved:" + symbol)
            valid_reference = False
        if mark is None or unit not in {"cent", "mill"}:
            warnings.append("reference_mark_or_explicit_quote_unit_invalid:" + symbol)
            valid_reference = False
        wanted = _integer(row.get("engine_target_quantity"))
        held = _integer(row.get("held_after"))
        if wanted is None or held is None:
            warnings.append("target_or_held_quantity_unknown:" + symbol)
        if mark is not None and unit in {"cent", "mill"}:
            target_values.append(notional_cents(mark, wanted, unit) if wanted is not None else None)
            held_values.append(notional_cents(mark, held, unit) if held is not None else None)
    if not valid_reference:
        return target, None, None
    equity = equities[0]
    lot = sum(target_values) / equity if all(value is not None for value in target_values) else None
    realized = sum(held_values) / equity if all(value is not None for value in held_values) else None
    return target, lot, realized


def _mean(values):
    return float(mean(values)) if values else None


def analyze(config: dict, state: dict, daily: list[dict]) -> dict:
    """Project saved facts only; unknown inputs never become zero exposure.

    ``daily`` money fields are saved RMB values (JSON numbers or CSV strings).
    Diagnostic ticks and their common opening-equity denominator are cents;
    mill-price conversion uses the same notional rounding as the ledger.
    ``state.events`` is the event source. Embedded copies in ``daily.events``
    are deliberately not concatenated with it.
    """
    saved_days, diagnostics = defaultdict(list), defaultdict(list)
    warnings = []
    for row in daily:
        day = _day(row.get("date")) if isinstance(row, dict) else None
        if day is None:
            warnings.append("saved_daily_row_date_unknown")
        else:
            saved_days[day].append(row)
    events = state.get("events")
    if not isinstance(events, list):
        warnings.append("saved_state_event_list_missing_no_trace_inference")
        events = []
    for row in events:
        if not isinstance(row, dict) or row.get("kind") != "target_execution_diagnostic":
            continue
        day = _day(row.get("day"))
        if day is None:
            warnings.append("diagnostic_row_date_unknown")
        else:
            diagnostics[day].append(row)

    reasons, reason_days = Counter(), defaultdict(set)
    sessions = []
    for day in sorted(set(saved_days) | set(diagnostics)):
        own_warnings = []
        saved, rows = saved_days.get(day, []), diagnostics.get(day, [])
        if len(saved) == 1:
            close = _close(saved[0], own_warnings)
            stale = _stale(saved[0], own_warnings)
        else:
            close, stale = None, None
            own_warnings.append("duplicate_saved_daily_date" if saved else "diagnostic_date_missing_saved_daily")
        target, lot, realized = _trace(rows, own_warnings)
        mechanical, constraints = Counter(), Counter()
        for row in rows:
            reason = row.get("mechanical_reason")
            if isinstance(reason, str) and reason:
                mechanical[reason] += 1
            else:
                own_warnings.append("mechanical_reason_unknown")
            flags = row.get("constraint_flags")
            if not isinstance(flags, list) or any(not isinstance(flag, str) or not flag for flag in flags):
                own_warnings.append("constraint_flags_unknown")
            else:
                constraints.update(set(flags))  # A reason counts at most once per saved row.
        for category, counts in (("mechanical", mechanical), ("constraint", constraints)):
            for reason, count in counts.items():
                reasons[category, reason] += count
                reason_days[category, reason].add(day)
        matched = len(saved) == 1 and all(value is not None for value in (target, lot, realized, close))
        sessions.append({
            "date": day, "saved_daily_rows": len(saved), "diagnostic_rows": len(rows),
            "target_adopted_gross": target, "lot_target_gross": lot,
            "realized_reference_gross": realized, "close_gross": close,
            "target_to_lot_gap": target - lot if target is not None and lot is not None else None,
            "lot_to_realized_gap": lot - realized if lot is not None and realized is not None else None,
            "target_to_realized_gap": target - realized if target is not None and realized is not None else None,
            "matched": matched, "stale_valuation": stale,
            "warnings": sorted(set(own_warnings)),
            "mechanical_reasons": dict(sorted(mechanical.items())),
            "constraint_reasons": dict(sorted(constraints.items())),
        })

    matched = [row for row in sessions if row["matched"]]
    known_close = [row for row in sessions if row["saved_daily_rows"] == 1 and row["close_gross"] is not None]
    cap = _number(config.get("gross_limit"))
    if cap is None or not 0 <= cap <= 1:
        cap = None
        warnings.append("configured_gross_limit_unknown_or_invalid")
    summary = {
        "configured_gross_limit": float(cap) if cap is not None else None,
        "committed_sessions": len(saved_days), "saved_daily_rows": len(daily),
        "trace_sessions": sum(bool(row["diagnostic_rows"]) for row in sessions),
        "matched_sessions": len(matched), "matched_dates": [row["date"] for row in matched],
        "close_gross_known_sessions": len(known_close),
        "mean_adopted_target_gross": _mean([row["target_adopted_gross"] for row in matched]),
        "mean_lot_target_gross": _mean([row["lot_target_gross"] for row in matched]),
        "mean_realized_reference_gross": _mean([row["realized_reference_gross"] for row in matched]),
        "mean_close_gross_on_matched_sessions": _mean([row["close_gross"] for row in matched]),
        "mean_close_gross_all_saved_sessions": _mean([row["close_gross"] for row in known_close]),
        "mean_target_to_lot_gap": _mean([row["target_to_lot_gap"] for row in matched]),
        "mean_lot_to_realized_gap": _mean([row["lot_to_realized_gap"] for row in matched]),
        "mean_target_to_realized_gap": _mean([row["target_to_realized_gap"] for row in matched]),
        "last_saved_day": max(saved_days, default=None),
        "stale_valuation_sessions": sum(row["stale_valuation"] is True for row in sessions if row["saved_daily_rows"] == 1),
        "stale_valuation_unknown_sessions": sum(row["stale_valuation"] is None for row in sessions if row["saved_daily_rows"] == 1),
    }
    return {
        "summary": summary, "sessions": sessions,
        "reason_counts": [
            {"category": category, "reason": reason, "count": count,
             "days": sorted(reason_days[category, reason]), "day_count": len(reason_days[category, reason])}
            for (category, reason), count in sorted(reasons.items())
        ],
        "warnings": sorted(set(warnings)),
        "notes": [
            "gross_limit is a configured cap, not a target, filled position, or observed mean.",
            "Opening-reference quantities use explicit cent/mill ticks and one positive opening equity; fees and cash are not bridged by these ratios.",
            "Adopted weights, lot targets, reference holdings and close gross means use the same matched date intersection; the all-saved-session close mean has its own denominator.",
            "Saved close gross uses (equity-cash-receivables)/equity at saved close marks; its prices and denominator differ from opening-reference ratios.",
            "Unknown/quantity-retention target weights, old or missing execution opens, duplicate symbols and invalid units are not zeros or last-row replacements.",
            "No diagnostic trace does not establish NO_CHANGE, an empty target, or an empty position. Saved state events are used once; daily embedded event copies are not added.",
            "Reason counts are saved diagnostic-row occurrences and distinct dates, may overlap, and are neither loss amounts nor unique-stock or trade counts.",
            "Stale close valuations retain their saved numeric valuation and an explicit stale flag; they are not independently fresh quotes.",
        ],
    }
