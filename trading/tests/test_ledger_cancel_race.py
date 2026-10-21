"""Cancellation races must retain reservations until a terminal callback."""
import json
import unittest

from trading.config import StrategyConfig
from trading.ledger import Ledger, new_state


DAY = "2024-01-02"
NEXT_DAY = "2024-01-03"
SYMBOL = "600000.SSE"


class CancellationRaceTests(unittest.TestCase):
    def book_with_order(self, side):
        book = Ledger(new_state(10000), StrategyConfig())
        if side == "sell":
            book.state["positions"][SYMBOL] = [{"qty": 300, "available": DAY}]
        order = book.submit("order", DAY, SYMBOL, side, 300, 1000)
        self.assertEqual(order["status"], "submitted")
        book.request_cancel(order["id"], DAY)
        return book, order

    def test_partial_fill_retains_cancel_request_and_remaining_reservation(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                book, order = self.book_with_order(side)
                book.fill("partial", order["id"], 100, 1000, DAY, NEXT_DAY)
                self.assertEqual(order["filled"], 100)
                self.assertEqual(order["status"], "cancel_pending")
                self.assertEqual(book.state["events"][-1]["kind"], "partial")
                if side == "buy":
                    self.assertGreater(order["reserve"], 200000)
                    self.assertEqual(book.cash_available(), book.state["cash"] - order["reserve"])
                    self.assertEqual(book.available(SYMBOL, DAY), 0)
                else:
                    self.assertEqual(book.quantity(SYMBOL), 200)
                    self.assertEqual(book.available(SYMBOL, DAY), 0)

                # Reloading the persisted state must not forget the request.
                book = Ledger(json.loads(json.dumps(book.state)), book.config)
                book.confirm_cancel(order["id"], DAY)
                self.assertEqual(book.state["orders"][order["id"]]["status"], "cancelled")
                self.assertEqual(book.state["orders"][order["id"]]["reserve"], 0)
                if side == "sell":
                    self.assertEqual(book.available(SYMBOL, DAY), 200)
                with self.assertRaisesRegex(ValueError, "Invalid fill"):
                    book.fill("too-late", order["id"], 100, 1000, DAY, NEXT_DAY)

    def test_full_fill_wins_cancellation_race(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                book, order = self.book_with_order(side)
                book.fill("full", order["id"], 300, 1000, DAY, NEXT_DAY)
                self.assertEqual(order["status"], "filled")
                self.assertEqual(order["reserve"], 0)
                before = book.snapshot()
                book.confirm_cancel(order["id"], DAY)
                self.assertEqual(book.state, before)

    def test_repeated_cancel_requests_do_not_duplicate_events(self):
        book, order = self.book_with_order("buy")
        before = book.snapshot()
        book.request_cancel(order["id"], DAY)
        self.assertEqual(book.state, before)

        book.fill("partial", order["id"], 100, 1000, DAY, NEXT_DAY)
        before = book.snapshot()
        book.request_cancel(order["id"], DAY)
        self.assertEqual(book.state, before)
        self.assertFalse(book.fill("partial", order["id"], 100, 1000, DAY, NEXT_DAY))
        self.assertEqual(book.state, before)


if __name__ == "__main__":
    unittest.main()
