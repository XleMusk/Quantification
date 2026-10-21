"""Explain saved close-lot estimates; never create orders or alternative PnL."""
from collections import Counter
from decimal import Decimal
from fractions import Fraction
import math

import pandas as pd

from ..portfolio_allocation import LOT_ALLOCATION
from ..rules import cents, execution_rule, notional_cents, quote_value


STAGES = ("price_transition", "equity_transition", "stored_weight_rounding",
          "binary_arithmetic", "minimum_step_rule", "subsequent_execution")


def saved_close_lot_transitions(account, traces, marks, config, *, profiles=None):
    allocations = [e for e in account["events"] if e["kind"] == "close_lot_allocation"]
    if not allocations:
        return pd.DataFrame(), {"status": "not_applicable", "allocation_anchors": 0}
    targets = {e["signal_date"]: e for e in account["events"] if e["kind"] == "targets_generated"}
    attempts = {(e["day"], e["symbol"]): e for e in traces}
    equity_by_day = {e["date"]: cents(e["equity"]) for e in marks}
    if (len(attempts) != len(traces) or len(equity_by_day) != len(marks)
            or len({e["day"] for e in allocations}) != len(allocations)):
        raise ValueError("Duplicate saved close-lot identities")
    rows = []
    for allocation in allocations:
        day = allocation["day"]
        target, equity = targets.get(day), equity_by_day.get(day)
        if (allocation["policy"] != LOT_ALLOCATION or not target or not equity or equity <= 0
                or target["day"] != day or target["execution_not_before"] <= day
                or allocation["sequence"] >= target["sequence"]):
            raise ValueError("Saved close-lot policy, equity or target timing changed")
        estimates = allocation["estimates"]
        if len({e["symbol"] for e in estimates}) != len(estimates):
            raise ValueError("Duplicate saved close-lot estimate")
        spent, budget = 0, Decimal(0)
        for estimate in estimates:
            symbol = estimate["symbol"]
            quantity, weight = estimate["quantity_at_decision_close"], estimate["target_weight"]
            rule = execution_rule(symbol, day, profiles)
            price = quote_value(estimate["close_rmb"], rule.price_unit)
            if (rule.is_fund or rule.price_unit != "cent" or price <= 0
                    or type(quantity) is not int or quantity < 0
                    or (quantity and (quantity < rule.quantity.minimum or quantity % rule.quantity.step))
                    or estimate["minimum_buy_quantity"] != rule.quantity.minimum
                    or not math.isfinite(weight) or weight < 0 or weight > config.single_limit
                    or target["targets"].get(symbol) != weight or weight != quantity * price / equity):
                raise ValueError("Saved close-lot quantity, mark or emitted weight changed")
            spent += quantity * price
            budget += Decimal(equity) * Decimal(str(estimate["original_weight"]))
            row = {"signal_date": day, "execution_date": target["execution_not_before"], "symbol": symbol,
                   "close_equity_cents": equity, "close_mark": price, "close_quantity": quantity,
                   "saved_target_weight": weight, "status": "missing_saved_attempt",
                   "has_execution_open": None, "open_equity_cents": None, "open_mark": None,
                   "price_only_quantity": None, "exact_close_weight_open_quantity": None,
                   "decimal_stored_weight_quantity": None, "engine_target_quantity": None,
                   "quantity_rule_only_quantity": None, "held_after": None,
                   "numeric_net_quantity_gap": None,
                   **{stage + "_quantity_gap": None for stage in STAGES},
                   **{stage + "_attempt_mark_gap_cny": None for stage in STAGES}}
            trace = attempts.get((row["execution_date"], symbol))
            if trace is None and weight == 0:
                row["status"] = "explicit_zero_budget_no_saved_attempt"
            elif trace:
                row["has_execution_open"] = trace["has_execution_open"]
                if trace["target_weight"] != weight:
                    row["status"] = "execution_target_override"
                elif not trace["engine_mark"]:
                    row["status"] = "saved_mark_unknown"
                else:
                    open_rule = execution_rule(symbol, row["execution_date"], profiles)
                    if (open_rule.price_unit != "cent" or trace["price_unit"] != "cent"
                            or trace["quantity_step"] != open_rule.quantity.step
                            or trace["minimum_buy_quantity"] != open_rule.quantity.minimum
                            or any(type(trace[k]) is not int or trace[k] < 0 for k in
                                   ("engine_mark", "open_equity_cents", "engine_target_quantity", "held_before", "held_after"))
                            or not trace["open_equity_cents"]):
                        raise ValueError("Saved close-lot execution amounts or rule changed")
                    mark, opening, step = trace["engine_mark"], trace["open_equity_cents"], open_rule.quantity.step
                    price_only = quantity * price // (mark * step) * step
                    exact = opening * quantity * price // (equity * mark * step) * step
                    decimal_weight = int(Fraction(opening) * Fraction(str(weight)) / (mark * step)) * step
                    legacy_binary = int(opening * weight / mark / step) * step
                    if trace["engine_target_quantity"] != legacy_binary:
                        raise ValueError("Saved target arithmetic is not the recorded legacy cent-stock rule")
                    desired, held = trace["engine_target_quantity"], trace["held_before"]
                    delta = desired - held
                    legal_delta = open_rule.quantity.rounded(abs(delta), "buy" if delta > 0 else "sell", held)
                    legal = held + (legal_delta if delta > 0 else -legal_delta)
                    stages = (quantity, price_only, exact, decimal_weight, desired, legal, trace["held_after"])
                    row.update(status="saved_attempt_decomposed", open_equity_cents=opening, open_mark=mark,
                               price_only_quantity=price_only, exact_close_weight_open_quantity=exact,
                               decimal_stored_weight_quantity=decimal_weight, engine_target_quantity=desired,
                               quantity_rule_only_quantity=legal, held_after=trace["held_after"],
                               numeric_net_quantity_gap=exact - desired)
                    for stage, before, after in zip(STAGES, stages, stages[1:]):
                        row[stage + "_quantity_gap"] = before - after
                        row[stage + "_attempt_mark_gap_cny"] = (before - after) * mark / 100
            rows.append(row)
        if spent != cents(allocation["allocated_stock_cny"]) or float(budget / 100) != allocation["stock_budget_cny"]:
            raise ValueError("Saved close-lot stock budget does not reconcile decision equity")
    frame = pd.DataFrame(rows)
    observed = frame.loc[frame.status.eq("saved_attempt_decomposed")]
    summary = {"status": "saved_attempts_only", "allocation_anchors": len(allocations),
               "estimate_records": len(frame), "observed_records": len(observed),
               "record_statuses": dict(Counter(frame.status)), "stages": {}}
    numeric = observed.numeric_net_quantity_gap
    summary["numeric_net_counts"] = {"reduction_records": int(numeric.gt(0).sum()),
                                     "increase_records": int(numeric.lt(0).sum()),
                                     "unchanged_records": int(numeric.eq(0).sum())}
    summary["positive_close_to_zero_open_target_records"] = int((observed.close_quantity.gt(0)
                                                                 & observed.engine_target_quantity.eq(0)).sum())
    summary["positive_close_to_zero_quantity_rule_records"] = int((observed.close_quantity.gt(0)
                                                                   & observed.quantity_rule_only_quantity.eq(0)).sum())
    summary["missing_execution_open_records"] = int(observed.has_execution_open.eq(False).sum())
    for stage in STAGES:
        gap = observed[stage + "_quantity_gap"]
        summary["stages"][stage] = {"reduction_records": int(gap.gt(0).sum()),
                                  "increase_records": int(gap.lt(0).sum()), "unchanged_records": int(gap.eq(0).sum())}
    return frame, summary
