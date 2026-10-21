import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd

from trading.config import StrategyConfig
from trading.research.experiment import replay
from trading.research.seats import money_cny, normalize, period
from trading.research.seat_trend import (TrendFeed, candidates_by_day, delayed_day, donchian,
                                          event_signals, select_seats)


def raw(identifier, name="Broker A branch", buy=200., sell=10., reason="日涨幅偏离值达到7%的前5只证券"):
    return {"TRADE_DATE": "2025-09-26 00:00:00", "EXPLANATION": reason,
            "OPERATEDEPT_NAME": name, "OPERATEDEPT_CODE": "001", "BUY": buy, "SELL": sell,
            "NET": buy-sell, "ACCUM_AMOUNT": 10000., "SECUCODE": "600000.SH",
            "TRADE_ID": "board1", "_raw_id": identifier, "_retrieved_at": "2026-09-26T16:00:00+08:00",
            "RISE_PROBABILITY_3DAY": 999, "D5_CLOSE_ADJCHRATE": 999}


class SeatTrendTests(unittest.TestCase):
    def test_duplicates_do_not_double_count_and_multiday_stays_separate(self):
        buy = [raw("b1"), raw("b2", reason="日换手率达到20%的前5只证券"),
               raw("b3", reason="连续三个交易日内，涨幅偏离值累计达到20%的证券")]
        frame = normalize(buy, [raw("s1")])
        single = frame[frame.window_kind.eq("single")]
        self.assertEqual(len(single), 1)
        self.assertEqual(single.iloc[0].buy_cny, 200)
        self.assertEqual(single.iloc[0].listed_side, "buy|sell")
        self.assertEqual(len(frame), 2)
        self.assertEqual(int(frame.eligible.sum()), 1)
        self.assertIsNone(frame.loc[frame.window_kind.ne("single"), "window_start"].iloc[0])
        self.assertNotIn("RISE_PROBABILITY_3DAY", frame.columns)
        self.assertNotIn("D5_CLOSE_ADJCHRATE", frame.columns)

    def test_generic_seats_are_not_a_single_investor(self):
        a, b = raw("a", name="机构专用"), raw("b", name="机构专用")
        frame = normalize([a, b], [])
        self.assertEqual(len(frame), 2)
        self.assertFalse(frame.eligible.any())
        renamed = normalize([raw("a", name="Old Broker road"), raw("b", name="New Broker road")], [])
        self.assertEqual(len(renamed), 2)

    def test_conflicts_and_unknown_amounts_are_not_zero_filled(self):
        frame = normalize([raw("a", buy=200), raw("b", buy=250)], [])
        self.assertTrue(frame.amount_conflict.all())
        self.assertFalse(frame.eligible.any())
        bad = raw("c")
        bad["BUY"] = None
        frame = normalize([bad], [])
        self.assertTrue(pd.isna(frame.buy_cny.iloc[0]))
        self.assertEqual(frame.sell_cny.iloc[0], 10.)
        self.assertEqual(frame.period_turnover_cny.iloc[0], 10000.)
        self.assertFalse(frame.eligible.iloc[0])
        duplicate_missing = normalize([bad], [{**bad, "_raw_id": "other_side"}])
        self.assertEqual(len(duplicate_missing), 2)

    def test_units_and_window(self):
        self.assertEqual(money_cny(2, "thousand_CNY"), 2000.)
        self.assertEqual(money_cny(2, "CNY"), 2.)
        with self.assertRaises(ValueError):
            money_cny(None, "CNY")
        self.assertEqual(period("连续三个交易日内，涨幅偏离值累计达到20%的证券"), ("multi_up_to_3", None))
        self.assertEqual(period("未识别披露标准"), ("unknown_or_multi", None))

    def test_disclosure_delay_and_future_records(self):
        calendar = ["2025-09-26", "2025-09-29", "2025-09-30"]
        events = normalize([raw("a")], [])
        summaries = [{"TRADE_DATE": "2025-09-26", "SECUCODE": "600000.SH", "EXPLANATION": "日振幅值达到15%"}]
        signals = event_signals(events, summaries, ["Broker A branch"], calendar, 1, .01)
        self.assertNotIn((calendar[0], "600000.SSE"), signals)
        self.assertTrue(signals[(calendar[1], "600000.SSE")]["buy"])
        self.assertIsNone(delayed_day(calendar[-1], calendar, 1))
        with self.assertRaises(ValueError):
            delayed_day(calendar[0], calendar, 0)

    def test_selection_is_earlier_frequency_only(self):
        data = pd.DataFrame({"seat_name": ["old", "old", "new", "new", "new"],
            "trade_date": ["2025-01-02", "2025-01-03", "2026-01-02", "2026-01-05", "2026-01-06"],
            "symbol": "600000.SSE", "eligible": True})
        seats, _ = select_seats(data, ["2024-09-26", "2025-09-25"])
        self.assertEqual(seats, ["old"])
        altered = pd.concat([data, data[data.seat_name.eq("new")]] * 5)
        self.assertEqual(select_seats(altered, ["2024-09-26", "2025-09-25"])[0], seats)

    def test_donchian_excludes_current_bar_and_is_prefix_causal(self):
        dates = pd.bdate_range("2025-01-02", periods=16)
        close = np.array([10.] * 5 + [11, 12, 13, 13, 13, 13, 10, 10, 14, 14, 15])
        panel = pd.DataFrame({"date": dates, "symbol": "600000.SSE", "open": close,
                              "high": close+.1, "low": close-.1, "close": close})
        calendar = dates.strftime("%Y-%m-%d").tolist()
        a = donchian(panel, calendar, 5, 3, 3)
        self.assertTrue(a.loc[(dates[5], "600000.SSE"), "trend_active"])
        self.assertFalse(a.loc[(dates[11], "600000.SSE"), "trend_active"])
        panel.loc[panel.date >= dates[12], ["open", "high", "low", "close"]] *= 2
        b = donchian(panel, calendar, 5, 3, 3)
        pd.testing.assert_frame_equal(a.loc[:dates[11]], b.loc[:dates[11]])

    def test_sell_veto_and_no_same_day_ledger_entry(self):
        days = ["2025-09-26", "2025-09-29", "2025-09-30", "2025-10-09"]
        symbol = "600000.SSE"
        index = pd.MultiIndex.from_product([pd.to_datetime(days), [symbol]], names=["date", "symbol"])
        features = pd.DataFrame({"regime": 1, "trend_active": True, "score": 1.,
                                 "entry_channel": 9., "exit_channel": 8., "atr": .1}, index=index)
        signals = {(days[1], symbol): {"buy": True, "lhb": True}, (days[2], symbol): {"sell": True}}
        by_day, _ = candidates_by_day(features, pd.Series(True, index=index), days, signals, "TREND_H1_H4")
        self.assertFalse(by_day[days[0]])
        self.assertTrue(by_day[days[1]])
        self.assertFalse(by_day[days[2]])
        self.assertFalse(by_day[days[3]])
        rows = {d: {symbol: {"open": 10., "close": 10., "preclose": 10., "isST": 0, "tradestatus": 1}} for d in days}
        base = SimpleNamespace(calendar=days, rows=rows, actions=[], instruments={symbol: {"ipoDate": "2000-01-01", "outDate": ""}},
                               volumes=pd.DataFrame(1000000., index=days, columns=[symbol]),
                               next_day=lambda d: days[days.index(d)+1] if d != days[-1] else "2025-10-10")
        config = StrategyConfig(initial_cash=10000, holdings=1, retain_rank=1, single_limit=.8, rebalance_sessions=1)
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "replay"
            result = replay(TrendFeed(base, by_day), config, days, destination)
            state = json.loads((destination / "account.json").read_text())
            buys = [o for o in state["orders"].values() if o["side"] == "buy"]
            sells = [o for o in state["orders"].values() if o["side"] == "sell"]
            self.assertEqual(buys[0]["day"], days[2])
            self.assertEqual(sells[0]["day"], days[3])
            self.assertGreater(result["fees"], 0)
            self.assertGreaterEqual(state["cash"], 0)


if __name__ == "__main__":
    unittest.main()
