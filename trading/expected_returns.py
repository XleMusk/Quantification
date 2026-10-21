"""Explicit equal-slot expected-return selection for diagnostic replay only."""
from dataclasses import dataclass
import math
from typing import ClassVar

from .contracts import (IntentKind, PortfolioIntent, SignalFrame, SignalKind,
                        TradingMode, decision_time)
from .rules import notional_cents, quote_value


POLICY_ID = "equal_slots_legal_capacity_return_cost_v2"


def validate_return_frame(frame, horizon):
    if (not isinstance(frame, SignalFrame) or not frame.signals
            or frame.context.mode != TradingMode.REPLAY
            or any(signal.kind != SignalKind.EXPECTED_RETURN or signal.horizon_sessions != horizon
                   for signal in frame.signals)):
        raise ValueError("Expected-return policy requires one explicit replay horizon, not ranking scores")


def checked_return_intent(frame, intent, config, next_day):
    if (not isinstance(intent, PortfolioIntent) or intent.context != frame.context
            or intent.decided_at != decision_time(frame.day)):
        raise ValueError("Expected-return intent account/time mismatch")
    if intent.kind == IntentKind.NO_CHANGE:
        return intent
    if (intent.kind != IntentKind.FULL_TARGET or intent.execution_not_before != next_day
            or not set(dict(intent.weights)) <= {signal.security.symbol for signal in frame.signals}
            or len(intent.weights) > config.holdings
            or any(weight > config.single_limit + 1e-12 for _, weight in intent.weights)
            or sum(weight for _, weight in intent.weights) > config.gross_limit + 1e-12):
        raise ValueError("Expected-return intent violates admitted pool or portfolio limits")
    return intent


