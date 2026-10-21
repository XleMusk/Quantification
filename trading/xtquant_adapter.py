"""Official XtTrader boundary. SDK/account connection is explicitly opt-in.

Not registered as an execution backend in the app. Default is read-only.
"""
from collections import deque

STATUS = {48: "pending", 49: "pending", 50: "submitted", 51: "cancel_pending", 52: "cancel_pending",
          53: "cancelled", 54: "cancelled", 55: "partial", 56: "filled", 57: "rejected", 255: "unknown"}


def broker_symbol(value):
    return value.replace(".SSE", ".SH").replace(".SZSE", ".SZ")


class XtQuantAdapter:
    def __init__(self, trader, account, constants, live_enabled=False):
        self.trader, self.account, self.constants = trader, account, constants
        self.live_enabled = live_enabled
        self.events = deque()
        self.accepted = {}
        self.connected = False

    def connect(self):
        self.trader.start()
        if self.trader.connect() != 0 or self.trader.subscribe(self.account) != 0:
            self.connected = False
            raise ConnectionError("MiniQMT connection/subscription failed")
        self.connected = True

    def snapshot(self):
        if not self.connected:
            raise ConnectionError("Broker disconnected")
        asset = self.trader.query_stock_asset(self.account)
        positions = self.trader.query_stock_positions(self.account)
        orders = self.trader.query_stock_orders(self.account)
        trades = self.trader.query_stock_trades(self.account)
        if asset is None or positions is None or orders is None or trades is None:
            raise RuntimeError("Incomplete broker reconciliation; submissions must remain blocked")
        return {"cash": asset.cash, "total_asset": asset.total_asset,
                "positions": [{"symbol": p.stock_code, "quantity": p.volume, "sellable": p.can_use_volume} for p in positions],
                "orders": [{"id": o.order_id, "status": STATUS.get(o.order_status, "unknown"), "filled": o.traded_volume} for o in orders],
                "trades": [{"id": t.traded_id, "order_id": t.order_id, "qty": t.traded_volume, "price": t.traded_price} for t in trades]}

    def submit(self, client_id, symbol, side, qty, price):
        if not self.live_enabled:
            raise PermissionError("Live submission disabled; separate account authorization and validation required")
        if not self.connected:
            raise ConnectionError("Broker disconnected")
        if client_id in self.accepted:
            if self.accepted[client_id]["intent"] != (symbol, side, qty, price):
                raise ValueError("Conflicting broker intent key")
            return self.accepted[client_id]
        if side not in {"buy", "sell"} or qty <= 0 or price <= 0:
            raise ValueError("Invalid order intent")
        self.accepted[client_id] = {"status": "unknown", "intent": (symbol, side, qty, price)}
        result = self.trader.order_stock(self.account, broker_symbol(symbol),
                    self.constants.STOCK_BUY if side == "buy" else self.constants.STOCK_SELL,
                    qty, self.constants.FIX_PRICE, price, "parallel_v1", client_id)
        # A returned order id acknowledges submission, never books a fill.
        if result <= 0:
            raise RuntimeError("Submission rejected or ambiguous; reconcile before retry")
        self.accepted[client_id] = {"broker_order_id": result, "status": "submitted", "intent": (symbol, side, qty, price)}
        return self.accepted[client_id]

    def cancel(self, order_id):
        if not self.live_enabled or not self.connected:
            raise PermissionError("Live cancellation disabled/disconnected")
        result = self.trader.cancel_order_stock(self.account, order_id)
        return {"request_result": result, "status": "cancel_pending" if result == 0 else "cancel_rejected"}

    def on_disconnected(self):
        self.connected = False
        self.events.append({"kind": "disconnected"})

    def on_stock_order(self, order):
        self.events.append({"kind": "order", "order_id": order.order_id, "status": STATUS.get(order.order_status, "unknown"), "filled": order.traded_volume})

    def on_stock_trade(self, trade):
        self.events.append({"kind": "trade", "execution_id": trade.traded_id, "order_id": trade.order_id, "qty": trade.traded_volume, "price": trade.traded_price})

    def on_order_error(self, error):
        self.events.append({"kind": "order_error", "order_id": error.order_id, "reason": error.error_msg})

    def on_cancel_error(self, error):
        self.events.append({"kind": "cancel_error", "order_id": error.order_id, "reason": error.error_msg})

    @classmethod
    def read_only_client(cls, userdata_path, session_id, account_id, authorized=False):
        if not authorized:
            raise PermissionError("Explicit read-only account authorization required")
        from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
        from xtquant.xttype import StockAccount
        from xtquant import xtconstant
        trader = XtQuantTrader(userdata_path, session_id)
        adapter = cls(trader, StockAccount(account_id), xtconstant)
        class Callbacks(XtQuantTraderCallback):
            def on_disconnected(self): adapter.on_disconnected()
            def on_stock_order(self, order): adapter.on_stock_order(order)
            def on_stock_trade(self, trade): adapter.on_stock_trade(trade)
            def on_order_error(self, error): adapter.on_order_error(error)
            def on_cancel_error(self, error): adapter.on_cancel_error(error)
        trader.register_callback(Callbacks())
        return adapter
