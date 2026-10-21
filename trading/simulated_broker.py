"""Simulation channel over the existing Ledger, not another account or database."""
from copy import deepcopy
from datetime import date

from .broker import BrokerAdapter, BrokerCapabilities, READ_KINDS
from .contracts import ExecutionEvent, OrderIntent
from .ledger import Ledger


class SimulatedBrokerAdapter(BrokerAdapter):
    def __init__(self, ledger: Ledger):
        if ledger.state.get("broker_binding"):
            raise PermissionError("A broker-bound account cannot use the simulated channel")
        self.ledger = ledger

    @property
    def capabilities(self):
        return BrokerCapabilities("simulated_ledger", self.ledger.context.mode.value,
                                  READ_KINDS, False, True, True)

    def submit(self, intent: OrderIntent):
        return self.ledger.submit_intent(intent)

    def apply_execution(self, event: ExecutionEvent):
        return self.ledger.apply_execution(event)

    def cancel(self, order_id, *, day):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("Cancellation needs a canonical session date")
        order = self.ledger.state["orders"][order_id]
        if day < order["day"]:
            raise ValueError("Cancellation predates the simulated order")
        # The simulator confirms immediately; this is never a broker acknowledgement.
        self.ledger.request_cancel(order_id, day)
        self.ledger.confirm_cancel(order_id, day)
        return order

    def read(self, kind, *, trading_date):
        if kind not in READ_KINDS:
            raise ValueError("Unsupported broker observation kind")
        if date.fromisoformat(trading_date).isoformat() != trading_date:
            raise ValueError("Observation needs a canonical session date")
        state = self.ledger.state
        if state["day"] != trading_date:
            raise ValueError("Read the marked ledger session, not an invented account date")
        value = {"kind": kind, "account_alias": self.ledger.context.account_id,
                 "context": self.ledger.context.to_dict(), "trading_date": trading_date,
                 "observed_at": None, "source": "simulated_ledger"}
        if kind == "account":
            positions = {symbol: {"quantity": self.ledger.quantity(symbol),
                                  "sellable": self.ledger.available(symbol, trading_date)}
                         for symbol in state["positions"] if self.ledger.quantity(symbol)}
            value.update(cash_available=self.ledger.cash_available(), cash_total=state["cash"],
                         positions=positions, rows=[{"symbol": s, **p} for s, p in positions.items()])
        elif kind == "orders":
            value["rows"] = [{"order_id": order["id"], "symbol": order["symbol"],
                              "side": order["side"], "qty": order["qty"],
                              **self.quote_fields(order["limit_price"], order.get("price_unit", "cent")),
                              "filled_qty": order["filled"],
                              "status": "accepted" if order["status"] == "submitted" else order["status"],
                              "broker_status": order["status"]}
                             for order in state["orders"].values() if order["day"] == trading_date]
        else:
            value["rows"] = [{"execution_id": identifier, "order_id": fill["order_id"],
                              "symbol": fill["symbol"], "side": fill["side"],
                              "qty": fill["qty"], **self.quote_fields(fill["price"], fill.get("price_unit", "cent")),
                              "fees_cents": fill["fees"]}
                             for identifier, fill in state["fills"].items() if fill["day"] == trading_date]
        return deepcopy(value)

    @staticmethod
    def quote_fields(value, unit):
        if unit == "cent":
            return {"price_cents": value}
        if unit == "mill":
            return {"price_mills": value, "price_unit": "mill"}
        raise ValueError("Unknown simulated quote unit")