@dataclass(frozen=True)
class EqualSlotReturnPolicy:
    cost_aware: bool
    horizon_sessions: int = 20
    signal_kind: ClassVar[SignalKind] = SignalKind.EXPECTED_RETURN

    def __post_init__(self):
        if type(self.cost_aware) is not bool or type(self.horizon_sessions) is not int or self.horizon_sessions <= 0:
            raise ValueError("Explicit cost arm and positive forecast horizon required")

    def contract(self, config):
        return {"id": POLICY_ID, "config": config.to_dict(), "cost_aware": self.cost_aware,
                "horizon_sessions": self.horizon_sessions,
                "objective": "close_legal_position_relative_return_minus_include_cost_plus_exclude_cost",
                "cost_scale": 1, "cost_basis": "decision_raw_close_parent_fee_plus_declared_slippage",
                "benefit_basis": "close_observed_position_after_legal_quantity_change; zero_unbuyable_benefit",
                "selection": "exact_top_k_additive_utilities; no rank-buffer; no cash timing",
                "execution_admitted": False}

    def validate_frame(self, frame):
        validate_return_frame(frame, self.horizon_sessions)

    def __call__(self, frame, book, feed, config):
        from .engine import shares_for_target

        self.validate_frame(frame)
        if frame.context != book.context or book.state.get("broker_binding"):
            raise ValueError("Expected-return policy needs the same unbound replay account")
        day, equity = frame.day, book.equity()
        if equity <= 0:
            raise ValueError("Positive observed equity required for return selection")
        weight = min(config.single_limit, config.gross_limit / config.holdings)
        rows, diagnostics = feed.rows.get(day, {}), []

        def trade_cost(symbol, price, quantity, side):
            if not quantity:
                return 0
            notional = notional_cents(price, quantity, book.rule(symbol, day).price_unit)
            return book.fee(notional, side, symbol, day) + round(notional * config.slippage_bps / 10000)

        for signal in frame.signals:
            symbol, row = signal.security.symbol, rows.get(signal.security.symbol)
            if row is None or not math.isfinite(row["close"]) or row["close"] <= 0:
                raise ValueError(f"Unknown decision quote for return selection: {symbol} {day}")
            rule = book.rule(symbol, day)
            if rule.is_fund:
                raise ValueError("This declared expected-return policy is ordinary-equity only")
            price, held = quote_value(row["close"], rule.price_unit), book.quantity(symbol)
            desired = shares_for_target(equity, weight, price, rule)
            side = "buy" if desired > held else "sell"
            change = rule.quantity.rounded(abs(desired - held), side, held)
            include = trade_cost(symbol, price, change, side)
            exclude = trade_cost(symbol, price, held, "sell")
            reachable = held + change if side == "buy" else held - change
            capital = notional_cents(price, reachable, rule.price_unit)
            benefit = capital * signal.value
            utility = benefit - (include - exclude if self.cost_aware else 0)
            diagnostics.append({"symbol": symbol, "expected_return_fraction": signal.value,
                                "held_quantity": held, "nominal_weight": weight, "close_target_quantity": desired,
                                "close_legal_position_quantity": reachable, "close_legal_notional_cents": capital,
                                "forecast_relative_benefit_cents": benefit,
                                "estimated_include_cost_cents": include, "estimated_exclude_cost_cents": exclude,
                                "selection_utility_cents": utility})
        # Fixed slots with observed legal quantities make the objective additive;
        # not an approximation of a general covariance/constrained optimizer.
        diagnostics.sort(key=lambda row: (-row["selection_utility_cents"], row["symbol"]))
        selected = diagnostics[:config.holdings]
        chosen = {row["symbol"] for row in selected}
        raw = sorted(diagnostics, key=lambda row: (-row["forecast_relative_benefit_cents"], row["symbol"]))[:config.holdings]
        changed = chosen ^ {row["symbol"] for row in raw}
        outside_cost, outside_symbols = 0, []
        pool = {row["symbol"] for row in diagnostics}
        for symbol in book.state["positions"]:
            quantity = book.quantity(symbol)
            if not quantity or symbol in pool:
                continue
            row = rows.get(symbol)
            if row is None or not math.isfinite(row["close"]) or row["close"] <= 0:
                raise ValueError(f"Unknown departed-holding decision quote: {symbol} {day}")
            outside_symbols.append(symbol)
            outside_cost += trade_cost(symbol, quote_value(row["close"], book.rule(symbol, day).price_unit), quantity, "sell")
        # Persist all incumbents and both comparison frontiers, not thousands of
        # unselected rows in every account event. Full forecasts are saved upstream.
        detail = [row for row in diagnostics if row["symbol"] in chosen | changed or row["held_quantity"]]
        book.emit(day, "expected_return_selection", policy_id=POLICY_ID, cost_aware=self.cost_aware,
                  horizon_sessions=self.horizon_sessions, candidate_count=len(diagnostics),
                  utility_units="CNY_cents_at_close_legal_position", cost_scale=1,
                  estimated_selected_transition_cost_cents=outside_cost + sum(row["estimated_include_cost_cents"] for row in selected)
                  + sum(row["estimated_exclude_cost_cents"] for row in diagnostics if row["symbol"] not in chosen),
                  departed_holding_symbols=sorted(outside_symbols), estimated_departed_exit_cost_cents=outside_cost,
                  cost_changed_members=sorted(changed), selected_symbols=sorted(chosen),
                  nominal_gross=weight * len(selected), diagnostics=detail,
                  estimated_selected_legal_gross=sum(row["close_legal_notional_cents"] for row in selected) / equity,
                  selected_zero_legal_quantity=sum(row["close_legal_position_quantity"] == 0 for row in selected),
                  estimate_is_execution=False, covariance_risk_optimized=False)
        return PortfolioIntent.full_target({row["symbol"]: weight for row in selected}, context=frame.context,
            decided_at=decision_time(day), execution_not_before=feed.next_day(day), source=frame.source,
            reason="expected_relative_return_net_transition_cost" if self.cost_aware else "expected_relative_return_top_k")
