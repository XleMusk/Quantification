"""One cash/position ledger for replay and paper orders. Money is integer cents."""
from copy import deepcopy
from datetime import datetime

from .contracts import BrokerFillBatch, BrokerSessionBalance, ExecutionEvent, OrderIntent, RunContext, TradingMode
from .rules import cents, execution_rule, fees, notional_cents

ACTIVE = {"submitted", "partial", "cancel_pending"}


def new_state(cash):
    return {"cash": cents(cash), "positions": {}, "orders": {}, "fills": {}, "events": [],
            "pending_targets": None, "targets": {}, "candidates": [], "prices": {},
            "entitlements": {}, "receivables": {}, "day": None, "sequence": 0}


class Ledger:
    def __init__(self, state, config, context=None, *, instrument_profiles=None):
        self.state, self.config = state, config
        self.context = context if context is not None else RunContext("standalone_replay", TradingMode.REPLAY)
        if not isinstance(self.context, RunContext):
            raise ValueError("Ledger requires a simulation context")
        self.instrument_profiles = dict(instrument_profiles or {})

    def rule(self, symbol, day):
        return execution_rule(symbol, day, self.instrument_profiles)

    def fee(self, notional, side, symbol, day):
        return fees(notional, side, self.config, equity_etf=self.rule(symbol, day).is_fund, day=day)

    def order_cost(self, qty, price, symbol, day):
        value = notional_cents(price, qty, self.rule(symbol, day).price_unit)
        return value + self.fee(value, "buy", symbol, day)

    def mark_price(self, symbol, price, day, *, price_unit="cent"):
        if type(price) is not int or price <= 0 or price_unit != self.rule(symbol, day).price_unit:
            raise ValueError("Invalid valuation price or unit")
        recorded = self.state.get("price_units", {}).get(symbol, price_unit)
        if recorded != price_unit:
            raise ValueError("Valuation quote unit changed")
        self.state["prices"][symbol] = price
        if price_unit != "cent":
            self.state.setdefault("price_units", {})[symbol] = price_unit

    def position_value(self, symbol):
        return notional_cents(self.state["prices"].get(symbol, 0), self.quantity(symbol),
                              self.state.get("price_units", {}).get(symbol, "cent"))

    def submit_intent(self, intent):
        if not isinstance(intent, OrderIntent) or intent.context != self.context:
            raise ValueError("Order account/mode does not match ledger")
        return self.submit(intent.id, intent.day, intent.security.symbol, intent.side,
                           intent.quantity.shares, intent.limit_price.value, price_unit=intent.limit_price.unit)

    def apply_execution(self, event):
        if not isinstance(event, ExecutionEvent) or event.context != self.context:
            raise ValueError("Fill account/mode does not match ledger")
        order = self.state["orders"][event.order_id]
        if (order["symbol"], order["side"]) != (event.security.symbol, event.side):
            raise ValueError("Fill security/side does not match order")
        if event.same_day_resale != self.rule(order["symbol"], event.day).same_day_resale:
            raise ValueError("Fill resale rule does not match instrument profile")
        return self.fill(event.id, event.order_id, event.quantity.shares, event.price.value, event.day,
                         event.available_day, price_unit=event.price.unit)

    def quantity(self, symbol):
        return sum(lot["qty"] for lot in self.state["positions"].get(symbol, []))

    def available(self, symbol, day):
        total = sum(lot["qty"] for lot in self.state["positions"].get(symbol, []) if lot["available"] <= day)
        reserved = sum(o["qty"] - o["filled"] for o in self.state["orders"].values()
                       if o["symbol"] == symbol and o["side"] == "sell" and o["status"] in ACTIVE)
        return total - reserved

    def cash_available(self):
        if self.state.get("broker_binding"):
            return self.state["cash"] - self.state["broker_cash_frozen"]
        reserved = sum(o["reserve"] for o in self.state["orders"].values() if o["side"] == "buy" and o["status"] in ACTIVE)
        return self.state["cash"] - reserved

    def emit(self, day, kind, **values):
        self.state["sequence"] += 1
        self.state["events"].append({"sequence": self.state["sequence"], "day": day, "kind": kind, **values})

    def _execution_source(self, broker):
        binding = self.state.get("broker_binding")
        if broker:
            if (not binding or binding.get("broker_account_kind") != "simulated"
                    or self.context.mode != TradingMode.PAPER or binding.get("run_id") != self.context.account_id):
                raise PermissionError("Broker accounting requires a bound simulated account")
        elif binding:
            raise PermissionError("A broker-bound account cannot receive simulator mutations")

    def submit(self, identifier, day, symbol, side, qty, limit_price, *, price_unit="cent", _broker=False):
        self._execution_source(_broker)
        signature = (day, symbol, side, qty, limit_price)
        if identifier in self.state["orders"]:
            previous = self.state["orders"][identifier]
            if (tuple(previous[k] for k in ("day", "symbol", "side", "qty", "limit_price")) != signature
                    or previous.get("price_unit", "cent") != price_unit):
                raise ValueError("Conflicting order idempotency key")
            return previous
        if side not in {"buy", "sell"} or type(qty) is not int or qty <= 0 or type(limit_price) is not int or limit_price <= 0:
            raise ValueError("Invalid order")
        rule = self.rule(symbol, day)
        if price_unit != rule.price_unit:
            raise ValueError("Order quote unit does not match instrument profile")
        reason = rule.quantity.rejection(qty, side, self.quantity(symbol))
        reserve = notional_cents(limit_price, qty, price_unit) if _broker else self.order_cost(qty, limit_price, symbol, day)
        if side == "buy" and reserve > self.cash_available():
            reason = "insufficient_cash"
        if side == "sell" and qty > self.available(symbol, day):
            reason = "insufficient_sellable_T1"
        order = {"id": identifier, "day": day, "symbol": symbol, "side": side, "qty": qty,
                 "limit_price": limit_price, "filled": 0, "notional": 0, "fees": 0,
                 "reserve": reserve if side == "buy" and not reason and not _broker else 0,
                 "status": "rejected" if reason else "submitted", "reason": reason}
        if price_unit != "cent":
            order.update(price_unit=price_unit, notional_minor=0, execution_profile=self.instrument_profiles[symbol])
        self.state["orders"][identifier] = order
        if _broker:
            order["source"] = "broker_observation"
        self.emit(day, order["status"], order_id=identifier, symbol=symbol, reason=reason)
        return order

    def fill(self, execution_id, order_id, qty, price, day, available_day, *, price_unit="cent", _broker=False):
        self._execution_source(_broker)
        order = self.state["orders"][order_id]
        same_day = self.rule(order["symbol"], day).same_day_resale
        if day != order["day"] or (available_day != day if same_day else available_day <= day):
            raise ValueError("Invalid settlement day")
        signature = {"order_id": order_id, "qty": qty, "price": price, "day": day}
        previous = self.state["fills"].get(execution_id)
        if previous:
            if (any(previous[k] != signature[k] for k in signature)
                    or previous.get("price_unit", "cent") != price_unit
                    or previous.get("available_day", available_day) != available_day):
                raise ValueError("Conflicting execution id")
            return False
        if order["status"] not in ACTIVE or type(qty) is not int or qty <= 0 or qty > order["qty"] - order["filled"] or type(price) is not int or price <= 0:
            raise ValueError("Invalid fill")
        side, symbol = order["side"], order["symbol"]
        if (price_unit != order.get("price_unit", "cent") or price_unit != self.rule(symbol, day).price_unit
                or order.get("execution_profile") != self.instrument_profiles.get(symbol)):
            raise ValueError("Fill quote unit/profile does not match order")
        if (side == "buy" and price > order["limit_price"]) or (side == "sell" and price < order["limit_price"]):
            raise ValueError("Fill violates limit price")
        # Round cumulative mill notional once; callback fragmentation must not change cash.
        total_minor = order.get("notional_minor", order["notional"]) + price * qty
        total_notional = notional_cents(total_minor, 1, price_unit)
        notional = total_notional - order["notional"]
        new_fees = order["fees"] if _broker else self.fee(total_notional, side, symbol, day)
        fee_delta = new_fees - order["fees"]
        if side == "buy":
            if self.state["cash"] < notional + fee_delta:
                raise ValueError("Fill would create negative cash")
            self.state["cash"] -= notional + fee_delta
            self.state["positions"].setdefault(symbol, []).append({"qty": qty, "available": available_day})
        else:
            lots = self.state["positions"].get(symbol, [])
            if self.state["cash"] + notional < fee_delta:
                raise ValueError("Sell fill fees would create negative cash")
            if sum(lot["qty"] for lot in lots if lot["available"] <= day) < qty:
                raise ValueError("Fill exceeds settled holdings")
            remaining = qty
            for lot in lots:
                if lot["available"] <= day:
                    take = min(remaining, lot["qty"])
                    lot["qty"] -= take
                    remaining -= take
            self.state["positions"][symbol] = [lot for lot in lots if lot["qty"]]
            self.state["cash"] += notional - fee_delta
        order["filled"] += qty
        order["notional"] += notional
        if price_unit != "cent":
            order["notional_minor"] = total_minor
        order["fees"] = new_fees
        fill_status = "filled" if order["filled"] == order["qty"] else "partial"
        # A partial execution can arrive while cancellation is still pending.
        # It changes the filled quantity, not the outstanding cancel request.
        cancel_pending = order["status"] == "cancel_pending" and fill_status == "partial"
        order["status"] = "cancel_pending" if cancel_pending else fill_status
        remaining = order["qty"] - order["filled"]
        future_notional = notional_cents(total_minor + remaining * order["limit_price"], 1, price_unit) - order["notional"]
        future_fee = 0 if _broker else self.fee(order["notional"] + future_notional, "buy", symbol, day) - new_fees
        order["reserve"] = future_notional + max(0, future_fee) if side == "buy" and remaining and not _broker else 0
        self.state["fills"][execution_id] = {**signature, "fees": fee_delta, "symbol": symbol, "side": side,
                                              "available_day": available_day}
        extra = {}
        if cancel_pending:
            extra["order_status"] = "cancel_pending"
        if _broker:
            extra.update(source="broker_observation", fee_basis="cumulative_order_statement", notional=notional, fees=None)
            self.state["fills"][execution_id].update(extra)
        if price_unit != "cent":
            extra.update(price_unit=price_unit, notional=notional)
            self.state["fills"][execution_id].update(extra)
        self.emit(day, fill_status, order_id=order_id, execution_id=execution_id, symbol=symbol, qty=qty, price=price, **extra)
        return True

    def apply_broker_receipt(self, receipt):
        """Apply one cumulative actual-fee receipt atomically through this same ledger."""
        if not isinstance(receipt, BrokerFillBatch) or receipt.order.context != self.context:
            raise ValueError("Broker receipt account/mode does not match ledger")
        self._execution_source(True)
        binding = self.state["broker_binding"]
        if binding["account_alias"] != receipt.account_alias:
            raise ValueError("Broker alias does not match account binding")
        payload = receipt.to_dict()
        economic = {k: v for k, v in payload.items() if k not in {"observed_at", "source_ref"}}
        prior = self.state.get("broker_receipts", {}).get(receipt.order.id)
        if prior and prior["economic"] == economic:
            return False
        if self.state["day"] and receipt.order.day < self.state["day"]:
            raise ValueError("An old changed broker receipt needs reconciliation, not replay into current cash")
        trial = deepcopy(self.state)
        book = Ledger(trial, self.config, self.context, instrument_profiles=self.instrument_profiles)
        order = book.submit(receipt.order.id, receipt.order.day, receipt.order.security.symbol, receipt.order.side,
                            receipt.order.quantity.shares, receipt.order.limit_price.value, _broker=True)
        if order["status"] == "rejected" and receipt.status != "rejected":
            raise ValueError("Broker order conflicts with account cash/quantity constraints")
        if order.get("broker_contract_id", receipt.contract_id) != receipt.contract_id:
            raise ValueError("Broker contract identity changed")
        order["broker_contract_id"] = receipt.contract_id
        supplied = {f"ths:{receipt.order.day}:{receipt.contract_id}:{f.id}": f for f in receipt.fills}
        known = {key for key, f in trial["fills"].items() if f["order_id"] == order["id"]}
        if known - supplied.keys():
            raise ValueError("Cumulative broker receipt lost earlier executions")
        if prior and sum(f.quantity.shares for f in receipt.fills) == order["filled"] and receipt.fees_cents != order["fees"]:
            raise ValueError("Confirmed broker order fees changed")
        for key, fill in supplied.items():
            book.fill(key, order["id"], fill.quantity.shares, fill.price.value, receipt.order.day,
                      receipt.available_day, _broker=True)
        fee_delta = receipt.fees_cents - order["fees"]
        if fee_delta < 0:
            raise ValueError("Cumulative actual broker fees decreased")
        if trial["cash"] < fee_delta:
            raise ValueError("Actual broker fees would create negative cash")
        trial["cash"] -= fee_delta
        order.update(fees=receipt.fees_cents, status=receipt.status, reserve=0,
                     fee_basis="cumulative_order_statement")
        if fee_delta:
            book.emit(receipt.order.day, "broker_fee_adjustment", order_id=order["id"], cash=-fee_delta,
                      cumulative_fees=receipt.fees_cents, source_ref=receipt.source_ref)
        trial["broker_cash_frozen"] = receipt.cash_frozen
        actual = tuple(sorted((s, book.quantity(s), book.available(s, receipt.order.day))
                              for s in trial["positions"] if book.quantity(s)))
        if trial["cash"] != receipt.cash_total or actual != tuple(sorted(receipt.positions)):
            raise ValueError("Broker receipt cash/holdings do not match accounted executions and fees")
        for symbol, price in receipt.marks:
            book.mark_price(symbol, price, receipt.order.day)
        trial.update(day=receipt.order.day, broker_valuation_observed_at=receipt.observed_at)
        trial.setdefault("broker_receipts", {})[order["id"]] = {
            "economic": economic, "source_ref": receipt.source_ref, "observed_at": receipt.observed_at}
        book.emit(receipt.order.day, "broker_receipt_applied", order_id=order["id"], source_ref=receipt.source_ref)
        self.state.clear()
        self.state.update(trial)
        return True

    def apply_broker_session(self, receipt):
        """Reconcile a following-session observation without rebasing cash or lots."""
        if not isinstance(receipt, BrokerSessionBalance) or receipt.context != self.context:
            raise ValueError("Broker session account/mode does not match ledger")
        self._execution_source(True)
        if receipt.account_alias != self.state["broker_binding"]["account_alias"]:
            raise ValueError("Broker session alias does not match binding")
        economic = {k: v for k, v in receipt.to_dict().items() if k not in {"observed_at", "source_ref"}}
        previous = self.state.get("broker_sessions", {}).get(receipt.day)
        if previous and previous.get("economic") == economic:
            return False
        if receipt.day <= self.state["day"]:
            raise ValueError("Changed/old session baseline cannot be rebased over existing account activity")
        trial = deepcopy(self.state)
        book = Ledger(trial, self.config, self.context, instrument_profiles=self.instrument_profiles)
        for order_id, status, proof_ref in receipt.terminalizations:
            order = trial["orders"][order_id]
            if (order.get("source") != "broker_observation" or order["day"] >= receipt.day
                    or order["status"] not in ACTIVE
                    or status == "filled" and order["filled"] != order["qty"]
                    or status == "cancelled" and order["filled"] >= order["qty"]
                    or status == "rejected" and order["filled"]):
                raise ValueError("Terminal broker status conflicts with accounted executions")
            order.update(status=status, reserve=0)
            trial.setdefault("broker_order_transitions", {})[order_id] = {
                "day": receipt.day, "status": status, "source_ref": proof_ref, "observed_at": receipt.observed_at}
            book.emit(receipt.day, "broker_order_terminal", order_id=order_id, status=status, source_ref=proof_ref)
        if any(o["status"] in ACTIVE for o in trial["orders"].values()):
            raise ValueError("Unresolved prior broker orders cannot be assumed expired")
        for credit in receipt.share_credits:
            key = f"ths:{receipt.day}:{credit.id}"
            symbol = credit.security.symbol
            if key in trial.get("broker_share_credits", {}):
                raise ValueError("Share credit already belongs to another session receipt")
            if book.rule(symbol, receipt.day).is_fund:
                raise ValueError("Only ordinary-stock bonus/capital-transfer credits are supported")
            trial["positions"].setdefault(symbol, []).append({"qty": credit.quantity.shares,
                                                            "available": credit.available_day})
            trial.setdefault("broker_share_credits", {})[key] = {
                **credit.to_dict(), "source_ref": receipt.source_ref, "observed_at": receipt.observed_at}
            book.emit(receipt.day, "broker_share_credit", credit_id=key, symbol=symbol,
                      qty=credit.quantity.shares, available_day=credit.available_day,
                      credit_kind=credit.kind, source_ref=receipt.source_ref)
        for release in receipt.restriction_releases:
            key = f"ths:{receipt.day}:{release.id}"
            symbol = release.security.symbol
            if key in trial.get("broker_restriction_releases", {}):
                raise ValueError("Restriction release already belongs to another session receipt")
            lots = trial["positions"].get(symbol, [])
            if sum(lot["qty"] for lot in lots if lot["available"] == "9999-12-31") < release.quantity.shares:
                raise ValueError("Release exceeds this account's unknown opening restriction; cannot unlock T1/new shares")
            remaining, updated = release.quantity.shares, []
            for lot in lots:
                take = min(remaining, lot["qty"]) if lot["available"] == "9999-12-31" else 0
                if lot["qty"] > take:
                    updated.append({**lot, "qty": lot["qty"] - take})
                if take:
                    updated.append({**lot, "qty": take, "available": receipt.day})
                    remaining -= take
            trial["positions"][symbol] = updated
            trial.setdefault("broker_restriction_releases", {})[key] = {
                **release.to_dict(), "source_ref": receipt.source_ref, "observed_at": receipt.observed_at}
            book.emit(receipt.day, "broker_restriction_release", release_id=key, symbol=symbol,
                      qty=release.quantity.shares, source_ref=receipt.source_ref)
        for movement in receipt.movements:
            key = f"ths:{receipt.day}:{movement.id}"
            if key in trial.get("broker_cash_movements", {}):
                raise ValueError("Cash movement already belongs to another session receipt")
            trial["cash"] += movement.cash_change_cents
            trial.setdefault("broker_cash_movements", {})[key] = {**movement.to_dict(), "day": receipt.day,
                "source_ref": receipt.source_ref, "observed_at": receipt.observed_at}
            book.emit(receipt.day, "broker_cash_movement", movement_id=key, movement_kind=movement.kind,
                      cash=movement.cash_change_cents, source_ref=receipt.source_ref)
        actual = tuple(sorted((s, book.quantity(s), book.available(s, receipt.day))
                              for s in trial["positions"] if book.quantity(s)))
        if trial["cash"] != receipt.cash_total or actual != tuple(sorted(receipt.positions)):
            raise ValueError("Session cash/holdings/availability do not reconcile; unexplained movements or restrictions")
        trial["broker_cash_frozen"] = 0
        for symbol, price in receipt.marks:
            book.mark_price(symbol, price, receipt.day)
        trial.update(day=receipt.day, broker_valuation_observed_at=receipt.observed_at)
        trial.setdefault("broker_sessions", {})[receipt.day] = {"economic": economic,
            "source_ref": receipt.source_ref, "observed_at": receipt.observed_at, "available_day": receipt.available_day}
        book.emit(receipt.day, "broker_session_reconciled", source_ref=receipt.source_ref)
        self.state.clear()
        self.state.update(trial)
        return True

    def apply_broker_cash_statement(self, receipt):
        """Append proven same-session cash events without replacing its opening balance."""
        if (not isinstance(receipt, BrokerSessionBalance) or receipt.context != self.context
                or receipt.terminalizations or receipt.restriction_releases or receipt.share_credits):
            raise ValueError("Cash statement requires matching simulated context and no order/share/restriction transitions")
        self._execution_source(True)
        if receipt.account_alias != self.state["broker_binding"]["account_alias"]:
            raise ValueError("Cash statement alias does not match binding")
        key = f"ths:{receipt.day}:{receipt.observed_at}"
        economic = {k: v for k, v in receipt.to_dict().items() if k not in {"observed_at", "source_ref"}}
        prior = self.state.get("broker_cash_statements", {}).get(key)
        if prior:
            if prior["economic"] != economic or prior["source_ref"] != receipt.source_ref:
                raise ValueError("Previously imported cash statement changed")
            return False
        session = self.state.get("broker_sessions", {}).get(receipt.day, {})
        if (receipt.day != self.state["day"] or "economic" not in session
                or receipt.available_day != session["available_day"]):
            raise ValueError("Cash statement requires the existing reconciled current session")
        if datetime.fromisoformat(receipt.observed_at) <= datetime.fromisoformat(self.state["broker_valuation_observed_at"]):
            raise ValueError("Cash statement must follow the last accounted observation")
        if (self.state.get("broker_cash_frozen", 0) or any(o["status"] in ACTIVE or o["day"] == receipt.day
                                                        for o in self.state["orders"].values())):
            raise ValueError("Same-session cash import is supported only before new orders and with no frozen funds")
        supplied = {f"ths:{receipt.day}:{m.id}": m for m in receipt.movements}
        retained = {k: m for k, m in self.state.get("broker_cash_movements", {}).items() if m["day"] == receipt.day}
        if retained.keys() - supplied.keys() or any(
                any(m[field] != supplied[k].to_dict()[field] for field in ("id", "kind", "cash_change_cents"))
                for k, m in retained.items()):
            raise ValueError("Cumulative cash statement lost or changed previously accounted movements")
        trial = deepcopy(self.state)
        book = Ledger(trial, self.config, self.context, instrument_profiles=self.instrument_profiles)
        for movement_id, movement in supplied.items():
            if movement_id in retained:
                continue
            trial["cash"] += movement.cash_change_cents
            trial.setdefault("broker_cash_movements", {})[movement_id] = {
                **movement.to_dict(), "day": receipt.day, "source_ref": receipt.source_ref,
                "observed_at": receipt.observed_at}
            book.emit(receipt.day, "broker_cash_movement", movement_id=movement_id, movement_kind=movement.kind,
                      cash=movement.cash_change_cents, source_ref=receipt.source_ref)
        actual = tuple(sorted((s, book.quantity(s), book.available(s, receipt.day))
                              for s in trial["positions"] if book.quantity(s)))
        if trial["cash"] != receipt.cash_total or actual != tuple(sorted(receipt.positions)):
            raise ValueError("Cash statement does not reconcile cash/holdings/availability")
        for symbol, price in receipt.marks:
            book.mark_price(symbol, price, receipt.day)
        trial["broker_valuation_observed_at"] = receipt.observed_at
        trial.setdefault("broker_cash_statements", {})[key] = {
            "economic": economic, "source_ref": receipt.source_ref, "observed_at": receipt.observed_at}
        book.emit(receipt.day, "broker_cash_statement_applied", source_ref=receipt.source_ref)
        self.state.clear()
        self.state.update(trial)
        return True

    def request_cancel(self, order_id, day):
        self._execution_source(False)
        order = self.state["orders"][order_id]
        if order["status"] in {"submitted", "partial"}:
            order["status"] = "cancel_pending"
            self.emit(day, "cancel_pending", order_id=order_id)

    def confirm_cancel(self, order_id, day):
        self._execution_source(False)
        order = self.state["orders"][order_id]
        if order["status"] in ACTIVE:
            order["status"], order["reserve"] = "cancelled", 0
            self.emit(day, "cancelled", order_id=order_id, remaining=order["qty"] - order["filled"])

    def equity(self):
        return self.state["cash"] + sum(self.position_value(s) for s in self.state["prices"]) + sum(self.state["receivables"].values())

    def snapshot(self):
        return deepcopy(self.state)
